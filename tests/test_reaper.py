"""Stale-claim reaper (gap analysis phase 3): worker.reap_stale_claims /
worker._reap_one / worker.touch_claim_heartbeat / the per-claim heartbeat
thread wired into run_slot.

worker.py's SQL is Postgres-only by contract (psycopg %s params, a raw
'(now() at time zone 'utc')' literal) — the rest of the suite only exercises
it via string-invariant checks or mocks, deferring real-DB behavior to
scripts/neon_smoke_v2.py against live Neon. But this ticket's own acceptance
criteria are about *behavior* under a real race, which a mock can't prove.
SqliteShimConn below adapts the exact SQL shapes reap_stale_claims/_reap_one/
touch_claim_heartbeat issue onto stdlib sqlite3 (translating '%s' -> '?' and
the UTC_NOW literal -> CURRENT_TIMESTAMP), so the race tests run the real
production functions against a real embedded database with real file-level
write locking — SQLite's writer serialization gives the same "loser's
UPDATE sees rowcount 0" guarantee this code relies on under Postgres READ
COMMITTED (already documented and relied on by finish_work's rowcount guard).
"""
import datetime
import sqlite3
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import worker  # noqa: E402

# sqlite3's default datetime adapter is deprecated (3.12+); register our own
# so heartbeat_at/claimed_at (written as CURRENT_TIMESTAMP text) and a
# Python-side cutoff datetime compare correctly as same-shaped ISO text.
sqlite3.register_adapter(datetime.datetime, lambda dt: dt.strftime("%Y-%m-%d %H:%M:%S.%f"))


class _ShimCursor:
    def __init__(self, raw):
        self._raw = raw
        self.rowcount = -1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._raw.close()
        return False

    def execute(self, sql, params=()):
        sql = sql.replace("%s", "?").replace("(now() at time zone 'utc')", "CURRENT_TIMESTAMP")
        self._raw.execute(sql, params)
        self.rowcount = self._raw.rowcount

    def fetchone(self):
        return self._raw.fetchone()

    def fetchall(self):
        return self._raw.fetchall()


class _ShimTxn:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        self._conn.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(self, exc_type, exc, tb):
        self._conn.execute("ROLLBACK" if exc_type else "COMMIT")
        return False


class SqliteShimConn:
    """Presents just enough of psycopg's connection API for worker.py's raw
    SQL functions: .cursor(), .transaction(), .closed, .close()."""

    def __init__(self, path):
        self._raw = sqlite3.connect(str(path), timeout=30, isolation_level=None)

    def cursor(self):
        return _ShimCursor(self._raw.cursor())

    def transaction(self):
        return _ShimTxn(self._raw)

    @property
    def closed(self):
        return False

    def close(self):
        self._raw.close()


SCHEMA = """
CREATE TABLE work_queue (
    id INTEGER PRIMARY KEY,
    ticket_id INTEGER, cluster_id INTEGER, status TEXT,
    claimed_by INTEGER, queued_at TEXT, claimed_at TEXT,
    heartbeat_at TEXT, finished_at TEXT, result TEXT
);
CREATE TABLE tickets (
    id INTEGER PRIMARY KEY, board_id INTEGER, status TEXT,
    assigned_worker INTEGER, attempts INTEGER, updated_at TEXT
);
CREATE TABLE boards (id INTEGER PRIMARY KEY, cluster_id INTEGER);
"""


def _init_db(path):
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def _seed(path, *, item_id=1, ticket_id=1, board_id=1, cluster_id=1,
         status="claimed", attempts=1, heartbeat_age_seconds=999):
    heartbeat_at = (datetime.datetime(2026, 1, 1, 12, 0, 0)
                    - datetime.timedelta(seconds=heartbeat_age_seconds))
    claimed_at = heartbeat_at
    conn = sqlite3.connect(str(path))
    conn.execute("INSERT INTO boards (id, cluster_id) VALUES (?, ?)", (board_id, cluster_id))
    conn.execute(
        "INSERT INTO tickets (id, board_id, status, assigned_worker, attempts, updated_at) "
        "VALUES (?, ?, 'doing', 1, ?, ?)",
        (ticket_id, board_id, attempts, claimed_at),
    )
    conn.execute(
        "INSERT INTO work_queue (id, ticket_id, cluster_id, status, claimed_by, "
        "queued_at, claimed_at, heartbeat_at) VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
        (item_id, ticket_id, cluster_id, status, claimed_at, claimed_at, heartbeat_at),
    )
    conn.commit()
    conn.close()


NOW = datetime.datetime(2026, 1, 1, 12, 0, 0)


def test_live_claim_is_never_touched(tmp_path):
    db = tmp_path / "reap.db"
    _init_db(db)
    _seed(db, heartbeat_age_seconds=5)  # well inside the threshold

    conn = SqliteShimConn(db)
    reaped = worker.reap_stale_claims(conn, cluster_id=1, stale_after_seconds=300, now=NOW)
    conn.close()

    assert reaped == []
    raw = sqlite3.connect(str(db))
    assert raw.execute("SELECT status FROM work_queue WHERE id=1").fetchone() == ("claimed",)
    assert raw.execute("SELECT status FROM tickets WHERE id=1").fetchone() == ("doing",)


def test_stale_claim_is_requeued_and_ticket_goes_ready(tmp_path):
    db = tmp_path / "reap.db"
    _init_db(db)
    _seed(db, attempts=1, heartbeat_age_seconds=999)  # attempts < MAX_ATTEMPTS (2)

    conn = SqliteShimConn(db)
    reaped = worker.reap_stale_claims(conn, cluster_id=1, stale_after_seconds=300, now=NOW)
    conn.close()

    assert reaped == [(1, 1, "ready")]
    raw = sqlite3.connect(str(db))
    assert raw.execute("SELECT status, result FROM work_queue WHERE id=1").fetchone()[0] == "failed"
    assert raw.execute("SELECT status FROM tickets WHERE id=1").fetchone() == ("ready",)
    queued = raw.execute(
        "SELECT ticket_id, status FROM work_queue WHERE id != 1"
    ).fetchall()
    assert queued == [(1, "queued")]  # exactly one fresh row, not a duplicate


def test_stale_claim_at_max_attempts_fails_the_ticket_without_requeue(tmp_path):
    db = tmp_path / "reap.db"
    _init_db(db)
    _seed(db, attempts=worker.MAX_ATTEMPTS, heartbeat_age_seconds=999)

    conn = SqliteShimConn(db)
    reaped = worker.reap_stale_claims(conn, cluster_id=1, stale_after_seconds=300, now=NOW)
    conn.close()

    assert reaped == [(1, 1, "failed")]
    raw = sqlite3.connect(str(db))
    assert raw.execute("SELECT status FROM tickets WHERE id=1").fetchone() == ("failed",)
    assert raw.execute("SELECT count(*) FROM work_queue").fetchone() == (1,)  # no new row


def test_stale_claim_falls_back_to_claimed_at_when_never_heartbeated(tmp_path):
    """A claim from before this column existed (NULL heartbeat_at) must still
    be reapable, falling back to claimed_at."""
    db = tmp_path / "reap.db"
    _init_db(db)
    _seed(db, heartbeat_age_seconds=999)
    raw = sqlite3.connect(str(db))
    raw.execute("UPDATE work_queue SET heartbeat_at=NULL")
    raw.commit()
    raw.close()

    conn = SqliteShimConn(db)
    reaped = worker.reap_stale_claims(conn, cluster_id=1, stale_after_seconds=300, now=NOW)
    conn.close()

    assert reaped == [(1, 1, "ready")]


def test_two_reapers_racing_the_same_stale_claim_requeue_it_exactly_once(tmp_path):
    db = tmp_path / "reap.db"
    _init_db(db)
    _seed(db, attempts=1, heartbeat_age_seconds=999)

    results = []
    barrier = threading.Barrier(2)

    def run_one():
        conn = SqliteShimConn(db)
        barrier.wait(timeout=5)  # maximize actual overlap
        results.append(worker.reap_stale_claims(conn, cluster_id=1,
                                                 stale_after_seconds=300, now=NOW))
        conn.close()

    threads = [threading.Thread(target=run_one) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    flat = [r for sub in results for r in sub]
    assert len(flat) == 1, f"expected exactly one reap to win, got {flat}"
    assert flat[0] == (1, 1, "ready")

    raw = sqlite3.connect(str(db))
    assert raw.execute("SELECT status FROM work_queue WHERE id=1").fetchone() == ("failed",)
    # Exactly one fresh queued row was created, not two.
    fresh = raw.execute("SELECT status FROM work_queue WHERE id != 1").fetchall()
    assert fresh == [("queued",)]
    assert raw.execute("SELECT status FROM tickets WHERE id=1").fetchone() == ("ready",)


def test_touch_claim_heartbeat_noops_once_the_claim_is_resolved(tmp_path):
    """A heartbeat that lands after finish_work (or the reaper) already
    resolved the row must not resurrect it."""
    db = tmp_path / "reap.db"
    _init_db(db)
    _seed(db, status="failed", heartbeat_age_seconds=5)

    conn = SqliteShimConn(db)
    worker.touch_claim_heartbeat(conn, item_id=1)
    conn.close()

    raw = sqlite3.connect(str(db))
    assert raw.execute("SELECT status FROM work_queue WHERE id=1").fetchone() == ("failed",)


def test_claim_sql_sets_heartbeat_at_alongside_claimed_at():
    assert "heartbeat_at=" in worker.CLAIM_SQL


def test_reap_sql_falls_back_to_claimed_at_when_no_heartbeat_yet():
    import inspect
    src = inspect.getsource(worker.reap_stale_claims)
    assert "COALESCE(heartbeat_at, claimed_at)" in src


def test_reap_update_is_guarded_on_still_claimed():
    import inspect
    src = inspect.getsource(worker._reap_one)
    assert "WHERE id=%s AND status='claimed'" in src


def test_claim_heartbeat_loop_touches_periodically(monkeypatch):
    calls = []
    monkeypatch.setattr(worker, "touch_claim_heartbeat",
                        lambda conn, item_id: calls.append(item_id))

    class FakeConn:
        closed = False

        def close(self):
            pass

    monkeypatch.setattr(worker.psycopg, "connect", lambda dsn, **kw: FakeConn())

    stop = threading.Event()
    t = threading.Thread(target=worker._claim_heartbeat_loop,
                         args=("dsn", 42, stop, 0.02))
    t.start()
    stop.wait(0.1)
    stop.set()
    t.join(timeout=2)

    assert calls, "heartbeat loop never touched the claim"
    assert all(c == 42 for c in calls)


def test_claim_heartbeat_loop_survives_connection_errors(monkeypatch):
    """A transient DB hiccup must not kill the heartbeat thread; it just
    reconnects on the next tick."""
    def boom(dsn, **kw):
        raise worker.psycopg.OperationalError("connection reset")

    monkeypatch.setattr(worker.psycopg, "connect", boom)
    stop = threading.Event()
    t = threading.Thread(target=worker._claim_heartbeat_loop,
                         args=("dsn", 1, stop, 0.02))
    t.start()
    stop.wait(0.1)
    stop.set()
    t.join(timeout=2)
    assert not t.is_alive()


def test_run_slot_starts_a_heartbeat_thread_that_stops_after(monkeypatch, tmp_path):
    """The reaper only works if something is actually refreshing heartbeat_at
    while the executor runs; run_slot must start one thread for the duration
    of the claim and join it before moving on."""
    monkeypatch.setattr(worker, "CONFIG_PATH", tmp_path / "cfg.json")
    seen = {}

    def fake_claim(conn, wid, cid, boards):
        return {"assignment_id": 7, "session_id": "s",
                "board": {"id": 1, "name": "b"},
                "ticket": {"id": 1, "board_id": 1, "title": "t", "body": "",
                           "status": "doing", "attempts": 1}}

    class RecordingExecutor:
        name = "recording"

        def run(self, ticket, board=None, directory=None, session_id=None):
            time.sleep(0.05)
            seen["thread_count_during_run"] = threading.active_count()
            return True, "ok"

    class FakeConn:
        closed = False

        def close(self):
            pass

    monkeypatch.setattr(worker.psycopg, "connect", lambda dsn, **kw: FakeConn())
    monkeypatch.setattr(worker, "claim_next", fake_claim)
    monkeypatch.setattr(worker, "finish_work", lambda *a, **k: "review")

    cfg = {"dsn": "x", "worker_id": 1, "cluster_id": 1, "name": "pc",
          "boards": {"1": str(tmp_path)}}
    args = worker.build_parser().parse_args(["--poll", "0.01", "--once"])
    baseline = threading.active_count()
    worker.run_slot(cfg, args, RecordingExecutor(), threading.Event(), 0)
    assert seen["thread_count_during_run"] == baseline + 1  # heartbeat thread was up
    assert threading.active_count() == baseline  # ...and joined afterward
