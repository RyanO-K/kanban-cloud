"""Connection/transaction hygiene for the worker (regression: a live worker
wedged itself solid and every slot stopped claiming).

psycopg3 is not autocommit by default, so a bare `conn.cursor()` read — no
`with conn.transaction()` around it — leaves an implicit transaction open on
that connection forever. Several read helpers here are deliberately bare, and
once one of them has opened that transaction, every later
`with conn.transaction()` on the same connection is only a SAVEPOINT inside
it: writes never commit and row locks are never released. In production that
turned the maintenance tick's reaper scan into a permanent lock on this PC's
own `workers` row, which every slot's claim_next then blocked on while
holding the cluster-wide `cluster_settings` gate.

The fix is one invariant — every connection this process opens is autocommit,
and it opens them all through connect_db — plus the claim's lock order (own
row before cluster-wide row). Both are pinned here. The psycopg semantics
themselves are modeled by FakeConn below rather than mocked away, so a helper
that quietly starts issuing bare writes still shows up as an uncommitted
statement.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import worker  # noqa: E402


# ---------- connect_db: the invariant ----------

def test_connect_db_opens_autocommit_connections(monkeypatch):
    captured = {}

    def fake_connect(dsn, **kwargs):
        captured["dsn"] = dsn
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(worker.psycopg, "connect", fake_connect)
    worker.connect_db("postgresql://example/db")

    assert captured["dsn"] == "postgresql://example/db"
    assert captured["kwargs"]["autocommit"] is True
    assert captured["kwargs"]["connect_timeout"] == worker.CONNECT_TIMEOUT_SECONDS


def test_every_worker_connection_goes_through_connect_db():
    """The invariant is only worth anything if nothing bypasses it: one
    psycopg.connect call in the whole module, inside connect_db itself."""
    source = Path(worker.__file__).read_text(encoding="utf-8")
    calls = [m.start() for m in re.finditer(r"psycopg\.connect\(", source)]
    assert len(calls) == 1, "open connections with connect_db(), not psycopg.connect()"
    body_start = source.index("def connect_db(")
    body_end = source.index("\ndef ", body_start + 1)
    assert body_start < calls[0] < body_end


# ---------- a psycopg-shaped connection that remembers its transaction ----------

class FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._conn.record(sql)

    def fetchone(self):
        return self._conn.next_row()

    def fetchall(self):
        return []


class FakeTxn:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        self._outermost = not self._conn.in_transaction
        self._conn.log.append("BEGIN" if self._outermost else "SAVEPOINT")
        self._conn.depth += 1
        return self

    def __exit__(self, *a):
        self._conn.depth -= 1
        self._conn.log.append("COMMIT" if self._outermost else "RELEASE")
        return False


class FakeConn:
    """Models the one psycopg behavior that matters here: with autocommit
    off, the first statement outside an explicit transaction silently opens
    one, and it stays open."""

    def __init__(self, autocommit=True, rows=None):
        self.autocommit = autocommit
        self.closed = False
        self.depth = 0
        self.implicit = False
        self.log = []
        self._rows = list(rows or [])

    @property
    def in_transaction(self):
        return self.implicit or self.depth > 0

    def record(self, sql):
        if not self.autocommit and not self.in_transaction:
            self.implicit = True
            self.log.append("BEGIN(implicit)")
        self.log.append(sql)

    def next_row(self):
        return self._rows.pop(0) if self._rows else None

    def cursor(self):
        return FakeCursor(self)

    def transaction(self):
        return FakeTxn(self)

    def statements(self):
        return [s for s in self.log
                if s not in ("BEGIN", "COMMIT", "SAVEPOINT", "RELEASE", "BEGIN(implicit)")]


def open_conn(monkeypatch, rows=None):
    """A FakeConn built the way this process actually builds connections —
    through connect_db, honoring whatever autocommit setting it asks for. Every
    test below runs against that rather than asserting autocommit by hand, so
    dropping the flag fails these tests too, not just the one above."""
    def fake_connect(dsn, **kwargs):
        return FakeConn(autocommit=kwargs.get("autocommit", False), rows=rows)

    monkeypatch.setattr(worker.psycopg, "connect", fake_connect)
    return worker.connect_db("postgresql://example/db")


def maintenance_tick(conn):
    """Exactly what main()'s heartbeat thread does once per poll."""
    worker.set_slot_counts(conn, worker_id=6, concurrency=5, running=0)
    worker.reap_stale_claims(conn, cluster_id=3)
    worker.prune_ticket_log(conn, cluster_id=3)
    worker.triage_todo_tickets(conn, cluster_id=3, run_llm=lambda prompt: "")


# ---------- the wedge ----------

def test_maintenance_tick_leaves_no_transaction_open(monkeypatch):
    conn = open_conn(monkeypatch)
    maintenance_tick(conn)
    assert conn.in_transaction is False
    assert "BEGIN(implicit)" not in conn.log


def test_repeated_ticks_commit_the_workers_update_every_time(monkeypatch):
    """The wedge: on tick 2 the `workers` UPDATE landed inside a transaction
    tick 1's bare reaper scan had left open, so the row lock was held for the
    life of the process and every slot's claim_next blocked on it."""
    conn = open_conn(monkeypatch)
    for _ in range(3):
        maintenance_tick(conn)

    for i, entry in enumerate(conn.log):
        if entry.startswith("UPDATE workers"):
            assert conn.log[i - 1] == "BEGIN", "workers UPDATE nested in an outer transaction"
            assert conn.log[i + 1] == "COMMIT", "workers UPDATE left uncommitted"
    assert sum(1 for s in conn.log if s.startswith("UPDATE workers")) == 3


def test_a_bare_read_helper_does_not_strand_the_next_write(monkeypatch):
    """fetch_pending_chat is a bare read on the chat pump's own connection;
    mark_chat_delivered is the write right behind it. Under a non-autocommit
    connection the read opens the transaction and the write becomes a
    savepoint inside it — the browser never sees a message go delivered."""
    conn = open_conn(monkeypatch)
    worker.fetch_pending_chat(conn, ticket_id=42)
    worker.mark_chat_delivered(conn, [1, 2])

    assert conn.log.count("BEGIN") == 1
    assert conn.log[-1] == "COMMIT"
    assert conn.in_transaction is False


# ---------- claim lock order ----------

def test_claim_next_locks_its_own_worker_row_before_the_cluster_gate(monkeypatch):
    """Own row first, cluster-wide row second. The reverse order is what let
    one stuck writer on the hot `workers` row stall every slot in the
    cluster: the first blocked slot sat on the cluster_settings gate lock
    while it waited."""
    # gate reads (cap, enabled, stop_all); CLAIM_SQL then finds nothing.
    conn = open_conn(monkeypatch, rows=[(None, False, False), None])
    assert worker.claim_next(conn, worker_id=6, cluster_id=3, board_ids=None) is None

    statements = conn.statements()
    workers_at = next(i for i, s in enumerate(statements) if s.startswith("UPDATE workers"))
    gate_at = next(i for i, s in enumerate(statements) if "cluster_settings" in s)
    assert workers_at < gate_at
    assert "FOR UPDATE" in statements[gate_at]


def test_claim_next_heartbeats_even_when_the_gate_refuses(monkeypatch):
    """A capped or stopped cluster must still leave the PC looking alive on
    the board — otherwise the reaper and the Workers panel both read a
    perfectly healthy, deliberately-idle worker as dead."""
    conn = open_conn(monkeypatch, rows=[(None, False, True)])  # stop_all set
    assert worker.claim_next(conn, worker_id=6, cluster_id=3, board_ids=None) is None

    statements = conn.statements()
    assert any("last_seen" in s for s in statements)
    assert any("status='idle'" in s for s in statements)
    assert worker.CLAIM_SQL not in statements
