"""The ssh transport: a board whose agent runs on another machine.

`--set-ssh <board>=[user@]host:/path` points a board at a checkout on a
different PC. The worker still does the claiming, streaming and result-posting
from here; only the `claude` CLI and the git that lands its branch move to the
far end. That is the whole trick behind ticket #24 — because ssh is a
transparent pipe for stdin/stdout, the live transcript in the browser is
byte-for-byte what a local run produces, and the session id it mints is
resumable later with `ssh <host> -t "cd ... && claude --resume <id>"`.

No real ssh process runs in this suite: subprocess is faked throughout, the
same approach as tests/test_worker_push.py and tests/test_executor.py.
"""
import json
import subprocess
import sys
import threading
from pathlib import Path, PurePosixPath

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import worker  # noqa: E402

TARGET = {"target": "ryan@studio", "directory": "/srv/site-page"}
BOARDS = [(4, "site-page"), (7, "devtool-invoice")]


# ---------- the remote command string ----------
#
# Everything the far end runs arrives as one string for its `sh` to parse, so
# the quoting here is the whole safety story: an unquoted path with a space
# becomes two arguments, and an unquoted multi-line prompt becomes two
# commands.

def test_remote_shell_execs_the_command():
    """`exec` so the CLI replaces its wrapper shell instead of sitting under
    it — one less process to outlive a dropped connection."""
    assert worker.build_remote_shell(None, ["claude", "-p"]) == "exec claude -p"


def test_remote_shell_cds_into_the_remote_directory_first():
    assert (worker.build_remote_shell("/srv/site-page", ["claude"])
            == "cd /srv/site-page && exec claude")


def test_remote_shell_quotes_a_directory_with_spaces():
    """Unquoted, `cd /srv/my repo` is a `cd` with two arguments and the run
    happens in the wrong folder (or not at all)."""
    out = worker.build_remote_shell("/srv/my repo", ["claude"])
    assert out.startswith("cd '/srv/my repo' && ")


def test_remote_shell_quotes_multiline_arguments():
    """The system prompt is multi-line; unquoted, its second line would be
    read as a separate command by the remote shell."""
    out = worker.build_remote_shell(
        None, ["claude", "--append-system-prompt", "line one\nline two"])
    assert "\nline two" not in out.replace("'line one\nline two'", "")
    # round-trips back to the argv we meant
    assert worker.shlex.split(out)[1:] == [
        "claude", "--append-system-prompt", "line one\nline two"]


def test_remote_shell_applies_env_through_env_not_a_prefix():
    """`VAR=v exec cmd` is a corner of POSIX not worth relying on; `env` is
    unambiguous."""
    out = worker.build_remote_shell(None, ["git", "push"],
                                    env={"GIT_TERMINAL_PROMPT": "0"})
    assert out == "exec env GIT_TERMINAL_PROMPT=0 git push"


def test_remote_shell_stringifies_path_objects():
    """resolve_directory-style callers can hand back a Path; shlex.join would
    otherwise refuse it. PurePosixPath because the far end is POSIX — a
    Windows Path would render its own separators here."""
    out = worker.build_remote_shell(PurePosixPath("/srv/repo"),
                                    ["git", "status"])
    assert out == "cd /srv/repo && exec git status"


# ---------- the ssh argv ----------

def test_ssh_command_targets_the_host_with_one_remote_string():
    cmd = worker.build_ssh_command(TARGET, ["claude", "--session-id", "sid"])
    assert cmd[0] == "ssh"
    assert cmd[-2] == "ryan@studio"
    assert cmd[-1] == "cd /srv/site-page && exec claude --session-id sid"


def test_ssh_command_never_allocates_a_tty_and_never_prompts():
    """A pty would echo our stdin back and translate newlines, corrupting the
    stream-json the reader parses; a password prompt would hang the slot
    forever, since stdin belongs to the agent's turns."""
    cmd = worker.build_ssh_command(TARGET, ["claude"])
    assert "-T" in cmd
    assert cmd[cmd.index("-o") + 1] == "BatchMode=yes"


def test_ssh_command_directory_argument_overrides_the_configured_one():
    cmd = worker.build_ssh_command(TARGET, ["git", "status"],
                                   directory="/srv/other")
    assert cmd[-1].startswith("cd /srv/other && ")


def test_ssh_command_uses_the_resolved_ssh_executable():
    """shutil.which's answer, not a bare 'ssh' — same reason ClaudeExecutor
    resolves the CLI itself rather than trusting a shell to."""
    cmd = worker.build_ssh_command(TARGET, ["claude"],
                                   ssh_exe=r"C:\Windows\System32\OpenSSH\ssh.exe")
    assert cmd[0] == r"C:\Windows\System32\OpenSSH\ssh.exe"


# ---------- exit-code hints ----------

def test_connect_failure_explains_batchmode():
    hint = worker.ssh_exit_hint(worker.SSH_CONNECT_FAILED)
    assert "connect" in hint and "BatchMode" in hint


def test_command_not_found_explains_the_non_interactive_path():
    """Exit 127 over ssh almost always means `claude` is on the login shell's
    PATH but not the non-interactive one — the single most likely first
    failure when setting a target up."""
    hint = worker.ssh_exit_hint(worker.SSH_COMMAND_NOT_FOUND)
    assert "127" in hint and "PATH" in hint


def test_an_ordinary_failure_adds_no_hint():
    """The agent's own transcript already says what went wrong; a guess here
    would only be noise."""
    assert worker.ssh_exit_hint(1) == ""
    assert worker.ssh_exit_hint(0) == ""


# ---------- parsing --set-ssh ----------

def test_parse_set_ssh_splits_board_target_and_path():
    assert (worker.parse_set_ssh("4=ryan@studio:/srv/site-page")
            == ("4", "ryan@studio", "/srv/site-page"))


def test_parse_set_ssh_accepts_a_bare_host():
    assert (worker.parse_set_ssh("site-page=studio:/srv/repo")
            == ("site-page", "studio", "/srv/repo"))


def test_parse_set_ssh_splits_on_the_first_colon():
    """A POSIX remote path starts with '/' and the target never contains one,
    so the first colon is always the separator — later ones belong to the
    path."""
    assert (worker.parse_set_ssh("4=studio:/srv/a:b")
            == ("4", "studio", "/srv/a:b"))


def test_parse_set_ssh_with_an_empty_value_means_clear():
    """Without this the only way back to a local run would be hand-editing
    the config file."""
    assert worker.parse_set_ssh("4=") == ("4", None, None)


def test_parse_set_ssh_rejects_a_missing_equals():
    with pytest.raises(ValueError, match="--set-ssh needs"):
        worker.parse_set_ssh("4")


def test_parse_set_ssh_rejects_a_missing_board():
    with pytest.raises(ValueError, match="board id or name"):
        worker.parse_set_ssh("=studio:/srv/repo")


def test_parse_set_ssh_rejects_a_target_with_no_path():
    """A host alone would run the agent in whatever folder ssh happened to
    land in — the far end's home directory, not a checkout."""
    with pytest.raises(ValueError, match="no remote path"):
        worker.parse_set_ssh("4=studio")


def test_parse_set_ssh_rejects_a_half_empty_target():
    with pytest.raises(ValueError, match="both a host and a remote path"):
        worker.parse_set_ssh("4=:/srv/repo")
    with pytest.raises(ValueError, match="both a host and a remote path"):
        worker.parse_set_ssh("4=studio:")


# ---------- saving --set-ssh ----------

class FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, rows=()):
        self._rows = rows

    def cursor(self):
        return FakeCursor(self._rows)

    def transaction(self):
        class _T:
            def __enter__(self_):
                return self_

            def __exit__(self_, *a):
                return False

        return _T()


def test_apply_set_ssh_saves_keyed_by_board_id(tmp_path, monkeypatch):
    """Keyed by id, not name: renaming a board must not orphan the target."""
    monkeypatch.setattr(worker, "CONFIG_PATH", tmp_path / "cfg.json")
    cfg = {"cluster_id": 1, "worker_id": 2, "dsn": "x", "name": "pc"}
    out = worker.apply_set_ssh(FakeConn(BOARDS), cfg,
                               "site-page=ryan@studio:/srv/site-page")
    assert out["ssh"] == {"4": {"target": "ryan@studio",
                                "directory": "/srv/site-page"}}
    assert worker.load_config()["ssh"] == out["ssh"]


def test_apply_set_ssh_never_touches_the_target(tmp_path, monkeypatch):
    """A board is configured while the far end is asleep as readily as while
    it is up, and a reachability check that passed here would say nothing
    about the moment a ticket is actually claimed."""
    monkeypatch.setattr(worker, "CONFIG_PATH", tmp_path / "cfg.json")

    def boom(*a, **k):
        raise AssertionError("configuring a target must not run a subprocess")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    worker.apply_set_ssh(FakeConn(BOARDS), {"cluster_id": 1},
                         "4=studio:/srv/repo")


def test_apply_set_ssh_with_an_empty_value_clears_the_target(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "CONFIG_PATH", tmp_path / "cfg.json")
    cfg = {"cluster_id": 1, "ssh": {"4": dict(TARGET)}}
    out = worker.apply_set_ssh(FakeConn(BOARDS), cfg, "4=")
    assert out["ssh"] == {}
    assert worker.load_config()["ssh"] == {}


def test_apply_set_ssh_overwrites_an_existing_target(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "CONFIG_PATH", tmp_path / "cfg.json")
    cfg = {"cluster_id": 1, "ssh": {"4": dict(TARGET)}}
    out = worker.apply_set_ssh(FakeConn(BOARDS), cfg, "4=other:/srv/new")
    assert out["ssh"]["4"] == {"target": "other", "directory": "/srv/new"}


# ---------- reading the config back ----------

def test_resolve_ssh_finds_the_boards_target():
    cfg = {"ssh": {"4": dict(TARGET)}}
    assert worker.resolve_ssh({"id": 4}, cfg) == TARGET


def test_resolve_ssh_is_none_for_a_local_board():
    """None is what keeps every other board on the existing local path — the
    ssh branch is opt-in per board."""
    assert worker.resolve_ssh({"id": 7}, {"ssh": {"4": dict(TARGET)}}) is None
    assert worker.resolve_ssh({"id": 4}, {}) is None
    assert worker.resolve_ssh(None, {}) is None


@pytest.mark.parametrize("entry", [
    {"target": "studio"},                      # no directory
    {"directory": "/srv/repo"},                # no target
    {"target": "", "directory": "/srv/repo"},  # hand-edited to empty
])
def test_resolve_ssh_ignores_a_half_configured_entry(entry):
    """A hand-edited config missing either half is not a usable target;
    falling back to a local run is what the PC would have done without the
    entry at all."""
    assert worker.resolve_ssh({"id": 4}, {"ssh": {"4": entry}}) is None


def test_ssh_boards_are_claimable():
    """The claim query filters on the boards this PC can work. Without this
    an ssh-only board would never be polled for."""
    assert worker.configured_board_ids({"ssh": {"4": dict(TARGET)}}) == [4]


def test_ssh_and_local_boards_are_both_claimable_without_duplicates():
    cfg = {"boards": {"4": "/a", "7": "/b"}, "ssh": {"4": dict(TARGET)}}
    assert sorted(worker.configured_board_ids(cfg)) == [4, 7]
    assert len(worker.configured_board_ids(cfg)) == 2


def test_configured_board_ids_skips_junk_ssh_keys():
    cfg = {"ssh": {"4": dict(TARGET), "oops": dict(TARGET)}}
    assert worker.configured_board_ids(cfg) == [4]


def test_board_ssh_targets_defaults_to_empty():
    assert worker.board_ssh_targets({}) == {}


# ---------- the executor over ssh ----------

TICKET = {"id": 3, "title": "Do it", "body": "Details.", "attempts": 1}
BOARD = {"name": "site-page", "description": "The site.", "out_of_scope": None,
         "commit_requirements": None, "use_worktrees": False}


class _FakeStdout:
    def __init__(self, lines):
        self._lines = list(lines)
        self._i = 0

    def __iter__(self):
        return self

    def __next__(self):
        if self._i < len(self._lines):
            line = self._lines[self._i]
            self._i += 1
            return line
        raise StopIteration

    def close(self):
        pass


class _CapturingStdin:
    def __init__(self):
        self._buf = []
        self.closed = False

    def write(self, data):
        self._buf.append(data)

    def flush(self):
        pass

    def close(self):
        self.closed = True

    def getvalue(self):
        return "".join(self._buf)


class FakeProc:
    def __init__(self, lines=(), returncode=0):
        self.stdout = _FakeStdout(lines)
        self.stdin = _CapturingStdin()
        self.returncode = returncode

    def wait(self):
        return self.returncode

    def kill(self):
        pass

    def terminate(self):
        pass


def result_line(text):
    return json.dumps({"type": "result", "result": text}) + "\n"


def fake_popen(proc, captured):
    def _popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return proc
    return _popen


@pytest.fixture()
def ssh_on_path(monkeypatch):
    """Both binaries resolvable, so a test can tell "took the ssh branch"
    apart from "could not find ssh"."""
    monkeypatch.setattr(worker.shutil, "which",
                        lambda name: f"/usr/bin/{name}")


def test_executor_runs_the_cli_on_the_target(monkeypatch, ssh_on_path):
    captured = {}
    monkeypatch.setattr(subprocess, "Popen",
                        fake_popen(FakeProc([result_line("done")]), captured))
    ok, out = worker.ClaudeExecutor().run(
        TICKET, board=BOARD, directory="/srv/site-page", session_id="sid-1",
        ssh=TARGET)
    assert (ok, out) == (True, "done")
    cmd = captured["cmd"]
    assert cmd[0] == "/usr/bin/ssh"
    assert cmd[-2] == "ryan@studio"
    assert cmd[-1].startswith("cd /srv/site-page && exec claude ")


def test_executor_does_not_chdir_locally_for_a_remote_run(monkeypatch, ssh_on_path):
    """`directory` is a path on the far end. Handing it to Popen's cwd would
    raise here (the folder is on another machine), and cd'ing into it is the
    remote shell's job anyway."""
    captured = {}
    monkeypatch.setattr(subprocess, "Popen",
                        fake_popen(FakeProc([result_line("done")]), captured))
    worker.ClaudeExecutor().run(TICKET, board=BOARD, directory="/srv/site-page",
                                session_id="s", ssh=TARGET)
    assert captured["kwargs"]["cwd"] is None


def test_executor_does_not_stat_the_remote_directory(monkeypatch, ssh_on_path):
    """A local run refuses a directory that does not exist; over ssh that same
    check would reject every correct configuration, since the folder is on
    another machine."""
    captured = {}
    monkeypatch.setattr(subprocess, "Popen",
                        fake_popen(FakeProc([result_line("done")]), captured))
    ok, _out = worker.ClaudeExecutor().run(
        TICKET, board=BOARD, directory="/definitely/not/here",
        session_id="s", ssh=TARGET)
    assert ok is True


def test_a_remote_run_still_streams_and_still_mints_a_session(monkeypatch, ssh_on_path):
    """The point of the ticket: ssh is a transparent pipe, so the transcript
    the browser live-tails is what a local run would have produced, and the
    session id is recorded for a later --resume."""
    captured = {}
    proc = FakeProc([
        json.dumps({"type": "assistant",
                    "message": {"content": [{"type": "text",
                                             "text": "Reading the ticket."}]}}) + "\n",
        result_line("done"),
    ])
    monkeypatch.setattr(subprocess, "Popen", fake_popen(proc, captured))
    seen = []
    worker.ClaudeExecutor().run(TICKET, board=BOARD, directory="/srv/site-page",
                                session_id="sid-1", ssh=TARGET,
                                log_cb=lambda *a, **k: seen.append(a))
    remote = captured["cmd"][-1]
    assert "--session-id sid-1" in remote
    assert "--output-format stream-json" in remote
    assert seen, "the remote run produced no live log lines"


def test_the_prompt_still_travels_on_stdin_not_the_remote_argv(monkeypatch, ssh_on_path):
    """The multi-line prompt goes over the same stdin pipe as a local run —
    ssh forwards it — so it never has to survive a round of shell quoting."""
    captured = {}
    proc = FakeProc([result_line("done")])
    monkeypatch.setattr(subprocess, "Popen", fake_popen(proc, captured))
    worker.ClaudeExecutor().run(TICKET, board=BOARD, directory="/srv/site-page",
                                session_id="s", ssh=TARGET)
    assert "Details." not in captured["cmd"][-1]
    assert "Details." in proc.stdin.getvalue()


def test_a_remote_run_without_a_local_ssh_client_fails_with_advice(monkeypatch):
    captured = {}
    monkeypatch.setattr(worker.shutil, "which", lambda name: None)
    monkeypatch.setattr(subprocess, "Popen",
                        fake_popen(FakeProc([result_line("done")]), captured))
    ok, comment = worker.ClaudeExecutor().run(
        TICKET, board=BOARD, directory="/srv/site-page", session_id="s",
        ssh=TARGET)
    assert ok is False
    assert "ssh" in comment and "ryan@studio" in comment
    assert "cmd" not in captured, "must not launch anything without an ssh client"


def test_command_not_found_on_the_far_end_is_explained(monkeypatch, ssh_on_path):
    """Exit 127 arrives with an empty transcript, so the diagnosis has to come
    from the exit code or it comes from nowhere."""
    captured = {}
    monkeypatch.setattr(
        subprocess, "Popen",
        fake_popen(FakeProc([], returncode=worker.SSH_COMMAND_NOT_FOUND), captured))
    ok, comment = worker.ClaudeExecutor().run(
        TICKET, board=BOARD, directory="/srv/site-page", session_id="s",
        ssh=TARGET)
    assert ok is False
    assert "PATH" in comment


def test_a_local_run_gets_no_ssh_hint(monkeypatch, tmp_path):
    """127 from a local CLI means something else entirely; the ssh advice
    would be a wrong guess."""
    captured = {}
    monkeypatch.setattr(worker.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(subprocess, "Popen",
                        fake_popen(FakeProc([], returncode=127), captured))
    ok, comment = worker.ClaudeExecutor().run(
        TICKET, board=BOARD, directory=str(tmp_path), session_id="s")
    assert ok is False
    assert "non-interactive" not in comment


# ---------- git over ssh ----------

class FakeSubprocessRun:
    def __init__(self, outputs=None):
        self.outputs = outputs or {}
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        # the git subcommand, wherever it sits: local argv or remote string
        key = next((s for s in ("rev-parse", "push", "status")
                    if any(s in str(part) for part in cmd)), None)
        returncode, stdout, stderr = self.outputs.get(key, (0, "", ""))
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout,
                                           stderr=stderr)


def test_git_runs_on_the_machine_that_holds_the_checkout(monkeypatch):
    """The remote board's repo is over there; running git here would at best
    address a path this PC does not have."""
    fake = FakeSubprocessRun()
    monkeypatch.setattr(subprocess, "run", fake)
    worker._run_git(["status"], cwd="/srv/site-page", ssh=TARGET)
    cmd, kwargs = fake.calls[0]
    assert cmd[0] == "ssh"
    assert cmd[-2] == "ryan@studio"
    assert cmd[-1] == ("cd /srv/site-page && exec env GIT_TERMINAL_PROMPT=0 "
                       "git status")
    assert kwargs["cwd"] is None, "a remote path must not be a local chdir"


def test_remote_git_still_cannot_prompt():
    """Same reasoning as the local path's GIT_TERMINAL_PROMPT=0: a credential
    prompt on the far end would hang the slot with nobody able to answer."""
    remote = worker.build_remote_shell("/srv/repo", ["git", "push"],
                                       env={"GIT_TERMINAL_PROMPT": "0"})
    assert "GIT_TERMINAL_PROMPT=0" in remote


def test_push_sends_both_git_commands_to_the_target(monkeypatch):
    """Push auth stays ambient to whoever owns the repo — the far end's own
    git credentials — exactly as it is for a local board."""
    fake = FakeSubprocessRun()
    monkeypatch.setattr(subprocess, "run", fake)
    pushed, note = worker.push_ticket_branch("/srv/site-page", "5-my-ticket",
                                             ssh=TARGET)
    assert pushed is True
    assert len(fake.calls) == 2
    for cmd, _kwargs in fake.calls:
        assert cmd[0] == "ssh" and cmd[-2] == "ryan@studio"
    assert "git push origin 5-my-ticket" in fake.calls[1][0][-1]


def test_a_remote_push_failure_is_reported_not_raised(monkeypatch):
    fake = FakeSubprocessRun(outputs={"push": (1, "", "fatal: no upstream")})
    monkeypatch.setattr(subprocess, "run", fake)
    pushed, note = worker.push_ticket_branch("/srv/site-page", "5-my-ticket",
                                             ssh=TARGET)
    assert pushed is False
    assert "no upstream" in note


def test_resolve_push_names_the_target_when_auto_push_is_off(monkeypatch):
    """The note tells a human where the unpushed branch actually is. "on the
    worker PC" would send them to the wrong machine."""
    monkeypatch.setattr(subprocess, "run", FakeSubprocessRun())
    ticket = {"id": 5, "board_id": 6, "title": "My Ticket", "attempts": 1}
    pushed, note = worker.resolve_push({"auto_push": False}, ticket,
                                       "/srv/site-page", None, ssh=TARGET)
    assert pushed is False
    assert "ryan@studio" in note


def test_resolve_push_passes_the_target_through_to_git(monkeypatch):
    calls = []
    monkeypatch.setattr(worker, "push_ticket_branch",
                        lambda d, b, ssh=None: calls.append(ssh) or (True, ""))
    ticket = {"id": 5, "board_id": 6, "title": "My Ticket", "attempts": 1}
    worker.resolve_push({"auto_push": True}, ticket, "/srv/site-page", None,
                        ssh=TARGET)
    assert calls == [TARGET]


# ---------- recording where the session ran ----------

class RecordingCursor:
    def __init__(self, calls):
        self.calls = calls

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class RecordingConn:
    def __init__(self):
        self.calls = []

    def cursor(self):
        return RecordingCursor(self.calls)

    def transaction(self):
        class _T:
            def __enter__(self_):
                return self_

            def __exit__(self_, *a):
                return False

        return _T()


def test_record_session_dir_stores_the_host_for_a_remote_run():
    """Without the host the board would hand a human `cd /srv/site-page` for a
    folder that does not exist on their PC."""
    conn = RecordingConn()
    worker.record_session_dir(conn, 42, "/srv/site-page", host="ryan@studio")
    _sql, params = conn.calls[0]
    assert params == ("/srv/site-page", "ryan@studio", 42)


def test_record_session_dir_blanks_the_host_for_a_local_run():
    """Written on every call, not only when set: a board moved back off ssh
    would otherwise keep aiming the takeover command at a machine its
    transcript is no longer on."""
    conn = RecordingConn()
    worker.record_session_dir(conn, 42, r"C:\repos\site-page")
    _sql, params = conn.calls[0]
    assert params == (r"C:\repos\site-page", None, 42)


# ---------- end to end through run_slot ----------

WORK = {"assignment_id": 1, "session_id": "sess-1",
        "board": {"id": 1, "name": "site-page", "auto_push": False},
        "ticket": {"id": 9, "board_id": 1, "title": "T", "body": "b",
                   "status": "doing", "attempts": 1}}


class QuietExecutor:
    def __init__(self):
        self.seen = {}

    def run(self, ticket, board=None, directory=None, session_id=None,
            progress_cb=None, should_kill=None, chat_source=None,
            chat_delivered=None, log_cb=None, profile=None, resume=None,
            ssh=None):
        self.seen["directory"] = directory
        self.seen["ssh"] = ssh
        return True, "done"


def _run_one_slot(monkeypatch, tmp_path, cfg_extra):
    monkeypatch.setattr(worker, "CONFIG_PATH", tmp_path / "cfg.json")
    claims = iter([WORK])
    recorded = []

    class C:
        closed = False

        def close(self):
            pass

    monkeypatch.setattr(worker.psycopg, "connect", lambda dsn, **kw: C())
    monkeypatch.setattr(worker, "claim_next", lambda *a, **k: next(claims, None))
    monkeypatch.setattr(worker, "finish_work", lambda *a, **k: "done")
    monkeypatch.setattr(worker, "set_slot_counts", lambda *a, **k: None)
    monkeypatch.setattr(worker, "heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(worker, "resolve_push", lambda *a, **k: (True, ""))
    monkeypatch.setattr(worker, "fetch_pending_chat", lambda *a, **k: [])
    monkeypatch.setattr(worker, "_claim_heartbeat_loop", lambda *a, **k: None)
    monkeypatch.setattr(
        worker, "record_session_dir",
        lambda conn, tid, d, host=None: recorded.append((tid, d, host)))

    def no_local_resolve(board, cfg):
        raise AssertionError("a remote board has no local checkout to resolve")

    monkeypatch.setattr(worker, "resolve_directory", no_local_resolve)

    cfg = {"dsn": "x", "worker_id": 1, "cluster_id": 1, "name": "pc",
           **cfg_extra}
    args = worker.build_parser().parse_args(["--poll", "0.01"])
    executor = QuietExecutor()
    stop = threading.Event()
    t = threading.Thread(target=worker.run_slot,
                         args=(cfg, args, executor, stop, 0))
    t.start()
    import time
    time.sleep(0.3)
    stop.set()
    t.join(timeout=5)
    return executor, recorded


def test_a_remote_board_skips_local_resolution_and_runs_over_ssh(monkeypatch, tmp_path):
    """A board pointed at a target has no local checkout to clone, refresh or
    even stat — the configured remote directory is used exactly as an explicit
    --set-path is."""
    executor, _recorded = _run_one_slot(
        monkeypatch, tmp_path, {"ssh": {"1": dict(TARGET)}})
    assert executor.seen["ssh"] == TARGET
    assert executor.seen["directory"] == "/srv/site-page"


def test_a_remote_run_records_the_host_it_ran_on(monkeypatch, tmp_path):
    executor, recorded = _run_one_slot(
        monkeypatch, tmp_path, {"ssh": {"1": dict(TARGET)}})
    assert recorded == [(9, "/srv/site-page", "ryan@studio")]
