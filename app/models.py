"""SQLAlchemy ORM models for kanban-cloud.

Status vocabulary (borrowed/adapted from the local .kanban tool):
  todo    - backlog
  ready   - queued for an agent (all prerequisites met; enqueued in work_queue)
  doing   - claimed / in progress (worker or human)
  blocked - agent raised a question and is parked awaiting a human answer
  review  - agent finished, awaiting human review
  done    - completed
  failed  - agent gave up after max attempts
  killed  - owner terminated a running attempt; distinct from failed, so it
            does not consume a retry attempt
"""
import datetime
import secrets
import string

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

TICKET_STATUSES = ["todo", "ready", "doing", "blocked", "review", "done", "failed", "killed"]
# Moving a ticket into this status queues it for an agent.
AGENT_READY_STATUS = "ready"
MAX_ATTEMPTS = 2  # keep in sync with worker.py MAX_ATTEMPTS
WORKER_ONLINE_SECONDS = 30


def utcnow() -> datetime.datetime:
    """Naive UTC timestamp (stored consistently across SQLite and Postgres)."""
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def new_join_code() -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(8))


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)


class AuthToken(Base):
    __tablename__ = "auth_tokens"
    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)

    user: Mapped[User] = relationship()


class Cluster(Base):
    __tablename__ = "clusters"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    join_code: Mapped[str] = mapped_column(String(16), unique=True, nullable=False, default=new_join_code)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)


class ClusterMember(Base):
    __tablename__ = "cluster_members"
    __table_args__ = (UniqueConstraint("cluster_id", "user_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cluster_id: Mapped[int] = mapped_column(ForeignKey("clusters.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    joined_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)


class Board(Base):
    """A project. The metadata below is the context every agent working one of
    this board's tickets is given (see app/prompt.build_agent_prompt).

    `repo_url` is the shared clone source for every worker on this board —
    where its code lives on any given PC is a different thing: that folder is
    per-PC (the same board is worked by several machines with different
    layouts) and is either set by hand (`--set-path`) or derived from
    `repo_url` under the worker's own AppData folder. Neither the folder nor
    that derivation lives in this table.
    """
    __tablename__ = "boards"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cluster_id: Mapped[int] = mapped_column(ForeignKey("clusters.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    out_of_scope: Mapped[str | None] = mapped_column(Text, nullable=True)
    commit_requirements: Mapped[str | None] = mapped_column(Text, nullable=True)
    use_worktrees: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="0"
    )
    repo_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)


class Worker(Base):
    __tablename__ = "workers"
    __table_args__ = (UniqueConstraint("cluster_id", "name"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cluster_id: Mapped[int] = mapped_column(ForeignKey("clusters.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Per-PC Postgres role issued at enrollment; None until first enroll.
    role_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="idle")  # idle | working
    # How many tickets this PC will run at once, and how many are running now.
    # Reported by the worker itself every heartbeat; defaults keep rows written
    # by exes that predate these columns readable.
    concurrency: Mapped[int] = mapped_column(
        Integer, default=1, nullable=False, server_default="1"
    )
    running: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, server_default="0"
    )
    # Website-set concurrency request (ticket #18). NULL = the PC picks its own
    # limit (flag/local config), same as before this column existed. When set,
    # the worker honors it ahead of its local config the next time it starts
    # — see worker.py's resolve_concurrency/fetch_worker_settings.
    desired_concurrency: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_seen: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)

    def is_online(self, now: datetime.datetime | None = None) -> bool:
        now = now or utcnow()
        return (now - self.last_seen).total_seconds() <= WORKER_ONLINE_SECONDS


class Ticket(Base):
    __tablename__ = "tickets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    board_id: Mapped[int] = mapped_column(ForeignKey("boards.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    body: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="todo")
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    assigned_worker: Mapped[int | None] = mapped_column(ForeignKey("workers.id"), nullable=True)
    # NULL target_worker = "any worker in the cluster may claim".
    target_worker: Mapped[int | None] = mapped_column(ForeignKey("workers.id"), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    # Claude CLI session id of the most recent attempt, so a human can take a
    # stuck run over with `claude --resume <id>`.
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Drag-order rank, ascending; see CLAIM_ORDER_BY in worker.py.
    order: Mapped[int] = mapped_column(Integer, default=0, nullable=False, server_default="0")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class TicketDep(Base):
    """A dependency edge: `ticket_id` cannot be claimed until `depends_on_id`
    reaches DEP_MET_STATUSES. No cluster_id of its own — both tickets carry
    one via their board, and the app layer enforces they match. `blocks`
    (the reverse edge) is derived by querying this table by depends_on_id
    rather than stored."""
    __tablename__ = "ticket_deps"
    __table_args__ = (UniqueConstraint("ticket_id", "depends_on_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), nullable=False)
    depends_on_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), nullable=False)


# A dependency is satisfied once the prerequisite ticket reaches either of
# these statuses. Keep in sync with worker.py's CLAIM_SQL, which encodes the
# same rule directly in the claim predicate (see DEPS_MET_SQL there).
DEP_MET_STATUSES = ("done", "review")


class Comment(Base):
    __tablename__ = "comments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), nullable=False)
    writer: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)


class TicketChat(Base):
    """A human's mid-run message queued for delivery to the agent's live
    stdin — the cloud analogue of the local tool's per-run JSONL inbox.
    `delivered_at` is set once the worker's chat pump has written the row to
    the CLI's stdin; unset rows are what a pump run picks up, in id order,
    whether they were queued before the agent started or typed while it was
    already running.
    """
    __tablename__ = "ticket_chat"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), nullable=False)
    sender: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
    delivered_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)


class TicketLog(Base):
    """Fine-grained live transcript of one agent run, one row per parsed
    stream-json turn as the worker reads it — the cloud analogue of the local
    `.kanban` tool's per-run log file. Kept in Postgres rather than a local
    file because worker PCs have no durable/shared disk and no inbound
    reachability from a browser: the DB is what makes a run's transcript
    visible from any browser live, and what survives a worker PC's disk being
    wiped (the worker also writes the raw stream to a local file per run, but
    that copy is best-effort/debugging-only). `work_queue_id` scopes rows to
    one attempt, since a retried ticket's earlier attempt has its own
    transcript; `seq` is assigned by the worker (the only writer for a given
    run) and is what a live viewer's `since_seq` polling tails.
    """
    __tablename__ = "ticket_log"
    __table_args__ = (Index("idx_ticket_log_ticket_seq", "ticket_id", "seq"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), nullable=False)
    work_queue_id: Mapped[int | None] = mapped_column(ForeignKey("work_queue.id"), nullable=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(16), default="assistant", nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)


class WorkItem(Base):
    """Work queue + assignment log. One row per delegation attempt."""
    __tablename__ = "work_queue"
    # Matches idx_work_queue_claim in schema.sql so auto-created DBs get it too.
    __table_args__ = (Index("idx_work_queue_claim", "cluster_id", "status", "queued_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), nullable=False)
    cluster_id: Mapped[int] = mapped_column(ForeignKey("clusters.id"), nullable=False)
    # queued | claimed | done | failed | killed
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    claimed_by: Mapped[int | None] = mapped_column(ForeignKey("workers.id"), nullable=True)
    # Owner-requested cancellation of the in-flight claim; the worker polls
    # this while the agent runs and terminates the child process when set.
    kill_requested: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="0"
    )
    queued_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
    claimed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    # Set at claim time and refreshed periodically by the running slot while
    # the executor is in flight (worker.py's _claim_heartbeat_loop). A claim
    # whose heartbeat goes stale (dead worker PC) is what the reaper looks for
    # — claimed_at alone can't tell a dead claim from a slow-but-alive one.
    heartbeat_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)


class TicketQuestion(Base):
    """An agent's human-in-the-loop escalation (cloud counterpart of the local
    tool's `orchestrator.question`). Raised by the worker when the agent's
    reply carries the `KANBAN_QUESTION:` marker (see app/prompt.parse_question);
    answering it is what auto-requeues the ticket (see delegation.py).
    """
    __tablename__ = "ticket_questions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    # input | choice
    type: Mapped[str] = mapped_column(String(16), default="input", nullable=False)
    format: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # JSON-encoded list of strings; only meaningful for type="choice".
    options: Mapped[str | None] = mapped_column(Text, nullable=True)
    multi: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    answer_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    answer_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
    answered_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
