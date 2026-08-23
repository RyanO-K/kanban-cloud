"""SQLAlchemy ORM models for kanban-cloud.

Status vocabulary (borrowed/adapted from the local .kanban tool):
  todo    - backlog
  ready   - queued for an agent (all prerequisites met; enqueued in work_queue)
  doing   - claimed / in progress (worker or human)
  blocked - needs a human: the agent raised a question, or it finished without
            landing its work on the remote (see worker.finish_work)
  done    - committed and pushed. Nothing else counts: work sitting on one
            worker PC's disk is not done, it is waiting on somebody.
  failed  - agent gave up after max attempts
  killed  - owner terminated a running attempt; distinct from failed, so it
            does not consume a retry attempt

There is no `review` status: it used to mean "the agent finished, now look at
it", which is precisely what blocked-without-a-push means today, so it was
folded into blocked (ticket #20; see app/db.py's _REMOVED_TICKET_STATUSES for
what happened to the rows that had it).
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

TICKET_STATUSES = ["todo", "ready", "doing", "blocked", "done", "failed", "killed"]
# Moving a ticket into this status queues it for an agent.
AGENT_READY_STATUS = "ready"

# The board's five columns, in display order, and the statuses each one holds.
# A column is a grouping, not a status: `failed` and `killed` stay distinct
# statuses because the retry and kill machinery reads them (a kill must not
# burn a retry attempt), but to a human all three mean the same thing — this
# one needs you — so they share the Blocked column and the card says which.
# The UI renders from its own copy of this (COLS in app/static/index.html);
# the server needs it because a drag reorder is per column, not per status.
BOARD_COLUMNS = [
    ("todo", "TODO", ("todo",)),
    ("ready", "Ready", ("ready",)),
    ("doing", "In progress", ("doing",)),
    ("blocked", "Blocked", ("blocked", "failed", "killed")),
    ("done", "Done", ("done",)),
]
COLUMN_STATUSES = {key: statuses for key, _label, statuses in BOARD_COLUMNS}


def column_statuses(column: str) -> tuple[str, ...]:
    """The statuses a column holds. An unknown key is treated as a bare status
    so a caller that still speaks in statuses (an older browser tab mid-deploy,
    a script) keeps working instead of silently addressing an empty column."""
    return COLUMN_STATUSES.get(column, (column,))
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


class ClusterSettings(Base):
    """Cluster-wide dispatch controls (gap analysis phase 2, item 5): a
    concurrency cap enforced inside the claim transaction itself (see
    worker.cluster_claim_gate) so it holds across N independent worker PCs
    with no central dispatcher, and a stop-all switch that blocks every claim
    outright. `enabled` toggles the cap without losing the configured number.
    One row per cluster, created alongside it (see app/main.py) and
    backfilled for older clusters by app/db.run_migrations.
    """
    __tablename__ = "cluster_settings"
    cluster_id: Mapped[int] = mapped_column(ForeignKey("clusters.id"), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="0")
    concurrency_cap: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stop_all_requested: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="0"
    )


class Profile(Base):
    """A named agent configuration, cluster-scoped: the tool allowlist, model
    and system prompt an agent run is launched with. The cloud counterpart of
    the local `.kanban` tool's profiles (gap analysis section 8, item 12).

    Referenced by id from `Board.default_profile_id` and `Ticket.profile_id`,
    both soft references (no FK-enforced cascade): deleting a profile that is
    still named by a board or ticket is allowed, and worker.resolve_profile
    treats the dangling id the same as "no profile chosen" rather than
    erroring, so a stale reference can never launch an agent with no tools.
    """
    __tablename__ = "profiles"
    __table_args__ = (UniqueConstraint("cluster_id", "name"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cluster_id: Mapped[int] = mapped_column(ForeignKey("clusters.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    allowed_tools: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)


class Board(Base):
    """A project. The metadata below is the context every agent working one of
    this board's tickets is given (see app/prompt.build_agent_prompt).

    `repo_url` is the shared clone source for every worker on this board —
    where its code lives on any given PC is a different thing: that folder is
    per-PC (the same board is worked by several machines with different
    layouts) and is either set by hand (`--set-path`) or derived from
    `repo_url` under the worker's own AppData folder. Neither the folder nor
    that derivation lives in this table.

    `default_profile_id` is this board's fallback agent profile: used when a
    ticket names none of its own (see Ticket.profile_id and
    worker.resolve_profile).

    `is_default` marks the board a visitor lands on (see main.default_board).
    At most one per cluster: marking one clears the rest, enforced by the app
    rather than an index, since "none marked" is legal and is where every
    cluster starts.
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
    default_profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("profiles.id"), nullable=True
    )
    # Opt-in switch (default off): a worker only pushes a finished ticket's
    # branch to origin when this is true, and even then only if the ticket's
    # commit_gate reports requirements_met — see worker.py's run_slot.
    auto_push: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="0"
    )
    is_default: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="0"
    )
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
    # Per-ticket profile override; beats the board's default_profile_id. NULL
    # falls through to the board, then to the worker's own --allowed-tools
    # default — see worker.resolve_profile.
    profile_id: Mapped[int | None] = mapped_column(ForeignKey("profiles.id"), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    # Claude CLI session id of the most recent attempt, so a human can take a
    # stuck run over with `claude --resume <id>`.
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Working directory that session ran in, reported by the worker once
    # resolve_directory() has picked one. Claude scopes sessions per-cwd, so
    # the resume command the UI offers is `cd '<session_dir>'; claude --resume
    # <session_id>` — without the directory the id alone finds nothing.
    session_dir: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # Drag-order rank, ascending; see CLAIM_ORDER_BY in worker.py.
    order: Mapped[int] = mapped_column(Integer, default=0, nullable=False, server_default="0")
    # NULL = not yet triaged (or a pre-triage row). Set once, by initial triage
    # (worker.triage_todo_tickets), which also promotes the ticket to ready;
    # never overwritten afterward. See app/triage.py.
    model: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # JSON-encoded {"requirements_met": bool, "summary": str}, the agent's
    # self-reported verdict on the board's commit_requirements. Written by
    # worker.py's finish_work; see app/prompt.py's parse_commit_gate.
    commit_gate: Mapped[str | None] = mapped_column(Text, nullable=True)
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


# A dependency is satisfied only once the prerequisite ticket is done — which
# now means committed and pushed, so a dependent ticket's agent can actually
# fetch the work it was waiting for rather than racing a branch that only
# exists on some other PC's disk. Keep in sync with worker.py's CLAIM_SQL,
# which encodes the same rule directly in the claim predicate (DEPS_MET_SQL).
DEP_MET_STATUSES = ("done",)


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
    # Set when this row exists because a blocked ticket's question was just
    # answered. worker.py's claim_next reads it to continue the ticket's
    # prior Claude CLI session instead of restarting from scratch.
    resume: Mapped[bool] = mapped_column(
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
