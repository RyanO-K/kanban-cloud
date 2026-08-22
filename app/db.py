"""Database engine/session setup.

DATABASE_URL env var (Neon Postgres, sslmode=require) when set; otherwise an
automatic zero-setup SQLite fallback at ./kanban_cloud.db.
"""
import os

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

DEFAULT_SQLITE_URL = "sqlite:///./kanban_cloud.db"

# (table, column DDL) pairs added by the "agents do real repo work" change.
# Every entry is nullable or carries a DEFAULT: existing rows back-fill from
# the default, and already-deployed workers never write these columns.
_PHASE1_COLUMNS = [
    ("boards", "description TEXT"),
    ("boards", "out_of_scope TEXT"),
    ("boards", "commit_requirements TEXT"),
    ("boards", "use_worktrees BOOLEAN NOT NULL DEFAULT 0"),
    ("tickets", "session_id VARCHAR(64)"),
    ("workers", "concurrency INTEGER NOT NULL DEFAULT 1"),
    ("workers", "running INTEGER NOT NULL DEFAULT 0"),
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
    """Idempotent v2 schema fixes for existing Postgres DBs.

    No alembic: the prod DB predates these columns but is (near-)empty, so a
    few guarded ALTERs at startup are enough. SQLite DBs are scratch files —
    delete and let create_all rebuild them instead.
    """
    if engine.url.get_backend_name() == "postgresql":
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE workers ADD COLUMN IF NOT EXISTS role_name VARCHAR(64)"))
            conn.execute(text(
                "ALTER TABLE workers ADD COLUMN IF NOT EXISTS revoked BOOLEAN NOT NULL DEFAULT FALSE"
            ))
            conn.execute(text("ALTER TABLE workers DROP COLUMN IF EXISTS token"))
    _add_missing_columns(engine)


def _add_missing_columns(engine) -> None:
    """Add any of `_PHASE1_COLUMNS` the database does not have yet.

    Backend-agnostic on purpose: it asks the database what columns exist rather
    than relying on `ADD COLUMN IF NOT EXISTS`, which SQLite does not support.
    That keeps one code path for Neon and for a developer's local SQLite file.
    """
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    with engine.begin() as conn:
        for table, ddl in _PHASE1_COLUMNS:
            if table not in tables:
                continue  # a brand-new DB: create_all() already built the shape
            column = ddl.split()[0]
            if column in {c["name"] for c in insp.get_columns(table)}:
                continue
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))
