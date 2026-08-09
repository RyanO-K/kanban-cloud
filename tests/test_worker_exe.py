"""worker.py exe-packaging behavior: frozen paths, parser, executor default,
first-run enrollment prompt (first-run tests are added in Task 2)."""
import io
import json
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import worker  # noqa: E402


def test_app_dir_script_mode():
    assert worker.app_dir() == Path(worker.__file__).resolve().parent


def test_app_dir_frozen(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\some\place\kanban-worker.exe")
    assert worker.app_dir() == Path(r"C:\some\place")


def test_default_server_constant():
    assert worker.DEFAULT_SERVER == "https://kanban-cloud.onrender.com"


def test_executor_default_is_real():
    args = worker.build_parser().parse_args([])
    assert isinstance(worker.pick_executor(args), worker.ClaudeExecutor)


def test_stub_flag_selects_stub():
    args = worker.build_parser().parse_args(["--stub"])
    assert isinstance(worker.pick_executor(args), worker.StubExecutor)


def test_real_alias_still_accepted():
    args = worker.build_parser().parse_args(["--real"])
    assert isinstance(worker.pick_executor(args), worker.ClaudeExecutor)


def test_first_run_reprompts_then_enrolls(monkeypatch, capsys):
    calls = {}

    def fake_enroll(server, code, name):
        calls.update(server=server, code=code, name=name)
        return {"worker_id": 1, "dsn": "x", "cluster_id": 2,
                "name": name, "cluster_name": "Main"}

    monkeypatch.setattr(worker, "enroll", fake_enroll)
    prompts = iter(["", "   ", "  ABC123  "])  # two empties re-prompt
    monkeypatch.setattr("builtins.input", lambda prompt="": next(prompts))
    monkeypatch.setenv("COMPUTERNAME", "TESTPC")
    args = worker.build_parser().parse_args([])
    cfg = worker.first_run_enroll(args)
    assert cfg["worker_id"] == 1
    assert calls == {"server": worker.DEFAULT_SERVER, "code": "ABC123",
                     "name": "TESTPC"}


def test_first_run_server_and_name_overrides(monkeypatch):
    calls = {}
    monkeypatch.setattr(worker, "enroll",
                        lambda server, code, name: calls.update(
                            server=server, code=code, name=name) or {"worker_id": 9})
    monkeypatch.setattr("builtins.input", lambda prompt="": "JOIN1")
    args = worker.build_parser().parse_args(
        ["--server", "http://localhost:8000", "--name", "devbox"])
    assert worker.first_run_enroll(args) == {"worker_id": 9}
    assert calls == {"server": "http://localhost:8000", "code": "JOIN1",
                     "name": "devbox"}


def test_first_run_http_error_prints_detail(monkeypatch, capsys):
    err = urllib.error.HTTPError(
        "url", 404, "Not Found", {},
        io.BytesIO(json.dumps({"detail": "Invalid join code"}).encode()))

    def boom(server, code, name):
        raise err

    monkeypatch.setattr(worker, "enroll", boom)
    monkeypatch.setattr("builtins.input", lambda prompt="": "WRONG")
    args = worker.build_parser().parse_args([])
    assert worker.first_run_enroll(args) is None
    out = capsys.readouterr().out
    assert "Invalid join code" in out and "404" in out


def test_first_run_cancelled_by_eof(monkeypatch):
    def eof(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    args = worker.build_parser().parse_args([])
    assert worker.first_run_enroll(args) is None


def test_pause_if_frozen_noop_when_not_frozen(monkeypatch):
    called = []
    monkeypatch.setattr("builtins.input", lambda prompt="": called.append(1))
    monkeypatch.delattr(sys, "frozen", raising=False)
    worker.pause_if_frozen()
    assert called == []


def test_save_config_unwritable_message(monkeypatch, tmp_path, capsys):
    target = tmp_path / "no-such-dir" / "cfg.json"  # parent missing -> OSError
    monkeypatch.setattr(worker, "CONFIG_PATH", target)
    try:
        worker.save_config({"a": 1})
        raised = False
    except OSError:
        raised = True
    assert raised
    assert "writable folder" in capsys.readouterr().out
