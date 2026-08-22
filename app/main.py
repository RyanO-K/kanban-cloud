"""kanban-cloud server: multi-user hosted kanban with work delegation to PCs.

Run:  py -m uvicorn app.main:app --host 0.0.0.0 --port 8900
Env:  DATABASE_URL (optional; Neon Postgres). Falls back to ./kanban_cloud.db.
      PROXY_SHARED_SECRET (optional; enables trusted-reverse-proxy mode — see
      "Deploying behind a reverse proxy" in README.md).
      PROXY_LOGIN_URL (optional; a site-relative URL the spectator UI offers as
      a "Sign in with GitHub" button — e.g. /auth/github?return=/board/).
"""
import hmac
import json
import os
import re
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import delegation, enrollment, importer
from .auth import hash_password, verify_password
from .db import make_engine, make_session_factory, run_migrations
from .models import (
    AGENT_READY_STATUS,
    DEP_MET_STATUSES,
    TICKET_STATUSES,
    AuthToken,
    Base,
    Board,
    Cluster,
    ClusterMember,
    Comment,
    Ticket,
    TicketDep,
    TicketQuestion,
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
# /api/clusters (leaks join codes) — spectators get the default cluster id
# from /api/session instead.
SPECTATOR_ALLOWED_RE = re.compile(
    r"^(?:/"
    r"|/b/\d+"
    r"|/api/health"
    r"|/api/session"
    r"|/api/clusters/\d+/(?:boards|workers|queue|blocked)"
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


class BoardBody(BaseModel):
    name: str


class BoardPatch(BaseModel):
    """A board's project metadata. Every field is optional so patches are
    partial — one panel saving must not blank a field it did not send."""
    description: str | None = None
    out_of_scope: str | None = None
    commit_requirements: str | None = None
    use_worktrees: bool | None = None
    repo_url: str | None = None


class ImportBody(BaseModel):
    """A local .kanban board folder, as read and key-whitelisted by the browser.

    `tickets` holds raw local ticket objects; app/importer.py owns every
    decision about what they mean.
    """
    name: str
    tickets: list[dict]


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
    # None = leave dependencies unchanged; [] = clear them; a list replaces
    # the full set (the ticket editor sends its whole picklist selection).
    depends_on: list[int] | None = None


class ReorderBody(BaseModel):
    """New drag order for every ticket in one status column of one board."""
    status: str
    ticket_ids: list[int]


class CommentBody(BaseModel):
    message: str


class AnswerBody(BaseModel):
    """A human's answer to an agent's question. `notes` is authoritative over
    `value` when both are given — same convention as the local .kanban tool's
    answer shape."""
    value: str
    notes: str | None = None


class WorkerEnrollBody(BaseModel):
    join_code: str
    name: str


# ---------- app factory ----------

def create_app(
    db_url: str | None = None,
    proxy_secret: str | None = None,
    proxy_login_url: str | None = None,
) -> FastAPI:
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

    # Where the spectator UI points its sign-in button. Site-specific, so it is
    # configuration rather than a hardcoded path: kanban-cloud does not know or
    # care that the proxy in front of it is a portfolio site.
    proxy_login_url = (
        proxy_login_url
        if proxy_login_url is not None
        else os.environ.get("PROXY_LOGIN_URL")
    ) or None

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

    def default_board(db: Session, cluster: Cluster | None) -> Board | None:
        """The board a visitor should land on: the demo board when one exists,
        otherwise the cluster's first board."""
        if cluster is None:
            return None
        boards = db.scalars(
            select(Board).where(Board.cluster_id == cluster.id).order_by(Board.id)
        ).all()
        for board in boards:
            if board.name.strip().lower() == "demo":
                return board
        return boards[0] if boards else None

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

    def board_json(b: Board) -> dict:
        return {
            "id": b.id,
            "cluster_id": b.cluster_id,
            "name": b.name,
            "description": b.description,
            "out_of_scope": b.out_of_scope,
            "commit_requirements": b.commit_requirements,
            "use_worktrees": bool(b.use_worktrees),
            "repo_url": b.repo_url,
        }

    def depends_on_ids(db: Session, ticket_id: int) -> list[int]:
        return list(
            db.scalars(select(TicketDep.depends_on_id).where(TicketDep.ticket_id == ticket_id))
        )

    def blocks_ids(db: Session, ticket_id: int) -> list[int]:
        """Reverse edge, derived rather than stored: tickets that name this
        one as a dependency."""
        return list(
            db.scalars(select(TicketDep.ticket_id).where(TicketDep.depends_on_id == ticket_id))
        )

    def is_blocked(db: Session, ticket_id: int) -> bool:
        dep_ids = depends_on_ids(db, ticket_id)
        if not dep_ids:
            return False
        unmet = db.scalar(
            select(Ticket.id).where(
                Ticket.id.in_(dep_ids), Ticket.status.notin_(DEP_MET_STATUSES)
            ).limit(1)
        )
        return unmet is not None

    def dep_creates_cycle(db: Session, ticket_id: int, new_dep_ids: list[int]) -> bool:
        """True if setting ticket_id's dependencies to new_dep_ids would let
        the graph reach back to ticket_id (a self-dependency is the ticket_id
        == 1 case). Walks forward from each candidate dep over the *existing*
        edges of every other ticket — ticket_id's own current edges are about
        to be replaced, so they are irrelevant here."""
        seen = set()
        stack = list(new_dep_ids)
        while stack:
            node = stack.pop()
            if node == ticket_id:
                return True
            if node in seen:
                continue
            seen.add(node)
            stack.extend(
                db.scalars(select(TicketDep.depends_on_id).where(TicketDep.ticket_id == node))
            )
        return False

    def set_ticket_deps(db: Session, ticket: Ticket, board: Board, new_dep_ids: list[int]) -> None:
        new_dep_ids = sorted(set(new_dep_ids))
        if new_dep_ids:
            rows = db.scalars(select(Ticket).where(Ticket.id.in_(new_dep_ids))).all()
            found = {t.id for t in rows}
            missing = set(new_dep_ids) - found
            if missing:
                raise HTTPException(400, f"unknown ticket id(s) in depends_on: {sorted(missing)}")
            for dep in rows:
                dep_board = db.get(Board, dep.board_id)
                if dep_board.cluster_id != board.cluster_id:
                    raise HTTPException(400, "depends_on must reference tickets in this cluster")
            if dep_creates_cycle(db, ticket.id, new_dep_ids):
                raise HTTPException(400, "that dependency would create a cycle")
        for existing in db.scalars(select(TicketDep).where(TicketDep.ticket_id == ticket.id)):
            db.delete(existing)
        for dep_id in new_dep_ids:
            db.add(TicketDep(ticket_id=ticket.id, depends_on_id=dep_id))

    def ticket_question_json(q: TicketQuestion) -> dict:
        return {
            "id": q.id,
            "ticket_id": q.ticket_id,
            "question": q.question,
            "type": q.type,
            "format": q.format,
            "options": json.loads(q.options) if q.options else None,
            "multi": bool(q.multi),
            "answer_value": q.answer_value,
            "answer_notes": q.answer_notes,
            "created_at": q.created_at.isoformat(),
            "answered_at": q.answered_at.isoformat() if q.answered_at else None,
        }

    def open_question(db: Session, ticket_id: int) -> TicketQuestion | None:
        return db.scalar(
            select(TicketQuestion)
            .where(TicketQuestion.ticket_id == ticket_id, TicketQuestion.answered_at.is_(None))
            .order_by(TicketQuestion.id.desc())
        )

    def ticket_json(db: Session, t: Ticket) -> dict:
        comments = db.scalars(
            select(Comment).where(Comment.ticket_id == t.id).order_by(Comment.created_at)
        ).all()
        question = open_question(db, t.id)
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
            "order": t.order,
            "created_at": t.created_at.isoformat(),
            "updated_at": t.updated_at.isoformat(),
            "depends_on": depends_on_ids(db, t.id),
            "blocks": blocks_ids(db, t.id),
            "blocked": is_blocked(db, t.id),
            "question": ticket_question_json(question) if question else None,
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

    @app.get("/b/{board_id}")
    def board_page(board_id: int):
        """Same SPA shell as '/' — the frontend reads the board id from the
        URL on boot so a refresh (or a shared link) lands back on this board
        instead of whatever was first alphabetically."""
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
        - {"mode": "spectator", "cluster": {...}|null, "board": {...}|null,
          "login_url": str|null} — read-only; `cluster` is the default cluster
          to render (null if none exists yet), `board` is the board to land on
          (the demo board if there is one), and `login_url`, when set, is where
          the sign-in button sends the visitor.
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
        board = default_board(db, cluster)
        return {
            "mode": "spectator",
            "cluster": {"id": cluster.id, "name": cluster.name} if cluster else None,
            "board": {"id": board.id, "name": board.name} if board else None,
            "login_url": proxy_login_url,
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
                # The PC's own limit and current load. Advisory: the server
                # displays these, it does not schedule on them.
                "concurrency": w.concurrency,
                "running": w.running,
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
                "kill_requested": bool(i.kill_requested),
                "queued_at": i.queued_at.isoformat(),
                "claimed_at": i.claimed_at.isoformat() if i.claimed_at else None,
                "finished_at": i.finished_at.isoformat() if i.finished_at else None,
            }
            for i in items
        ]

    @app.get("/api/clusters/{cluster_id}/blocked")
    def blocked_tickets(
        cluster_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)
    ):
        """Every blocked ticket in the cluster with its open question,
        across all boards — what the notification bell polls."""
        require_member(db, user, cluster_id)
        board_ids = db.scalars(
            select(Board.id).where(Board.cluster_id == cluster_id)
        ).all()
        if not board_ids:
            return []
        tickets = db.scalars(
            select(Ticket)
            .where(Ticket.board_id.in_(board_ids), Ticket.status == "blocked")
            .order_by(Ticket.updated_at.desc())
        ).all()
        out = []
        for t in tickets:
            q = open_question(db, t.id)
            out.append({
                "ticket_id": t.id,
                "board_id": t.board_id,
                "title": t.title,
                "question": ticket_question_json(q) if q else None,
            })
        return out

    # ----- boards & tickets -----

    @app.get("/api/clusters/{cluster_id}/boards")
    def list_boards(
        cluster_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)
    ):
        require_member(db, user, cluster_id)
        boards = db.scalars(
            select(Board).where(Board.cluster_id == cluster_id).order_by(Board.id)
        ).all()
        return [board_json(b) for b in boards]

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
        return board_json(board)

    @app.get("/api/boards/{board_id}")
    def get_board(
        board_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)
    ):
        """A single board's metadata, including its cluster — how the
        frontend resolves a `/b/{id}` URL to a cluster it may not have
        selected yet."""
        board = board_for_user(db, user, board_id)
        return board_json(board)

    @app.patch("/api/boards/{board_id}")
    def patch_board(
        board_id: int,
        body: BoardPatch,
        user: User = Depends(current_user),
        db: Session = Depends(get_db),
    ):
        """Edit a board's project metadata — the context agents are given.

        Partial by design: a field absent from the request keeps its stored
        value, while an explicit empty string clears it.
        """
        board = board_for_user(db, user, board_id)
        for field in ("description", "out_of_scope", "commit_requirements",
                      "use_worktrees", "repo_url"):
            value = getattr(body, field)
            if value is not None:
                setattr(board, field, value)
        db.commit()
        return board_json(board)

    @app.post("/api/clusters/{cluster_id}/import")
    def import_board(
        cluster_id: int,
        body: ImportBody,
        user: User = Depends(current_user),
        db: Session = Depends(get_db),
    ):
        """Create a new board from a local .kanban board folder.

        Always creates; never merges into an existing board. A name collision
        gets a "(2)" suffix, so re-importing the same folder is safe and leaves
        the earlier copy alone.
        """
        require_member(db, user, cluster_id)
        if len(body.tickets) > importer.MAX_IMPORT_TICKETS:
            raise HTTPException(
                400, f"too many tickets (limit {importer.MAX_IMPORT_TICKETS})"
            )

        slug = (body.name or "").strip() or importer.DEFAULT_BOARD_NAME
        ordered = sorted(body.tickets, key=lambda t: importer.sort_key(
            t.get("id") if isinstance(t, dict) else None
        ))
        prepared = []
        skipped = 0
        for raw in ordered:
            local_id = raw.get("id") if isinstance(raw, dict) else None
            normalized = importer.normalize_ticket(raw, slug, local_id)
            if normalized is None:
                skipped += 1
                continue
            prepared.append(normalized)

        # Nothing usable: don't leave an empty board behind as a side effect.
        if not prepared:
            raise HTTPException(400, "no tickets to import")

        existing = db.scalars(
            select(Board.name).where(Board.cluster_id == cluster_id)
        ).all()
        board = Board(
            cluster_id=cluster_id,
            name=importer.unique_board_name(list(existing), slug),
        )
        db.add(board)
        db.flush()

        tickets = []
        for item in prepared:
            ticket = Ticket(
                board_id=board.id,
                title=item["title"],
                body=item["body"],
                status=item["status"],
                created_by=user.id,
            )
            db.add(ticket)
            db.flush()
            for c in item["comments"]:
                db.add(
                    Comment(
                        ticket_id=ticket.id,
                        writer=c["writer"],
                        message=c["message"],
                        created_at=c["created_at"],
                    )
                )
            tickets.append(ticket)
        db.commit()

        # Imported "ready" tickets are handed to agents, same as moving a
        # ticket to ready in the UI. Enqueued after the commit so a failure
        # here cannot roll back the import itself.
        queued = 0
        for ticket in tickets:
            if ticket.status == AGENT_READY_STATUS:
                delegation.enqueue_ticket(db, ticket)
                queued += 1

        return {
            "board_id": board.id,
            "name": board.name,
            "imported": len(tickets),
            "skipped": skipped,
            "queued": queued,
        }

    @app.get("/api/boards/{board_id}/tickets")
    def list_tickets(
        board_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)
    ):
        board_for_user(db, user, board_id)
        tickets = db.scalars(
            select(Ticket).where(Ticket.board_id == board_id)
            .order_by(Ticket.order, Ticket.id)
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

    @app.patch("/api/boards/{board_id}/reorder")
    def reorder_tickets(
        board_id: int,
        body: ReorderBody,
        user: User = Depends(current_user),
        db: Session = Depends(get_db),
    ):
        """Persist a drag reorder: `ticket_ids` is the full new top-to-bottom
        order of one status column. Reindexed as 0..n-1 so ties among
        never-dragged tickets elsewhere on the board are unaffected."""
        board = board_for_user(db, user, board_id)
        column = db.scalars(
            select(Ticket).where(Ticket.board_id == board.id, Ticket.status == body.status)
        ).all()
        by_id = {t.id: t for t in column}
        if set(body.ticket_ids) != set(by_id):
            raise HTTPException(400, "ticket_ids must match every ticket in that column")
        for index, ticket_id in enumerate(body.ticket_ids):
            by_id[ticket_id].order = index
        db.commit()
        return [ticket_json(db, by_id[i]) for i in body.ticket_ids]

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
        if body.depends_on is not None:
            board = db.get(Board, ticket.board_id)
            set_ticket_deps(db, ticket, board, body.depends_on)
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

    @app.post("/api/tickets/{ticket_id}/kill")
    def kill_ticket(
        ticket_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)
    ):
        """Ask the worker holding this ticket's active claim to stop.

        A no-op (not an error) when there is no outstanding claim — e.g. the
        agent already finished, or the ticket was never running — so a kill
        that loses the race never turns into a spurious failure.
        """
        ticket = ticket_for_user(db, user, ticket_id)
        item = db.scalar(
            select(WorkItem).where(
                WorkItem.ticket_id == ticket.id, WorkItem.status == "claimed"
            )
        )
        if item is not None:
            item.kill_requested = True
            db.commit()
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

    @app.post("/api/tickets/{ticket_id}/questions/{question_id}/answer")
    def answer_question(
        ticket_id: int,
        question_id: int,
        body: AnswerBody,
        user: User = Depends(current_user),
        db: Session = Depends(get_db),
    ):
        """A human resolves an agent's escalation. Auto-requeues the ticket
        exactly once: an already-answered question 409s rather than enqueuing
        a second work item."""
        ticket = ticket_for_user(db, user, ticket_id)
        question = db.get(TicketQuestion, question_id)
        if question is None or question.ticket_id != ticket.id:
            raise HTTPException(404, "Question not found")
        if question.answered_at is not None:
            raise HTTPException(409, "Question already answered")
        question.answer_value = body.value
        question.answer_notes = body.notes
        question.answered_at = utcnow()
        note = f" ({body.notes})" if body.notes else ""
        db.add(Comment(
            ticket_id=ticket.id, writer=user.email,
            message=f"Answered: {body.value}{note}",
        ))
        db.commit()
        if ticket.status == "blocked":
            delegation.enqueue_ticket(db, ticket)
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
        for d in db.scalars(
            select(TicketDep).where(
                (TicketDep.ticket_id == ticket.id) | (TicketDep.depends_on_id == ticket.id)
            )
        ):
            db.delete(d)
        for q in db.scalars(select(TicketQuestion).where(TicketQuestion.ticket_id == ticket.id)):
            db.delete(q)
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

    @app.post("/api/workers/{worker_id}/revoke")
    def worker_revoke(
        worker_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)
    ):
        """Kill a PC's DB credentials. Enforced at the database: the role is
        dropped, live sessions terminated. Re-enrolling restores access."""
        worker = db.get(Worker, worker_id)
        if worker is None:
            raise HTTPException(404, "Worker not found")
        require_member(db, user, worker.cluster_id)
        worker.revoked = True
        worker.status = "idle"
        db.commit()
        if worker.role_name and enrollment.can_provision(engine):
            enrollment.revoke_role(engine, worker.role_name)
        return {"ok": True}

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8900")))
