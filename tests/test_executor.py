"""The executor's contract with the Claude CLI: right folder, right permissions.

The prompt is multi-line, which is exactly what `shell=True` destroys on
Windows — see test_never_uses_shell_true.
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import worker  # noqa: E402

TICKET = {"id": 3, "title": "Do it", "body": "Details.", "attempts": 1}
BOARD = {"name": "site-page", "description": "The site.", "out_of_scope": None,
         "commit_requirements": None, "use_worktrees": False}


class FakePopen:
    """Stands in for subprocess.Popen. `alive=True` simulates a child still
    running: communicate(timeout=...) raises TimeoutExpired until terminate()/
    kill() is called (or alive is flipped by hand); `alive=False` simulates
    one that has already exited, so communicate() returns immediately."""

    def __init__(self, cmd, **kwargs):
        self.cmd = cmd
        self.kwargs = kwargs
        self.pid = 4242
        self.returncode = 0
        self.stdout_data = "done"
        self.stderr_data = ""
        self.alive = True
        self.terminated = False
        self.killed = False

    def communicate(self, timeout=None):
        if self.alive and timeout is not None:
            raise subprocess.TimeoutExpired(self.cmd, timeout)
        self.alive = False
        return self.stdout_data, self.stderr_data

    def wait(self, timeout=None):
        self.alive = False
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.alive = False

    def kill(self):
        self.killed = True
        self.alive = False


def fake_popen(captured, stdout="done", stderr="", returncode=0):
    """A Popen replacement whose process is already finished by the time
    anyone calls communicate() — the common case for these tests."""
    def _popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        p = FakePopen(cmd, **kwargs)
        p.alive = False
        p.stdout_data, p.stderr_data, p.returncode = stdout, stderr, returncode
        return p
    return _popen


def test_executor_runs_in_the_board_directory(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(subprocess, "Popen", fake_popen(captured))
    ok, out = worker.ClaudeExecutor().run(
        TICKET, board=BOARD, directory=str(tmp_path), session_id="sid-1")
    assert ok and out == "done"
    assert captured["kwargs"]["cwd"] == str(tmp_path)


def test_executor_passes_allowed_tools_and_session_id(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(subprocess, "Popen", fake_popen(captured))
    worker.ClaudeExecutor().run(TICKET, board=BOARD,
                                directory=str(tmp_path), session_id="sid-1")
    cmd = captured["cmd"]
    assert "--allowedTools" in cmd
    assert cmd[cmd.index("--allowedTools") + 1] == worker.DEFAULT_ALLOWED_TOOLS
    assert "--session-id" in cmd
    assert cmd[cmd.index("--session-id") + 1] == "sid-1"


def test_executor_prompt_carries_project_context(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(subprocess, "Popen", fake_popen(captured))
    worker.ClaudeExecutor().run(TICKET, board=BOARD,
                                directory=str(tmp_path), session_id="s")
    prompt = captured["cmd"][captured["cmd"].index("-p") + 1]
    assert "The site." in prompt
    assert str(tmp_path) in prompt


def test_never_uses_shell_true(monkeypatch, tmp_path):
    """Regression: on Windows `shell=True` re-parses the argument list through
    cmd.exe, which truncates the multi-line prompt at its first newline. The
    agent then received the title and none of the ticket body. The executable
    is resolved with shutil.which instead, which finds the .CMD shim without
    needing a shell."""
    captured = {}
    monkeypatch.setattr(subprocess, "Popen", fake_popen(captured))
    worker.ClaudeExecutor().run(TICKET, board=BOARD,
                                directory=str(tmp_path), session_id="s")
    assert captured["kwargs"].get("shell") in (None, False)
    prompt = captured["cmd"][captured["cmd"].index("-p") + 1]
    assert "\n" in prompt, "the prompt is multi-line; that is the whole point"


def test_resolves_the_cli_through_which(monkeypatch, tmp_path):
    """A bare 'claude' with shell=False misses the Windows .CMD/.EXE shim."""
    captured = {}
    monkeypatch.setattr(subprocess, "Popen", fake_popen(captured))
    monkeypatch.setattr(worker.shutil, "which",
                        lambda name: r"C:\tools\claude.CMD")
    worker.ClaudeExecutor().run(TICKET, board=BOARD,
                                directory=str(tmp_path), session_id="s")
    assert captured["cmd"][0] == r"C:\tools\claude.CMD"


def test_missing_cli_is_reported_without_running(monkeypatch, tmp_path):
    called = {}
    monkeypatch.setattr(subprocess, "Popen",
                        lambda *a, **k: called.setdefault("ran", True))
    monkeypatch.setattr(worker.shutil, "which", lambda name: None)
    ok, msg = worker.ClaudeExecutor().run(TICKET, board=BOARD,
                                          directory=str(tmp_path), session_id="s")
    assert ok is False
    assert "not found" in msg
    assert "ran" not in called


def test_executor_refuses_to_run_without_a_directory(monkeypatch):
    """Better to fail the attempt than run an agent in a random folder."""
    called = {}
    monkeypatch.setattr(subprocess, "Popen",
                        lambda *a, **k: called.setdefault("ran", True))
    ok, msg = worker.ClaudeExecutor().run(TICKET, board=BOARD,
                                          directory=None, session_id="s")
    assert ok is False
    assert "--set-path" in msg
    assert "ran" not in called


def test_executor_fails_clearly_when_the_directory_is_gone(monkeypatch, tmp_path):
    called = {}
    monkeypatch.setattr(subprocess, "Popen",
                        lambda *a, **k: called.setdefault("ran", True))
    ok, msg = worker.ClaudeExecutor().run(
        TICKET, board=BOARD, directory=str(tmp_path / "gone"), session_id="s")
    assert ok is False
    assert "no longer exists" in msg
    assert "ran" not in called


def test_custom_allowed_tools(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(subprocess, "Popen", fake_popen(captured))
    worker.ClaudeExecutor(allowed_tools="Read,Grep").run(
        TICKET, board=BOARD, directory=str(tmp_path), session_id="s")
    cmd = captured["cmd"]
    assert cmd[cmd.index("--allowedTools") + 1] == "Read,Grep"


def test_stub_executor_still_takes_the_new_kwargs():
    ok, out = worker.StubExecutor().run(TICKET, board=None,
                                        directory=None, session_id=None,
                                        should_kill=lambda: False)
    assert ok and "StubExecutor" in out


# ---------- kill support (worker.py's Popen migration) ----------

def test_kill_flag_terminates_the_child_and_reports_the_distinct_status(monkeypatch, tmp_path):
    """A should_kill() that fires mid-run must terminate the process tree and
    signal the distinct 'killed' outcome (KilledByRequest), not a plain
    failure — run_slot maps that to work_queue/tickets' 'killed' status,
    which finish_work does not treat as a genuine failure."""
    terminated = []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: FakePopen(cmd, **kw))
    monkeypatch.setattr(worker, "KILL_POLL_SECONDS", 0.01)
    monkeypatch.setattr(worker, "_terminate_process_tree",
                        lambda proc: terminated.append(proc))

    try:
        worker.ClaudeExecutor().run(TICKET, board=BOARD, directory=str(tmp_path),
                                    session_id="s", should_kill=lambda: True)
        assert False, "expected KilledByRequest"
    except worker.KilledByRequest:
        pass
    assert len(terminated) == 1


def test_should_kill_ignored_once_the_process_already_exited(monkeypatch, tmp_path):
    """A kill_requested flag flipped after the CLI already finished must not
    turn a successful attempt into a spurious failure — should_kill() is only
    ever consulted while still waiting on the child."""
    calls = {"n": 0}

    def should_kill():
        calls["n"] += 1
        return True  # would kill it -- if the loop ever asked

    monkeypatch.setattr(subprocess, "Popen", fake_popen({}))
    ok, out = worker.ClaudeExecutor().run(TICKET, board=BOARD, directory=str(tmp_path),
                                          session_id="s", should_kill=should_kill)
    assert ok and out == "done"
    assert calls["n"] == 0


def test_no_should_kill_defaults_to_never_killing(monkeypatch, tmp_path):
    """should_kill is optional; omitting it must not crash the poll loop."""
    monkeypatch.setattr(subprocess, "Popen", fake_popen({}))
    ok, out = worker.ClaudeExecutor().run(TICKET, board=BOARD,
                                          directory=str(tmp_path), session_id="s")
    assert ok and out == "done"
