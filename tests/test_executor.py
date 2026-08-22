"""The executor's contract with the Claude CLI: right folder, right
permissions, incremental streaming instead of an all-or-nothing block on
process exit, and a live should_kill()/timeout poll that can interrupt a
run in progress.

The prompt is multi-line, which is exactly what `shell=True` destroys on
Windows — see test_never_uses_shell_true.
"""
import json
import subprocess
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import worker  # noqa: E402

TICKET = {"id": 3, "title": "Do it", "body": "Details.", "attempts": 1}
BOARD = {"name": "site-page", "description": "The site.", "out_of_scope": None,
         "commit_requirements": None, "use_worktrees": False}


def text_line(text):
    return json.dumps({"type": "assistant",
                        "message": {"content": [{"type": "text", "text": text}]}}) + "\n"


def tool_line(name):
    return json.dumps({"type": "assistant",
                        "message": {"content": [{"type": "tool_use", "name": name}]}}) + "\n"


def result_line(text):
    return json.dumps({"type": "result", "result": text}) + "\n"


class _FakeStdout:
    """Iterates a canned list of stdout lines, optionally raising partway
    through to simulate a crashed/broken subprocess pipe. `still_running`
    makes the iterator block past the canned lines (like a live child with
    no output yet) until stop() is called — that's what lets the kill/
    timeout poll loop be exercised without a real subprocess."""

    def __init__(self, lines, crash_after=None, crash_exc=None, still_running=False):
        self._lines = list(lines)
        self._crash_after = crash_after
        self._crash_exc = crash_exc or OSError("pipe broke")
        self._still_running = still_running
        self._stopped = threading.Event()
        self._i = 0

    def __iter__(self):
        return self

    def __next__(self):
        if self._crash_after is not None and self._i == self._crash_after:
            raise self._crash_exc
        if self._i < len(self._lines):
            line = self._lines[self._i]
            self._i += 1
            return line
        if self._still_running and not self._stopped.is_set():
            self._stopped.wait(0.01)
            return self.__next__()
        raise StopIteration

    def stop(self):
        self._stopped.set()

    def close(self):
        pass


class _CapturingStdin:
    """A minimal writable fake that (unlike io.StringIO) keeps its buffered
    text readable via getvalue() after close() — production code closes
    stdin as part of a normal run, and tests need to inspect what was
    written afterwards."""

    def __init__(self):
        self._buf = []
        self.closed = False

    def write(self, data):
        if self.closed:
            raise ValueError("I/O operation on closed file")
        self._buf.append(data)

    def flush(self):
        pass

    def close(self):
        self.closed = True

    def getvalue(self):
        return "".join(self._buf)


class FakeProc:
    def __init__(self, lines=(), returncode=0, crash_after=None, crash_exc=None,
                 still_running=False):
        self.stdout = _FakeStdout(lines, crash_after, crash_exc, still_running)
        self.stdin = _CapturingStdin()
        self.returncode = returncode
        self.killed = False
        self.terminated = False

    def wait(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.stdout.stop()

    def terminate(self):
        self.terminated = True
        self.stdout.stop()


class _GatedFakeStdout(_FakeStdout):
    """Like _FakeStdout, but blocks before yielding its first line until
    `gate` is set — gives a background thread (the chat pump) a guaranteed
    chance to run before the read loop can finish and shut it down."""

    def __init__(self, lines, gate):
        super().__init__(lines)
        self._gate = gate

    def __next__(self):
        if self._i == 0:
            self._gate.wait(timeout=2)
        return super().__next__()


class GatedFakeProc(FakeProc):
    def __init__(self, lines, gate, returncode=0):
        super().__init__(lines, returncode=returncode)
        self.stdout = _GatedFakeStdout(lines, gate)


def fake_popen(proc, captured):
    def _popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return proc
    return _popen


def test_executor_runs_in_the_board_directory(monkeypatch, tmp_path):
    captured = {}
    proc = FakeProc([result_line("done")])
    monkeypatch.setattr(subprocess, "Popen", fake_popen(proc, captured))
    ok, out = worker.ClaudeExecutor().run(
        TICKET, board=BOARD, directory=str(tmp_path), session_id="sid-1")
    assert ok and out == "done"
    assert captured["kwargs"]["cwd"] == str(tmp_path)


def test_executor_passes_allowed_tools_and_session_id(monkeypatch, tmp_path):
    captured = {}
    proc = FakeProc([result_line("done")])
    monkeypatch.setattr(subprocess, "Popen", fake_popen(proc, captured))
    worker.ClaudeExecutor().run(TICKET, board=BOARD,
                                directory=str(tmp_path), session_id="sid-1")
    cmd = captured["cmd"]
    assert "--allowedTools" in cmd
    assert cmd[cmd.index("--allowedTools") + 1] == worker.DEFAULT_ALLOWED_TOOLS
    assert "--session-id" in cmd
    assert cmd[cmd.index("--session-id") + 1] == "sid-1"


def test_executor_requests_stream_json(monkeypatch, tmp_path):
    """Streaming output requires asking the CLI for it — the same format the
    local orchestrator's log viewer already consumes."""
    captured = {}
    proc = FakeProc([result_line("done")])
    monkeypatch.setattr(subprocess, "Popen", fake_popen(proc, captured))
    worker.ClaudeExecutor().run(TICKET, board=BOARD,
                                directory=str(tmp_path), session_id="s")
    cmd = captured["cmd"]
    assert "--output-format" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "stream-json"
    assert "--input-format" in cmd
    assert cmd[cmd.index("--input-format") + 1] == "stream-json"
    assert captured["kwargs"]["stdout"] == subprocess.PIPE
    assert captured["kwargs"]["stdin"] == subprocess.PIPE
    assert "timeout" not in captured["kwargs"]  # no more all-or-nothing timeout


def test_executor_prompt_carries_project_context(monkeypatch, tmp_path):
    captured = {}
    proc = FakeProc([result_line("done")])
    monkeypatch.setattr(subprocess, "Popen", fake_popen(proc, captured))
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
    proc = FakeProc([result_line("done")])
    monkeypatch.setattr(subprocess, "Popen", fake_popen(proc, captured))
    worker.ClaudeExecutor().run(TICKET, board=BOARD,
                                directory=str(tmp_path), session_id="s")
    assert captured["kwargs"].get("shell") in (None, False)
    prompt = captured["cmd"][captured["cmd"].index("-p") + 1]
    assert "\n" in prompt, "the prompt is multi-line; that is the whole point"


def test_resolves_the_cli_through_which(monkeypatch, tmp_path):
    """A bare 'claude' with shell=False misses the Windows .CMD/.EXE shim."""
    captured = {}
    proc = FakeProc([result_line("done")])
    monkeypatch.setattr(subprocess, "Popen", fake_popen(proc, captured))
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
    proc = FakeProc([result_line("done")])
    monkeypatch.setattr(subprocess, "Popen", fake_popen(proc, captured))
    worker.ClaudeExecutor(allowed_tools="Read,Grep").run(
        TICKET, board=BOARD, directory=str(tmp_path), session_id="s")
    cmd = captured["cmd"]
    assert cmd[cmd.index("--allowedTools") + 1] == "Read,Grep"


def test_stub_executor_still_takes_the_new_kwargs():
    ok, out = worker.StubExecutor().run(TICKET, board=None,
                                        directory=None, session_id=None,
                                        progress_cb=None, should_kill=lambda: False)
    assert ok and "StubExecutor" in out


def test_partial_output_streamed_before_process_exits(monkeypatch, tmp_path):
    """The whole point of switching from subprocess.run(capture_output=True)
    to Popen with incremental reads: progress must reach the caller as each
    turn comes off stdout, not only in one lump after the process exits."""
    captured = {}
    lines = [text_line("first update"), text_line("second update"),
             tool_line("Read"), result_line("all done")]
    proc = FakeProc(lines, returncode=0)
    monkeypatch.setattr(subprocess, "Popen", fake_popen(proc, captured))

    seen = []
    ok, out = worker.ClaudeExecutor(progress_batch=1).run(
        TICKET, board=BOARD, directory=str(tmp_path), session_id="s",
        progress_cb=seen.append)

    assert ok
    # More than one flush proves turns were handed off as they arrived,
    # not batched into a single post-exit call.
    assert len(seen) >= 2
    assert "first update" in seen[0]
    assert "second update" not in seen[0]
    assert any("second update" in s for s in seen[1:])


def test_progress_batches_before_flushing(monkeypatch, tmp_path):
    """A larger batch size groups several turns into one comment instead of
    posting one per line."""
    captured = {}
    lines = [text_line("a"), text_line("b"), text_line("c"), result_line("done")]
    proc = FakeProc(lines, returncode=0)
    monkeypatch.setattr(subprocess, "Popen", fake_popen(proc, captured))

    seen = []
    worker.ClaudeExecutor(progress_batch=3).run(
        TICKET, board=BOARD, directory=str(tmp_path), session_id="s",
        progress_cb=seen.append)

    assert seen[0] == "a\nb\nc"


def test_crashed_agent_leaves_partial_log(monkeypatch, tmp_path):
    """If the CLI process dies mid-stream, whatever was read before the
    crash must already have reached progress_cb (so it is attached to the
    ticket as a comment even though the run ultimately fails) and must also
    come back in the failure comment rather than being silently dropped."""
    captured = {}
    lines = [text_line("step one"), text_line("step two")]
    proc = FakeProc(lines, returncode=-9, crash_after=2,
                    crash_exc=OSError("pipe broke"))
    monkeypatch.setattr(subprocess, "Popen", fake_popen(proc, captured))

    seen = []
    ok, comment = worker.ClaudeExecutor(progress_batch=1).run(
        TICKET, board=BOARD, directory=str(tmp_path), session_id="s",
        progress_cb=seen.append)

    assert ok is False
    assert "step one" in comment
    assert "step two" in comment
    assert proc.killed
    assert any("step one" in s for s in seen)
    assert any("step two" in s for s in seen)


def test_nonzero_exit_reports_partial_output(monkeypatch, tmp_path):
    captured = {}
    lines = [text_line("partial work"), ]
    proc = FakeProc(lines, returncode=1)
    monkeypatch.setattr(subprocess, "Popen", fake_popen(proc, captured))
    ok, comment = worker.ClaudeExecutor().run(
        TICKET, board=BOARD, directory=str(tmp_path), session_id="s")
    assert ok is False
    assert "exited 1" in comment
    assert "partial work" in comment


def test_progress_callback_error_does_not_abort_the_run(monkeypatch, tmp_path):
    """A DB hiccup while posting a progress comment must not take down an
    otherwise-healthy agent run."""
    captured = {}
    lines = [text_line("a"), result_line("done")]
    proc = FakeProc(lines, returncode=0)
    monkeypatch.setattr(subprocess, "Popen", fake_popen(proc, captured))

    def flaky(_msg):
        raise RuntimeError("db is down")

    ok, out = worker.ClaudeExecutor(progress_batch=1).run(
        TICKET, board=BOARD, directory=str(tmp_path), session_id="s",
        progress_cb=flaky)
    assert ok and out == "a\ndone"


# ---------- kill support (worker.py's Popen migration) ----------

def test_kill_flag_terminates_the_child_and_reports_the_distinct_status(monkeypatch, tmp_path):
    """A should_kill() that fires mid-run must terminate the process tree and
    signal the distinct 'killed' outcome (KilledByRequest), not a plain
    failure — run_slot maps that to work_queue/tickets' 'killed' status,
    which finish_work does not treat as a genuine failure."""
    captured = {}
    terminated = []
    proc = FakeProc(still_running=True)
    monkeypatch.setattr(subprocess, "Popen", fake_popen(proc, captured))
    monkeypatch.setattr(worker, "KILL_POLL_SECONDS", 0.01)

    def fake_terminate(p):
        terminated.append(p)
        p.kill()  # ends the fake "still running" pipe, as a real kill would

    monkeypatch.setattr(worker, "_terminate_process_tree", fake_terminate)

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

    captured = {}
    proc = FakeProc([result_line("done")], returncode=0)
    monkeypatch.setattr(subprocess, "Popen", fake_popen(proc, captured))
    ok, out = worker.ClaudeExecutor().run(TICKET, board=BOARD, directory=str(tmp_path),
                                          session_id="s", should_kill=should_kill)
    assert ok and out == "done"
    assert calls["n"] == 0


def test_no_should_kill_defaults_to_never_killing(monkeypatch, tmp_path):
    """should_kill is optional; omitting it must not crash the poll loop."""
    captured = {}
    proc = FakeProc([result_line("done")], returncode=0)
    monkeypatch.setattr(subprocess, "Popen", fake_popen(proc, captured))
    ok, out = worker.ClaudeExecutor().run(TICKET, board=BOARD,
                                          directory=str(tmp_path), session_id="s")
    assert ok and out == "done"


# ---------- agent chat: pumping queued messages onto the CLI's live stdin ----------

def test_stdin_closed_immediately_when_no_chat_wiring(monkeypatch, tmp_path):
    """No chat_source means nothing will ever write to stdin — it should be
    closed up front rather than left open for the run's whole duration."""
    captured = {}
    proc = FakeProc([result_line("done")])
    monkeypatch.setattr(subprocess, "Popen", fake_popen(proc, captured))
    worker.ClaudeExecutor().run(TICKET, board=BOARD,
                                directory=str(tmp_path), session_id="s")
    assert proc.stdin.closed


def test_messages_queued_before_the_agent_starts_reach_stdin_in_order(monkeypatch, tmp_path):
    """Messages already sitting in `chat_source` the moment the process comes
    up (i.e. typed before the agent started) must not be lost, and must
    arrive on stdin in the order they were queued."""
    captured = {}
    gate = threading.Event()
    proc = GatedFakeProc([result_line("done")], gate)
    monkeypatch.setattr(subprocess, "Popen", fake_popen(proc, captured))

    delivered = []
    calls = {"n": 0}

    def chat_source():
        calls["n"] += 1
        if calls["n"] == 1:
            gate.set()  # let stdout proceed only once the first poll has run
            return [(1, "first"), (2, "second")]
        return []

    ok, out = worker.ClaudeExecutor(chat_poll_seconds=0).run(
        TICKET, board=BOARD, directory=str(tmp_path), session_id="s",
        chat_source=chat_source, chat_delivered=delivered.extend)

    assert ok
    written = [json.loads(line)["message"]["content"][0]["text"]
               for line in proc.stdin.getvalue().splitlines()]
    assert written == ["first", "second"]
    assert delivered == [1, 2]
    assert proc.stdin.closed


def test_message_typed_mid_run_reaches_stdin_after_an_earlier_one(monkeypatch, tmp_path):
    """A message that only becomes available on a later poll (i.e. typed
    while the agent is already running) must still reach stdin, after
    whatever was already sent."""
    captured = {}
    gate = threading.Event()
    proc = GatedFakeProc([result_line("done")], gate)
    monkeypatch.setattr(subprocess, "Popen", fake_popen(proc, captured))

    delivered = []
    calls = {"n": 0}

    def chat_source():
        calls["n"] += 1
        if calls["n"] == 1:
            return [(1, "already queued")]
        if calls["n"] == 2:
            gate.set()
            return [(2, "typed while running")]
        return []

    ok, out = worker.ClaudeExecutor(chat_poll_seconds=0).run(
        TICKET, board=BOARD, directory=str(tmp_path), session_id="s",
        chat_source=chat_source, chat_delivered=delivered.extend)

    assert ok
    written = [json.loads(line)["message"]["content"][0]["text"]
               for line in proc.stdin.getvalue().splitlines()]
    assert written == ["already queued", "typed while running"]
    assert delivered == [1, 2]


def test_chat_pump_closes_stdin_even_when_the_process_crashes(monkeypatch, tmp_path):
    """The pump thread must be stopped and stdin closed on every exit path,
    including a crashed/broken subprocess pipe."""
    captured = {}
    lines = [text_line("step one"), text_line("step two")]
    proc = FakeProc(lines, returncode=-9, crash_after=2, crash_exc=OSError("pipe broke"))
    monkeypatch.setattr(subprocess, "Popen", fake_popen(proc, captured))

    ok, _ = worker.ClaudeExecutor(chat_poll_seconds=0).run(
        TICKET, board=BOARD, directory=str(tmp_path), session_id="s",
        chat_source=lambda: [], chat_delivered=None)

    assert ok is False
    assert proc.stdin.closed


def test_chat_source_error_does_not_abort_the_run(monkeypatch, tmp_path):
    """A DB hiccup while polling for chat messages must not take down an
    otherwise-healthy agent run, mirroring progress_cb's own error handling."""
    captured = {}
    proc = FakeProc([result_line("done")])
    monkeypatch.setattr(subprocess, "Popen", fake_popen(proc, captured))

    def flaky():
        raise RuntimeError("db is down")

    ok, out = worker.ClaudeExecutor(chat_poll_seconds=0).run(
        TICKET, board=BOARD, directory=str(tmp_path), session_id="s",
        chat_source=flaky, chat_delivered=None)
    assert ok and out == "done"
