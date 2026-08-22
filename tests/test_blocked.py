"""Phase 4: blocked status + agent questions, worker-side.

Covers the three properties the ticket calls out explicitly:
  - a blocked ticket is never claimed (its work_queue row leaves 'claimed'
    for 'blocked', never back to 'queued', so CLAIM_SQL's own
    `wq.status='queued'` predicate excludes it)
  - answering requeues it exactly once (delegation.enqueue_ticket idempotency
    is covered in test_claim.py; here we cover the worker's half: raise_question
    only fires once per escalation, guarded the same way finish_work is)
  - a blocked ticket's slot is genuinely freed for other work (the same slot
    goes on to claim and finish a second ticket right after)
"""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import worker  # noqa: E402
from app.prompt import QUESTION_MARKER, parse_question  # noqa: E402


# ---------- parse_question ----------

def test_parse_question_extracts_marker_json():
    text = f'{QUESTION_MARKER} {{"question": "Which auth library?"}}'
    q = parse_question(text)
    assert q == {"question": "Which auth library?", "type": "input",
                 "format": None, "options": None, "multi": False}


def test_parse_question_reads_choice_fields():
    text = (f'{QUESTION_MARKER} {{"question": "Which one?", "type": "choice", '
            '"format": "radio", "options": ["a", "b"], "multi": true}')
    q = parse_question(text)
    assert q["type"] == "choice"
    assert q["format"] == "radio"
    assert q["options"] == ["a", "b"]
    assert q["multi"] is True


def test_parse_question_returns_none_without_marker():
    assert parse_question("Done. Renamed the footer component.") is None


def test_parse_question_returns_none_on_empty_text():
    assert parse_question("") is None
    assert parse_question(None) is None


def test_parse_question_returns_none_on_malformed_json():
    assert parse_question(f"{QUESTION_MARKER} not json") is None


def test_parse_question_returns_none_on_blank_question():
    assert parse_question(f'{QUESTION_MARKER} {{"question": "   "}}') is None


def test_parse_question_ignores_an_unknown_type():
    q = parse_question(f'{QUESTION_MARKER} {{"question": "X?", "type": "essay"}}')
    assert q["type"] == "input"


def test_parse_question_uses_the_last_marker_occurrence():
    """The escalation guidance in the prompt itself contains the marker as an
    example; a real escalation must win over any incidental earlier mention."""
    text = (f'Earlier I considered using {QUESTION_MARKER} {{"question": "old"}} '
            f'but decided to actually ask: {QUESTION_MARKER} {{"question": "real one"}}')
    assert parse_question(text)["question"] == "real one"


# ---------- raise_question SQL shape ----------

class FakeCursor:
    def __init__(self, rowcount):
        self.calls = []
        self.rowcount = rowcount

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchone(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, rowcount=1):
        self.rowcount = rowcount
        self.cursors = []

    def cursor(self):
        cur = FakeCursor(self.rowcount)
        self.cursors.append(cur)
        return cur

    def transaction(self):
        class _T:
            def __enter__(self_):
                return self_

            def __exit__(self_, *a):
                return False

        return _T()


QUESTION = {"question": "Which lib?", "type": "input", "format": None,
           "options": None, "multi": False}


def test_raise_question_parks_the_work_queue_row_blocked_not_queued():
    """The row must leave 'claimed' for something CLAIM_SQL's own
    `wq.status='queued'` predicate can never match again on its own."""
    conn = FakeConn(rowcount=1)
    worker.raise_question(conn, 1, "pc", 5, 9, QUESTION, "asking")
    sql, params = conn.cursors[0].calls[0]
    assert "status='blocked'" in sql
    assert "status='claimed'" in sql  # guard: only touches its own live claim
    assert params[1:] == (5, 1)


def test_raise_question_inserts_the_question_row():
    conn = FakeConn(rowcount=1)
    worker.raise_question(conn, 1, "pc", 5, 9, QUESTION, "asking")
    sqls = [sql for sql, _ in conn.cursors[0].calls]
    assert any("INSERT INTO ticket_questions" in s for s in sqls)


def test_raise_question_sets_the_ticket_blocked_and_clears_assignment():
    conn = FakeConn(rowcount=1)
    worker.raise_question(conn, 1, "pc", 5, 9, QUESTION, "asking")
    ticket_update = next(sql for sql, _ in conn.cursors[0].calls
                         if sql.startswith("UPDATE tickets"))
    assert "status='blocked'" in ticket_update
    assert "assigned_worker=NULL" in ticket_update


def test_raise_question_returns_blocked_on_success():
    conn = FakeConn(rowcount=1)
    assert worker.raise_question(conn, 1, "pc", 5, 9, QUESTION, "asking") == "blocked"


def test_raise_question_is_a_noop_when_the_claim_was_superseded():
    """Same guard finish_work uses: if a human already dragged the ticket back
    to ready while the agent was mid-run, the late escalation must not stomp
    the fresh claim — no question row, no ticket flip."""
    conn = FakeConn(rowcount=0)
    status = worker.raise_question(conn, 1, "pc", 5, 9, QUESTION, "asking")
    assert status == "superseded"
    sqls = [sql for sql, _ in conn.cursors[0].calls]
    assert not any("INSERT INTO ticket_questions" in s for s in sqls)
    assert not any(s.startswith("UPDATE tickets") for s in sqls)


# ---------- run_slot integration ----------

def _fake_connect(collect=None):
    def _connect(dsn, **kw):
        class C:
            closed = False

            def close(self):
                pass
        c = C()
        if collect is not None:
            collect.append(c)
        return c
    return _connect


def test_run_slot_raises_a_question_instead_of_finishing(monkeypatch, tmp_path):
    """A clean run whose entire reply is the marker must go through
    raise_question, not finish_work."""
    monkeypatch.setattr(worker, "CONFIG_PATH", tmp_path / "cfg.json")

    def fake_claim(conn, wid, cid, boards):
        return {"assignment_id": 1, "session_id": "s", "board": {"id": 1, "name": "b"},
                "ticket": {"id": 9, "board_id": 1, "title": "t", "body": "", "attempts": 1}}

    class AskingExecutor:
        name = "asking"

        def run(self, ticket, board=None, directory=None, session_id=None,
                progress_cb=None, should_kill=None):
            return True, f'{QUESTION_MARKER} {{"question": "Which lib?"}}'

    calls = {"blocked": [], "finished": []}
    monkeypatch.setattr(worker, "raise_question",
                        lambda *a, **k: calls["blocked"].append(a) or "blocked")
    monkeypatch.setattr(worker, "finish_work",
                        lambda *a, **k: calls["finished"].append(a) or "review")
    monkeypatch.setattr(worker, "resolve_directory", lambda board, cfg: ("/tmp/x", None))
    monkeypatch.setattr(worker.psycopg, "connect", _fake_connect())
    monkeypatch.setattr(worker, "claim_next", fake_claim)

    cfg = {"dsn": "x", "worker_id": 1, "cluster_id": 1, "name": "pc"}
    args = worker.build_parser().parse_args(["--poll", "0.01", "--once"])
    worker.run_slot(cfg, args, AskingExecutor(), threading.Event(), 0)

    assert len(calls["blocked"]) == 1
    assert calls["finished"] == []
    assert calls["blocked"][0][4] == 9  # ticket id


def test_run_slot_treats_a_normal_completion_as_before(monkeypatch, tmp_path):
    """No marker -> unchanged finish_work path (regression guard)."""
    monkeypatch.setattr(worker, "CONFIG_PATH", tmp_path / "cfg.json")

    def fake_claim(conn, wid, cid, boards):
        return {"assignment_id": 1, "session_id": "s", "board": {"id": 1, "name": "b"},
                "ticket": {"id": 9, "board_id": 1, "title": "t", "body": "", "attempts": 1}}

    class DoneExecutor:
        name = "done"

        def run(self, ticket, board=None, directory=None, session_id=None,
                progress_cb=None, should_kill=None):
            return True, "All set, renamed the component."

    calls = {"blocked": [], "finished": []}
    monkeypatch.setattr(worker, "raise_question",
                        lambda *a, **k: calls["blocked"].append(a) or "blocked")
    monkeypatch.setattr(worker, "finish_work",
                        lambda *a, **k: calls["finished"].append(a) or "review")
    monkeypatch.setattr(worker, "resolve_directory", lambda board, cfg: ("/tmp/x", None))
    monkeypatch.setattr(worker.psycopg, "connect", _fake_connect())
    monkeypatch.setattr(worker, "claim_next", fake_claim)

    cfg = {"dsn": "x", "worker_id": 1, "cluster_id": 1, "name": "pc"}
    args = worker.build_parser().parse_args(["--poll", "0.01", "--once"])
    worker.run_slot(cfg, args, DoneExecutor(), threading.Event(), 0)

    assert calls["blocked"] == []
    assert len(calls["finished"]) == 1


def test_slot_claims_again_after_a_question_is_raised(monkeypatch, tmp_path):
    """The property the ticket calls "genuinely freed": the exact same slot
    loop must go on to claim and finish a second ticket right after the first
    one blocks, proving the claim it came from does not wedge the slot."""
    monkeypatch.setattr(worker, "CONFIG_PATH", tmp_path / "cfg.json")
    ticket_ids = iter([1, 2])

    def fake_claim(conn, wid, cid, boards):
        try:
            n = next(ticket_ids)
        except StopIteration:
            return None
        return {"assignment_id": n, "session_id": "s", "board": {"id": 1, "name": "b"},
                "ticket": {"id": n, "board_id": 1, "title": "t", "body": "", "attempts": 1}}

    class QuestionThenDone:
        name = "q"

        def run(self, ticket, board=None, directory=None, session_id=None,
                progress_cb=None, should_kill=None):
            if ticket["id"] == 1:
                return True, f'{QUESTION_MARKER} {{"question": "Which lib?"}}'
            return True, "done"

    blocked_calls, finished_calls = [], []
    monkeypatch.setattr(worker, "raise_question",
                        lambda *a, **k: blocked_calls.append(a[4]) or "blocked")
    monkeypatch.setattr(worker, "finish_work",
                        lambda *a, **k: finished_calls.append(a[4]) or "review")
    monkeypatch.setattr(worker, "resolve_directory", lambda board, cfg: ("/tmp/x", None))
    monkeypatch.setattr(worker.psycopg, "connect", _fake_connect())
    monkeypatch.setattr(worker, "claim_next", fake_claim)

    cfg = {"dsn": "x", "worker_id": 1, "cluster_id": 1, "name": "pc"}
    args = worker.build_parser().parse_args(["--poll", "0.01"])
    stop = threading.Event()
    t = threading.Thread(target=worker.run_slot, args=(cfg, args, QuestionThenDone(), stop, 0))
    t.start()
    time.sleep(0.3)
    stop.set()
    t.join(timeout=5)

    assert blocked_calls == [1]
    assert finished_calls == [2]
