"""SQLAlchemy ORM models for kanban-cloud.

Status vocabulary (borrowed/adapted from the local .kanban tool):
  todo   - backlog
  ready  - queued for an agent (all prerequisites met; enqueued in work_queue)
  doing  - claimed / in progress (worker or human)
  review - agent finished, awaiting human review
  done   - completed
  failed - agent gave up after max attempts
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

TICKET_STATUSES = ["todo", "ready", "doing", "review", "done", "failed"]
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
    # The PC owns the limit; the server only displays it. Defaults keep rows
    # written by exes that predate these columns readable.
    concurrency: Mapped[int] = mapped_column(
        Integer, default=1, nullable=False, server_default="1"
    )
    running: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, server_default="0"
    )
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
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


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


class WorkItem(Base):
    """Work queue + assignment log. One row per delegation attempt."""
    __tablename__ = "work_queue"
    # Matches idx_work_queue_claim in schema.sql so auto-created DBs get it too.
    __table_args__ = (Index("idx_work_queue_claim", "cluster_id", "status", "queued_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), nullable=False)
    cluster_id: Mapped[int] = mapped_column(ForeignKey("clusters.id"), nullable=False)
    # queued | claimed | done | failed
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    claimed_by: Mapped[int | None] = mapped_column(ForeignKey("workers.id"), nullable=True)
    queued_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
    claimed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
