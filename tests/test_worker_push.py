"""push_ticket_branch / resolve_push: the worker pushes a ticket's finished
branch to origin so the agent's "do not push - handled separately" promise
(app/prompt.py) is actually kept - and, since the five-column rework, reports
whether it got there, because that is what separates a Done ticket from one
parked in Blocked. Same faking approach as tests/test_worker_auto_clone.py -
no real git process runs in this suite.
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import worker  # noqa: E402
from app.prompt import ticket_branch_name  # noqa: E402


class FakeGit:
    def __init__(self, outputs=None):
        self.outputs = outputs or {}
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        assert cmd[0] == "git"
        returncode, stdout, stderr = self.outputs.get(cmd[1], (0, "", ""))
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)


def test_pushes_the_branch_the_agent_committed(tmp_path, monkeypatch):
    fake = FakeGit()
    monkeypatch.setattr(subprocess, "run", fake)
    pushed, note = worker.push_ticket_branch(str(tmp_path), "5-my-ticket")
    subcommands = [c[0][1] for c in fake.calls]
    assert subcommands == ["rev-parse", "push"]
    push_cmd = fake.calls[1][0]
    assert push_cmd[2:] == ["origin", "5-my-ticket"]
    assert fake.calls[1][1].get("cwd") == str(tmp_path)
    assert pushed is True
    assert "Pushed branch `5-my-ticket`" in note


def test_no_branch_means_nothing_was_pushed(tmp_path, monkeypatch):
    """The agent can finish successfully without committing (a no-op ticket).
    That is not a push failure - but it is not done either, so the boolean
    says no and the note says why."""
    fake = FakeGit(outputs={"rev-parse": (1, "", "unknown revision")})
    monkeypatch.setattr(subprocess, "run", fake)
    pushed, note = worker.push_ticket_branch(str(tmp_path), "5-my-ticket")
    assert pushed is False
    assert "no branch `5-my-ticket`" in note
    subcommands = [c[0][1] for c in fake.calls]
    assert subcommands == ["rev-parse"]  # never attempts the push


def test_push_failure_is_reported_not_raised(tmp_path, monkeypatch):
    fake = FakeGit(outputs={"push": (1, "", "fatal: could not read Username")})
    monkeypatch.setattr(subprocess, "run", fake)
    pushed, note = worker.push_ticket_branch(str(tmp_path), "5-my-ticket")
    assert pushed is False
    assert "Could not push branch `5-my-ticket`" in note
    assert "could not read Username" in note


def test_push_failure_redacts_credentials():
    """Same redaction contract as resolve_directory's errors - this note
    lands verbatim in a ticket comment visible to the whole cluster."""
    import subprocess as sp
    fake = FakeGit(outputs={"push": (
        1, "", "fatal: Authentication failed for "
        "'https://user:secret@github.com/org/repo.git/'",
    )})
    orig_run = sp.run
    sp.run = fake
    try:
        _pushed, note = worker.push_ticket_branch("/repo", "5-my-ticket")
    finally:
        sp.run = orig_run
    assert "secret" not in note
    assert "Authentication failed" in note


# ---------- resolve_push: the vetoes that keep a ticket out of Done ----------

TICKET = {"id": 5, "board_id": 6, "title": "My Ticket", "attempts": 1}


def _never(*_a, **_k):
    raise AssertionError("push_ticket_branch must not run: the push was vetoed")


def test_resolve_push_pushes_when_nothing_vetoes_it(monkeypatch):
    calls = []
    monkeypatch.setattr(worker, "push_ticket_branch",
                        lambda d, b: calls.append((d, b)) or (True, "\n\n(Pushed.)"))
    pushed, note = worker.resolve_push({"auto_push": True}, TICKET, "/repo", None)
    assert calls == [("/repo", ticket_branch_name(TICKET))]
    assert (pushed, note) == (True, "\n\n(Pushed.)")


def test_auto_push_off_never_reaches_git_and_says_so(monkeypatch):
    """The veto has to explain itself now: the ticket is about to sit in
    Blocked, and 'why' is the only thing that tells a human what to do."""
    monkeypatch.setattr(worker, "push_ticket_branch", _never)
    pushed, note = worker.resolve_push({"auto_push": False}, TICKET, "/repo", None)
    assert pushed is False
    assert "auto-push is off" in note


def test_unmet_commit_gate_vetoes_the_push(monkeypatch):
    monkeypatch.setattr(worker, "push_ticket_branch", _never)
    board = {"auto_push": True, "commit_requirements": "All tests pass."}
    gate = {"requirements_met": False, "summary": "Two tests still fail."}
    pushed, note = worker.resolve_push(board, TICKET, "/repo", gate)
    assert pushed is False
    assert "commit gate" in note


def test_missing_gate_is_treated_as_unmet(monkeypatch):
    monkeypatch.setattr(worker, "push_ticket_branch", _never)
    board = {"auto_push": True, "commit_requirements": "All tests pass."}
    pushed, _note = worker.resolve_push(board, TICKET, "/repo", None)
    assert pushed is False


def test_met_gate_lets_the_push_through(monkeypatch):
    monkeypatch.setattr(worker, "push_ticket_branch", lambda d, b: (True, None))
    board = {"auto_push": True, "commit_requirements": "All tests pass."}
    gate = {"requirements_met": True, "summary": "Ran the suite."}
    assert worker.resolve_push(board, TICKET, "/repo", gate)[0] is True


# ---------- end to end through run_slot ----------

def _run_slot_once(monkeypatch, board, push_result, finished):
    """One claim -> one successful executor run -> finish_work, with git and
    the database faked out. Returns nothing; `finished` collects the kwargs
    finish_work was called with."""
    class FakeExecutor:
        def run(self, ticket, board=None, directory=None, session_id=None,
                progress_cb=None, should_kill=None,
                chat_source=None, chat_delivered=None, log_cb=None, profile=None,
                resume=None):
            return True, "Implemented the thing."

    monkeypatch.setattr(worker, "resolve_directory", lambda b, cfg: ("/repo", None))
    monkeypatch.setattr(worker, "push_ticket_branch", lambda d, b: push_result)

    calls = {"claim": 0}

    def fake_claim_next(conn, worker_id, cluster_id, boards):
        calls["claim"] += 1
        if calls["claim"] > 1:
            return None
        return {"assignment_id": 1, "session_id": "sid", "board": board,
                "ticket": TICKET}

    def fake_finish_work(conn, worker_id, worker_name, item_id, ticket_id, ok, comment,
                         killed=False, commit_gate=None, pushed=False):
        finished["comment"] = comment
        finished["pushed"] = pushed
        return "done" if pushed else "blocked"

    class FakeConn:
        closed = False

        def close(self):
            pass

    monkeypatch.setattr(worker, "claim_next", fake_claim_next)
    monkeypatch.setattr(worker, "finish_work", fake_finish_work)
    monkeypatch.setattr(worker.psycopg, "connect", lambda *a, **k: FakeConn())

    class Args:
        once = True

    worker.run_slot({"dsn": "x", "worker_id": 6, "cluster_id": 3, "name": "HOME"},
                    Args(), FakeExecutor(), worker.threading.Event(), 1)


def test_run_slot_folds_the_push_note_into_the_comment(monkeypatch):
    """End-to-end through run_slot: a successful real-executor run pushes the
    ticket's branch and folds the result into the comment finish_work records,
    without needing its own git plumbing."""
    finished = {}
    _run_slot_once(monkeypatch, {"id": 6, "use_worktrees": True, "repo_url": "x",
                                 "auto_push": True},
                   (True, "\n\n(Pushed branch `5-my-ticket` to origin.)"), finished)
    assert finished["comment"] == (
        "Implemented the thing.\n\n(Pushed branch `5-my-ticket` to origin.)")


def test_run_slot_tells_finish_work_the_work_landed(monkeypatch):
    """The boolean, not the note, is what makes the ticket done."""
    finished = {}
    _run_slot_once(monkeypatch, {"id": 6, "repo_url": "x", "auto_push": True},
                   (True, "\n\n(Pushed.)"), finished)
    assert finished["pushed"] is True


def test_run_slot_reports_an_unpushed_run_as_not_landed(monkeypatch):
    """A failed push must not quietly become a done ticket - the work is
    still only on this PC."""
    finished = {}
    _run_slot_once(monkeypatch, {"id": 6, "repo_url": "x", "auto_push": True},
                   (False, "\n\n(Could not push branch `5-my-ticket`: nope)"), finished)
    assert finished["pushed"] is False
    assert "Could not push" in finished["comment"]
