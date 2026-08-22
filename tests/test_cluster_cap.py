"""Cluster-wide concurrency cap (gap analysis phase 2, item 5): worker.py's
cluster_claim_gate, wired into claim_next.

Like test_reaper.py, the real race guarantee comes from Postgres row locking
(`SELECT ... FOR UPDATE`) that SQLite has no equivalent syntax for, so a small
shim adapts the exact production SQL shapes cluster_claim_gate issues onto
stdlib sqlite3 (stripping "FOR UPDATE", which SQLite doesn't support, and
relying on SQLite's own BEGIN IMMEDIATE writer-serialization for the same
"loser blocks until the winner commits, then re-reads" guarantee the
production code relies on under Postgres). CLAIM_SQL itself stays untouched
and untested here (Postgres-only: SKIP LOCKED, ::int[] casts) — see
scripts/neon_smoke_v2.py and tests/test_worker.py's own docstring.
"""
import sqlite3
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import worker  # noqa: E402


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
        sql = sql.replace("FOR UPDATE", "").replace("%s", "?")
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


class GateShimConn:
    """Presents just enough of psycopg's connection API for
    cluster_claim_gate: .cursor(), .transaction()."""

    def __init__(self, path):
        self._raw = sqlite3.connect(str(path), timeout=30, isolation_level=None)

    def cursor(self):
        return _ShimCursor(self._raw.cursor())

    def transaction(self):
        return _ShimTxn(self._raw)

    def close(self):
        self._raw.close()


SCHEMA = """
CREATE TABLE cluster_settings (
    cluster_id INTEGER PRIMARY KEY,
    concurrency_cap INTEGER,
    enabled INTEGER NOT NULL,
    stop_all_requested INTEGER NOT NULL
);
CREATE TABLE work_queue (
    id INTEGER PRIMARY KEY, cluster_id INTEGER, status TEXT
);
"""


def _init_db(path, *, cap=None, enabled=False, stop_all=False, cluster_id=1,
            with_settings_row=True, n_claimed=0):
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA)
    if with_settings_row:
        conn.execute(
            "INSERT INTO cluster_settings (cluster_id, concurrency_cap, enabled, stop_all_requested) "
            "VALUES (?, ?, ?, ?)",
            (cluster_id, cap, int(enabled), int(stop_all)),
        )
    for _ in range(n_claimed):
        conn.execute(
            "INSERT INTO work_queue (cluster_id, status) VALUES (?, 'claimed')", (cluster_id,)
        )
    conn.commit()
    conn.close()


def try_claim(path, cluster_id):
    """One full gate-then-claim attempt, exactly as claim_next structures it:
    lock+check inside the transaction, and only insert the "claim" (a
    work_queue row, standing in for CLAIM_SQL's real UPDATE) if the gate
    allows it. Returns True on a successful simulated claim, False if the
    gate refused."""
    conn = GateShimConn(path)
    try:
        with conn.transaction(), conn.cursor() as cur:
            if not worker.cluster_claim_gate(cur, cluster_id):
                return False
            cur.execute(
                "INSERT INTO work_queue (cluster_id, status) VALUES (?, 'claimed')",
                (cluster_id,),
            )
            return True
    finally:
        conn.close()


# ---------- cluster_claim_gate: single-threaded behavior ----------

def test_gate_allows_when_disabled_even_over_a_configured_cap(tmp_path):
    db = tmp_path / "c.db"
    _init_db(db, cap=1, enabled=False, n_claimed=5)
    assert try_claim(db, 1) is True


def test_gate_allows_under_the_cap(tmp_path):
    db = tmp_path / "c.db"
    _init_db(db, cap=3, enabled=True, n_claimed=2)
    assert try_claim(db, 1) is True


def test_gate_refuses_at_the_cap(tmp_path):
    db = tmp_path / "c.db"
    _init_db(db, cap=3, enabled=True, n_claimed=3)
    assert try_claim(db, 1) is False


def test_gate_refuses_when_stop_all_requested_regardless_of_cap(tmp_path):
    db = tmp_path / "c.db"
    _init_db(db, cap=None, enabled=False, stop_all=True)
    assert try_claim(db, 1) is False


def test_gate_defaults_to_unlimited_when_no_settings_row_exists(tmp_path):
    """Defensive fallback: run_migrations backfills a row for every cluster,
    but a cluster somehow missing one must not have every claim silently
    break — it behaves as it did before this feature."""
    db = tmp_path / "c.db"
    _init_db(db, with_settings_row=False)
    assert try_claim(db, 1) is True


def test_gate_ignores_other_clusters_claimed_count(tmp_path):
    db = tmp_path / "c.db"
    _init_db(db, cap=1, enabled=True, cluster_id=1, n_claimed=0)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO cluster_settings (cluster_id, concurrency_cap, enabled, stop_all_requested) "
        "VALUES (2, 1, 1, 0)"
    )
    conn.execute("INSERT INTO work_queue (cluster_id, status) VALUES (2, 'claimed')")
    conn.commit()
    conn.close()
    assert try_claim(db, 1) is True  # cluster 1's own count is still 0


# ---------- real concurrent races ----------

def test_nth_plus_one_concurrent_claim_is_refused_while_at_the_cap(tmp_path):
    """Cap of 2, three concurrent attempts (two simulated workers each firing
    more than one claim into the race): exactly 2 succeed."""
    db = tmp_path / "race.db"
    _init_db(db, cap=2, enabled=True)

    results = []
    lock = threading.Lock()
    barrier = threading.Barrier(3)

    def run_one():
        barrier.wait(timeout=5)
        r = try_claim(db, 1)
        with lock:
            results.append(r)

    threads = [threading.Thread(target=run_one) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert sorted(results) == [False, True, True]
    raw = sqlite3.connect(str(db))
    assert raw.execute(
        "SELECT count(*) FROM work_queue WHERE status='claimed' AND cluster_id=1"
    ).fetchone() == (2,)


def test_claim_succeeds_again_once_one_finishes(tmp_path):
    """Same cap-of-2 setup: once one of the two in-flight claims finishes
    (leaves 'claimed'), a fresh attempt succeeds again."""
    db = tmp_path / "race2.db"
    _init_db(db, cap=2, enabled=True, n_claimed=2)  # already at the cap

    assert try_claim(db, 1) is False  # still at the cap

    conn = sqlite3.connect(str(db))
    conn.execute(
        "UPDATE work_queue SET status='done' WHERE id = "
        "(SELECT id FROM work_queue WHERE cluster_id=1 AND status='claimed' LIMIT 1)"
    )
    conn.commit()
    conn.close()

    assert try_claim(db, 1) is True  # a slot freed up


def test_two_racing_claims_at_a_cap_of_one_exactly_one_wins(tmp_path):
    """The simplest form of the acceptance criterion: cap=1 (N=1), two
    simulated workers racing the (N+1)th=2nd claim — exactly one wins."""
    db = tmp_path / "race3.db"
    _init_db(db, cap=1, enabled=True)

    results = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def run_one():
        barrier.wait(timeout=5)
        r = try_claim(db, 1)
        with lock:
            results.append(r)

    threads = [threading.Thread(target=run_one) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert sorted(results) == [False, True]


# ---------- claim_next integration ----------

class _FakeCursor:
    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchone(self):
        return None


class _FakeConn:
    def __init__(self):
        self.cursors = []

    def cursor(self):
        cur = _FakeCursor()
        self.cursors.append(cur)
        return cur

    def transaction(self):
        class _T:
            def __enter__(self_):
                return self_

            def __exit__(self_, *a):
                return False

        return _T()


def test_claim_next_never_runs_claim_sql_when_the_gate_refuses(monkeypatch):
    monkeypatch.setattr(worker, "cluster_claim_gate", lambda cur, cid: False)
    conn = _FakeConn()
    result = worker.claim_next(conn, worker_id=3, cluster_id=1, board_ids=None)
    assert result is None
    executed = [sql for sql, _ in conn.cursors[0].calls]
    assert worker.CLAIM_SQL not in executed
    # the worker is still parked idle, exactly like "nothing queued"
    assert any("status='idle'" in sql for sql in executed)


def test_claim_next_runs_claim_sql_when_the_gate_allows(monkeypatch):
    monkeypatch.setattr(worker, "cluster_claim_gate", lambda cur, cid: True)
    conn = _FakeConn()
    worker.claim_next(conn, worker_id=3, cluster_id=1, board_ids=None)
    executed = [sql for sql, _ in conn.cursors[0].calls]
    assert worker.CLAIM_SQL in executed
