"""Schema v2: workers get role_name/revoked, lose token."""
import sqlalchemy as sa

from app.db import make_engine, run_migrations
from app.models import Base, Worker


def test_worker_model_has_v2_columns_and_no_token():
    cols = {c.name for c in Worker.__table__.columns}
    assert "role_name" in cols
    assert "revoked" in cols
    assert "token" not in cols


def test_run_migrations_noop_on_sqlite(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'm.db'}")
    Base.metadata.create_all(engine)
    run_migrations(engine)  # must not raise
    insp = sa.inspect(engine)
    assert "workers" in insp.get_table_names()


def test_models_have_phase1_columns():
    from app.models import Board, Ticket
    board_cols = {c.name for c in Board.__table__.columns}
    assert {"description", "out_of_scope", "commit_requirements",
            "use_worktrees"} <= board_cols
    assert "directory" not in board_cols  # per-PC, never a server column
    assert "session_id" in {c.name for c in Ticket.__table__.columns}
    assert {"concurrency", "running"} <= {c.name for c in Worker.__table__.columns}


def test_migration_adds_phase1_columns_to_an_existing_db(tmp_path):
    """A database created before these columns must reach the new shape."""
    from app.db import _PHASE1_COLUMNS

    engine = make_engine(f"sqlite:///{tmp_path / 'old.db'}")
    Base.metadata.create_all(engine)
    # Simulate the pre-Phase-1 shape by dropping the new columns back off.
    with engine.begin() as conn:
        for table, ddl in _PHASE1_COLUMNS:
            conn.execute(sa.text(f"ALTER TABLE {table} DROP COLUMN {ddl.split()[0]}"))

    run_migrations(engine)
    run_migrations(engine)  # idempotent

    insp = sa.inspect(engine)
    for table, ddl in _PHASE1_COLUMNS:
        assert ddl.split()[0] in {c["name"] for c in insp.get_columns(table)}, ddl


def test_phase1_slot_defaults_are_backward_compatible(tmp_path):
    """A worker row written by an exe that predates the columns reads 1 slot / 0 busy."""
    from sqlalchemy.orm import Session

    from app.models import Cluster, User

    engine = make_engine(f"sqlite:///{tmp_path / 'd.db'}")
    Base.metadata.create_all(engine)
    run_migrations(engine)
    with Session(engine) as db:
        db.add(User(id=1, email="a@b.co", password_hash="x"))
        db.add(Cluster(id=1, name="T", join_code="J", created_by=1))
        db.commit()
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO workers (id, cluster_id, name, revoked, status,"
            " last_seen, created_at) VALUES"
            " (1,1,'old-pc',0,'idle','2020-01-01','2020-01-01')"
        ))
    with Session(engine) as db:
        w = db.get(Worker, 1)
        assert (w.concurrency, w.running) == (1, 0)
