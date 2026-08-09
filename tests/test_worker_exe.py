"""worker.py exe-packaging behavior: frozen paths, parser, executor default,
first-run enrollment prompt (first-run tests are added in Task 2)."""
import sys
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
