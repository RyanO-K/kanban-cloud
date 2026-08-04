"""Database engine/session setup.

DATABASE_URL env var (Neon Postgres, sslmode=require) when set; otherwise an
automatic zero-setup SQLite fallback at ./kanban_cloud.db.
"""
import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

DEFAULT_SQLITE_URL = "sqlite:///./kanban_cloud.db"


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
