"""Database engine/session setup.

DATABASE_URL env var (Neon Postgres, sslmode=require) when set; otherwise an
automatic zero-setup SQLite fallback at ./kanban_cloud.db.
"""
import os

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from .models import Worker

DEFAULT_SQLITE_URL = "sqlite:///./kanban_cloud.db"

# (table, column DDL) pairs added by the "agents do real repo work" change.
# Every entry is nullable or carries a DEFAULT: existing rows back-fill from
# the default, and already-deployed workers never write these columns.
# One DDL string has to satisfy both backends, so boolean defaults are spelled
# FALSE, not 0 — SQLite takes either, Postgres rejects the integer outright.
_PHASE1_COLUMNS = [
    ("boards", "description TEXT"),
    ("boards", "out_of_scope TEXT"),
    ("boards", "commit_requirements TEXT"),
    ("boards", "use_worktrees BOOLEAN NOT NULL DEFAULT FALSE"),
    ("boards", "repo_url TEXT"),
    ("tickets", "session_id VARCHAR(64)"),
    ("workers", "concurrency INTEGER NOT NULL DEFAULT 1"),
    ("workers", "running INTEGER NOT NULL DEFAULT 0"),
]

# (table, column DDL) pairs added by the ticket-ordering change. "order" is a
# reserved word in both backends, so the DDL quotes it; _add_missing_columns
# strips the quotes back off before comparing against the inspector's column
# names.
_PHASE2_COLUMNS = [
    ("tickets", '"order" INTEGER NOT NULL DEFAULT 0'),
]


def resolve_db_url(db_url: str | None = None) -> str:
    url = db_url or os.environ.get("DATABASE_URL") or DEFAULT_SQLITE_URL
    # Normalize common Postgres URL spellings to the psycopg3 dialect.
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def make_engine(db_url: str | None = None):
    url = resolve_db_url(db_url)
    kwargs = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        # Neon requires TLS; add sslmode=require unless the URL already has one.
        if "sslmode=" not in url:
            kwargs["connect_args"] = {"sslmode": "require"}
        kwargs["pool_pre_ping"] = True
    return create_engine(url, **kwargs)


def make_session_factory(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def run_migrations(engine) -> None:
    """Idempotent v2 schema fixes for DBs created before the v2 rewrite.

    No alembic: the DBs that predate these columns are (near-)empty, so a few
    guarded statements at startup are enough. Both backends get the same
    treatment — a local SQLite dev DB from v1 has to reach the v2 shape too,
    or every ``workers`` query 500s with "no such column: workers.role_name".
    """
    backend = engine.url.get_backend_name()
    if backend == "postgresql":
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE workers ADD COLUMN IF NOT EXISTS role_name VARCHAR(64)"))
            conn.execute(text(
                "ALTER TABLE workers ADD COLUMN IF NOT EXISTS revoked BOOLEAN NOT NULL DEFAULT FALSE"
            ))
            conn.execute(text("ALTER TABLE workers DROP COLUMN IF EXISTS token"))
    elif backend == "sqlite":
        _migrate_sqlite_workers(engine)
    else:
        return
    # create_all() skips a table that already exists, indexes included, so DBs
    # older than 627ce17 never got the claim index declared on WorkItem.
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_work_queue_claim "
            "ON work_queue (cluster_id, status, queued_at)"
        ))
        # Workers now authenticate the Claude CLI with their own local config
        # instead of a cluster-stored key; drop the table (and, on Postgres,
        # the kanban_worker group's SELECT grant on it) from DBs created
        # before this change.
        conn.execute(text("DROP TABLE IF EXISTS cluster_settings"))
    _add_missing_columns(engine)


def _migrate_sqlite_workers(engine) -> None:
    """The v1 -> v2 `workers` changes, SQLite flavor.

    SQLite can add columns but cannot DROP the v1 `token` column (it sits under
    a UNIQUE constraint, which its DROP COLUMN refuses), so a v1 table is
    rebuilt from the model instead. Row ids are carried over, keeping
    tickets.assigned_worker / target_worker and work_queue.claimed_by valid.
    """
    insp = inspect(engine)
    if "workers" not in insp.get_table_names():
        return  # brand-new DB: create_all() already built the v2 shape
    columns = {c["name"] for c in insp.get_columns("workers")}
    with engine.begin() as conn:
        if "token" in columns:
            # legacy_alter_table keeps the rename from rewriting the FK clauses
            # in tickets/work_queue to point at the temporary table name.
            conn.execute(text("PRAGMA legacy_alter_table=ON"))
            conn.execute(text("ALTER TABLE workers RENAME TO workers_v1"))
            Worker.__table__.create(conn)
            conn.execute(text(
                "INSERT INTO workers "
                "(id, cluster_id, name, role_name, revoked, status, last_seen, created_at) "
                "SELECT id, cluster_id, name, NULL, 0, status, last_seen, created_at "
                "FROM workers_v1"
            ))
            conn.execute(text("DROP TABLE workers_v1"))
            conn.execute(text("PRAGMA legacy_alter_table=OFF"))
            return
        if "role_name" not in columns:
            conn.execute(text("ALTER TABLE workers ADD COLUMN role_name VARCHAR(64)"))
        if "revoked" not in columns:
            conn.execute(text("ALTER TABLE workers ADD COLUMN revoked BOOLEAN NOT NULL DEFAULT 0"))


def _add_missing_columns(engine) -> None:
    """Add any of `_PHASE1_COLUMNS` the database does not have yet.

    Backend-agnostic on purpose: it asks the database what columns exist rather
    than relying on `ADD COLUMN IF NOT EXISTS`, which SQLite does not support.
    That keeps one code path for Neon and for a developer's local SQLite file.
    """
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    with engine.begin() as conn:
        for table, ddl in _PHASE1_COLUMNS + _PHASE2_COLUMNS:
            if table not in tables:
                continue  # a brand-new DB: create_all() already built the shape
            column = ddl.split()[0].strip('"')
            if column in {c["name"] for c in insp.get_columns(table)}:
                continue
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))
