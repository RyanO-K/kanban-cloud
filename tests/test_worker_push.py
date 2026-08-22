"""push_ticket_branch: the worker pushes a ticket's finished branch to origin
so the agent's "do not push - handled separately" promise (app/prompt.py) is
actually kept. Same faking approach as tests/test_worker_auto_clone.py - no
real git process runs in this suite.
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
    note = worker.push_ticket_branch(str(tmp_path), "5-my-ticket")
    subcommands = [c[0][1] for c in fake.calls]
    assert subcommands == ["rev-parse", "push"]
    push_cmd = fake.calls[1][0]
    assert push_cmd[2:] == ["origin", "5-my-ticket"]
    assert fake.calls[1][1].get("cwd") == str(tmp_path)
    assert "Pushed branch `5-my-ticket`" in note


def test_no_branch_means_nothing_to_push(tmp_path, monkeypatch):
    """The agent can finish successfully without committing (a no-op
    ticket) - that must not be reported as a push failure."""
    fake = FakeGit(outputs={"rev-parse": (1, "", "unknown revision")})
    monkeypatch.setattr(subprocess, "run", fake)
    note = worker.push_ticket_branch(str(tmp_path), "5-my-ticket")
    assert note is None
    subcommands = [c[0][1] for c in fake.calls]
    assert subcommands == ["rev-parse"]  # never attempts the push


def test_push_failure_is_reported_not_raised(tmp_path, monkeypatch):
    fake = FakeGit(outputs={"push": (1, "", "fatal: could not read Username")})
    monkeypatch.setattr(subprocess, "run", fake)
    note = worker.push_ticket_branch(str(tmp_path), "5-my-ticket")
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
        note = worker.push_ticket_branch("/repo", "5-my-ticket")
    finally:
        sp.run = orig_run
    assert "secret" not in note
    assert "Authentication failed" in note


def test_run_slot_appends_push_note_to_the_review_comment(monkeypatch):
    """End-to-end through run_slot: a successful real-executor run pushes
    the ticket's branch and folds the result into the comment finish_work
    records, without needing its own git plumbing."""
    ticket = {"id": 5, "board_id": 6, "title": "My Ticket", "attempts": 1}

    class FakeExecutor:
        def run(self, ticket, board=None, directory=None, session_id=None,
                progress_cb=None, should_kill=None,
                chat_source=None, chat_delivered=None, log_cb=None, profile=None):
            return True, "Implemented the thing."

    monkeypatch.setattr(worker, "resolve_directory", lambda board, cfg: ("/repo", None))
    pushed = {}

    def fake_push(directory, branch):
        pushed["args"] = (directory, branch)
        return "\n\n(Pushed branch `5-my-ticket` to origin.)"

    monkeypatch.setattr(worker, "push_ticket_branch", fake_push)

    calls = {"claim": 0}

    def fake_claim_next(conn, worker_id, cluster_id, boards):
        calls["claim"] += 1
        if calls["claim"] > 1:
            return None
        return {
            "assignment_id": 1, "session_id": "sid",
            "board": {"id": 6, "use_worktrees": True, "repo_url": "x", "auto_push": True},
            "ticket": ticket,
        }

    finished = {}

    def fake_finish_work(conn, worker_id, worker_name, item_id, ticket_id, ok, comment,
                         killed=False, commit_gate=None):
        finished["comment"] = comment
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

    assert pushed["args"] == ("/repo", ticket_branch_name(ticket))
    assert finished["comment"] == "Implemented the thing.\n\n(Pushed branch `5-my-ticket` to origin.)"
