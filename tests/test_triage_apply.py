"""worker.triage_todo_tickets / worker._apply_triage_one: applying a validated
triage result to the database, idempotently and race-safely.

worker.py's SQL is Postgres-only by contract (psycopg %s params, a raw
'(now() at time zone 'utc')' literal), same as the reaper (see
tests/test_reaper.py's own docstring). SqliteShimConn below adapts the exact
SQL shapes triage_todo_tickets/_apply_triage_one issue onto stdlib sqlite3, so
these tests run the real production functions against a real embedded
database rather than a mock.
"""
import json
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
CREATE TABLE boards (id INTEGER PRIMARY KEY, cluster_id INTEGER);
CREATE TABLE tickets (
    id INTEGER PRIMARY KEY, board_id INTEGER, title TEXT, body TEXT,
    status TEXT, model TEXT, assigned_worker INTEGER, updated_at TEXT
);
CREATE TABLE ticket_deps (
    id INTEGER PRIMARY KEY, ticket_id INTEGER, depends_on_id INTEGER,
    UNIQUE (ticket_id, depends_on_id)
);
CREATE TABLE work_queue (
    id INTEGER PRIMARY KEY, ticket_id INTEGER, cluster_id INTEGER,
    status TEXT, queued_at TEXT
);
"""


def _init_db(path):
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def _seed(path, *, board_id=1, cluster_id=1, extra_tickets=()):
    conn = sqlite3.connect(str(path))
    conn.execute("INSERT INTO boards (id, cluster_id) VALUES (?, ?)", (board_id, cluster_id))
    conn.execute(
        "INSERT INTO tickets (id, board_id, title, body, status, model) "
        "VALUES (2, ?, 'Add retry logic', 'Retries should back off.', 'todo', NULL)",
        (board_id,),
    )
    for tid, title, status in extra_tickets:
        conn.execute(
            "INSERT INTO tickets (id, board_id, title, body, status, model) "
            "VALUES (?, ?, ?, '', ?, NULL)",
            (tid, board_id, title, status),
        )
    conn.commit()
    conn.close()


def _fake_llm(reply_text):
    return lambda prompt: reply_text


def _raising_llm(exc=RuntimeError("claude CLI crashed")):
    def _raise(prompt):
        raise exc
    return _raise


def test_successful_triage_promotes_ticket_and_records_deps(tmp_path):
    db = tmp_path / "t.db"
    _init_db(db)
    _seed(db, extra_tickets=[(1, "Set up the queue", "done")])

    conn = SqliteShimConn(db)
    reply = json.dumps({"model": "sonnet", "depends_on": [1]})
    applied = worker.triage_todo_tickets(conn, cluster_id=1, run_llm=_fake_llm(reply))
    conn.close()

    assert applied == [(2, "sonnet", [1])]
    raw = sqlite3.connect(str(db))
    assert raw.execute("SELECT status, model FROM tickets WHERE id=2").fetchone() == ("ready", "sonnet")
    assert raw.execute(
        "SELECT ticket_id, depends_on_id FROM ticket_deps"
    ).fetchall() == [(2, 1)]
    assert raw.execute(
        "SELECT ticket_id, status FROM work_queue"
    ).fetchall() == [(2, "queued")]


def test_triage_with_no_dependencies(tmp_path):
    db = tmp_path / "t.db"
    _init_db(db)
    _seed(db)

    conn = SqliteShimConn(db)
    reply = json.dumps({"model": "haiku", "depends_on": []})
    applied = worker.triage_todo_tickets(conn, cluster_id=1, run_llm=_fake_llm(reply))
    conn.close()

    assert applied == [(2, "haiku", [])]
    raw = sqlite3.connect(str(db))
    assert raw.execute("SELECT count(*) FROM ticket_deps").fetchone() == (0,)


def test_second_pass_over_the_same_ticket_changes_nothing(tmp_path):
    """Idempotency: once a ticket is triaged, a later pass must not touch it
    again, call the LLM again, or duplicate its dependency edge / work item."""
    db = tmp_path / "t.db"
    _init_db(db)
    _seed(db, extra_tickets=[(1, "Set up the queue", "done")])

    conn = SqliteShimConn(db)
    reply = json.dumps({"model": "sonnet", "depends_on": [1]})
    first = worker.triage_todo_tickets(conn, cluster_id=1, run_llm=_fake_llm(reply))
    assert first == [(2, "sonnet", [1])]

    def _boom(prompt):
        raise AssertionError("run_llm must not be called for an already-triaged ticket")

    second = worker.triage_todo_tickets(conn, cluster_id=1, run_llm=_boom)
    conn.close()

    assert second == []
    raw = sqlite3.connect(str(db))
    assert raw.execute("SELECT status, model FROM tickets WHERE id=2").fetchone() == ("ready", "sonnet")
    assert raw.execute("SELECT count(*) FROM ticket_deps").fetchone() == (1,)
    assert raw.execute("SELECT count(*) FROM work_queue").fetchone() == (1,)


def test_llm_failure_leaves_ticket_in_todo(tmp_path):
    db = tmp_path / "t.db"
    _init_db(db)
    _seed(db)

    conn = SqliteShimConn(db)
    applied = worker.triage_todo_tickets(conn, cluster_id=1, run_llm=_raising_llm())
    conn.close()

    assert applied == []
    raw = sqlite3.connect(str(db))
    assert raw.execute("SELECT status, model FROM tickets WHERE id=2").fetchone() == ("todo", None)
    assert raw.execute("SELECT count(*) FROM work_queue").fetchone() == (0,)


def test_unparseable_reply_leaves_ticket_in_todo(tmp_path):
    db = tmp_path / "t.db"
    _init_db(db)
    _seed(db)

    conn = SqliteShimConn(db)
    applied = worker.triage_todo_tickets(conn, cluster_id=1, run_llm=_fake_llm("not json"))
    conn.close()

    assert applied == []
    raw = sqlite3.connect(str(db))
    assert raw.execute("SELECT status, model FROM tickets WHERE id=2").fetchone() == ("todo", None)


def test_invalid_model_in_reply_leaves_ticket_in_todo(tmp_path):
    db = tmp_path / "t.db"
    _init_db(db)
    _seed(db)

    conn = SqliteShimConn(db)
    reply = json.dumps({"model": "gpt-5", "depends_on": []})
    applied = worker.triage_todo_tickets(conn, cluster_id=1, run_llm=_fake_llm(reply))
    conn.close()

    assert applied == []
    raw = sqlite3.connect(str(db))
    assert raw.execute("SELECT status FROM tickets WHERE id=2").fetchone() == ("todo",)


def test_already_triaged_ticket_is_not_selected(tmp_path):
    """A ticket with a model already set (even if somehow still 'todo') must
    not be re-triaged."""
    db = tmp_path / "t.db"
    _init_db(db)
    _seed(db)
    raw = sqlite3.connect(str(db))
    raw.execute("UPDATE tickets SET model='opus' WHERE id=2")
    raw.commit()
    raw.close()

    conn = SqliteShimConn(db)

    def _boom(prompt):
        raise AssertionError("should never be called")

    applied = worker.triage_todo_tickets(conn, cluster_id=1, run_llm=_boom)
    conn.close()
    assert applied == []


def test_two_racing_applies_land_exactly_once(tmp_path):
    db = tmp_path / "t.db"
    _init_db(db)
    _seed(db, extra_tickets=[(1, "Set up the queue", "done")])

    results = []
    barrier = threading.Barrier(2)

    def run_one():
        conn = SqliteShimConn(db)
        barrier.wait(timeout=5)
        results.append(worker._apply_triage_one(conn, ticket_id=2, cluster_id=1,
                                                 model="sonnet", depends_on=[1]))
        conn.close()

    threads = [threading.Thread(target=run_one) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    winners = [r for r in results if r is not None]
    assert len(winners) == 1, f"expected exactly one apply to win, got {results}"
    raw = sqlite3.connect(str(db))
    assert raw.execute("SELECT status, model FROM tickets WHERE id=2").fetchone() == ("ready", "sonnet")
    assert raw.execute("SELECT count(*) FROM ticket_deps").fetchone() == (1,)
    assert raw.execute("SELECT count(*) FROM work_queue").fetchone() == (1,)


def test_apply_update_is_guarded_on_todo_and_no_model():
    import inspect
    src = inspect.getsource(worker._apply_triage_one)
    assert "WHERE id=%s AND status='todo' AND model IS NULL" in src


def test_triage_select_is_scoped_to_todo_with_no_model():
    import inspect
    src = inspect.getsource(worker.triage_todo_tickets)
    assert "t.status='todo' AND t.model IS NULL" in src
