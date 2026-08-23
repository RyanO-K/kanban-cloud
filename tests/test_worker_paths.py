"""Per-PC board paths: the machine-level half of "where does this agent run".

Which folder a board maps to is this PC's business — the same board is worked
by machines with different layouts, so the server never *decides* these paths.
It does learn one after the fact: run_slot reports the directory each run
actually happened in (record_session_dir), because the board's resume command
is `cd '<dir>'; claude --resume <id>` and the id alone resumes nothing.
"""
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import worker  # noqa: E402


class FakeCursor:
    """Minimal psycopg cursor over a canned board list."""

    def __init__(self, rows):
        self._rows = rows
        # Every execute, in order: claim_next issues several per call and the
        # later ones would otherwise clobber the one under test.
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, rows=()):
        self._rows = rows
        self.cursors = []

    def cursor(self):
        cur = FakeCursor(self._rows)
        self.cursors.append(cur)
        return cur

    def transaction(self):
        class _T:
            def __enter__(self_):
                return self_

            def __exit__(self_, *a):
                return False

        return _T()


BOARDS = [(4, "site-page"), (7, "devtool-invoice")]


# ---------- parsing ----------

def test_parse_set_path_splits_on_the_first_equals():
    """Windows paths can contain '=', so only the first one separates."""
    assert worker.parse_set_path("4=C:/repos/a=b") == ("4", "C:/repos/a=b")


def test_parse_set_path_rejects_missing_equals():
    with pytest.raises(ValueError):
        worker.parse_set_path("4")


def test_parse_set_path_rejects_empty_halves():
    with pytest.raises(ValueError):
        worker.parse_set_path("=C:/repos")
    with pytest.raises(ValueError):
        worker.parse_set_path("4=")


# ---------- board resolution ----------

def test_resolve_board_by_id():
    assert worker.resolve_board(FakeConn(BOARDS), 1, "4") == (4, "site-page")


def test_resolve_board_by_name_is_case_insensitive():
    assert worker.resolve_board(FakeConn(BOARDS), 1, "SITE-page") == (4, "site-page")


def test_resolve_board_rejects_unknown_id():
    with pytest.raises(ValueError, match="no board with id"):
        worker.resolve_board(FakeConn(BOARDS), 1, "99")


def test_resolve_board_rejects_unknown_name_and_lists_the_real_ones():
    with pytest.raises(ValueError, match="site-page"):
        worker.resolve_board(FakeConn(BOARDS), 1, "nope")


def test_resolve_board_rejects_ambiguous_name():
    """Guessing would send an agent into the wrong repo."""
    with pytest.raises(ValueError, match="matches 2 boards"):
        worker.resolve_board(FakeConn([(1, "dup"), (2, "DUP")]), 1, "dup")


# ---------- saving ----------

def test_apply_set_path_requires_an_existing_directory(tmp_path):
    with pytest.raises(ValueError, match="does not exist"):
        worker.apply_set_path(FakeConn(BOARDS), {"cluster_id": 1},
                              f"4={tmp_path / 'missing'}")


def test_apply_set_path_saves_keyed_by_board_id(tmp_path, monkeypatch):
    """Keyed by id, not name: renaming a board must not orphan the path."""
    monkeypatch.setattr(worker, "CONFIG_PATH", tmp_path / "cfg.json")
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = {"cluster_id": 1, "worker_id": 2, "dsn": "x", "name": "pc"}
    out = worker.apply_set_path(FakeConn(BOARDS), cfg, f"site-page={repo}")
    assert out["boards"] == {"4": str(repo)}
    assert worker.load_config()["boards"] == {"4": str(repo)}


def test_apply_set_path_overwrites_an_existing_mapping(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "CONFIG_PATH", tmp_path / "cfg.json")
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    cfg = {"cluster_id": 1, "boards": {"4": str(a)}}
    out = worker.apply_set_path(FakeConn(BOARDS), cfg, f"4={b}")
    assert out["boards"] == {"4": str(b)}


# ---------- reading ----------

def test_configured_board_ids_are_ints():
    assert sorted(worker.configured_board_ids({"boards": {"4": "/a", "7": "/b"}})) == [4, 7]


def test_configured_board_ids_empty_when_unset():
    assert worker.configured_board_ids({}) == []


def test_configured_board_ids_skips_junk_keys():
    assert worker.configured_board_ids({"boards": {"4": "/a", "oops": "/b"}}) == [4]


def test_board_paths_defaults_to_empty():
    assert worker.board_paths({}) == {}


# ---------- claim filter ----------

def test_claim_sql_filters_on_configured_boards():
    """The predicate must live in the SQL. Filtering after the fact would claim
    the row first and then have to abandon it."""
    assert "t.board_id = ANY(" in worker.CLAIM_SQL


def test_claim_sql_also_admits_boards_with_a_repo_url():
    """A board with no --set-path entry anywhere but a repo_url configured must
    still be claimable, via an EXISTS check against the boards table — this is
    a construction-level assertion on the SQL text only (same limitation as
    test_claim_sql_filters_on_configured_boards above): CLAIM_SQL is Postgres
    syntax (SKIP LOCKED, ::int[] casts) that can't run against the SQLite/
    FakeCursor test harness in this file, so we can't execute it end-to-end
    here."""
    assert "repo_url" in worker.CLAIM_SQL
    assert "EXISTS" in worker.CLAIM_SQL.upper()


def _boards_param(cursor):
    """The `boards` param passed to CLAIM_SQL, wherever it lands among this
    cursor's calls — claim_next now issues the cluster cap gate's queries
    first (see cluster_claim_gate), so CLAIM_SQL is no longer necessarily the
    first execute() call. FakeCursor.fetchone() always returns None, so the
    gate sees "no settings row" and falls through to CLAIM_SQL unconditionally."""
    return next(params["boards"] for _, params in cursor.calls if params and "boards" in params)


def test_claim_next_passes_configured_boards():
    conn = FakeConn()
    worker.claim_next(conn, 3, 1, [4, 7])
    assert _boards_param(conn.cursors[0]) == [4, 7]


def test_claim_next_with_none_boards_disables_the_filter():
    """--stub needs no repo, so it must still be able to claim anything."""
    conn = FakeConn()
    worker.claim_next(conn, 3, 1, None)
    assert _boards_param(conn.cursors[0]) is None


# ---------- claim_next board row ----------


class FakeCursorWithBoardRow(FakeCursor):
    """Like FakeCursor, but claim_next's several fetchone() calls need to
    return a fixed sequence: the claim row, the ticket-flip row, then the
    board row."""

    def __init__(self, fetchone_sequence):
        super().__init__(rows=())
        self._sequence = list(fetchone_sequence)

    def fetchone(self):
        return self._sequence.pop(0) if self._sequence else None


class FakeConnWithBoardRow(FakeConn):
    def __init__(self, fetchone_sequence):
        super().__init__()
        self._sequence = fetchone_sequence

    def cursor(self):
        cur = FakeCursorWithBoardRow(self._sequence)
        self.cursors.append(cur)
        return cur


def test_claim_next_returns_repo_url_on_the_board_dict():
    conn = FakeConnWithBoardRow([
        None,                                                 # cap gate: no settings row -> unlimited
        (5, 9, False),                                        # claim: item_id, ticket_id, resume
        (2, "Fix the thing", "Details.", 1, None, None),      # ticket flip: board_id, title, body, attempts, profile_id, session_id
        ("site-page", "Desc", None, None, False, "https://github.com/org/repo.git", None, False),  # board row
    ])
    work = worker.claim_next(conn, worker_id=1, cluster_id=1, board_ids=None)
    assert work["board"]["repo_url"] == "https://github.com/org/repo.git"
    assert work["resume"] is None


# ---------- claim_next resume ----------

def test_claim_next_builds_resume_context_from_the_answered_question():
    conn = FakeConnWithBoardRow([
        None,                                                   # cap gate: no settings row -> unlimited
        (5, 9, True),                                           # claim: item_id, ticket_id, resume=True
        (2, "Fix the thing", "Details.", 1, None, "prior-sid"), # ticket flip: board_id, title, body, attempts, profile_id, session_id
        ("What now?", "do it", "please"),                       # answered question row
        ("site-page", "Desc", None, None, False, None, None, False),  # board row
    ])
    work = worker.claim_next(conn, worker_id=1, cluster_id=1, board_ids=None)
    assert work["resume"] == {
        "question": "What now?", "answer_value": "do it", "answer_notes": "please",
    }
    assert work["session_id"] == "prior-sid"  # kept, not re-minted


def test_claim_next_ignores_resume_flag_without_a_prior_session():
    """resume=True with no prior session_id (e.g. a very old ticket) falls
    back to an ordinary fresh run rather than passing --resume with nothing
    to resume."""
    conn = FakeConnWithBoardRow([
        None,                                                   # cap gate: no settings row -> unlimited
        (5, 9, True),
        (2, "Fix the thing", "Details.", 1, None, None),        # no prior session_id
        ("site-page", "Desc", None, None, False, None, None, False),  # board row (no question fetch)
    ])
    work = worker.claim_next(conn, worker_id=1, cluster_id=1, board_ids=None)
    assert work["resume"] is None
    assert work["session_id"] is not None  # a fresh one was minted


# ---------- record_session_dir: the cwd half of the resume command ----------

class RecordingCursor:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchone(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class RecordingConn:
    """Captures every statement run against it, across all cursors."""

    def __init__(self):
        self.calls = []

    def cursor(self):
        cur = RecordingCursor()
        cur.calls = self.calls  # share one log, in issue order
        return cur

    def transaction(self):
        class _T:
            def __enter__(self_):
                return self_

            def __exit__(self_, *a):
                return False

        return _T()


def _session_dir_writes(conn):
    return [(sql, params) for sql, params in conn.calls if "session_dir" in sql]


def test_record_session_dir_writes_the_directory_against_the_ticket():
    conn = RecordingConn()
    worker.record_session_dir(conn, 42, r"C:\repos\site-page")
    writes = _session_dir_writes(conn)
    assert len(writes) == 1, f"expected one session_dir write, got {conn.calls}"
    sql, params = writes[0]
    assert "UPDATE tickets" in sql
    assert params == (r"C:\repos\site-page", 42)


def test_record_session_dir_stringifies_a_path_object():
    """resolve_directory can hand back a Path (the auto-clone branch returns
    app_data_boards_dir()/<id>); psycopg would not adapt it to VARCHAR."""
    conn = RecordingConn()
    worker.record_session_dir(conn, 7, Path(r"C:\repos") / "board-1")
    _, params = _session_dir_writes(conn)[0]
    assert isinstance(params[0], str)
    assert params[0].endswith("board-1")


def test_record_session_dir_ignores_a_missing_directory():
    """A stub run, or a resolve that failed: nothing to record, and the write
    must not blank out a directory an earlier run already reported."""
    conn = RecordingConn()
    worker.record_session_dir(conn, 7, None)
    assert _session_dir_writes(conn) == []


def test_record_session_dir_survives_a_failing_write(capsys):
    """Best-effort by contract. This column only feeds a convenience command
    for a human; a blip writing it must not take down the agent run that is
    about to start, so the failure is reported and swallowed."""
    class BrokenConn:
        def transaction(self):
            raise RuntimeError("connection is closed")

    worker.record_session_dir(BrokenConn(), 7, "/repo")
    assert "connection is closed" in capsys.readouterr().out


# ---------- run_slot wires the resolved directory through ----------

class QuietExecutor:
    """A non-stub executor: run_slot must resolve a directory for it."""
    name = "quiet"

    def run(self, ticket, board=None, directory=None, session_id=None,
            progress_cb=None, should_kill=None, chat_source=None,
            chat_delivered=None, log_cb=None, profile=None, resume=None):
        return True, "done"


WORK = {"assignment_id": 1, "session_id": "sess-1", "board": {"id": 1, "name": "b"},
        "ticket": {"id": 9, "board_id": 1, "title": "t", "body": "",
                   "status": "doing", "attempts": 1}}


def _run_one_slot(monkeypatch, tmp_path, resolve):
    """Drive run_slot through exactly one claim, then stop it. Returns the
    (ticket_id, directory) pairs record_session_dir was called with."""
    monkeypatch.setattr(worker, "CONFIG_PATH", tmp_path / "cfg.json")
    claims = iter([WORK])
    recorded = []

    class C:
        closed = False

        def close(self):
            pass

    monkeypatch.setattr(worker.psycopg, "connect", lambda dsn, **kw: C())
    monkeypatch.setattr(worker, "claim_next", lambda *a, **k: next(claims, None))
    monkeypatch.setattr(worker, "finish_work", lambda *a, **k: "done")
    monkeypatch.setattr(worker, "set_slot_counts", lambda *a, **k: None)
    monkeypatch.setattr(worker, "heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(worker, "resolve_push", lambda *a, **k: (True, ""))
    monkeypatch.setattr(worker, "fetch_pending_chat", lambda *a, **k: [])
    monkeypatch.setattr(worker, "_claim_heartbeat_loop", lambda *a, **k: None)
    monkeypatch.setattr(worker, "resolve_directory", resolve)
    monkeypatch.setattr(worker, "record_session_dir",
                        lambda conn, tid, d: recorded.append((tid, d)))

    cfg = {"dsn": "x", "worker_id": 1, "cluster_id": 1, "name": "pc",
           "boards": {"1": str(tmp_path)}}
    args = worker.build_parser().parse_args(["--poll", "0.01"])
    stop = threading.Event()
    t = threading.Thread(target=worker.run_slot,
                         args=(cfg, args, QuietExecutor(), stop, 0))
    t.start()
    time.sleep(0.3)
    stop.set()
    t.join(timeout=5)
    return recorded


def test_run_slot_records_the_directory_it_resolved(monkeypatch, tmp_path):
    """Without this the board can only offer a bare `claude --resume <id>`,
    which resolves to nothing from the wrong working directory."""
    where = str(tmp_path / "site-page")
    recorded = _run_one_slot(monkeypatch, tmp_path,
                             lambda board, cfg: (where, None))
    assert recorded == [(9, where)]


def test_run_slot_records_nothing_when_the_directory_cannot_be_resolved(
        monkeypatch, tmp_path):
    """resolve_directory failed (no --set-path, no repo_url): the agent never
    ran, so there is no session directory to report."""
    recorded = _run_one_slot(monkeypatch, tmp_path,
                             lambda board, cfg: (None, "no folder configured"))
    assert recorded == []


def test_a_failed_session_dir_write_never_fails_the_ticket(monkeypatch, tmp_path):
    """Recording the directory is bookkeeping for a convenience button. It
    happens inside run_slot's broad `except Exception` — so if it were allowed
    to raise, a dropped connection would turn a perfectly good agent run into
    'Executor error' and throw the work away. Convenience must not be able to
    fail the thing it is a convenience for.
    """
    class BrokenConn:
        """No .transaction()/.cursor() — exactly what a real psycopg
        connection looks like once it has been closed underneath us."""
        closed = False

        def close(self):
            pass

    monkeypatch.setattr(worker, "CONFIG_PATH", tmp_path / "cfg.json")
    claims = iter([WORK])
    finished = {}

    def fake_finish_work(conn, wid, wname, item_id, ticket_id, ok, comment,
                         killed=False, commit_gate=None, pushed=False):
        finished["ok"], finished["comment"] = ok, comment
        return "done"

    monkeypatch.setattr(worker.psycopg, "connect", lambda dsn, **kw: BrokenConn())
    monkeypatch.setattr(worker, "claim_next", lambda *a, **k: next(claims, None))
    monkeypatch.setattr(worker, "finish_work", fake_finish_work)
    monkeypatch.setattr(worker, "set_slot_counts", lambda *a, **k: None)
    monkeypatch.setattr(worker, "heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(worker, "resolve_push", lambda *a, **k: (True, ""))
    monkeypatch.setattr(worker, "fetch_pending_chat", lambda *a, **k: [])
    monkeypatch.setattr(worker, "_claim_heartbeat_loop", lambda *a, **k: None)
    monkeypatch.setattr(worker, "resolve_directory",
                        lambda board, cfg: (str(tmp_path), None))

    cfg = {"dsn": "x", "worker_id": 1, "cluster_id": 1, "name": "pc",
           "boards": {"1": str(tmp_path)}}
    args = worker.build_parser().parse_args(["--poll", "0.01"])
    stop = threading.Event()
    t = threading.Thread(target=worker.run_slot,
                         args=(cfg, args, QuietExecutor(), stop, 0))
    t.start()
    time.sleep(0.3)
    stop.set()
    t.join(timeout=5)

    assert finished.get("ok") is True, (
        f"the run should still have succeeded, got {finished!r}")
    assert "Executor error" not in (finished.get("comment") or "")
