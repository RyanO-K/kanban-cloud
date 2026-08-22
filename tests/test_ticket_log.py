"""Live agent transcript (`ticket_log`): the worker-side half of "the worker
should serve the text live in a stream" — replacing the local `.kanban`
tool's per-run log file with a durable, browser-visible Postgres table (see
docs on TicketLog in app/models.py for why). Covers turn parsing, the SQL
shape of the write/prune helpers (same FakeConn/FakeCursor convention as
tests/test_blocked.py, since the real SQL is Postgres-only), and
ClaudeExecutor actually calling log_cb per turn and writing the local backup
file (extends tests/test_executor.py's own fixtures for that part).
"""
import datetime
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import worker  # noqa: E402

from tests.test_executor import (  # noqa: E402
    BOARD,
    TICKET,
    FakeProc,
    fake_popen,
    result_line,
    text_line,
    tool_line,
)


# ---------- _stream_json_turn ----------

def test_stream_json_turn_tags_assistant_text():
    role, text = worker._stream_json_turn(text_line("hello"))
    assert role == "assistant"
    assert text == "hello"


def test_stream_json_turn_tags_result():
    role, text = worker._stream_json_turn(result_line("done"))
    assert role == "result"
    assert text == "done"


def test_stream_json_turn_returns_none_for_dropped_lines():
    """system/user echo lines carry nothing worth showing (same lines
    _stream_json_text already drops)."""
    role, text = worker._stream_json_turn(json.dumps({"type": "system"}))
    assert role is None and text is None


def test_stream_json_turn_tags_unparseable_lines_raw():
    role, text = worker._stream_json_turn("not json at all")
    assert role == "raw"
    assert text == "not json at all"


def test_stream_json_turn_truncates_oversized_text():
    huge = "x" * (worker._LOG_LINE_MAX_CHARS + 500)
    role, text = worker._stream_json_turn(text_line(huge))
    assert role == "assistant"
    assert len(text) <= worker._LOG_LINE_MAX_CHARS + len("\n…(truncated)")
    assert text.endswith("(truncated)")


# ---------- add_log_line / prune_ticket_log SQL shape ----------

class FakeCursor:
    def __init__(self, rowcount=0):
        self.calls = []
        self.rowcount = rowcount

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, rowcount=0):
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


def test_add_log_line_inserts_with_seq_and_role():
    conn = FakeConn()
    worker.add_log_line(conn, 5, 9, 3, "assistant", "hello")
    sql, params = conn.cursors[0].calls[0]
    assert "INSERT INTO ticket_log" in sql
    assert params == (5, 9, 3, "assistant", "hello")


def test_prune_ticket_log_scopes_to_cluster_and_terminal_statuses():
    conn = FakeConn(rowcount=4)
    now = datetime.datetime(2026, 8, 22)
    deleted = worker.prune_ticket_log(conn, cluster_id=7, retention_days=30, now=now)
    sql, params = conn.cursors[0].calls[0]
    assert "DELETE FROM ticket_log" in sql
    assert "status IN ('done', 'failed', 'killed')" in sql
    assert params[0] == 7
    assert params[1] == now - datetime.timedelta(days=30)
    assert deleted == 4


# ---------- ClaudeExecutor: log_cb wiring + local file backup ----------

def test_log_cb_called_once_per_turn_with_role(monkeypatch, tmp_path):
    captured = {}
    lines = [text_line("first"), tool_line("Read"), result_line("done")]
    proc = FakeProc(lines, returncode=0)
    monkeypatch.setattr(subprocess, "Popen", fake_popen(proc, captured))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    seen = []
    ok, out = worker.ClaudeExecutor(progress_batch=1).run(
        TICKET, board=BOARD, directory=str(tmp_path), session_id="s",
        log_cb=lambda role, text: seen.append((role, text)))

    assert ok
    assert seen[0] == ("assistant", "first")
    assert seen[1][0] == "assistant"  # tool_use turn is rendered as assistant too
    assert seen[-1] == ("result", "done")


def test_log_callback_error_does_not_abort_the_run(monkeypatch, tmp_path):
    captured = {}
    proc = FakeProc([text_line("a"), result_line("done")], returncode=0)
    monkeypatch.setattr(subprocess, "Popen", fake_popen(proc, captured))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    def flaky(role, text):
        raise RuntimeError("db is down")

    ok, out = worker.ClaudeExecutor(progress_batch=1).run(
        TICKET, board=BOARD, directory=str(tmp_path), session_id="s", log_cb=flaky)
    assert ok and out == "a\ndone"


def test_local_run_log_written_under_localappdata(monkeypatch, tmp_path):
    """Best-effort local copy of the raw stream, independent of the board
    checkout directory (which may be an operator's own hand-tended folder)."""
    captured = {}
    lines = [text_line("hello"), result_line("done")]
    proc = FakeProc(lines, returncode=0)
    monkeypatch.setattr(subprocess, "Popen", fake_popen(proc, captured))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    ok, out = worker.ClaudeExecutor().run(
        TICKET, board=BOARD, directory=str(tmp_path), session_id="s")

    assert ok
    logs_dir = tmp_path / "kanban-worker" / "logs"
    files = list(logs_dir.glob(f"{TICKET['id']}-*.log"))
    assert len(files) == 1
    content = files[0].read_text(encoding="utf-8")
    assert json.loads(lines[0])["type"] == "assistant"
    assert "hello" in content
    assert "done" in content


def test_local_run_log_survives_a_crashed_agent(monkeypatch, tmp_path):
    """Whatever was read before the crash must already be flushed to the
    local file, not lost with the rest of the in-memory buffer."""
    captured = {}
    lines = [text_line("step one"), text_line("step two")]
    proc = FakeProc(lines, returncode=-9, crash_after=2, crash_exc=OSError("pipe broke"))
    monkeypatch.setattr(subprocess, "Popen", fake_popen(proc, captured))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    ok, comment = worker.ClaudeExecutor(progress_batch=1).run(
        TICKET, board=BOARD, directory=str(tmp_path), session_id="s")

    assert ok is False
    logs_dir = tmp_path / "kanban-worker" / "logs"
    files = list(logs_dir.glob(f"{TICKET['id']}-*.log"))
    assert len(files) == 1
    content = files[0].read_text(encoding="utf-8")
    assert "step one" in content
    assert "step two" in content


def test_a_bad_logs_folder_does_not_abort_the_run(monkeypatch, tmp_path):
    """A read-only/unwritable LOCALAPPDATA must not fail the ticket — the
    local file is a best-effort backup, not the source of truth."""
    captured = {}
    proc = FakeProc([result_line("done")], returncode=0)
    monkeypatch.setattr(subprocess, "Popen", fake_popen(proc, captured))
    # A file (not a directory) where the logs dir would go: mkdir(parents=True)
    # fails with FileExistsError, an OSError subclass.
    blocker = tmp_path / "kanban-worker"
    blocker.parent.mkdir(parents=True, exist_ok=True)
    blocker.write_text("not a directory")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    ok, out = worker.ClaudeExecutor().run(
        TICKET, board=BOARD, directory=str(tmp_path), session_id="s")
    assert ok and out == "done"
