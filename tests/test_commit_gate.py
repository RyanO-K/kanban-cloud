"""Phase 6: commit gate + auto-commit/push from the worker.

Covers the two properties the ticket calls out explicitly:
  - a gate reporting unmet requirements does not push a branch (worker.py
    treats "not met" and "no gate reported at all" the same way — see
    app/prompt.parse_commit_gate)
  - the gate verdict round-trips: worker.finish_work writes it to
    tickets.commit_gate, and app/main.py's ticket_json reads it back out
    (see tests/test_board_settings.py for the API-level round trip)

Also covers auto_push being opt-in per board: even a fully-met gate does not
push unless the board has turned auto_push on.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import worker  # noqa: E402
from app.prompt import COMMIT_GATE_MARKER  # noqa: E402


# ---------- finish_work: commit_gate SQL shape ----------
# Same fake cursor/connection convention as tests/test_blocked.py, since the
# real SQL is Postgres-only (`%s` placeholders) and worker.py's own tests
# never run against a live database.

class FakeCursor:
    def __init__(self, rowcount):
        self.calls = []
        self.rowcount = rowcount

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchone(self):
        return (1, 6)  # (attempts, board_id) — enough for the requeue branch

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


def test_finish_work_writes_the_commit_gate_to_the_ticket():
    conn = FakeConn(rowcount=1)
    gate = {"requirements_met": True, "summary": "All tests passed."}
    worker.finish_work(conn, 1, "pc", 5, 9, True, "Done.", commit_gate=gate)
    params = next(p for sql, p in conn.cursors[0].calls
                 if sql.startswith("UPDATE tickets SET commit_gate"))
    assert params == ('{"requirements_met": true, "summary": "All tests passed."}', 9)


def test_finish_work_skips_the_commit_gate_update_when_none():
    """No gate was ever reported (board has no commit_requirements, or the
    agent's reply had no marker) — must not write a bogus NULL-shaped row."""
    conn = FakeConn(rowcount=1)
    worker.finish_work(conn, 1, "pc", 5, 9, True, "Done.")
    sqls = [sql for sql, _ in conn.cursors[0].calls]
    assert not any(s.startswith("UPDATE tickets SET commit_gate") for s in sqls)


def test_finish_work_skips_commit_gate_on_a_superseded_claim():
    """Superseded claims write nothing at all — same guard as every other
    finish_work field."""
    conn = FakeConn(rowcount=0)
    gate = {"requirements_met": False, "summary": "nope"}
    worker.finish_work(conn, 1, "pc", 5, 9, True, "Done.", commit_gate=gate)
    sqls = [sql for sql, _ in conn.cursors[0].calls]
    assert not any(s.startswith("UPDATE tickets SET commit_gate") for s in sqls)


# ---------- run_slot: push gating ----------

BOARD_WITH_GATE = {"id": 6, "repo_url": "x", "commit_requirements": "All tests must pass.",
                   "auto_push": True}


def _run_slot_once(monkeypatch, tmp_path, board, comment, push_calls, finished):
    ticket = {"id": 5, "board_id": 6, "title": "My Ticket", "attempts": 1}

    class FakeExecutor:
        def run(self, ticket, board=None, directory=None, session_id=None,
                progress_cb=None, should_kill=None,
                chat_source=None, chat_delivered=None):
            return True, comment

    monkeypatch.setattr(worker, "resolve_directory", lambda b, cfg: ("/repo", None))

    def fake_push(directory, branch):
        push_calls.append((directory, branch))
        return "\n\n(Pushed branch `5-my-ticket` to origin.)"

    monkeypatch.setattr(worker, "push_ticket_branch", fake_push)

    calls = {"claim": 0}

    def fake_claim_next(conn, worker_id, cluster_id, boards):
        calls["claim"] += 1
        if calls["claim"] > 1:
            return None
        return {"assignment_id": 1, "session_id": "sid", "board": board, "ticket": ticket}

    def fake_finish_work(conn, worker_id, worker_name, item_id, ticket_id, ok, comment,
                         killed=False, commit_gate=None):
        finished["comment"] = comment
        finished["commit_gate"] = commit_gate
        return "review"

    class FakeConn:
        closed = False

        def close(self):
            pass

    monkeypatch.setattr(worker, "claim_next", fake_claim_next)
    monkeypatch.setattr(worker, "finish_work", fake_finish_work)
    monkeypatch.setattr(worker.psycopg, "connect", lambda *a, **k: FakeConn())

    class Args:
        once = True

    stop_event = worker.threading.Event()
    worker.run_slot({"dsn": "x", "worker_id": 6, "cluster_id": 3, "name": "HOME"},
                     Args(), FakeExecutor(), stop_event, 1)


def test_gate_reporting_unmet_requirements_does_not_push(monkeypatch, tmp_path):
    comment = (f'Could not get the suite green.\n\n{COMMIT_GATE_MARKER} '
              '{"requirements_met": false, "summary": "Two tests still fail."}')
    push_calls, finished = [], {}
    _run_slot_once(monkeypatch, tmp_path, BOARD_WITH_GATE, comment, push_calls, finished)
    assert push_calls == []
    assert finished["commit_gate"] == {"requirements_met": False,
                                       "summary": "Two tests still fail."}
    assert "Not pushed" in finished["comment"]


def test_gate_reporting_met_requirements_pushes(monkeypatch, tmp_path):
    comment = (f'All green.\n\n{COMMIT_GATE_MARKER} '
              '{"requirements_met": true, "summary": "Ran the suite."}')
    push_calls, finished = [], {}
    _run_slot_once(monkeypatch, tmp_path, BOARD_WITH_GATE, comment, push_calls, finished)
    assert push_calls == [("/repo", "5-my-ticket")]
    assert finished["commit_gate"] == {"requirements_met": True, "summary": "Ran the suite."}
    assert "Pushed branch" in finished["comment"]


def test_missing_gate_is_treated_as_unmet_when_requirements_exist(monkeypatch, tmp_path):
    """The agent finished without reporting a verdict at all — must not push
    on the strength of an unverifiable claim."""
    comment = "All done, looks good."
    push_calls, finished = [], {}
    _run_slot_once(monkeypatch, tmp_path, BOARD_WITH_GATE, comment, push_calls, finished)
    assert push_calls == []
    assert finished["commit_gate"] is None


def test_auto_push_off_never_pushes_even_with_a_met_gate(monkeypatch, tmp_path):
    """auto_push is opt-in per board — a met gate alone is not enough."""
    board = {**BOARD_WITH_GATE, "auto_push": False}
    comment = (f'All green.\n\n{COMMIT_GATE_MARKER} '
              '{"requirements_met": true, "summary": "Ran the suite."}')
    push_calls, finished = [], {}
    _run_slot_once(monkeypatch, tmp_path, board, comment, push_calls, finished)
    assert push_calls == []
    assert finished["commit_gate"] == {"requirements_met": True, "summary": "Ran the suite."}


def test_no_commit_requirements_pushes_without_needing_a_gate(monkeypatch, tmp_path):
    """A board with nothing to gate on pushes normally once auto_push is on,
    even though the agent never reported a verdict."""
    board = {"id": 6, "repo_url": "x", "commit_requirements": None, "auto_push": True}
    comment = "All done, looks good."
    push_calls, finished = [], {}
    _run_slot_once(monkeypatch, tmp_path, board, comment, push_calls, finished)
    assert push_calls == [("/repo", "5-my-ticket")]
    assert finished["commit_gate"] is None
