"""kanban-cloud server: multi-user hosted kanban with work delegation to PCs.

Run:  py -m uvicorn app.main:app --host 0.0.0.0 --port 8900
Env:  DATABASE_URL (optional; Neon Postgres). Falls back to ./kanban_cloud.db.
"""
import os
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import delegation
from .auth import hash_password, mask_secret, verify_password
from .db import make_engine, make_session_factory
from .models import (
    TICKET_STATUSES,
    AuthToken,
    Base,
    Board,
    Cluster,
    ClusterMember,
    ClusterSettings,
    Comment,
    Ticket,
    User,
    WorkItem,
    Worker,
    new_token,
    utcnow,
)

STATIC_DIR = Path(__file__).parent / "static"


# ---------- request bodies ----------

class RegisterBody(BaseModel):
    email: str
    password: str


class ClusterBody(BaseModel):
    name: str


class JoinBody(BaseModel):
    join_code: str


class SettingsBody(BaseModel):
    claude_api_key: str


class BoardBody(BaseModel):
    name: str


class TicketBody(BaseModel):
    title: str
    body: str = ""
    status: str = "todo"
    target_worker: int | None = None


class TicketPatch(BaseModel):
    title: str | None = None
    body: str | None = None
    status: str | None = None
    target_worker: int | None = None
    clear_target: bool = False


class CommentBody(BaseModel):
    message: str


class WorkerRegisterBody(BaseModel):
    join_code: str
    name: str


class WorkResultBody(BaseModel):
    ok: bool
    comment: str | None = None


# ---------- app factory ----------

def create_app(db_url: str | None = None) -> FastAPI:
    engine = make_engine(db_url)
    Base.metadata.create_all(engine)
    SessionLocal = make_session_factory(engine)

    app = FastAPI(title="kanban-cloud")
    app.state.engine = engine

    def get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    # ----- auth dependencies -----

    def current_user(
        db: Session = Depends(get_db), authorization: str | None = Header(None)
    ) -> User:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "Missing bearer token")
        tok = db.get(AuthToken, authorization.removeprefix("Bearer ").strip())
        if tok is None:
            raise HTTPException(401, "Invalid token")
        return tok.user

    def current_worker(
        db: Session = Depends(get_db), x_worker_token: str | None = Header(None)
    ) -> Worker:
        if not x_worker_token:
            raise HTTPException(401, "Missing X-Worker-Token header")
        worker = db.scalar(select(Worker).where(Worker.token == x_worker_token))
        if worker is None:
            raise HTTPException(401, "Invalid worker token")
        return worker

    def require_member(db: Session, user: User, cluster_id: int) -> Cluster:
        cluster = db.get(Cluster, cluster_id)
        if cluster is None:
            raise HTTPException(404, "Cluster not found")
        member = db.scalar(
            select(ClusterMember).where(
                ClusterMember.cluster_id == cluster_id,
                ClusterMember.user_id == user.id,
            )
        )
        if member is None:
            raise HTTPException(403, "Not a member of this cluster")
        return cluster

    def board_for_user(db: Session, user: User, board_id: int) -> Board:
        board = db.get(Board, board_id)
        if board is None:
            raise HTTPException(404, "Board not found")
        require_member(db, user, board.cluster_id)
        return board

    def ticket_for_user(db: Session, user: User, ticket_id: int) -> Ticket:
        ticket = db.get(Ticket, ticket_id)
        if ticket is None:
            raise HTTPException(404, "Ticket not found")
        board_for_user(db, user, ticket.board_id)
        return ticket

    def ticket_json(db: Session, t: Ticket) -> dict:
        comments = db.scalars(
            select(Comment).where(Comment.ticket_id == t.id).order_by(Comment.created_at)
        ).all()
        return {
            "id": t.id,
            "board_id": t.board_id,
            "title": t.title,
            "body": t.body,
            "status": t.status,
            "created_by": t.created_by,
            "assigned_worker": t.assigned_worker,
            "target_worker": t.target_worker,
            "attempts": t.attempts,
            "created_at": t.created_at.isoformat(),
            "updated_at": t.updated_at.isoformat(),
            "comments": [
                {
                    "writer": c.writer,
                    "message": c.message,
                    "timestamp": c.created_at.isoformat(),
                }
                for c in comments
            ],
        }

    # ----- frontend -----

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/health")
    def health():
        return {"ok": True, "db": str(engine.url.drivername)}

    # ----- auth -----

    @app.post("/api/register")
    def register(body: RegisterBody, db: Session = Depends(get_db)):
        email = body.email.strip().lower()
        if not email or "@" not in email or len(body.password) < 4:
            raise HTTPException(400, "Valid email and password (min 4 chars) required")
        if db.scalar(select(User).where(User.email == email)):
            raise HTTPException(409, "Email already registered")
        user = User(email=email, password_hash=hash_password(body.password))
        db.add(user)
        db.flush()
        token = AuthToken(token=new_token(), user_id=user.id)
        db.add(token)
        db.commit()
        return {"token": token.token, "user": {"id": user.id, "email": user.email}}

    @app.post("/api/login")
    def login(body: RegisterBody, db: Session = Depends(get_db)):
        user = db.scalar(select(User).where(User.email == body.email.strip().lower()))
        if user is None or not verify_password(body.password, user.password_hash):
            raise HTTPException(401, "Bad email or password")
        token = AuthToken(token=new_token(), user_id=user.id)
        db.add(token)
        db.commit()
        return {"token": token.token, "user": {"id": user.id, "email": user.email}}

    @app.get("/api/me")
    def me(user: User = Depends(current_user)):
        return {"id": user.id, "email": user.email}

    # ----- clusters -----

    @app.post("/api/clusters")
    def create_cluster(
        body: ClusterBody, user: User = Depends(current_user), db: Session = Depends(get_db)
    ):
        cluster = Cluster(name=body.name.strip() or "Cluster", created_by=user.id)
        db.add(cluster)
        db.flush()
        db.add(ClusterMember(cluster_id=cluster.id, user_id=user.id))
        db.add(Board(cluster_id=cluster.id, name="Main"))
        db.commit()
        return {"id": cluster.id, "name": cluster.name, "join_code": cluster.join_code}

    @app.post("/api/clusters/join")
    def join_cluster(
        body: JoinBody, user: User = Depends(current_user), db: Session = Depends(get_db)
    ):
        cluster = db.scalar(
            select(Cluster).where(Cluster.join_code == body.join_code.strip().upper())
        )
        if cluster is None:
            raise HTTPException(404, "No cluster with that join code")
        exists = db.scalar(
            select(ClusterMember).where(
                ClusterMember.cluster_id == cluster.id, ClusterMember.user_id == user.id
            )
        )
        if not exists:
            db.add(ClusterMember(cluster_id=cluster.id, user_id=user.id))
            db.commit()
        return {"id": cluster.id, "name": cluster.name, "join_code": cluster.join_code}

    @app.get("/api/clusters")
    def my_clusters(user: User = Depends(current_user), db: Session = Depends(get_db)):
        rows = db.execute(
            select(Cluster)
            .join(ClusterMember, ClusterMember.cluster_id == Cluster.id)
            .where(ClusterMember.user_id == user.id)
            .order_by(Cluster.id)
        ).scalars().all()
        return [{"id": c.id, "name": c.name, "join_code": c.join_code} for c in rows]

    @app.get("/api/clusters/{cluster_id}/settings")
    def get_settings(
        cluster_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)
    ):
        require_member(db, user, cluster_id)
        settings = db.get(ClusterSettings, cluster_id)
        # Masked: the raw key is never returned to browsers after save.
        return {
            "cluster_id": cluster_id,
            "claude_api_key_masked": mask_secret(settings.claude_api_key if settings else None),
            "has_key": bool(settings and settings.claude_api_key),
        }

    @app.put("/api/clusters/{cluster_id}/settings")
    def put_settings(
        cluster_id: int,
        body: SettingsBody,
        user: User = Depends(current_user),
        db: Session = Depends(get_db),
    ):
        require_member(db, user, cluster_id)
        settings = db.get(ClusterSettings, cluster_id)
        if settings is None:
            settings = ClusterSettings(cluster_id=cluster_id)
            db.add(settings)
        settings.claude_api_key = body.claude_api_key.strip()
        db.commit()
        return {"ok": True, "claude_api_key_masked": mask_secret(settings.claude_api_key)}

    @app.get("/api/clusters/{cluster_id}/workers")
    def cluster_workers(
        cluster_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)
    ):
        require_member(db, user, cluster_id)
        now = utcnow()
        workers = db.scalars(
            select(Worker).where(Worker.cluster_id == cluster_id).order_by(Worker.name)
        ).all()
        return [
            {
                "id": w.id,
                "name": w.name,
                "status": w.status,
                "online": w.is_online(now),
                "last_seen": w.last_seen.isoformat(),
            }
            for w in workers
        ]

    @app.get("/api/clusters/{cluster_id}/queue")
    def cluster_queue(
        cluster_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)
    ):
        require_member(db, user, cluster_id)
        items = db.scalars(
            select(WorkItem)
            .where(WorkItem.cluster_id == cluster_id)
            .order_by(WorkItem.id.desc())
            .limit(50)
        ).all()
        return [
            {
                "id": i.id,
                "ticket_id": i.ticket_id,
                "status": i.status,
                "claimed_by": i.claimed_by,
                "queued_at": i.queued_at.isoformat(),
                "claimed_at": i.claimed_at.isoformat() if i.claimed_at else None,
                "finished_at": i.finished_at.isoformat() if i.finished_at else None,
            }
            for i in items
        ]

    # ----- boards & tickets -----

    @app.get("/api/clusters/{cluster_id}/boards")
    def list_boards(
        cluster_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)
    ):
        require_member(db, user, cluster_id)
        boards = db.scalars(
            select(Board).where(Board.cluster_id == cluster_id).order_by(Board.id)
        ).all()
        return [{"id": b.id, "name": b.name} for b in boards]

    @app.post("/api/clusters/{cluster_id}/boards")
    def create_board(
        cluster_id: int,
        body: BoardBody,
        user: User = Depends(current_user),
        db: Session = Depends(get_db),
    ):
        require_member(db, user, cluster_id)
        board = Board(cluster_id=cluster_id, name=body.name.strip() or "Board")
        db.add(board)
        db.commit()
        return {"id": board.id, "name": board.name}

    @app.get("/api/boards/{board_id}/tickets")
    def list_tickets(
        board_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)
    ):
        board_for_user(db, user, board_id)
        tickets = db.scalars(
            select(Ticket).where(Ticket.board_id == board_id).order_by(Ticket.id)
        ).all()
        return [ticket_json(db, t) for t in tickets]

    @app.post("/api/boards/{board_id}/tickets")
    def create_ticket(
        board_id: int,
        body: TicketBody,
        user: User = Depends(current_user),
        db: Session = Depends(get_db),
    ):
        board = board_for_user(db, user, board_id)
        if body.status not in TICKET_STATUSES:
            raise HTTPException(400, f"status must be one of {TICKET_STATUSES}")
        ticket = Ticket(
            board_id=board.id,
            title=body.title.strip(),
            body=body.body,
            status="todo",
            created_by=user.id,
            target_worker=body.target_worker,
        )
        if not ticket.title:
            raise HTTPException(400, "title required")
        db.add(ticket)
        db.commit()
        if body.status == "ready":
            delegation.enqueue_ticket(db, ticket)
        return ticket_json(db, ticket)

    @app.patch("/api/tickets/{ticket_id}")
    def patch_ticket(
        ticket_id: int,
        body: TicketPatch,
        user: User = Depends(current_user),
        db: Session = Depends(get_db),
    ):
        ticket = ticket_for_user(db, user, ticket_id)
        if body.title is not None:
            ticket.title = body.title.strip() or ticket.title
        if body.body is not None:
            ticket.body = body.body
        if body.clear_target:
            ticket.target_worker = None
        elif body.target_worker is not None:
            worker = db.get(Worker, body.target_worker)
            board = db.get(Board, ticket.board_id)
            if worker is None or worker.cluster_id != board.cluster_id:
                raise HTTPException(400, "target_worker must be a worker in this cluster")
            ticket.target_worker = body.target_worker
        if body.status is not None:
            if body.status not in TICKET_STATUSES:
                raise HTTPException(400, f"status must be one of {TICKET_STATUSES}")
            ticket.status = body.status
        ticket.updated_at = utcnow()
        db.commit()
        # Moving into "ready" queues the ticket for an agent.
        if body.status == "ready":
            delegation.enqueue_ticket(db, ticket)
        return ticket_json(db, ticket)

    @app.post("/api/tickets/{ticket_id}/run")
    def run_ticket(
        ticket_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)
    ):
        ticket = ticket_for_user(db, user, ticket_id)
        delegation.enqueue_ticket(db, ticket)
        return ticket_json(db, ticket)

    @app.post("/api/tickets/{ticket_id}/comments")
    def add_comment(
        ticket_id: int,
        body: CommentBody,
        user: User = Depends(current_user),
        db: Session = Depends(get_db),
    ):
        ticket = ticket_for_user(db, user, ticket_id)
        db.add(Comment(ticket_id=ticket.id, writer=user.email, message=body.message))
        db.commit()
        return ticket_json(db, ticket)

    @app.delete("/api/tickets/{ticket_id}")
    def delete_ticket(
        ticket_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)
    ):
        ticket = ticket_for_user(db, user, ticket_id)
        for c in db.scalars(select(Comment).where(Comment.ticket_id == ticket.id)):
            db.delete(c)
        for i in db.scalars(select(WorkItem).where(WorkItem.ticket_id == ticket.id)):
            db.delete(i)
        db.delete(ticket)
        db.commit()
        return {"ok": True}

    # ----- worker API -----

    @app.post("/api/workers/register")
    def worker_register(body: WorkerRegisterBody, db: Session = Depends(get_db)):
        cluster = db.scalar(
            select(Cluster).where(Cluster.join_code == body.join_code.strip().upper())
        )
        if cluster is None:
            raise HTTPException(404, "No cluster with that join code")
        name = body.name.strip()
        if not name:
            raise HTTPException(400, "worker name required")
        worker = db.scalar(
            select(Worker).where(Worker.cluster_id == cluster.id, Worker.name == name)
        )
        if worker is None:
            worker = Worker(cluster_id=cluster.id, name=name)
            db.add(worker)
        else:
            worker.token = new_token()  # re-registration rotates the token
        worker.last_seen = utcnow()
        db.commit()
        return {
            "worker_id": worker.id,
            "worker_token": worker.token,
            "cluster": {"id": cluster.id, "name": cluster.name},
        }

    @app.post("/api/work/poll")
    def work_poll(worker: Worker = Depends(current_worker), db: Session = Depends(get_db)):
        """Heartbeat + attempt to claim work. Returns {work: null} when idle."""
        db.add(worker)
        claim = delegation.claim_next(db, worker)
        return {"work": claim}

    @app.post("/api/work/{item_id}/result")
    def work_result(
        item_id: int,
        body: WorkResultBody,
        worker: Worker = Depends(current_worker),
        db: Session = Depends(get_db),
    ):
        db.add(worker)
        item = db.get(WorkItem, item_id)
        if item is None or item.claimed_by != worker.id:
            raise HTTPException(404, "No such assignment for this worker")
        if item.status != "claimed":
            raise HTTPException(409, "Assignment already finished")
        return delegation.finish_work(db, worker, item, body.ok, body.comment)

    @app.post("/api/work/{item_id}/progress")
    def work_progress(
        item_id: int,
        body: CommentBody,
        worker: Worker = Depends(current_worker),
        db: Session = Depends(get_db),
    ):
        db.add(worker)
        item = db.get(WorkItem, item_id)
        if item is None or item.claimed_by != worker.id:
            raise HTTPException(404, "No such assignment for this worker")
        worker.last_seen = utcnow()
        db.add(
            Comment(
                ticket_id=item.ticket_id,
                writer=f"worker:{worker.name}",
                message=body.message,
            )
        )
        db.commit()
        return {"ok": True}

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8900")))
