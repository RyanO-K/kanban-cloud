"""Auto-clone: a board with a repo_url needs no manual --set-path.

Every git interaction goes through subprocess.run, so it's faked the same
way tests/test_executor.py fakes the Claude CLI call — no real git process
runs in this suite.
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import worker  # noqa: E402

BOARD = {"id": 9, "name": "site-page", "repo_url": "https://github.com/org/repo.git"}


class FakeGit:
    """Records every git invocation; returns canned output keyed by subcommand.

    `outputs` maps a subcommand (args[1], e.g. "clone", "fetch", "remote") to
    (returncode, stdout, stderr). Missing entries default to success/"".
    """

    def __init__(self, outputs=None):
        self.outputs = outputs or {}
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        assert cmd[0] == "git"
        returncode, stdout, stderr = self.outputs.get(cmd[1], (0, "", ""))
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)


def test_explicit_set_path_wins_and_touches_no_git(monkeypatch, tmp_path):
    fake = FakeGit()
    monkeypatch.setattr(subprocess, "run", fake)
    cfg = {"boards": {"9": str(tmp_path / "manual-checkout")}}
    directory, error = worker.resolve_directory(BOARD, cfg)
    assert directory == str(tmp_path / "manual-checkout")
    assert error is None
    assert fake.calls == []


def test_clones_when_the_appdata_directory_is_missing(monkeypatch, tmp_path):
    boards_root = tmp_path / "appdata" / "boards"
    monkeypatch.setattr(worker, "app_data_boards_dir", lambda: boards_root)
    fake = FakeGit(outputs={"rev-parse": (0, "origin/main\n", "")})
    monkeypatch.setattr(subprocess, "run", fake)
    directory, error = worker.resolve_directory(BOARD, {})
    assert error is None
    assert directory == str(boards_root / "9")
    subcommands = [c[0][1] for c in fake.calls]
    assert subcommands == ["clone", "fetch", "rev-parse", "checkout", "reset", "clean"]
    clone_cmd = fake.calls[0][0]
    assert clone_cmd[2] == BOARD["repo_url"]
    assert clone_cmd[3] == str(boards_root / "9")
    # The clone itself has no cwd to run inside yet (the directory doesn't
    # exist until git creates it); every call after that must run inside the
    # board's own directory — a regression that dropped cwd here would run
    # fetch/checkout/reset/clean in the worker's own repo instead.
    assert fake.calls[0][1].get("cwd") is None
    for cmd, kwargs in fake.calls[1:]:
        assert kwargs.get("cwd") == str(boards_root / "9"), cmd


def test_refreshes_an_existing_clone_instead_of_recloning(monkeypatch, tmp_path):
    boards_root = tmp_path / "appdata" / "boards"
    existing = boards_root / "9"
    existing.mkdir(parents=True)
    monkeypatch.setattr(worker, "app_data_boards_dir", lambda: boards_root)
    fake = FakeGit(outputs={
        "remote": (0, BOARD["repo_url"] + "\n", ""),
        "rev-parse": (0, "origin/main\n", ""),
    })
    monkeypatch.setattr(subprocess, "run", fake)
    directory, error = worker.resolve_directory(BOARD, {})
    assert error is None
    assert directory == str(existing)
    subcommands = [c[0][1] for c in fake.calls]
    assert "clone" not in subcommands
    assert subcommands == ["remote", "fetch", "rev-parse", "checkout", "reset", "clean"]
    # Every one of these destructive/refresh calls must run inside the
    # board's own existing checkout, never the worker's own repo.
    for cmd, kwargs in fake.calls:
        assert kwargs.get("cwd") == str(existing), cmd


def test_default_branch_is_read_from_origin_head(monkeypatch, tmp_path):
    boards_root = tmp_path / "appdata" / "boards"
    monkeypatch.setattr(worker, "app_data_boards_dir", lambda: boards_root)
    fake = FakeGit(outputs={"rev-parse": (0, "origin/develop\n", "")})
    monkeypatch.setattr(subprocess, "run", fake)
    worker.resolve_directory(BOARD, {})
    checkout_cmd = next(c[0] for c in fake.calls if c[0][1] == "checkout")
    reset_cmd = next(c[0] for c in fake.calls if c[0][1] == "reset")
    # --force: a dirty working tree must not permanently wedge this board's
    # auto-managed checkout by making checkout fail before reset --hard can
    # clean it up.
    assert checkout_cmd[2] == "--force"
    assert checkout_cmd[3] == "develop"
    assert reset_cmd[3] == "origin/develop"


def test_origin_mismatch_is_a_hard_error_not_a_recreate(monkeypatch, tmp_path):
    boards_root = tmp_path / "appdata" / "boards"
    existing = boards_root / "9"
    existing.mkdir(parents=True)
    monkeypatch.setattr(worker, "app_data_boards_dir", lambda: boards_root)
    fake = FakeGit(outputs={"remote": (0, "https://github.com/org/other-repo.git\n", "")})
    monkeypatch.setattr(subprocess, "run", fake)
    directory, error = worker.resolve_directory(BOARD, {})
    assert directory is None
    assert "does not match" in error
    subcommands = [c[0][1] for c in fake.calls]
    assert subcommands == ["remote"]  # never proceeds to fetch/reset


def test_clone_failure_is_reported_not_raised(monkeypatch, tmp_path):
    boards_root = tmp_path / "appdata" / "boards"
    monkeypatch.setattr(worker, "app_data_boards_dir", lambda: boards_root)
    fake = FakeGit(outputs={"clone": (128, "", "fatal: Authentication failed")})
    monkeypatch.setattr(subprocess, "run", fake)
    directory, error = worker.resolve_directory(BOARD, {})
    assert directory is None
    assert "Authentication failed" in error


def test_git_timeout_is_reported_not_raised(monkeypatch, tmp_path):
    """A git process that hangs (e.g. an interactive credential prompt with no
    ambient auth) must not hang resolve_directory forever or propagate an
    exception — it should come back as an ordinary (None, error) result."""
    boards_root = tmp_path / "appdata" / "boards"
    monkeypatch.setattr(worker, "app_data_boards_dir", lambda: boards_root)

    def timeout_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 600)

    monkeypatch.setattr(subprocess, "run", timeout_run)
    directory, error = worker.resolve_directory(BOARD, {})
    assert directory is None
    assert "timed out" in error.lower()


def test_origin_mismatch_error_redacts_credentials_from_both_urls(monkeypatch, tmp_path):
    """A repo_url with embedded credentials must never reach the error string
    resolve_directory returns — that string is written verbatim into a
    ticket comment, visible to the whole cluster and mirrored to Discord."""
    boards_root = tmp_path / "appdata" / "boards"
    existing = boards_root / "9"
    existing.mkdir(parents=True)
    monkeypatch.setattr(worker, "app_data_boards_dir", lambda: boards_root)
    fake = FakeGit(outputs={
        "remote": (0, "https://other:secret@github.com/org/other-repo.git\n", ""),
    })
    monkeypatch.setattr(subprocess, "run", fake)
    board = {"id": 9, "name": "site-page",
             "repo_url": "https://user:secret@github.com/org/repo.git"}
    directory, error = worker.resolve_directory(board, {})
    assert directory is None
    assert "secret" not in error
    assert "does not match" in error


def test_clone_failure_stderr_redacts_credentials(monkeypatch, tmp_path):
    """git's own stderr can echo a credentialed URL back verbatim even when
    the caller only ever handed git a bare repo_url string."""
    boards_root = tmp_path / "appdata" / "boards"
    monkeypatch.setattr(worker, "app_data_boards_dir", lambda: boards_root)
    fake = FakeGit(outputs={"clone": (
        128, "",
        "fatal: Authentication failed for "
        "'https://user:secret@github.com/org/repo.git/'",
    )})
    monkeypatch.setattr(subprocess, "run", fake)
    board = {"id": 9, "name": "site-page",
             "repo_url": "https://user:secret@github.com/org/repo.git"}
    directory, error = worker.resolve_directory(board, {})
    assert directory is None
    assert "secret" not in error
    assert "Authentication failed" in error


def test_neither_set_path_nor_repo_url_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(subprocess, "run", FakeGit())
    directory, error = worker.resolve_directory(
        {"id": 9, "name": "site-page", "repo_url": None}, {})
    assert directory is None
    assert "--set-path" in error
    assert "Repo URL" in error
