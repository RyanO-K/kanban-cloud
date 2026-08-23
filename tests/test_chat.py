"""Agent chat: `ticket_chat` replaces the local tool's per-run JSONL inbox,
and these are the ported `chat_*` helpers that encode a typed message and
pump it onto a running CLI's stdin. Pure functions — no subprocess, no DB —
so the encoding and close rules are exercised directly here; ClaudeExecutor's
actual wiring (thread + real stdin pipe) is covered in test_executor.py.
"""
import io
import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import worker  # noqa: E402


class _CapturingStdin:
    """A minimal writable fake that (unlike io.StringIO) keeps its buffered
    text readable via getvalue() after close() — `_chat_pump_loop` always
    closes stdin on its way out, and tests need to inspect what was written
    afterwards."""

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


def test_chat_encode_shapes_a_stream_json_user_turn():
    line = worker.chat_encode("hello there")
    obj = json.loads(line)
    assert obj["type"] == "user"
    assert obj["message"]["role"] == "user"
    assert obj["message"]["content"] == [{"type": "text", "text": "hello there"}]
    assert "\n" not in line


def test_chat_pump_writes_all_pending_in_order():
    stdin = io.StringIO()
    sent = worker.chat_pump(stdin, [(1, "first"), (2, "second"), (3, "third")])
    assert sent == [1, 2, 3]
    lines = stdin.getvalue().splitlines()
    assert len(lines) == 3
    texts = [json.loads(line)["message"]["content"][0]["text"] for line in lines]
    assert texts == ["first", "second", "third"]


def test_chat_pump_stops_at_the_first_broken_write():
    """A closed/broken pipe means the process is already gone; every message
    after the failure point is left off the returned list so it stays
    unmarked as delivered and survives for a later run."""
    class _BreaksOnSecondWrite:
        def __init__(self):
            self.writes = []

        def write(self, data):
            if len(self.writes) == 1:
                raise BrokenPipeError()
            self.writes.append(data)

        def flush(self):
            pass

    stdin = _BreaksOnSecondWrite()
    sent = worker.chat_pump(stdin, [(1, "ok"), (2, "boom"), (3, "never sent")])
    assert sent == [1]


def test_chat_close_swallows_a_broken_pipe():
    class _BrokenStdin:
        def close(self):
            raise BrokenPipeError()

    worker.chat_close(_BrokenStdin())  # must not raise


def test_chat_close_closes_a_healthy_stdin():
    stdin = io.StringIO()
    worker.chat_close(stdin)
    assert stdin.closed


def test_pump_loop_delivers_messages_present_on_the_first_poll():
    """The very first poll happens before any wait, so messages already
    queued when the pump starts (i.e. before the agent/process started
    producing output) are not lost."""
    stdin = _CapturingStdin()
    stop_pump = threading.Event()
    delivered = []
    calls = {"n": 0}

    def chat_source():
        calls["n"] += 1
        if calls["n"] == 1:
            return [(1, "queued before start")]
        stop_pump.set()
        return []

    worker._chat_pump_loop(stdin, chat_source, delivered.extend, stop_pump, poll_interval=0)

    assert delivered == [1]
    assert "queued before start" in stdin.getvalue()


def test_pump_loop_closes_stdin_when_stopped():
    """The pump must always close stdin on its way out, whatever ended the
    loop, so the CLI sees a clean EOF instead of an abandoned pipe."""
    stdin = io.StringIO()
    stop_pump = threading.Event()
    stop_pump.set()  # loop must exit on its very first check, with no polls
    worker._chat_pump_loop(stdin, lambda: [], None, stop_pump, poll_interval=0)
    assert stdin.closed


def test_pump_loop_stops_once_the_pipe_breaks():
    """If a write fails partway through a batch, the loop must not keep
    spinning against a dead pipe."""
    class _BreaksImmediately:
        def write(self, data):
            raise BrokenPipeError()

        def flush(self):
            pass

        def close(self):
            pass

    stop_pump = threading.Event()
    calls = {"n": 0}

    def chat_source():
        calls["n"] += 1
        return [(1, "never arrives")]

    worker._chat_pump_loop(_BreaksImmediately(), chat_source, None, stop_pump, poll_interval=0)
    # One poll, one failed write, then the loop gives up rather than
    # retrying against a pipe that will never accept anything again.
    assert calls["n"] == 1


# ---------- ending the run: stdin must close once the agent is done ----------
#
# With stdin held open the CLI waits for another user turn after it emits its
# top-level result, so the process never exits, the reader never sees EOF, and
# the run burns the full 30-minute deadline even after the work is finished.
# Same rule as the local tool's chat_should_close: close once a result has
# arrived since the last message we sent and nothing else is queued.

def run_pump(stdin, chat_source, stop_pump, result_seen, delivered=None):
    """Run the pump on a thread and return it, so a loop that never exits
    shows up as a still-alive thread instead of hanging the test run."""
    th = threading.Thread(
        target=worker._chat_pump_loop,
        args=(stdin, chat_source, delivered, stop_pump, 0, result_seen),
        daemon=True)
    th.start()
    th.join(timeout=2)
    return th


def test_chat_should_close_only_after_a_result_with_nothing_queued():
    assert worker.chat_should_close(True, True) is True
    assert worker.chat_should_close(False, True) is False
    assert worker.chat_should_close(True, False) is False


def test_pump_loop_closes_stdin_once_the_agent_emits_its_result():
    stdin = _CapturingStdin()
    stop_pump = threading.Event()  # never set: only the result may end this
    result_seen = threading.Event()
    result_seen.set()
    th = run_pump(stdin, lambda: [], stop_pump, result_seen)
    assert not th.is_alive(), "pump kept the CLI's stdin open past its result"
    assert stdin.closed


def test_pump_loop_keeps_going_when_a_message_beats_the_result():
    """A message queued before the result is sent instead, and the agent runs
    another turn — the next result re-arms the close."""
    stdin = _CapturingStdin()
    stop_pump = threading.Event()
    result_seen = threading.Event()
    delivered = []
    calls = {"n": 0}

    def chat_source():
        calls["n"] += 1
        if calls["n"] == 1:
            return [(1, "one more thing")]
        result_seen.set()  # the agent answers, then reports done
        return []

    th = run_pump(stdin, chat_source, stop_pump, result_seen, delivered.extend)
    assert not th.is_alive()
    assert delivered == [1]
    assert "one more thing" in stdin.getvalue()
    assert stdin.closed
