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
