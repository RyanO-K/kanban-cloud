"""worker.py v2 pure logic: config round-trip, enroll parsing, SQL invariants.

The claim/finish SQL itself is Postgres-only (SKIP LOCKED) and is exercised
by scripts/neon_smoke_v2.py against the real database.
"""
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import worker  # noqa: E402


def test_config_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "CONFIG_PATH", tmp_path / "cfg.json")
    cfg = {"dsn": "postgresql://r:p@h/db", "worker_id": 1,
           "cluster_id": 2, "name": "pc", "cluster_name": "Main"}
    worker.save_config(cfg)
    assert worker.load_config() == cfg


def test_enroll_saves_full_config(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "CONFIG_PATH", tmp_path / "cfg.json")
    response = {"worker_id": 7, "cluster": {"id": 3, "name": "Main"},
                "dsn": "postgresql://worker_c3_w7:pw@h/db?sslmode=require"}

    def fake_urlopen(req, data=None, timeout=None):
        assert req.full_url == "https://srv.example/api/workers/enroll"
        body = json.loads(data.decode())
        assert body == {"join_code": "ABC12345", "name": "pc"}
        return io.BytesIO(json.dumps(response).encode())

    monkeypatch.setattr(worker.urllib.request, "urlopen", fake_urlopen)
    cfg = worker.enroll("https://srv.example", "ABC12345", "pc")
    assert cfg == {"dsn": response["dsn"], "worker_id": 7, "cluster_id": 3,
                   "name": "pc", "cluster_name": "Main"}
    assert worker.load_config() == cfg


def test_claim_sql_is_race_safe_and_utc():
    assert "FOR UPDATE OF wq SKIP LOCKED" in worker.CLAIM_SQL
    assert "now() at time zone 'utc'" in worker.CLAIM_SQL
    assert "target_worker IS NULL OR" in worker.CLAIM_SQL
    assert "LIMIT 1" in worker.CLAIM_SQL


def test_max_attempts_matches_server():
    from app.models import MAX_ATTEMPTS
    assert worker.MAX_ATTEMPTS == MAX_ATTEMPTS


def test_executor_selection():
    assert worker.StubExecutor().name == "stub"
    assert worker.ClaudeExecutor().name == "claude"


class _NullCtx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeCursor(_NullCtx):
    def __init__(self, row):
        self._row = row

    def execute(self, *a, **k):
        pass

    def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self, row):
        self._row = row

    def cursor(self):
        return _FakeCursor(self._row)

    def transaction(self):
        return _NullCtx()


def test_kill_requested_reads_the_live_flag():
    assert worker.kill_requested(_FakeConn((True,)), 1) is True
    assert worker.kill_requested(_FakeConn((False,)), 1) is False


def test_kill_requested_defaults_false_when_the_row_is_gone():
    """The claim finished (or never existed) by the time we polled — treat
    that the same as "no kill", not an error."""
    assert worker.kill_requested(_FakeConn(None), 1) is False


def test_test_flag_targets_local_server():
    args = worker.build_parser().parse_args(["--enroll", "--join-code", "X", "--test"])
    assert worker.resolve_server(args) == worker.TEST_SERVER
    assert worker.TEST_SERVER != worker.DEFAULT_SERVER


def test_explicit_server_flag_overrides_test_flag():
    args = worker.build_parser().parse_args(
        ["--enroll", "--join-code", "X", "--test", "--server", "https://custom.example"]
    )
    assert worker.resolve_server(args) == "https://custom.example"


def test_no_flags_targets_default_server():
    args = worker.build_parser().parse_args(["--enroll", "--join-code", "X"])
    assert worker.resolve_server(args) == worker.DEFAULT_SERVER
