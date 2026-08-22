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


def fake_run(captured):
    def _run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0, stdout="done", stderr="")
    return _run


def test_executor_runs_in_the_board_directory(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(subprocess, "run", fake_run(captured))
    ok, out = worker.ClaudeExecutor().run(
        TICKET, board=BOARD, directory=str(tmp_path), session_id="sid-1")
    assert ok and out == "done"
    assert captured["kwargs"]["cwd"] == str(tmp_path)


def test_executor_passes_allowed_tools_and_session_id(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(subprocess, "run", fake_run(captured))
    worker.ClaudeExecutor().run(TICKET, board=BOARD,
                                directory=str(tmp_path), session_id="sid-1")
    cmd = captured["cmd"]
    assert "--allowedTools" in cmd
    assert cmd[cmd.index("--allowedTools") + 1] == worker.DEFAULT_ALLOWED_TOOLS
    assert "--session-id" in cmd
    assert cmd[cmd.index("--session-id") + 1] == "sid-1"


def test_executor_prompt_carries_project_context(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(subprocess, "run", fake_run(captured))
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
    monkeypatch.setattr(subprocess, "run", fake_run(captured))
    worker.ClaudeExecutor().run(TICKET, board=BOARD,
                                directory=str(tmp_path), session_id="s")
    assert captured["kwargs"].get("shell") in (None, False)
    prompt = captured["cmd"][captured["cmd"].index("-p") + 1]
    assert "\n" in prompt, "the prompt is multi-line; that is the whole point"


def test_resolves_the_cli_through_which(monkeypatch, tmp_path):
    """A bare 'claude' with shell=False misses the Windows .CMD/.EXE shim."""
    captured = {}
    monkeypatch.setattr(subprocess, "run", fake_run(captured))
    monkeypatch.setattr(worker.shutil, "which",
                        lambda name: r"C:\tools\claude.CMD")
    worker.ClaudeExecutor().run(TICKET, board=BOARD,
                                directory=str(tmp_path), session_id="s")
    assert captured["cmd"][0] == r"C:\tools\claude.CMD"


def test_missing_cli_is_reported_without_running(monkeypatch, tmp_path):
    called = {}
    monkeypatch.setattr(subprocess, "run",
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
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: called.setdefault("ran", True))
    ok, msg = worker.ClaudeExecutor().run(TICKET, board=BOARD,
                                          directory=None, session_id="s")
    assert ok is False
    assert "--set-path" in msg
    assert "ran" not in called


def test_executor_fails_clearly_when_the_directory_is_gone(monkeypatch, tmp_path):
    called = {}
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: called.setdefault("ran", True))
    ok, msg = worker.ClaudeExecutor().run(
        TICKET, board=BOARD, directory=str(tmp_path / "gone"), session_id="s")
    assert ok is False
    assert "no longer exists" in msg
    assert "ran" not in called


def test_custom_allowed_tools(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(subprocess, "run", fake_run(captured))
    worker.ClaudeExecutor(allowed_tools="Read,Grep").run(
        TICKET, board=BOARD, directory=str(tmp_path), session_id="s")
    cmd = captured["cmd"]
    assert cmd[cmd.index("--allowedTools") + 1] == "Read,Grep"


def test_stub_executor_still_takes_the_new_kwargs():
    ok, out = worker.StubExecutor().run(TICKET, board=None,
                                        directory=None, session_id=None)
    assert ok and "StubExecutor" in out
