# DB-Centric Workers (kanban-cloud v2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Worker PCs stop polling the web service over HTTP and instead talk directly to Neon Postgres with per-PC database roles issued at enrollment; the FastAPI service slims down to the human-facing board plus one enroll route.

**Architecture:** The server keeps its admin `DATABASE_URL` (neondb_owner) and uses it to provision a `worker_c<cluster>_w<id>` Postgres role per enrolled PC (all inheriting one `kanban_worker` group role that carries the grants). `worker.py` is rewritten on psycopg: claim via `FOR UPDATE SKIP LOCKED`, result/progress/heartbeat as direct SQL. The old worker HTTP routes and token auth are deleted; revocation drops the role.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, psycopg 3 (`psycopg[binary]`), Neon Postgres, pytest + httpx TestClient.

**Spec:** `docs/superpowers/specs/2026-08-08-db-centric-workers-design.md` (approved). One deliberate addition to the spec's grant list: `INSERT` on `work_queue` (the failure-requeue path inserts a fresh queue row from the worker).

## Global Constraints

- Repo: `C:\Users\ryan\Documents\Github\kanban-cloud` (git branch `master`; push auto-deploys Render service `kanban-cloud`, srv-d9r9oh0n74is73ebnpmg).
- Test command: `.venv\Scripts\python.exe -m pytest tests -q` from the repo root (PowerShell) — suite must stay green after every task.
- All DB timestamps are **naive UTC** (`models.utcnow()`); SQL written by the worker must use `(now() at time zone 'utc')`, never bare `now()`.
- Status vocabularies are fixed: tickets `todo|ready|doing|review|done|failed`; work_queue `queued|claimed|done|failed`; `MAX_ATTEMPTS = 2`.
- The server's SQLite fallback keeps working for the human-facing board; enrollment/provisioning is Postgres-only and must fail cleanly (HTTP 400) on SQLite.
- Never commit secrets: `.env` is gitignored and holds the real `DATABASE_URL`. Smoke scripts take the DSN as argv, never hardcode it.
- The Claude Code permission classifier blocks agent-side writes to the production Neon DB. Any step that mutates prod data (the live smoke) is delivered as a `!` one-liner for Ryan with **absolute paths** (`/c/Users/ryan/...` — the `!` shell does not start in Documents\Github).
- site-page (the okeefe.work proxy) is **not** touched by this plan; its exempt-route handling lives entirely in kanban-cloud's middleware.

---

### Task 1: Schema v2 — Worker role columns, drop token, startup migrations

**Files:**
- Modify: `app/models.py` (Worker class, ~lines 104-117)
- Modify: `app/db.py` (add `run_migrations`)
- Modify: `app/main.py:126-129` (call `run_migrations` after `create_all`)
- Modify: `schema.sql` (workers DDL)
- Test: `tests/test_migrations.py` (new)

**Interfaces:**
- Consumes: existing `make_engine`, `Base.metadata`.
- Produces: `Worker.role_name: str | None` (String(64)), `Worker.revoked: bool` (default False), **no** `Worker.token` column; `app.db.run_migrations(engine) -> None` (idempotent, Postgres-only, no-op on SQLite). Later tasks rely on exactly these attribute names.

- [ ] **Step 1: Write the failing test**

Create `tests/test_migrations.py`:

```python
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
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_migrations.py -q`
Expected: FAIL — `role_name` missing / `run_migrations` import error.

- [ ] **Step 3: Implement**

In `app/models.py`, replace the `Worker` class body's `token` line:

```python
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
    last_seen: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)

    def is_online(self, now: datetime.datetime | None = None) -> bool:
        now = now or utcnow()
        return (now - self.last_seen).total_seconds() <= WORKER_ONLINE_SECONDS
```

`new_token` stays in models.py (auth tokens still use it). In `app/db.py` add:

```python
from sqlalchemy import text


def run_migrations(engine) -> None:
    """Idempotent v2 schema fixes for existing Postgres DBs.

    No alembic: the prod DB predates these columns but is (near-)empty, so a
    few guarded ALTERs at startup are enough. SQLite DBs are scratch files —
    delete and let create_all rebuild them instead.
    """
    if engine.url.get_backend_name() != "postgresql":
        return
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE workers ADD COLUMN IF NOT EXISTS role_name VARCHAR(64)"))
        conn.execute(text(
            "ALTER TABLE workers ADD COLUMN IF NOT EXISTS revoked BOOLEAN NOT NULL DEFAULT FALSE"
        ))
        conn.execute(text("ALTER TABLE workers DROP COLUMN IF EXISTS token"))
```

In `app/main.py` `create_app`, right after `Base.metadata.create_all(engine)`:

```python
    from .db import run_migrations  # add to the existing `from .db import ...` line instead
    run_migrations(engine)
```

(Actually extend the existing import: `from .db import make_engine, make_session_factory, run_migrations`.)

In `schema.sql`, update the `workers` table DDL: delete the `token` column line and add `role_name VARCHAR(64)` and `revoked BOOLEAN NOT NULL DEFAULT FALSE` after `name`. Keep the rest of the file untouched.

Delete the stale dev scratch DB so create_all rebuilds it: `Remove-Item kanban_cloud.db` (it's gitignored scratch; the old file has the token column as NOT NULL and would break inserts).

- [ ] **Step 4: Run the full suite**

Run: `.venv\Scripts\python.exe -m pytest tests -q`
Expected: `tests/test_migrations.py` passes. `tests/test_claim.py` and `tests/conftest.py` will now FAIL (they use `worker_token` from the register route — that route still returns it via `new_token` default which no longer exists on the model). **Expected collateral**: `test_claim.py` failures and the `register_worker` helper break here; they are deleted/rewritten in Task 3. If more than test_claim/test_proxy worker-flow tests fail, stop and investigate. To keep the tree green-ish for this commit, apply the *minimal* bridge: in `app/main.py`'s `worker_register`, replace the token rotation logic with `raise HTTPException(410, "Gone: use /api/workers/enroll (v2)")` at the top of the function, and in `tests/test_claim.py` add at the top of the file `import pytest; pytestmark = pytest.mark.skip(reason="v1 worker HTTP API removed in v2 (Task 3 rewrites these)")`, and in `tests/test_proxy.py` skip the two worker-flow tests the same way (`test_worker_routes_exempt_from_gate`, `test_worker_full_flow_without_proxy_secret`).

Run again: `.venv\Scripts\python.exe -m pytest tests -q`
Expected: PASS (with the skips).

- [ ] **Step 5: Commit**

```bash
git add app/models.py app/db.py app/main.py schema.sql tests/test_migrations.py tests/test_claim.py tests/test_proxy.py
git commit -m "feat: schema v2 - worker role_name/revoked, drop token, startup migrations"
```

---

### Task 2: `app/enrollment.py` — per-PC Postgres role provisioning

**Files:**
- Create: `app/enrollment.py`
- Test: `tests/test_enrollment.py` (new)

**Interfaces:**
- Consumes: a SQLAlchemy `Engine` (admin connection, neondb_owner).
- Produces (Task 3/4/8 call exactly these):
  - `GROUP_ROLE = "kanban_worker"`
  - `role_name_for(cluster_id: int, worker_id: int) -> str` → `"worker_c{cluster_id}_w{worker_id}"`
  - `can_provision(engine) -> bool` — True iff backend is postgresql
  - `ensure_worker_group(engine) -> None` — idempotent; creates group role + grants
  - `provision_role(engine, role_name: str) -> str` — (re)creates the LOGIN role, returns the new password
  - `revoke_role(engine, role_name: str) -> None` — terminates its backends (best-effort) and drops it
  - `build_worker_dsn(admin_url, role_name: str, password: str) -> str` — plain `postgresql://` DSN with `sslmode=require`

- [ ] **Step 1: Write the failing tests** (pure logic only — role SQL is exercised by the Task 8 live smoke)

Create `tests/test_enrollment.py`:

```python
"""Enrollment module: DSN building and pure helpers (role SQL covered by live smoke)."""
import pytest

from app import enrollment
from app.db import make_engine


def test_role_name_shape():
    assert enrollment.role_name_for(3, 17) == "worker_c3_w17"


def test_can_provision_only_on_postgres(tmp_path):
    sqlite = make_engine(f"sqlite:///{tmp_path / 'e.db'}")
    assert enrollment.can_provision(sqlite) is False
    pg = make_engine("postgresql://u:p@example.invalid/db")  # never connected
    assert enrollment.can_provision(pg) is True


def test_build_worker_dsn_swaps_credentials_and_keeps_host():
    admin = "postgresql+psycopg://neondb_owner:adminpw@ep-x.aws.neon.tech/neondb?sslmode=require"
    dsn = enrollment.build_worker_dsn(admin, "worker_c1_w2", "s3cr3t")
    assert dsn.startswith("postgresql://worker_c1_w2:s3cr3t@ep-x.aws.neon.tech/neondb")
    assert "sslmode=require" in dsn
    assert "adminpw" not in dsn
    assert "+psycopg" not in dsn


def test_build_worker_dsn_adds_sslmode_when_missing():
    admin = "postgresql://u:p@host/db"
    dsn = enrollment.build_worker_dsn(admin, "worker_c1_w1", "pw")
    assert "sslmode=require" in dsn


def test_provision_role_rejects_bad_role_names(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'e.db'}")
    with pytest.raises(AssertionError):
        enrollment.provision_role(engine, "worker_c1_w1; DROP TABLE users--")
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv\Scripts\python.exe -m pytest tests/test_enrollment.py -q`
Expected: FAIL — `cannot import name 'enrollment'`.

- [ ] **Step 3: Implement `app/enrollment.py`**

```python
"""Per-PC Postgres role provisioning for direct-DB workers (v2).

The server's admin connection (neondb_owner) creates one LOGIN role per
enrolled PC. All worker roles inherit the kanban_worker group role, which
carries the actual grants — future schema changes need one GRANT to the
group, not per-PC surgery. Identifiers are interpolated into SQL text (DDL
can't take bind params); safety comes from the strict ROLE_RE shape and the
token_urlsafe password alphabet, both asserted.
"""
import re
import secrets

from sqlalchemy import text
from sqlalchemy.engine import make_url

GROUP_ROLE = "kanban_worker"
ROLE_RE = re.compile(r"^worker_c\d+_w\d+$")
# token_urlsafe alphabet: A-Za-z0-9_- ; never contains quotes.
PASSWORD_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# INSERT on work_queue is required by the failure-requeue path (worker inserts
# the retry row itself). No DELETE anywhere; users/auth_tokens untouched.
GROUP_GRANTS = [
    f"GRANT SELECT ON tickets, boards, clusters, cluster_settings, workers, "
    f"work_queue, comments TO {GROUP_ROLE}",
    f"GRANT INSERT ON comments, work_queue TO {GROUP_ROLE}",
    f"GRANT UPDATE ON work_queue, tickets, workers TO {GROUP_ROLE}",
    f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {GROUP_ROLE}",
]


def role_name_for(cluster_id: int, worker_id: int) -> str:
    return f"worker_c{cluster_id}_w{worker_id}"


def can_provision(engine) -> bool:
    return engine.url.get_backend_name() == "postgresql"


def ensure_worker_group(engine) -> None:
    """Create the NOLOGIN group role and (re)apply its grants. Idempotent."""
    if not can_provision(engine):
        return
    with engine.begin() as conn:
        conn.execute(text(
            f"DO $$ BEGIN "
            f"IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{GROUP_ROLE}') "
            f"THEN CREATE ROLE {GROUP_ROLE} NOLOGIN; END IF; END $$"
        ))
        for grant in GROUP_GRANTS:
            conn.execute(text(grant))


def provision_role(engine, role_name: str) -> str:
    """(Re)create the per-PC LOGIN role; returns its fresh password.

    Re-enrolling an existing PC rotates the password: old backends are
    terminated and the role recreated.
    """
    assert ROLE_RE.match(role_name), f"unsafe role name: {role_name!r}"
    password = secrets.token_urlsafe(24)
    assert PASSWORD_RE.match(password)
    with engine.begin() as conn:
        _terminate_backends(conn, role_name)
        conn.execute(text(f'DROP ROLE IF EXISTS "{role_name}"'))
        conn.execute(text(f"CREATE ROLE \"{role_name}\" LOGIN PASSWORD '{password}'"))
        conn.execute(text(f'GRANT {GROUP_ROLE} TO "{role_name}"'))
    return password


def revoke_role(engine, role_name: str) -> None:
    assert ROLE_RE.match(role_name), f"unsafe role name: {role_name!r}"
    with engine.begin() as conn:
        _terminate_backends(conn, role_name)
        conn.execute(text(f'DROP ROLE IF EXISTS "{role_name}"'))


def _terminate_backends(conn, role_name: str) -> None:
    """Kick live sessions so DROP ROLE fully cuts access. Best-effort: on
    providers where pg_terminate_backend is restricted, the drop still
    prevents *new* connections."""
    try:
        conn.execute(text(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE usename = '{role_name}'"
        ))
    except Exception:
        pass


def build_worker_dsn(admin_url, role_name: str, password: str) -> str:
    """Worker DSN: same host/db as the admin URL, worker credentials, plain
    postgresql:// scheme (psycopg-ready), sslmode=require guaranteed."""
    url = make_url(str(admin_url))
    url = url.set(drivername="postgresql", username=role_name, password=password)
    q = dict(url.query)
    q.setdefault("sslmode", "require")
    url = url.set(query=q)
    return url.render_as_string(hide_password=False)
```

- [ ] **Step 4: Run the tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_enrollment.py -q`
Expected: 5 passed. Then full suite: `.venv\Scripts\python.exe -m pytest tests -q` — green (same skips as Task 1).

- [ ] **Step 5: Commit**

```bash
git add app/enrollment.py tests/test_enrollment.py
git commit -m "feat: enrollment module - per-PC Postgres role provisioning"
```

---

### Task 3: Server slim-down — enroll route replaces the worker HTTP API

**Files:**
- Modify: `app/main.py` (routes ~607-677, `WORKER_EXEMPT_RE` ~46-48, `current_worker` ~234-242, request bodies ~114-122, create_app startup)
- Modify: `app/delegation.py` (delete `claim_next` + `finish_work`, ~54-141)
- Modify: `tests/conftest.py` (replace `register_worker` helper)
- Rewrite: `tests/test_claim.py`
- Modify: `tests/test_proxy.py` (exempt-route tests)
- Test: `tests/test_enroll_route.py` (new)

**Interfaces:**
- Consumes: `enrollment.*` from Task 2 (exact names above), `run_migrations` from Task 1.
- Produces: `POST /api/workers/enroll` body `{"join_code": str, "name": str}` → 200 `{"worker_id": int, "cluster": {"id": int, "name": str}, "dsn": str}`; 404 unknown join code; 400 blank name; 400 on SQLite ("enrollment requires Postgres"). `WORKER_EXEMPT_RE` matches **only** `/api/workers/enroll`. Routes `/api/workers/register`, `/api/work/poll`, `/api/work/{id}/result`, `/api/work/{id}/progress` no longer exist (404). `delegation.enqueue_ticket` unchanged. Worker.py (Task 5) and the smoke (Task 8) consume the enroll response shape verbatim.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_enroll_route.py`:

```python
"""v2 enrollment route: join-code gate, role provisioning, DSN response."""
import pytest

from app import enrollment


@pytest.fixture()
def fake_provisioning(monkeypatch):
    """Pretend the SQLite test engine can provision roles; capture calls."""
    calls = {"provisioned": [], "revoked": []}
    monkeypatch.setattr(enrollment, "can_provision", lambda engine: True)
    monkeypatch.setattr(enrollment, "ensure_worker_group", lambda engine: None)
    monkeypatch.setattr(
        enrollment, "provision_role",
        lambda engine, role: calls["provisioned"].append(role) or "fakepw",
    )
    monkeypatch.setattr(
        enrollment, "build_worker_dsn",
        lambda admin_url, role, pw: f"postgresql://{role}:{pw}@fakehost/db?sslmode=require",
    )
    monkeypatch.setattr(
        enrollment, "revoke_role",
        lambda engine, role: calls["revoked"].append(role),
    )
    return calls


def test_enroll_requires_postgres(client, cluster):
    r = client.post("/api/workers/enroll",
                    json={"join_code": cluster["join_code"], "name": "pc1"})
    assert r.status_code == 400
    assert "Postgres" in r.json()["detail"]


def test_enroll_provisions_role_and_returns_dsn(client, cluster, fake_provisioning):
    r = client.post("/api/workers/enroll",
                    json={"join_code": cluster["join_code"], "name": "pc1"})
    assert r.status_code == 200, r.text
    data = r.json()
    role = enrollment.role_name_for(cluster["id"], data["worker_id"])
    assert data["cluster"]["id"] == cluster["id"]
    assert data["dsn"] == f"postgresql://{role}:fakepw@fakehost/db?sslmode=require"
    assert fake_provisioning["provisioned"] == [role]


def test_enroll_bad_join_code_404(client, fake_provisioning):
    r = client.post("/api/workers/enroll", json={"join_code": "NOPE1234", "name": "pc1"})
    assert r.status_code == 404


def test_enroll_blank_name_400(client, cluster, fake_provisioning):
    r = client.post("/api/workers/enroll",
                    json={"join_code": cluster["join_code"], "name": "  "})
    assert r.status_code == 400


def test_reenroll_same_name_reuses_worker_and_unrevokes(client, user, cluster, fake_provisioning):
    r1 = client.post("/api/workers/enroll",
                     json={"join_code": cluster["join_code"], "name": "pc1"})
    r2 = client.post("/api/workers/enroll",
                     json={"join_code": cluster["join_code"], "name": "pc1"})
    assert r1.json()["worker_id"] == r2.json()["worker_id"]
    assert len(fake_provisioning["provisioned"]) == 2  # password rotated
    workers = client.get(f"/api/clusters/{cluster['id']}/workers",
                         headers=user["headers"]).json()
    assert workers[0]["revoked"] is False


def test_old_worker_routes_are_gone(client):
    assert client.post("/api/workers/register",
                       json={"join_code": "X", "name": "n"}).status_code in (404, 405)
    assert client.post("/api/work/poll").status_code in (404, 405)
    assert client.post("/api/work/1/result", json={"ok": True}).status_code in (404, 405)
    assert client.post("/api/work/1/progress", json={"message": "hi"}).status_code in (404, 405)
```

(The `workers[0]["revoked"]` assertion needs the listing field added in this task — include it here, not Task 4, since re-enroll touches it.)

Rewrite `tests/test_claim.py` entirely (v1 claim/poll tests die with the HTTP API; enqueue-side semantics stay, with direct-SQL helpers standing in for a claiming worker):

```python
"""Enqueue-side delegation semantics (v2: claiming itself moved into worker.py SQL).

A 'claimed' state is simulated with a direct UPDATE against the test DB —
exactly what a v2 worker does, minus the Postgres-only SKIP LOCKED wrapper.
"""
from sqlalchemy import text

from tests.conftest import make_ticket


def queue_rows(client, user, cluster_id):
    return client.get(f"/api/clusters/{cluster_id}/queue", headers=user["headers"]).json()


def mark_claimed(client, item_id, worker_id=999):
    engine = client.app.state.engine
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE work_queue SET status='claimed', claimed_by=:w WHERE id=:i"
        ), {"w": worker_id, "i": item_id})


def test_ready_status_enqueues(client, user, cluster):
    t = make_ticket(client, user, cluster["board_id"], status="ready")
    rows = queue_rows(client, user, cluster["id"])
    assert len(rows) == 1
    assert rows[0]["ticket_id"] == t["id"]
    assert rows[0]["status"] == "queued"


def test_enqueue_is_idempotent(client, user, cluster):
    t = make_ticket(client, user, cluster["board_id"], status="ready")
    client.post(f"/api/tickets/{t['id']}/run", headers=user["headers"])
    assert len(queue_rows(client, user, cluster["id"])) == 1


def test_reenqueue_supersedes_orphaned_claim(client, user, cluster):
    t = make_ticket(client, user, cluster["board_id"], status="ready")
    item = queue_rows(client, user, cluster["id"])[0]
    mark_claimed(client, item["id"])  # worker dies mid-ticket
    client.post(f"/api/tickets/{t['id']}/run", headers=user["headers"])
    rows = queue_rows(client, user, cluster["id"])
    by_id = {r["id"]: r for r in rows}
    assert by_id[item["id"]]["status"] == "failed"  # superseded
    assert sum(1 for r in rows if r["status"] == "queued") == 1


def test_rerun_resets_attempts(client, user, cluster):
    t = make_ticket(client, user, cluster["board_id"], status="ready")
    engine = client.app.state.engine
    with engine.begin() as conn:
        conn.execute(text("UPDATE tickets SET attempts=2, status='failed' WHERE id=:i"),
                     {"i": t["id"]})
    client.post(f"/api/tickets/{t['id']}/run", headers=user["headers"])
    r = client.get(f"/api/boards/{cluster['board_id']}/tickets", headers=user["headers"])
    fresh = [x for x in r.json() if x["id"] == t["id"]][0]
    assert fresh["attempts"] == 0
    assert fresh["status"] == "ready"
```

- [ ] **Step 2: Run to verify failures**

Run: `.venv\Scripts\python.exe -m pytest tests/test_enroll_route.py tests/test_claim.py -q`
Expected: FAIL — enroll route doesn't exist yet; conftest still imports fine but `register_worker` removal comes with implementation.

- [ ] **Step 3: Implement**

`app/main.py` changes:

1. Replace the exempt regex (~line 46):
```python
# The single worker-facing route left in v2: one-time enrollment. Everything
# else workers do is direct SQL against Postgres.
WORKER_EXEMPT_RE = re.compile(r"^/api/workers/enroll$")
```
2. Add `from . import enrollment` next to `from . import delegation`; extend the db import with `run_migrations` (done in Task 1).
3. In `create_app` after `run_migrations(engine)` add `enrollment.ensure_worker_group(engine)`.
4. Delete request bodies `WorkerRegisterBody` and `WorkResultBody`; add:
```python
class WorkerEnrollBody(BaseModel):
    join_code: str
    name: str
```
5. Delete `current_worker` (~234-242) and the entire `# ----- worker API -----` section (register/poll/result/progress, ~607-677). Replace with:
```python
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
```
6. In `cluster_workers` (~434), add `"revoked": w.revoked,` to the returned dict.

`app/delegation.py`: delete `claim_next` and `finish_work` and their now-unused imports (`or_`, `update`, `ClusterSettings`, `Worker`, `MAX_ATTEMPTS`). Keep `enqueue_ticket` and module docstring (update it: "Enqueue-side delegation. Claiming and result handling moved into worker.py (direct SQL) in v2."). Note: `MAX_ATTEMPTS` stays defined in `app/models.py` — the worker duplicates the value; a comment in models.py should say `# keep in sync with worker.py MAX_ATTEMPTS`.

`tests/conftest.py`: delete the `register_worker` helper (nothing uses it after this task).

`tests/test_proxy.py`: remove the Task 1 skips. Rewrite the two worker-flow tests:

```python
def test_enroll_route_exempt_from_gate(pclient):
    """Enrollment bypasses the proxy secret (workers hit the service directly);
    it still fails its own validation (404 bad code / 400 sqlite), never 403."""
    r = pclient.post("/api/workers/enroll", json={"join_code": "NOPE1234", "name": "pc"})
    assert r.status_code in (400, 404)  # NOT 403


def test_old_worker_routes_not_exempt_anymore(pclient):
    """v1 worker paths are gone AND gated: without the secret they 403."""
    assert pclient.post("/api/workers/register", json={}).status_code == 403
    assert pclient.post("/api/work/poll").status_code == 403
    assert pclient.post("/api/work/1/result", json={"ok": True}).status_code == 403
```

Also update the module docstring's exempt-route list. Delete `tests/test_claim.py`'s Task-1 skip marker (the file is fully rewritten above).

- [ ] **Step 4: Run the full suite**

Run: `.venv\Scripts\python.exe -m pytest tests -q`
Expected: all green, zero skips. If `test_tickets.py::test_create_ticket_rejects_foreign_target_worker` fails because it used `register_worker`, replace its worker setup with a direct insert:

```python
    engine = client.app.state.engine
    from sqlalchemy import text
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO workers (cluster_id, name, revoked, status, last_seen, created_at) "
            "VALUES (:c, 'other-pc', 0, 'idle', :n, :n)"
        ), {"c": other_cluster_id, "n": __import__('datetime').datetime.utcnow()})
```
(adapt to the test's actual variable names when editing).

- [ ] **Step 5: Commit**

```bash
git add app/main.py app/delegation.py app/models.py tests/
git commit -m "feat: v2 enroll route; delete worker HTTP API (poll/result/progress/register)"
```

---

### Task 4: Revocation endpoint

**Files:**
- Modify: `app/main.py` (add route next to `worker_enroll`)
- Test: `tests/test_enroll_route.py` (extend)

**Interfaces:**
- Consumes: `enrollment.revoke_role`, `enrollment.can_provision`, `require_member`, `current_user`.
- Produces: `POST /api/workers/{worker_id}/revoke` (member-authenticated; goes through the proxy for owners, so NOT in the exempt list) → 200 `{"ok": true}`; 404 unknown worker; 403 non-member. Sets `revoked=True`, `status="idle"`, drops the role. UI (Task 6) calls it.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_enroll_route.py`:

```python
def test_revoke_marks_worker_and_drops_role(client, user, cluster, fake_provisioning):
    r = client.post("/api/workers/enroll",
                    json={"join_code": cluster["join_code"], "name": "pc1"})
    wid = r.json()["worker_id"]
    r = client.post(f"/api/workers/{wid}/revoke", headers=user["headers"])
    assert r.status_code == 200
    role = enrollment.role_name_for(cluster["id"], wid)
    assert fake_provisioning["revoked"] == [role]
    workers = client.get(f"/api/clusters/{cluster['id']}/workers",
                         headers=user["headers"]).json()
    assert workers[0]["revoked"] is True


def test_revoke_requires_membership(client, user, cluster, fake_provisioning):
    r = client.post("/api/workers/enroll",
                    json={"join_code": cluster["join_code"], "name": "pc1"})
    wid = r.json()["worker_id"]
    r2 = client.post("/api/register",
                     json={"email": "other@example.com", "password": "pass1234"})
    outsider = {"Authorization": f"Bearer {r2.json()['token']}"}
    assert client.post(f"/api/workers/{wid}/revoke", headers=outsider).status_code == 403
    assert client.post("/api/workers/424242/revoke", headers=user["headers"]).status_code == 404
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv\Scripts\python.exe -m pytest tests/test_enroll_route.py -q`
Expected: the two new tests FAIL (405/404 — route missing).

- [ ] **Step 3: Implement** — in `app/main.py` after `worker_enroll`:

```python
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
```

Note the fake_provisioning fixture patches `can_provision` to True, so `revoke_role` is reached in tests.

- [ ] **Step 4: Run the suite**

Run: `.venv\Scripts\python.exe -m pytest tests -q` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/main.py tests/test_enroll_route.py
git commit -m "feat: worker revocation endpoint (drops the per-PC role)"
```

---

### Task 5: `worker.py` rewrite — direct-SQL loop on psycopg

**Files:**
- Rewrite: `worker.py`
- Test: `tests/test_worker.py` (new)

**Interfaces:**
- Consumes: enroll response `{"worker_id", "cluster": {"id", "name"}, "dsn"}` (Task 3); DB schema (Task 1); grants (Task 2).
- Produces (Task 8 smoke imports these): `enroll(server: str, join_code: str, name: str) -> dict` (saves + returns cfg `{"dsn", "worker_id", "cluster_id", "name", "cluster_name"}`), `claim_next(conn, worker_id: int, cluster_id: int) -> dict | None` (returns `{"assignment_id", "claude_api_key", "ticket": {"id","board_id","title","body","status","attempts"}}` — same shape v1 delivered over HTTP), `finish_work(conn, worker_id: int, worker_name: str, item_id: int, ticket_id: int, ok: bool, comment: str | None) -> str` (returns the resulting ticket status; no-ops with `"superseded"` if the claim was superseded), `heartbeat(conn, worker_id: int) -> None`, `StubExecutor`, `ClaudeExecutor`, `CONFIG_PATH`, `MAX_ATTEMPTS = 2`, `POLL_SECONDS = 10`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_worker.py`:

```python
"""worker.py v2 pure logic: config round-trip, enroll parsing, SQL invariants.

The claim/finish SQL itself is Postgres-only (SKIP LOCKED) and is exercised
by scripts/neon_smoke_v2.py against the real database.
"""
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import worker  # noqa: E402


def test_config_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "CONFIG_PATH", tmp_path / "cfg.json")
    cfg = {"dsn": "postgresql://r:p@h/db", "worker_id": 1,
           "cluster_id": 2, "name": "pc", "cluster_name": "Main"}
    worker.save_config(cfg)
    assert worker.load_config() == cfg


def test_enroll_saves_full_config(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "CONFIG_PATH", tmp_path / "cfg.json")
    response = {"worker_id": 7, "cluster": {"id": 3, "name": "Main"},
                "dsn": "postgresql://worker_c3_w7:pw@h/db?sslmode=require"}

    def fake_urlopen(req, data=None, timeout=None):
        assert req.full_url == "https://srv.example/api/workers/enroll"
        body = json.loads(data.decode())
        assert body == {"join_code": "ABC12345", "name": "pc"}
        return io.BytesIO(json.dumps(response).encode())

    monkeypatch.setattr(worker.urllib.request, "urlopen", fake_urlopen)
    cfg = worker.enroll("https://srv.example", "ABC12345", "pc")
    assert cfg == {"dsn": response["dsn"], "worker_id": 7, "cluster_id": 3,
                   "name": "pc", "cluster_name": "Main"}
    assert worker.load_config() == cfg


def test_claim_sql_is_race_safe_and_utc():
    assert "FOR UPDATE OF wq SKIP LOCKED" in worker.CLAIM_SQL
    assert "now() at time zone 'utc'" in worker.CLAIM_SQL
    assert "target_worker IS NULL OR" in worker.CLAIM_SQL
    assert "LIMIT 1" in worker.CLAIM_SQL


def test_max_attempts_matches_server():
    from app.models import MAX_ATTEMPTS
    assert worker.MAX_ATTEMPTS == MAX_ATTEMPTS


def test_executor_selection():
    assert worker.StubExecutor().name == "stub"
    assert worker.ClaudeExecutor().name == "claude"
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv\Scripts\python.exe -m pytest tests/test_worker.py -q`
Expected: FAIL — `worker` has no `enroll`/`CLAIM_SQL`.

- [ ] **Step 3: Rewrite `worker.py`**

Full replacement (keep the executor classes verbatim from v1 — they are unchanged; shown compressed here but copy them from the existing file):

```python
"""kanban-cloud worker client (v2: direct Postgres).

One-time enrollment over HTTP issues this PC its own database role; after
that the worker never contacts the web service — polling, claiming,
progress, results, and heartbeats are SQL against Neon.

Setup (once per PC):
    pip install "psycopg[binary]"
    py worker.py --enroll --server https://kanban-cloud.onrender.com \
                 --join-code ABC12345 --name ryans-pc

Run:
    py worker.py            # stub executor
    py worker.py --real     # execute tickets via the Claude CLI

Note: while this worker runs, its polling keeps Neon compute awake
(free tier autosuspends only when idle). Stop the worker when not in use.
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import psycopg

CONFIG_PATH = Path(__file__).parent / ".worker_config.json"
POLL_SECONDS = 10
MAX_ATTEMPTS = 2  # keep in sync with app/models.py MAX_ATTEMPTS

UTC_NOW = "(now() at time zone 'utc')"

# Atomic, race-safe claim: SKIP LOCKED means concurrent workers never block
# or double-claim; the subquery orders by queue age and honors target_worker.
CLAIM_SQL = f"""
UPDATE work_queue SET status='claimed', claimed_by=%(wid)s, claimed_at={UTC_NOW}
WHERE id = (
  SELECT wq.id FROM work_queue wq
  JOIN tickets t ON t.id = wq.ticket_id
  WHERE wq.status='queued' AND wq.cluster_id=%(cid)s
    AND (t.target_worker IS NULL OR t.target_worker = %(wid)s)
  ORDER BY wq.queued_at, wq.id
  FOR UPDATE OF wq SKIP LOCKED
  LIMIT 1
)
RETURNING id, ticket_id
"""


# ---------- executors (unchanged from v1) ----------
# ... StubExecutor and ClaudeExecutor exactly as in the current worker.py ...


# ---------- config & enrollment ----------

def load_config() -> dict | None:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return None


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    print(f"Saved worker config to {CONFIG_PATH}")


def enroll(server: str, join_code: str, name: str) -> dict:
    """One-time HTTP call; the server creates this PC's Postgres role and
    returns a ready-to-use DSN."""
    req = urllib.request.Request(
        server.rstrip("/") + "/api/workers/enroll", method="POST"
    )
    req.add_header("Content-Type", "application/json")
    data = json.dumps({"join_code": join_code, "name": name}).encode()
    with urllib.request.urlopen(req, data=data, timeout=30) as resp:
        payload = json.loads(resp.read().decode())
    cfg = {
        "dsn": payload["dsn"],
        "worker_id": payload["worker_id"],
        "cluster_id": payload["cluster"]["id"],
        "name": name,
        "cluster_name": payload["cluster"]["name"],
    }
    save_config(cfg)
    print(f"Enrolled worker '{name}' in cluster '{cfg['cluster_name']}'")
    return cfg


# ---------- direct-SQL work protocol ----------

def heartbeat(conn, worker_id: int) -> None:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            f"UPDATE workers SET last_seen={UTC_NOW} WHERE id=%s", (worker_id,)
        )


def claim_next(conn, worker_id: int, cluster_id: int) -> dict | None:
    """Claim the oldest eligible queued item; returns the v1 poll payload
    shape or None. One transaction: claim + ticket flip + key read."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(CLAIM_SQL, {"wid": worker_id, "cid": cluster_id})
        row = cur.fetchone()
        if row is None:
            cur.execute(
                f"UPDATE workers SET status='idle', last_seen={UTC_NOW} WHERE id=%s",
                (worker_id,),
            )
            return None
        item_id, ticket_id = row
        cur.execute(
            f"UPDATE tickets SET status='doing', assigned_worker=%s, "
            f"attempts=COALESCE(attempts,0)+1, updated_at={UTC_NOW} "
            f"WHERE id=%s RETURNING board_id, title, body, attempts",
            (worker_id, ticket_id),
        )
        board_id, title, body, attempts = cur.fetchone()
        cur.execute(
            f"UPDATE workers SET status='working', last_seen={UTC_NOW} WHERE id=%s",
            (worker_id,),
        )
        cur.execute(
            "SELECT claude_api_key FROM cluster_settings WHERE cluster_id=%s",
            (cluster_id,),
        )
        key_row = cur.fetchone()
        return {
            "assignment_id": item_id,
            "claude_api_key": key_row[0] if key_row else None,
            "ticket": {
                "id": ticket_id, "board_id": board_id, "title": title,
                "body": body, "status": "doing", "attempts": attempts,
            },
        }


def add_progress(conn, worker_id: int, worker_name: str, ticket_id: int, message: str) -> None:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO comments (ticket_id, writer, message, created_at) "
            f"VALUES (%s, %s, %s, {UTC_NOW})",
            (ticket_id, f"worker:{worker_name}", message),
        )
        cur.execute(
            f"UPDATE workers SET last_seen={UTC_NOW} WHERE id=%s", (worker_id,)
        )


def finish_work(conn, worker_id: int, worker_name: str, item_id: int,
                ticket_id: int, ok: bool, comment: str | None) -> str:
    """Record the result. Mirrors v1 delegation.finish_work: success ->
    review; failure -> requeue until MAX_ATTEMPTS then failed. The rowcount
    guard on the first UPDATE preserves v1's 409-on-superseded semantics:
    if the claim was superseded while we worked, nothing else is written."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            f"UPDATE work_queue SET status=%s, finished_at={UTC_NOW}, result=%s "
            f"WHERE id=%s AND status='claimed' AND claimed_by=%s",
            ("done" if ok else "failed", (comment or "")[:10000], item_id, worker_id),
        )
        if cur.rowcount != 1:
            cur.execute(
                f"UPDATE workers SET status='idle', last_seen={UTC_NOW} WHERE id=%s",
                (worker_id,),
            )
            return "superseded"
        if comment:
            cur.execute(
                f"INSERT INTO comments (ticket_id, writer, message, created_at) "
                f"VALUES (%s, %s, %s, {UTC_NOW})",
                (ticket_id, f"worker:{worker_name}", comment),
            )
        if ok:
            ticket_status = "review"
            cur.execute(
                f"UPDATE tickets SET status='review', updated_at={UTC_NOW} WHERE id=%s",
                (ticket_id,),
            )
        else:
            cur.execute("SELECT attempts, board_id FROM tickets WHERE id=%s", (ticket_id,))
            attempts, board_id = cur.fetchone()
            if (attempts or 0) < MAX_ATTEMPTS:
                ticket_status = "ready"
                cur.execute("SELECT cluster_id FROM boards WHERE id=%s", (board_id,))
                cluster_id = cur.fetchone()[0]
                cur.execute(
                    f"INSERT INTO work_queue (ticket_id, cluster_id, status, queued_at) "
                    f"VALUES (%s, %s, 'queued', {UTC_NOW})",
                    (ticket_id, cluster_id),
                )
                cur.execute(
                    f"UPDATE tickets SET status='ready', assigned_worker=NULL, "
                    f"updated_at={UTC_NOW} WHERE id=%s",
                    (ticket_id,),
                )
            else:
                ticket_status = "failed"
                cur.execute(
                    f"UPDATE tickets SET status='failed', updated_at={UTC_NOW} WHERE id=%s",
                    (ticket_id,),
                )
        cur.execute(
            f"UPDATE workers SET status='idle', last_seen={UTC_NOW} WHERE id=%s",
            (worker_id,),
        )
        return ticket_status


# ---------- main loop ----------

def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # cp1252 consoles must not kill us
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description="kanban-cloud worker (v2 direct-DB)")
    parser.add_argument("--enroll", action="store_true",
                        help="enroll this PC (needs --server and --join-code)")
    parser.add_argument("--server", help="server base URL (enrollment only)")
    parser.add_argument("--join-code", help="cluster join code (enrollment only)")
    parser.add_argument("--name", help="worker name (defaults to computer name)")
    parser.add_argument("--real", action="store_true",
                        help="use the Claude CLI executor instead of the stub")
    parser.add_argument("--poll", type=float, default=POLL_SECONDS,
                        help=f"poll interval seconds (default {POLL_SECONDS})")
    parser.add_argument("--once", action="store_true",
                        help="poll a single time then exit (for testing)")
    args = parser.parse_args()

    if args.enroll:
        if not args.server or not args.join_code:
            print("--enroll needs --server and --join-code")
            return 2
        name = (args.name or os.environ.get("COMPUTERNAME")
                or os.environ.get("HOSTNAME") or "worker")
        enroll(args.server, args.join_code, name)
        return 0

    cfg = load_config()
    if cfg is None:
        print("No saved config. Enroll first: "
              "py worker.py --enroll --server <url> --join-code <code>")
        return 2

    executor = ClaudeExecutor() if args.real else StubExecutor()
    print(f"Worker '{cfg['name']}' polling Postgres every {args.poll}s "
          f"(executor: {executor.name}). Ctrl+C to stop.")

    conn = None
    while True:
        try:
            if conn is None or conn.closed:
                conn = psycopg.connect(cfg["dsn"], connect_timeout=15)
            work = claim_next(conn, cfg["worker_id"], cfg["cluster_id"])
            if work:
                ticket = work["ticket"]
                item_id = work["assignment_id"]
                print(f"Claimed ticket #{ticket['id']} '{ticket['title']}' "
                      f"(assignment {item_id})")
                try:
                    ok, comment = executor.run(ticket, work.get("claude_api_key"))
                except Exception as exc:
                    ok, comment = False, f"Executor error: {exc!r}"
                status = finish_work(conn, cfg["worker_id"], cfg["name"],
                                     item_id, ticket["id"], ok, comment)
                print(f"  reported {'success' if ok else 'FAILURE'} -> "
                      f"ticket status: {status}")
            else:
                heartbeat(conn, cfg["worker_id"])
        except psycopg.OperationalError as e:
            msg = str(e)
            print(f"Database unreachable ({msg[:200]}); retrying...")
            if "password authentication failed" in msg or "does not exist" in msg:
                print("Credentials rejected — this PC may be revoked. Re-enroll.")
                return 1
            conn = None
        except KeyboardInterrupt:
            print("\nStopping worker.")
            return 0

        if args.once:
            return 0
        try:
            time.sleep(args.poll)
        except KeyboardInterrupt:
            print("\nStopping worker.")
            return 0


if __name__ == "__main__":
    sys.exit(main())
```

Implementation notes for this step:
- Copy `StubExecutor`/`ClaudeExecutor` verbatim from the current `worker.py` (lines 54-102) into the marked section.
- `heartbeat()` on idle polls replaces v1's poll-side `last_seen` bump; `claim_next` already sets `status='idle'` on empty polls, so `heartbeat` only needs `last_seen`.
- `conn.transaction()` gives one atomic transaction per protocol step; the claim's `FOR UPDATE OF wq SKIP LOCKED` guarantees exactly one winner under concurrency.

- [ ] **Step 4: Run the tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_worker.py -q` then the full suite `.venv\Scripts\python.exe -m pytest tests -q`.
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add worker.py tests/test_worker.py
git commit -m "feat: worker v2 - direct Postgres protocol (enroll once, then pure SQL)"
```

---

### Task 6: Board UI — revoked badge, revoke button, enroll hint

**Files:**
- Modify: `app/static/index.html` (workers panel render ~lines 366-370; add one function)

**Interfaces:**
- Consumes: `GET ./api/clusters/{id}/workers` now includes `revoked: bool` (Task 3); `POST ./api/workers/{id}/revoke` (Task 4). All fetches stay **relative** (`./api/...`) so the okeefe.work `/board/` prefix keeps working.

- [ ] **Step 1: Update the workers panel renderer**

Replace the workers panel block (currently rendering name + online dot) with:

```javascript
  workers = await api("GET", `./api/clusters/${currentCluster.id}/workers`);
  document.getElementById("workersPanel").innerHTML = workers.length
    ? workers.map(w => `<div class="worker-row"><span class="dot ${w.online && !w.revoked ? "on" : ""}"></span>
        ${esc(w.name)}${w.revoked ? ' <span class="muted">(revoked)</span>'
          : ` <button class="ghost small" onclick="revokeWorker(${w.id})">Revoke</button>`}</div>`).join("")
    : `<span class="muted">No PCs enrolled yet. On the PC:<br>
       <code>py worker.py --enroll --server ${esc(location.origin)} --join-code &lt;code&gt;</code></span>`;
```

Add next to the other action functions:

```javascript
async function revokeWorker(id) {
  if (!confirm("Revoke this PC's database access? It stops working immediately; re-enrolling restores it.")) return;
  await api("POST", `./api/workers/${id}/revoke`);
  await refresh();
}
```

Notes: keep the existing `esc()`/`api()`/`refresh()` helpers; match the file's existing style (no framework, template literals). If the target-worker `<select>` (~line 313) lists workers, filter out revoked ones: `workers.filter(w => !w.revoked).map(...)`. The enroll hint uses `location.origin` — when viewed through the proxy this shows okeefe.work, which is wrong for enrollment (workers enroll against the Render URL directly); hardcode the hint as `--server https://kanban-cloud.onrender.com` instead of `location.origin` when `session.mode` is `owner`/`spectator` (proxy mode), else `location.origin`. The session object is already fetched at startup (`GET ./api/session`).

- [ ] **Step 2: Verify by hand + suite**

Run: `.venv\Scripts\python.exe -m pytest tests -q` (UI has no JS tests; just confirm nothing server-side broke).
Then eyeball locally: `.venv\Scripts\python.exe -m uvicorn app.main:app --port 8900` → open http://127.0.0.1:8900, confirm the workers panel renders the enroll hint (empty DB), no console errors. Ctrl+C the server.

- [ ] **Step 3: Commit**

```bash
git add app/static/index.html
git commit -m "feat: workers panel v2 - revoke button, revoked badge, enroll hint"
```

---

### Task 7: Docs — README, STATUS.md, schema notes

**Files:**
- Modify: `README.md` (worker setup + architecture sections)
- Modify: `STATUS.md` (v2 entry)

**Interfaces:** none (prose only), but the commands documented must match Task 5's argparse exactly.

- [ ] **Step 1: Update README.md**

Rewrite the worker section to cover, in this order: architecture summary (one paragraph: workers are direct-DB clients with per-PC Postgres roles; the web service is the human face + enrollment desk); PC setup:

```
pip install "psycopg[binary]"
py worker.py --enroll --server https://kanban-cloud.onrender.com --join-code <CODE> --name my-pc
py worker.py            # stub executor
py worker.py --real     # Claude CLI executor
```

plus: revocation (UI button drops the Postgres role; re-enroll to restore), the grant model (`kanban_worker` group role; SELECT/INSERT/UPDATE only, no DELETE, no users/auth_tokens), the Neon-compute note (a running worker keeps compute awake), and delete every reference to `X-Worker-Token`, `/api/work/poll`, `/api/workers/register`. Update the "Deploying behind a reverse proxy" section's exempt-route list to just `/api/workers/enroll`.

- [ ] **Step 2: Append STATUS.md entry**

Add a dated `## 2026-08-08 — v2: DB-centric workers` section: what changed (workers→direct SQL, per-PC roles, HTTP worker API deleted), test counts, the spec/plan paths, and the remaining caveats that still hold from v1 (key plaintext in DB, no stale-claim reaper — note the supersede path covers it manually).

- [ ] **Step 3: Commit**

```bash
git add README.md STATUS.md
git commit -m "docs: v2 DB-centric worker architecture"
```

---

### Task 8: Deploy + live Neon smoke (enroll → race → result → revoke)

**Files:**
- Create: `scripts/neon_smoke_v2.py`

**Interfaces:**
- Consumes: `create_app` (real Neon URL), `worker.enroll`-shape config (built inline from the enroll response), `worker.claim_next` / `worker.finish_work` (imported from `worker.py`), `app.enrollment.revoke_role`.

- [ ] **Step 1: Write `scripts/neon_smoke_v2.py`**

```python
"""Live v2 smoke against the real Neon DB. Creates ONLY scratch fixtures and
deletes exactly those rows (plus drops the scratch roles) at the end — never
wipes tables (Ryan's real cluster may exist).

Usage: python scripts/neon_smoke_v2.py "<admin DATABASE_URL>"
Exit 0 = PASS.
"""
import concurrent.futures
import sys
from pathlib import Path

import psycopg
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import worker as worker_mod  # noqa: E402
from app import enrollment  # noqa: E402
from app.main import create_app  # noqa: E402

RACE_N = 8


def main() -> int:
    admin_url = sys.argv[1]
    app = create_app(admin_url)  # no proxy secret: local-mode API
    client = TestClient(app)
    created = {}
    try:
        # -- fixtures via the human API --
        r = client.post("/api/register", json={"email": "smoke-v2@example.com",
                                               "password": "smokepass"})
        assert r.status_code == 200, r.text
        headers = {"Authorization": f"Bearer {r.json()['token']}"}
        c = client.post("/api/clusters", json={"name": "smoke-v2-cluster"},
                        headers=headers).json()
        created["cluster_id"] = c["id"]
        board = client.get(f"/api/clusters/{c['id']}/boards", headers=headers).json()[0]
        client.put(f"/api/clusters/{c['id']}/settings",
                   json={"claude_api_key": "sk-smoke-fake"}, headers=headers)
        t = client.post(f"/api/boards/{board['id']}/tickets",
                        json={"title": "smoke v2 ticket", "status": "ready"},
                        headers=headers).json()

        # -- enroll two scratch workers --
        dsns, wids = [], []
        for name in ("smoke-pc-a", "smoke-pc-b"):
            e = client.post("/api/workers/enroll",
                            json={"join_code": c["join_code"], "name": name})
            assert e.status_code == 200, e.text
            dsns.append(e.json()["dsn"])
            wids.append(e.json()["worker_id"])
        print("enrolled 2 workers; roles live")

        # -- N-way claim race on worker A's DSN: exactly one winner --
        def try_claim(_):
            with psycopg.connect(dsns[0], connect_timeout=15) as conn:
                return worker_mod.claim_next(conn, wids[0], c["id"])
        with concurrent.futures.ThreadPoolExecutor(RACE_N) as ex:
            results = list(ex.map(try_claim, range(RACE_N)))
        wins = [r for r in results if r]
        assert len(wins) == 1, f"expected exactly 1 winner, got {len(wins)}"
        work = wins[0]
        assert work["claude_api_key"] == "sk-smoke-fake"
        print(f"claim race: 1/{RACE_N} winner (assignment {work['assignment_id']})")

        # -- finish -> review --
        with psycopg.connect(dsns[0]) as conn:
            status = worker_mod.finish_work(conn, wids[0], "smoke-pc-a",
                                            work["assignment_id"], t["id"],
                                            True, "smoke result")
        assert status == "review", status
        print("result recorded; ticket -> review")

        # -- worker role must NOT see auth tables --
        with psycopg.connect(dsns[0]) as conn, conn.cursor() as cur:
            try:
                cur.execute("SELECT count(*) FROM users")
                raise AssertionError("worker role can read users table!")
            except psycopg.errors.InsufficientPrivilege:
                conn.rollback()
        print("grants: users table correctly denied")

        # -- revoke worker B; its DSN must stop connecting --
        rv = client.post(f"/api/workers/{wids[1]}/revoke", headers=headers)
        assert rv.status_code == 200, rv.text
        try:
            psycopg.connect(dsns[1], connect_timeout=15).close()
            raise AssertionError("revoked worker can still connect!")
        except psycopg.OperationalError:
            pass
        print("revocation: dropped role can no longer connect")
        print("SMOKE PASS")
        return 0
    finally:
        # -- precise cleanup: scratch rows + scratch roles only --
        with psycopg.connect(admin_url.replace("postgresql+psycopg://", "postgresql://"),
                             connect_timeout=15) as conn, conn.cursor() as cur:
            cid = created.get("cluster_id")
            if cid is not None:
                cur.execute("DELETE FROM comments WHERE ticket_id IN "
                            "(SELECT t.id FROM tickets t JOIN boards b ON b.id=t.board_id "
                            "WHERE b.cluster_id=%s)", (cid,))
                cur.execute("DELETE FROM work_queue WHERE cluster_id=%s", (cid,))
                cur.execute("DELETE FROM tickets WHERE board_id IN "
                            "(SELECT id FROM boards WHERE cluster_id=%s)", (cid,))
                cur.execute("DELETE FROM boards WHERE cluster_id=%s", (cid,))
                cur.execute("SELECT id, role_name FROM workers WHERE cluster_id=%s", (cid,))
                for _, role in cur.fetchall():
                    if role:
                        cur.execute(f'DROP ROLE IF EXISTS "{role}"')
                cur.execute("DELETE FROM workers WHERE cluster_id=%s", (cid,))
                cur.execute("DELETE FROM cluster_settings WHERE cluster_id=%s", (cid,))
                cur.execute("DELETE FROM cluster_members WHERE cluster_id=%s", (cid,))
                cur.execute("DELETE FROM clusters WHERE id=%s", (cid,))
            cur.execute("DELETE FROM auth_tokens WHERE user_id IN "
                        "(SELECT id FROM users WHERE email='smoke-v2@example.com')")
            cur.execute("DELETE FROM users WHERE email='smoke-v2@example.com'")
            conn.commit()
        print("fixtures cleaned")


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Commit and push (this deploys)**

```bash
git add scripts/neon_smoke_v2.py
git commit -m "feat: live Neon smoke for v2 direct-DB worker protocol"
git push origin master
```

Watch the Render deploy (`mcp__render__list_deploys` for srv-d9r9oh0n74is73ebnpmg) until live, then `GET https://kanban-cloud.onrender.com/api/health` → expect 403 (proxy gate — that IS healthy) and `https://www.okeefe.work/board/api/health` → 200.

- [ ] **Step 3: Run the live smoke (Ryan runs it — classifier blocks agent-side prod-DB writes)**

Give Ryan this `!` one-liner (absolute paths; DSN read from .env, never pasted into chat):

```
! cd /c/Users/ryan/Documents/Github/kanban-cloud && ./.venv/Scripts/python.exe scripts/neon_smoke_v2.py "$(grep '^DATABASE_URL=' .env | cut -d= -f2-)"
```

Expected output ends with `SMOKE PASS` then `fixtures cleaned`. This is the step that proves Neon accepts SQL-created roles end-to-end (CREATE ROLE, login, SKIP LOCKED, DROP ROLE) — if Neon rejects role login, stop and re-plan (fallback documented in spec: shared-DSN delivery).

- [ ] **Step 4: Record the outcome**

Append the result (pass/fail, any surprises) to `STATUS.md` and `C:\Users\ryan\Documents\Github\OVERNIGHT.md`; update the `kanban-cloud-architecture` memory file to describe v2 (workers = direct DB, enroll route only, no X-Worker-Token). Commit docs:

```bash
git add STATUS.md
git commit -m "docs: v2 live smoke results"
git push origin master
```

---

## Self-review notes (already applied)

- Spec's grant list amended: `INSERT ON work_queue` added (failure-requeue inserts from the worker) — called out in the header and enrollment.py comment.
- Spec's illustrative claim SQL used `worker_id/claimed at now()`; the plan uses the real columns (`claimed_by`) and naive-UTC timestamps throughout.
- v1's "orphaned claim blocks re-delegation" fix is preserved: enqueue-side supersede stays in `delegation.enqueue_ticket`; the worker-side rowcount guard in `finish_work` replaces the old 409.
- `ensure_worker_group` runs at every startup, so grants self-heal after schema changes.
- Type consistency check: `enrollment.role_name_for/can_provision/provision_role/revoke_role/build_worker_dsn` signatures match across Tasks 2, 3, 4, 8; `worker.claim_next(conn, worker_id, cluster_id)` and `finish_work(conn, worker_id, worker_name, item_id, ticket_id, ok, comment)` match between Tasks 5 and 8.
