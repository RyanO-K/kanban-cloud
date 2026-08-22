"""N slots per PC: the machine's own throttle on how much it runs at once."""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import worker  # noqa: E402


def test_resolve_concurrency_prefers_the_flag():
    args = worker.build_parser().parse_args(["--concurrency", "4"])
    assert worker.resolve_concurrency(args, {"concurrency": 2}) == 4


def test_resolve_concurrency_falls_back_to_config():
    args = worker.build_parser().parse_args([])
    assert worker.resolve_concurrency(args, {"concurrency": 3}) == 3


def test_resolve_concurrency_defaults_to_one():
    args = worker.build_parser().parse_args([])
    assert worker.resolve_concurrency(args, {}) == 1


def test_resolve_concurrency_floors_at_one():
    args = worker.build_parser().parse_args(["--concurrency", "0"])
    assert worker.resolve_concurrency(args, {}) == 1


def test_resolve_concurrency_survives_a_junk_config_value():
    args = worker.build_parser().parse_args([])
    assert worker.resolve_concurrency(args, {"concurrency": "lots"}) == 1


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


def test_slots_run_concurrently_and_never_exceed_the_limit(monkeypatch, tmp_path):
    """Three slots, a slow executor: all three must be in flight at once."""
    monkeypatch.setattr(worker, "CONFIG_PATH", tmp_path / "cfg.json")
    live = []
    peak = {"n": 0}
    lock = threading.Lock()
    claims = iter(range(6))

    def fake_claim(conn, wid, cid, boards):
        with lock:
            try:
                n = next(claims)
            except StopIteration:
                return None
        return {"assignment_id": n, "session_id": "s",
                "board": {"id": 1, "name": "b"},
                "ticket": {"id": n, "board_id": 1, "title": "t", "body": "",
                           "status": "doing", "attempts": 1}}

    class SlowExecutor:
        name = "slow"

        def run(self, ticket, board=None, directory=None, session_id=None):
            with lock:
                live.append(ticket["id"])
                peak["n"] = max(peak["n"], len(live))
            time.sleep(0.2)
            with lock:
                live.remove(ticket["id"])
            return True, "ok"

    monkeypatch.setattr(worker.psycopg, "connect", _fake_connect())
    monkeypatch.setattr(worker, "claim_next", fake_claim)
    monkeypatch.setattr(worker, "finish_work", lambda *a, **k: "review")
    monkeypatch.setattr(worker, "set_slot_counts", lambda *a, **k: None)
    monkeypatch.setattr(worker, "heartbeat", lambda *a, **k: None)

    cfg = {"dsn": "x", "worker_id": 1, "cluster_id": 1, "name": "pc",
           "boards": {"1": str(tmp_path)}}
    args = worker.build_parser().parse_args(["--poll", "0.01"])
    stop = threading.Event()
    threads = [threading.Thread(target=worker.run_slot,
                                args=(cfg, args, SlowExecutor(), stop, i))
               for i in range(3)]
    for t in threads:
        t.start()
    time.sleep(0.5)
    stop.set()
    for t in threads:
        t.join(timeout=5)
    assert peak["n"] == 3, f"expected 3 concurrent, saw {peak['n']}"


def test_running_count_returns_to_zero(monkeypatch, tmp_path):
    """The count the Workers panel reads must not drift up over time."""
    monkeypatch.setattr(worker, "CONFIG_PATH", tmp_path / "cfg.json")
    claims = iter([1])

    def fake_claim(conn, wid, cid, boards):
        try:
            n = next(claims)
        except StopIteration:
            return None
        return {"assignment_id": n, "session_id": "s",
                "board": {"id": 1, "name": "b"},
                "ticket": {"id": n, "board_id": 1, "title": "t", "body": "",
                           "status": "doing", "attempts": 1}}

    class Boom:
        name = "boom"

        def run(self, *a, **k):
            raise RuntimeError("agent exploded")

    monkeypatch.setattr(worker.psycopg, "connect", _fake_connect())
    monkeypatch.setattr(worker, "claim_next", fake_claim)
    monkeypatch.setattr(worker, "finish_work", lambda *a, **k: "review")
    cfg = {"dsn": "x", "worker_id": 1, "cluster_id": 1, "name": "pc",
           "boards": {"1": str(tmp_path)}}
    args = worker.build_parser().parse_args(["--poll", "0.01", "--once"])
    worker.run_slot(cfg, args, Boom(), threading.Event(), 0)
    assert worker.running_count() == 0


def test_each_slot_opens_its_own_connection(monkeypatch, tmp_path):
    monkeypatch.setattr(worker, "CONFIG_PATH", tmp_path / "cfg.json")
    conns = []
    monkeypatch.setattr(worker.psycopg, "connect", _fake_connect(conns))
    monkeypatch.setattr(worker, "claim_next", lambda *a, **k: None)
    monkeypatch.setattr(worker, "set_slot_counts", lambda *a, **k: None)
    monkeypatch.setattr(worker, "heartbeat", lambda *a, **k: None)

    cfg = {"dsn": "x", "worker_id": 1, "cluster_id": 1, "name": "pc"}
    args = worker.build_parser().parse_args(["--poll", "0.01", "--once"])
    stop = threading.Event()
    threads = [threading.Thread(target=worker.run_slot,
                                args=(cfg, args, worker.StubExecutor(), stop, i))
               for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert len({id(c) for c in conns}) == 3


def test_stub_slots_ignore_the_board_filter(monkeypatch, tmp_path):
    """--stub needs no repo, so a PC with no paths configured can still test."""
    monkeypatch.setattr(worker, "CONFIG_PATH", tmp_path / "cfg.json")
    seen = {}
    monkeypatch.setattr(worker.psycopg, "connect", _fake_connect())
    monkeypatch.setattr(worker, "claim_next",
                        lambda conn, wid, cid, boards: seen.setdefault("boards", boards))
    cfg = {"dsn": "x", "worker_id": 1, "cluster_id": 1, "name": "pc"}
    args = worker.build_parser().parse_args(["--poll", "0.01", "--once"])
    worker.run_slot(cfg, args, worker.StubExecutor(), threading.Event(), 0)
    assert seen["boards"] is None


def test_real_slots_pass_the_configured_boards(monkeypatch, tmp_path):
    monkeypatch.setattr(worker, "CONFIG_PATH", tmp_path / "cfg.json")
    seen = {}
    monkeypatch.setattr(worker.psycopg, "connect", _fake_connect())
    monkeypatch.setattr(worker, "claim_next",
                        lambda conn, wid, cid, boards: seen.setdefault("boards", boards))
    cfg = {"dsn": "x", "worker_id": 1, "cluster_id": 1, "name": "pc",
           "boards": {"4": str(tmp_path)}}
    args = worker.build_parser().parse_args(["--poll", "0.01", "--once"])
    worker.run_slot(cfg, args, worker.ClaudeExecutor(), threading.Event(), 0)
    assert seen["boards"] == [4]


def test_revoked_credentials_stop_every_slot(monkeypatch, tmp_path):
    """One slot learning the PC is revoked must bring the others down too."""
    monkeypatch.setattr(worker, "CONFIG_PATH", tmp_path / "cfg.json")

    def boom(dsn, **kw):
        raise worker.psycopg.OperationalError(
            'password authentication failed for user "pc"')

    monkeypatch.setattr(worker.psycopg, "connect", boom)
    cfg = {"dsn": "x", "worker_id": 1, "cluster_id": 1, "name": "pc"}
    args = worker.build_parser().parse_args(["--poll", "0.01"])
    stop = threading.Event()
    worker.run_slot(cfg, args, worker.StubExecutor(), stop, 0)
    assert stop.is_set()


def test_real_slots_auto_clone_when_no_set_path_is_configured(monkeypatch, tmp_path):
    """No --set-path entry, but the board carries a repo_url: resolve_directory
    must be consulted and its directory handed to the executor."""
    monkeypatch.setattr(worker, "CONFIG_PATH", tmp_path / "cfg.json")
    seen = {}

    def fake_claim(conn, wid, cid, boards):
        return {"assignment_id": 1, "session_id": "s",
                "board": {"id": 9, "name": "site-page",
                          "repo_url": "https://github.com/org/repo.git"},
                "ticket": {"id": 1, "board_id": 9, "title": "t", "body": "",
                           "status": "doing", "attempts": 1}}

    class RecordingExecutor:
        name = "recording"

        def run(self, ticket, board=None, directory=None, session_id=None):
            seen["directory"] = directory
            return True, "ok"

    monkeypatch.setattr(worker.psycopg, "connect", _fake_connect())
    monkeypatch.setattr(worker, "claim_next", fake_claim)
    monkeypatch.setattr(worker, "finish_work", lambda *a, **k: "review")
    monkeypatch.setattr(worker, "resolve_directory",
                        lambda board, cfg: (str(tmp_path / "cloned"), None))

    cfg = {"dsn": "x", "worker_id": 1, "cluster_id": 1, "name": "pc"}
    args = worker.build_parser().parse_args(["--poll", "0.01", "--once"])
    worker.run_slot(cfg, args, RecordingExecutor(), threading.Event(), 0)
    assert seen["directory"] == str(tmp_path / "cloned")


def test_resolve_error_skips_the_executor_entirely(monkeypatch, tmp_path):
    """When neither --set-path nor repo_url resolves, don't waste a Claude
    CLI invocation on a directory that doesn't exist."""
    monkeypatch.setattr(worker, "CONFIG_PATH", tmp_path / "cfg.json")
    finished = {}

    def fake_claim(conn, wid, cid, boards):
        return {"assignment_id": 1, "session_id": "s",
                "board": {"id": 9, "name": "site-page", "repo_url": None},
                "ticket": {"id": 1, "board_id": 9, "title": "t", "body": "",
                           "status": "doing", "attempts": 1}}

    class Unreachable:
        name = "unreachable"

        def run(self, *a, **k):
            raise AssertionError("executor.run must not be called")

    def fake_finish(conn, wname, wid, item_id, ticket_id, ok, comment):
        finished["ok"], finished["comment"] = ok, comment
        return "failed"

    monkeypatch.setattr(worker.psycopg, "connect", _fake_connect())
    monkeypatch.setattr(worker, "claim_next", fake_claim)
    monkeypatch.setattr(worker, "finish_work", fake_finish)
    monkeypatch.setattr(worker, "resolve_directory",
                        lambda board, cfg: (None, "no folder configured, no repo_url"))

    cfg = {"dsn": "x", "worker_id": 1, "cluster_id": 1, "name": "pc"}
    args = worker.build_parser().parse_args(["--poll", "0.01", "--once"])
    worker.run_slot(cfg, args, Unreachable(), threading.Event(), 0)
    assert finished["ok"] is False
    assert finished["comment"] == "no folder configured, no repo_url"


def test_stub_slots_never_call_resolve_directory(monkeypatch, tmp_path):
    """--stub needs no repo at all; it must not trigger a clone attempt."""
    monkeypatch.setattr(worker, "CONFIG_PATH", tmp_path / "cfg.json")
    monkeypatch.setattr(worker.time, "sleep", lambda s: None)  # skip StubExecutor's 2s delay

    def fake_claim(conn, wid, cid, boards):
        return {"assignment_id": 1, "session_id": "s",
                "board": {"id": 9, "name": "b", "repo_url": "https://x/y.git"},
                "ticket": {"id": 1, "board_id": 9, "title": "t", "body": "",
                           "status": "doing", "attempts": 1}}

    def boom(board, cfg):
        raise AssertionError("resolve_directory must not run for --stub")

    monkeypatch.setattr(worker.psycopg, "connect", _fake_connect())
    monkeypatch.setattr(worker, "claim_next", fake_claim)
    monkeypatch.setattr(worker, "finish_work", lambda *a, **k: "review")
    monkeypatch.setattr(worker, "resolve_directory", boom)

    cfg = {"dsn": "x", "worker_id": 1, "cluster_id": 1, "name": "pc"}
    args = worker.build_parser().parse_args(["--poll", "0.01", "--once"])
    worker.run_slot(cfg, args, worker.StubExecutor(), threading.Event(), 0)
