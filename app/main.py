"""kanban-cloud server: multi-user hosted kanban with work delegation to PCs.

Run:  py -m uvicorn app.main:app --host 0.0.0.0 --port 8900
Env:  DATABASE_URL (optional; Neon Postgres). Falls back to ./kanban_cloud.db.
      PROXY_SHARED_SECRET (optional; enables trusted-reverse-proxy mode — see
      "Deploying behind a reverse proxy" in README.md).
"""
import hmac
import os
import re
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import delegation, enrollment
from .auth import hash_password, mask_secret, verify_password
from .db import make_engine, make_session_factory, run_migrations
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

# ---------- reverse-proxy mode constants ----------

# The single worker-facing route left in v2: one-time enrollment. Everything
# else workers do is direct SQL against Postgres.
WORKER_EXEMPT_RE = re.compile(r"^/api/workers/enroll$")

# The only paths a SPECTATOR (read-only, unauthenticated-through-proxy visitor)
# may GET. Default-deny: everything else is 403. Deliberately excluded:
# /api/clusters (leaks join codes) and /api/clusters/{id}/settings (API-key
# state) — spectators get the default cluster id from /api/session instead.
SPECTATOR_ALLOWED_RE = re.compile(
    r"^(?:/"
    r"|/api/health"
    r"|/api/session"
    r"|/api/clusters/\d+/(?:boards|workers|queue)"
    r"|/api/boards/\d+/tickets"
    r")$"
)

DEFAULT_CLUSTER_NAME = "Main"
# Not a valid pbkdf2 record -> verify_password() always fails, so proxy-managed
# accounts can never be logged into with a password.
PROXY_PASSWORD_HASH = "proxy-auth"
# Leading/trailing hyphens are invalid in GitHub logins, so this synthetic
# account can never collide with a real proxied user named "spectator".
SPECTATOR_LOGIN = "-spectator-"


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


class WorkerEnrollBody(BaseModel):
    join_code: str
    name: str


# ---------- app factory ----------

def create_app(db_url: str | None = None, proxy_secret: str | None = None) -> FastAPI:
    engine = make_engine(db_url)
    Base.metadata.create_all(engine)
    run_migrations(engine)
    enrollment.ensure_worker_group(engine)
    SessionLocal = make_session_factory(engine)

    # Reverse-proxy mode is ON iff a (non-empty) shared secret is configured.
    # With it unset, every proxy-related branch below is dead code and the app
    # behaves exactly as before (local dev login/register).
    proxy_secret = proxy_secret if proxy_secret is not None else os.environ.get("PROXY_SHARED_SECRET")
    proxy_secret = proxy_secret or None

    app = FastAPI(title="kanban-cloud")
    app.state.engine = engine

    def get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    # ----- reverse-proxy gate -----

    @app.middleware("http")
    async def proxy_gate(request: Request, call_next):
        if proxy_secret is None:
            return await call_next(request)
        path = request.url.path
        if WORKER_EXEMPT_RE.match(path):
            # Worker routes keep their own token auth and bypass the proxy.
            return await call_next(request)
        provided = request.headers.get("x-proxy-secret", "")
        if not hmac.compare_digest(provided.encode(), proxy_secret.encode()):
            return JSONResponse({"detail": "Forbidden"}, status_code=403)
        login = (request.headers.get("x-proxy-user") or "").strip()
        readonly = request.headers.get("x-proxy-readonly") == "1"
        if login and not readonly:
            request.state.proxy_user = login
            return await call_next(request)
        # Spectator: server-side enforcement — safe whitelisted GETs only.
        request.state.proxy_spectator = True
        if request.method != "GET" or not SPECTATOR_ALLOWED_RE.match(path):
            return JSONResponse(
                {"detail": "Read-only spectator mode"}, status_code=403
            )
        return await call_next(request)

    # ----- proxy identity helpers -----

    def default_cluster(db: Session) -> Cluster | None:
        """The instance's default cluster: the oldest one (a proxy deployment
        is single-tenant, so the first cluster ever created is 'the' board)."""
        return db.scalar(select(Cluster).order_by(Cluster.id).limit(1))

    def get_or_create_proxy_user(
        db: Session, login: str, *, create_default_cluster: bool
    ) -> User:
        """Auto-provision a proxy-authenticated account and join it to the
        default cluster (creating that cluster on first use for owners)."""
        email = f"{login.strip().lower()[:100]}@proxy.user"
        user = db.scalar(select(User).where(User.email == email))
        if user is None:
            user = User(email=email, password_hash=PROXY_PASSWORD_HASH)
            db.add(user)
            db.flush()
        cluster = default_cluster(db)
        if cluster is None and create_default_cluster:
            cluster = Cluster(name=DEFAULT_CLUSTER_NAME, created_by=user.id)
            db.add(cluster)
            db.flush()
            db.add(Board(cluster_id=cluster.id, name=DEFAULT_CLUSTER_NAME))
        if cluster is not None:
            member = db.scalar(
                select(ClusterMember).where(
                    ClusterMember.cluster_id == cluster.id,
                    ClusterMember.user_id == user.id,
                )
            )
            if member is None:
                db.add(ClusterMember(cluster_id=cluster.id, user_id=user.id))
        db.commit()
        return user

    # ----- auth dependencies -----

    def current_user(
        request: Request,
        db: Session = Depends(get_db),
        authorization: str | None = Header(None),
    ) -> User:
        proxy_login = getattr(request.state, "proxy_user", None)
        if proxy_login:
            return get_or_create_proxy_user(
                db, proxy_login, create_default_cluster=True
            )
        if getattr(request.state, "proxy_spectator", False):
            # Only reachable on whitelisted GETs (the middleware blocked the
            # rest); reads are scoped to the default cluster via membership.
            return get_or_create_proxy_user(
                db, SPECTATOR_LOGIN, create_default_cluster=False
            )
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "Missing bearer token")
        tok = db.get(AuthToken, authorization.removeprefix("Bearer ").strip())
        if tok is None:
            raise HTTPException(401, "Invalid token")
        return tok.user

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

    @app.get("/api/session")
    def session_info(request: Request, db: Session = Depends(get_db)):
        """Who am I to this deployment?  Contract:
        - {"mode": "local"} — no PROXY_SHARED_SECRET; normal login UI applies.
        - {"mode": "owner", "user": {"id", "email"}} — trusted proxy supplied
          X-Proxy-User; account auto-provisioned, full rights, no login UI.
        - {"mode": "spectator", "cluster": {"id", "name"} | null} — read-only;
          `cluster` is the default cluster to render (null if none exists yet).
        """
        if proxy_secret is None:
            return {"mode": "local"}
        proxy_login = getattr(request.state, "proxy_user", None)
        if proxy_login:
            user = get_or_create_proxy_user(
                db, proxy_login, create_default_cluster=True
            )
            return {"mode": "owner", "user": {"id": user.id, "email": user.email}}
        cluster = default_cluster(db)
        return {
            "mode": "spectator",
            "cluster": {"id": cluster.id, "name": cluster.name} if cluster else None,
        }

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
                "revoked": w.revoked,
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
        if body.target_worker is not None:
            worker = db.get(Worker, body.target_worker)
            if worker is None or worker.cluster_id != board.cluster_id:
                raise HTTPException(400, "target_worker must be a worker in this cluster")
        ticket = Ticket(
            board_id=board.id,
            title=body.title.strip(),
            body=body.body,
            status=body.status,  # validated above; "ready" also enqueues below
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

    # ----- worker enrollment (the only worker-facing HTTP in v2) -----

    @app.post("/api/workers/enroll")
    def worker_enroll(body: WorkerEnrollBody, db: Session = Depends(get_db)):
        """Issue this PC its own Postgres credentials. Re-enrolling the same
        name rotates the password and clears any revocation."""
        if not enrollment.can_provision(engine):
            raise HTTPException(
                400, "Enrollment requires a Postgres DATABASE_URL on the server"
            )
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
            db.flush()
        worker.role_name = enrollment.role_name_for(cluster.id, worker.id)
        worker.revoked = False
        worker.last_seen = utcnow()
        db.commit()
        password = enrollment.provision_role(engine, worker.role_name)
        dsn = enrollment.build_worker_dsn(engine.url, worker.role_name, password)
        return {
            "worker_id": worker.id,
            "cluster": {"id": cluster.id, "name": cluster.name},
            "dsn": dsn,
        }

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8900")))
