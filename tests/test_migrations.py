"""Schema v2: workers get role_name/revoked, lose token."""
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db import make_engine, run_migrations
from app.main import create_app
from app.models import Base, Worker

# The pre-v2 workers table, exactly as create_all() used to build it.
V1_WORKERS_DDL = """
CREATE TABLE workers (
    id         INTEGER NOT NULL,
    cluster_id INTEGER NOT NULL,
    name       VARCHAR(255) NOT NULL,
    token      VARCHAR(64) NOT NULL,
    status     VARCHAR(32) NOT NULL,
    last_seen  DATETIME NOT NULL,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    UNIQUE (cluster_id, name),
    FOREIGN KEY(cluster_id) REFERENCES clusters (id),
    UNIQUE (token)
)
"""


def v1_sqlite_url(tmp_path) -> str:
    """A SQLite dev DB left over from v1: old workers table, one enrolled PC."""
    url = f"sqlite:///{tmp_path / 'v1.db'}"
    engine = make_engine(url)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("DROP TABLE workers"))
        conn.execute(sa.text(V1_WORKERS_DDL))
        conn.execute(sa.text(
            "INSERT INTO workers (id, cluster_id, name, token, status, last_seen, created_at)"
            " VALUES (5, 1, 'old-pc', 'tok', 'working',"
            " '2020-01-01 00:00:00', '2020-01-01 00:00:00')"
        ))
    engine.dispose()
    return url


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
    assert any(i["name"] == "idx_work_queue_claim"
               for i in insp.get_indexes("work_queue"))


def test_migrates_v1_sqlite_workers_table(tmp_path):
    engine = make_engine(v1_sqlite_url(tmp_path))
    run_migrations(engine)
    run_migrations(engine)  # idempotent
    insp = sa.inspect(engine)
    assert {c["name"] for c in insp.get_columns("workers")} == {
        c.name for c in Worker.__table__.columns
    }
    with Session(engine) as db:
        w = db.get(Worker, 5)  # id preserved: tickets/work_queue still point here
        assert (w.name, w.status, w.role_name, w.revoked) == ("old-pc", "working", None, False)


def test_workers_endpoint_works_on_a_v1_sqlite_db(tmp_path, monkeypatch):
    """Regression: the workers panel 500'd ("no such column: workers.role_name")
    against a local dev DB created before the v2 rewrite."""
    monkeypatch.delenv("PROXY_SHARED_SECRET", raising=False)
    with TestClient(create_app(v1_sqlite_url(tmp_path))) as c:
        tok = c.post("/api/register",
                     json={"email": "a@b.co", "password": "pass1234"}).json()["token"]
        headers = {"Authorization": f"Bearer {tok}"}
        cid = c.post("/api/clusters", json={"name": "T"}, headers=headers).json()["id"]
        r = c.get(f"/api/clusters/{cid}/workers", headers=headers)
        assert r.status_code == 200, r.text
        assert [w["name"] for w in r.json()] == ["old-pc"]  # v1 row survived


def test_models_have_phase1_columns():
    from app.models import Board, Ticket, WorkItem
    board_cols = {c.name for c in Board.__table__.columns}
    assert {"description", "out_of_scope", "commit_requirements",
            "use_worktrees", "repo_url"} <= board_cols
    assert "directory" not in board_cols  # per-PC, never a server column
    assert "session_id" in {c.name for c in Ticket.__table__.columns}
    assert "order" in {c.name for c in Ticket.__table__.columns}
    assert {"concurrency", "running"} <= {c.name for c in Worker.__table__.columns}
    assert "kill_requested" in {c.name for c in WorkItem.__table__.columns}


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


def test_migration_adds_ticket_order_column_to_an_existing_db(tmp_path):
    """A database created before tickets.order must reach the new shape."""
    from app.db import _PHASE2_COLUMNS

    engine = make_engine(f"sqlite:///{tmp_path / 'old2.db'}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for table, ddl in _PHASE2_COLUMNS:
            conn.execute(sa.text(f"ALTER TABLE {table} DROP COLUMN {ddl.split()[0]}"))

    run_migrations(engine)
    run_migrations(engine)  # idempotent

    insp = sa.inspect(engine)
    assert "order" in {c["name"] for c in insp.get_columns("tickets")}


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


def test_work_item_model_has_heartbeat_at():
    from app.models import WorkItem

    assert "heartbeat_at" in {c.name for c in WorkItem.__table__.columns}


def test_migration_adds_phase3_columns_to_an_existing_db(tmp_path):
    """A database created before the reaper must reach the new shape too."""
    from app.db import _PHASE3_COLUMNS

    engine = make_engine(f"sqlite:///{tmp_path / 'old3.db'}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for table, ddl in _PHASE3_COLUMNS:
            conn.execute(sa.text(f"ALTER TABLE {table} DROP COLUMN {ddl.split()[0]}"))

    run_migrations(engine)
    run_migrations(engine)  # idempotent

    insp = sa.inspect(engine)
    for table, ddl in _PHASE3_COLUMNS:
        assert ddl.split()[0] in {c["name"] for c in insp.get_columns(table)}, ddl


def test_migration_adds_phase4_columns_to_an_existing_db(tmp_path):
    """A database created before website-side worker control (ticket #18)
    must reach the new shape too."""
    from app.db import _PHASE4_COLUMNS

    engine = make_engine(f"sqlite:///{tmp_path / 'old4.db'}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for table, ddl in _PHASE4_COLUMNS:
            conn.execute(sa.text(f"ALTER TABLE {table} DROP COLUMN {ddl.split()[0]}"))

    run_migrations(engine)
    run_migrations(engine)  # idempotent

    insp = sa.inspect(engine)
    for table, ddl in _PHASE4_COLUMNS:
        assert ddl.split()[0] in {c["name"] for c in insp.get_columns(table)}, ddl


def test_ticket_model_has_triage_column():
    from app.models import Ticket

    assert "model" in {c.name for c in Ticket.__table__.columns}


def test_migration_adds_triage_columns_to_an_existing_db(tmp_path):
    """A database created before initial triage must reach the new shape too."""
    from app.db import _TRIAGE_COLUMNS

    engine = make_engine(f"sqlite:///{tmp_path / 'old_triage.db'}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for table, ddl in _TRIAGE_COLUMNS:
            conn.execute(sa.text(f"ALTER TABLE {table} DROP COLUMN {ddl.split()[0]}"))

    run_migrations(engine)
    run_migrations(engine)  # idempotent

    insp = sa.inspect(engine)
    for table, ddl in _TRIAGE_COLUMNS:
        assert ddl.split()[0] in {c["name"] for c in insp.get_columns(table)}, ddl


def test_cluster_settings_table_is_created(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'cs.db'}")
    Base.metadata.create_all(engine)
    run_migrations(engine)
    insp = sa.inspect(engine)
    assert "cluster_settings" in insp.get_table_names()
    cols = {c["name"] for c in insp.get_columns("cluster_settings")}
    assert cols == {"cluster_id", "enabled", "concurrency_cap", "stop_all_requested"}


def test_run_migrations_backfills_a_settings_row_for_a_pre_existing_cluster(tmp_path):
    """A cluster created before this table existed (or whose row was somehow
    lost) must still get one — worker.cluster_claim_gate's race-safety
    depends on every cluster having a row to lock."""
    from app.models import Cluster, User

    engine = make_engine(f"sqlite:///{tmp_path / 'backfill.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(id=1, email="a@b.co", password_hash="x"))
        db.add(Cluster(id=1, name="T", join_code="J", created_by=1))
        db.commit()
    with engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM cluster_settings"))

    run_migrations(engine)
    run_migrations(engine)  # idempotent: must not duplicate or error

    with engine.begin() as conn:
        rows = conn.execute(sa.text("SELECT cluster_id FROM cluster_settings")).fetchall()
    assert rows == [(1,)]


def test_desired_concurrency_defaults_to_none_for_pre_ticket_18_rows(tmp_path):
    """A worker row written before this column existed must read as
    'no website override' rather than erroring or defaulting to 0."""
    from app.models import Cluster, User

    engine = make_engine(f"sqlite:///{tmp_path / 'd4.db'}")
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
        assert w.desired_concurrency is None


def test_models_have_phase5_columns():
    """Ticket #15: commit gate + auto-commit/push."""
    from app.models import Board, Ticket

    assert "auto_push" in {c.name for c in Board.__table__.columns}
    assert "commit_gate" in {c.name for c in Ticket.__table__.columns}


def test_migration_adds_phase5_columns_to_an_existing_db(tmp_path):
    """A database created before the commit gate / auto-push columns existed
    must reach the new shape too."""
    from app.db import _PHASE6_COLUMNS

    engine = make_engine(f"sqlite:///{tmp_path / 'old5.db'}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for table, ddl in _PHASE6_COLUMNS:
            conn.execute(sa.text(f"ALTER TABLE {table} DROP COLUMN {ddl.split()[0]}"))

    run_migrations(engine)
    run_migrations(engine)  # idempotent

    insp = sa.inspect(engine)
    for table, ddl in _PHASE6_COLUMNS:
        assert ddl.split()[0] in {c["name"] for c in insp.get_columns(table)}, ddl


def test_auto_push_defaults_to_false_for_pre_ticket_15_rows(tmp_path):
    """A board row written before this column existed must read as 'never
    pushes', not as an error or an accidental opt-in."""
    from app.models import Cluster, User

    engine = make_engine(f"sqlite:///{tmp_path / 'd5.db'}")
    Base.metadata.create_all(engine)
    run_migrations(engine)
    with Session(engine) as db:
        db.add(User(id=1, email="a@b.co", password_hash="x"))
        db.add(Cluster(id=1, name="T", join_code="J", created_by=1))
        db.commit()
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO boards (id, cluster_id, name, created_at) VALUES"
            " (1, 1, 'old-board', '2020-01-01')"
        ))
    from app.models import Board
    with Session(engine) as db:
        b = db.get(Board, 1)
        assert b.auto_push is False


def test_phase1_boolean_defaults_are_postgres_legal():
    """Regression: `BOOLEAN NOT NULL DEFAULT 0` deployed green and then broke
    startup on Neon — "column is of type boolean but default expression is of
    type integer". SQLite accepts the integer, so the SQLite-only migration
    tests below could never catch it. One DDL string serves both backends, so
    the spelling has to be the one both accept.
    """
    from app.db import _PHASE1_COLUMNS

    for table, ddl in _PHASE1_COLUMNS:
        if "BOOLEAN" not in ddl.upper():
            continue
        default = ddl.upper().split("DEFAULT", 1)[1].strip() if "DEFAULT" in ddl.upper() else ""
        assert default in ("", "TRUE", "FALSE"), f"{table}.{ddl}: use TRUE/FALSE, not {default}"


def test_phase5_boolean_defaults_are_postgres_legal():
    """Same regression guard as test_phase1_boolean_defaults_are_postgres_legal,
    for auto_push."""
    from app.db import _PHASE6_COLUMNS

    for table, ddl in _PHASE6_COLUMNS:
        if "BOOLEAN" not in ddl.upper():
            continue
        default = ddl.upper().split("DEFAULT", 1)[1].strip() if "DEFAULT" in ddl.upper() else ""
        assert default in ("", "TRUE", "FALSE"), f"{table}.{ddl}: use TRUE/FALSE, not {default}"
