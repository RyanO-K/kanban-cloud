"""kanban-cloud worker client (v2: direct Postgres).

One-time enrollment over HTTP issues this PC its own database role; after
that the worker never contacts the web service — polling, claiming,
progress, results, and heartbeats are SQL against Neon.

Client PCs (packaged exe): download kanban-worker.exe from the latest
worker-v* GitHub Release, put it in its own folder, and run it — on first
run it asks for the cluster join code, then starts polling. Real ticket
execution shells out to the `claude` CLI, which must be installed
separately and already authenticated on this PC (`claude login`, or your
own ANTHROPIC_API_KEY) — the cluster does not store or forward a key.

Dev / script setup (once per PC):
    pip install "psycopg[binary]"
    py worker.py --enroll --join-code ABC12345 --name ryans-pc

    # against a local dev server (http://localhost:8900) instead of prod:
    py worker.py --enroll --join-code ABC12345 --name ryans-pc --test

Run:
    py worker.py            # real executor (Claude CLI)
    py worker.py --stub     # stub executor for testing

Note: while this worker runs, its polling keeps Neon compute awake
(free tier autosuspends only when idle). Stop the worker when not in use.
"""
import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

try:
    import psycopg
except ImportError as exc:
    sys.exit(
        "Missing Postgres driver: no working psycopg backend is installed "
        f"({exc}).\nFix: pip install \"psycopg[binary]\"\n"
        'Then re-run: py worker.py --enroll --join-code ABC12345'
    )

from app.prompt import (
    build_agent_prompt,
    parse_commit_gate,
    parse_question,
    ticket_branch_name,
)
from app.triage import build_triage_prompt, parse_triage_result

DEFAULT_SERVER = "https://kanban-cloud.onrender.com"
TEST_SERVER = "http://localhost:8900"

# The local .kanban tool's `default` profile list. Without an explicit grant a
# headless `claude -p` cannot get permission to edit a file, so an agent with
# no tools looks like it ran and silently changed nothing.
DEFAULT_ALLOWED_TOOLS = "Read,Edit,Write,Bash,Grep,Glob"


def app_dir() -> Path:
    """Directory the worker lives in: next to the exe when frozen by
    PyInstaller (whose __file__ points into a temp dir deleted after each
    run), next to the script otherwise."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


CONFIG_PATH = app_dir() / ".worker_config.json"
POLL_SECONDS = 10
MAX_ATTEMPTS = 2  # keep in sync with app/models.py MAX_ATTEMPTS

# How often a slot touches work_queue.heartbeat_at while its claim is in
# flight, and how stale a heartbeat has to be before the reaper gives up on
# it. 5x the heartbeat interval is the same margin models.Worker.is_online
# gives WORKER_ONLINE_SECONDS over the poll interval — generous enough that a
# GC pause or a slow DB round-trip never gets a live claim reaped, but tight
# enough that a PC that died mid-ticket doesn't block re-delegation for long.
HEARTBEAT_INTERVAL_SECONDS = 60
STALE_CLAIM_SECONDS = 300

UTC_NOW = "(now() at time zone 'utc')"

# A queued ticket is eligible only once every dependency it has is done or in
# review — matching the local .kanban tool's `_dep_met_fn`. Plain ANSI SQL
# (no Postgres-only syntax), unlike the rest of CLAIM_SQL, so it also runs
# unmodified against SQLite in tests (see tests/test_worker.py) — the two
# places this predicate can drift apart from CLAIM_SQL are guarded by that
# test importing this exact constant rather than re-typing it.
DEPS_MET_SQL = """NOT EXISTS (
         SELECT 1 FROM ticket_deps td
         JOIN tickets dep ON dep.id = td.depends_on_id
         WHERE td.ticket_id = t.id AND dep.status NOT IN ('done', 'review'))"""

# Atomic, race-safe claim: SKIP LOCKED means concurrent workers never block
# or double-claim; the subquery orders by ticket rank ahead of queue age and
# honors target_worker. The board filter keeps a PC from claiming work it
# cannot do: a ticket is claimable here when its board either has a
# --set-path entry configured on this PC (in %(boards)s), or the board itself
# has a repo_url set, in which case resolve_directory() can clone/refresh it
# on demand — no --set-path needed. A NULL %(boards)s disables the
# configured-boards half entirely, which is how --stub (no repo needed) opts
# out and still claims everything. The dependency predicate is enforced here
# rather than in Python: a Python check would have to claim the row first and
# then abandon it, and it would not hold across N independent workers racing
# the same queue.
#
# t."order" lets a human rank a ticket ahead of others via UI drag (lower
# claims first); most tickets are never dragged and share the default 0, so
# queued_at (then id) is the deterministic tie-break. Shared as its own
# constant so tests can run this exact ORDER BY against the SQLite test DB —
# the rest of CLAIM_SQL (SKIP LOCKED, ::int[] casts) is Postgres-only and
# can't run there.
CLAIM_ORDER_BY = 't."order", wq.queued_at, wq.id'

CLAIM_SQL = f"""
UPDATE work_queue SET status='claimed', claimed_by=%(wid)s, claimed_at={UTC_NOW},
    heartbeat_at={UTC_NOW}
WHERE id = (
  SELECT wq.id FROM work_queue wq
  JOIN tickets t ON t.id = wq.ticket_id
  WHERE wq.status='queued' AND wq.cluster_id=%(cid)s
    AND (t.target_worker IS NULL OR t.target_worker = %(wid)s)
    AND (%(boards)s::int[] IS NULL
         OR t.board_id = ANY(%(boards)s::int[])
         OR EXISTS (SELECT 1 FROM boards b
                    WHERE b.id = t.board_id AND COALESCE(b.repo_url, '') <> ''))
    AND {DEPS_MET_SQL}
  ORDER BY {CLAIM_ORDER_BY}
  FOR UPDATE OF wq SKIP LOCKED
  LIMIT 1
)
RETURNING id, ticket_id
"""


# ---------- executors ----------

class KilledByRequest(RuntimeError):
    """Raised by an executor when it terminates its own child process because
    work_queue.kill_requested was set for the in-flight claim. Distinct from
    a genuine executor failure so run_slot can report it as such."""


# How often a running agent's Popen is polled for exit and for a kill
# request. Small enough to feel responsive; large enough not to hammer the
# DB with a SELECT every run of the loop.
KILL_POLL_SECONDS = 2

AGENT_TIMEOUT_SECONDS = 1800


def _terminate_process_tree(proc: subprocess.Popen) -> None:
    """Kill the CLI and any child processes it spawned. A plain terminate()
    only signals the immediate child; the Claude CLI's own subprocesses (the
    tools it shells out to) would otherwise survive it."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       capture_output=True)
    else:
        proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


class StubExecutor:
    """Fake executor: waits a moment and produces a canned result."""

    name = "stub"

    def run(self, ticket, board=None, directory=None, session_id=None,
            progress_cb=None, should_kill=None,
            chat_source=None, chat_delivered=None, log_cb=None, profile=None):
        print(f"  [stub] pretending to work on ticket #{ticket['id']}: {ticket['title']}")
        time.sleep(2)
        return True, (
            f"[StubExecutor] Completed ticket '{ticket['title']}' (attempt "
            f"{ticket.get('attempts', '?')}). This is a placeholder result — "
            "run the worker without --stub to execute via the Claude CLI."
        )


# ---------- stream-json progress parsing ----------
#
# `--output-format stream-json` (the same format the local orchestrator's log
# viewer already parses) emits one JSON object per line: `assistant` turns
# (text and tool_use blocks) and a final `result` summary. `system` init and
# `user` tool-result echoes carry nothing worth surfacing in a progress feed
# and are dropped.

def _stream_json_text(raw_line: str):
    """One human-readable progress line from a raw stream-json line, or
    None if it has nothing worth showing."""
    raw_line = raw_line.strip()
    if not raw_line:
        return None
    try:
        obj = json.loads(raw_line)
    except (json.JSONDecodeError, TypeError):
        return raw_line
    kind = obj.get("type")
    if kind == "assistant":
        parts = []
        for block in (obj.get("message") or {}).get("content") or []:
            if block.get("type") == "text":
                text = (block.get("text") or "").strip()
                if text:
                    parts.append(text)
            elif block.get("type") == "tool_use":
                parts.append(f"-> {block.get('name', 'tool')}")
        return "\n".join(parts) or None
    if kind == "result":
        return (obj.get("result") or "").strip() or None
    return None


def _render_stream_lines(raw_lines) -> str:
    rendered = [t for t in (_stream_json_text(l) for l in raw_lines) if t]
    return "\n".join(rendered).strip()


# How much of one rendered turn's text is kept in a `ticket_log` row. A tool
# result can be arbitrarily large (a big file read/grep dump); this bounds it
# the same way the local `.kanban` tool bounds a tool-result preview, so one
# chatty turn cannot blow up the live-log table.
_LOG_LINE_MAX_CHARS = 20000


def _stream_json_turn(raw_line: str):
    """(role, text) for one raw stream-json line, for the live `ticket_log`
    stream — role is the CLI's own event `type` (assistant/result), or "raw"
    for a line that didn't parse as JSON at all. Returns (None, None) for a
    line with nothing worth logging (same lines `_stream_json_text` drops)."""
    text = _stream_json_text(raw_line)
    if text is None:
        return None, None
    raw_line = raw_line.strip()
    try:
        role = json.loads(raw_line).get("type") or "raw"
    except (json.JSONDecodeError, TypeError):
        role = "raw"
    if len(text) > _LOG_LINE_MAX_CHARS:
        text = text[:_LOG_LINE_MAX_CHARS] + "\n…(truncated)"
    return role, text


# ---------- agent chat: pump queued messages onto the CLI's live stdin ----------
#
# `--input-format stream-json` keeps the CLI's stdin open for additional user
# turns for as long as the process is alive. `chat_encode`/`chat_pump`/
# `chat_close` mirror the local orchestrator's `_chat_pump` helpers: encode a
# typed message as one stream-json user-turn line, write pending ones in
# order, and close stdin without letting an already-dead process's broken
# pipe surface as an error. They operate on a bare writable object rather
# than a Popen, so they're exercised directly with no subprocess involved.

def chat_encode(message: str) -> str:
    """One stream-json user-turn line for `message` (no trailing newline)."""
    return json.dumps({
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": message}]},
    })


def chat_pump(stdin, pending) -> list:
    """Write each `(id, message)` in `pending` to `stdin`, in order, as one
    stream-json line. Returns the ids actually written.

    Stops at the first failed write instead of raising: a closed/broken pipe
    means the process is already gone, and every message after it would fail
    the same way. An id left off the returned list stays unmarked as
    delivered by the caller, so the message survives (in `ticket_chat`,
    unlike the local tool's JSONL file) for a later run.
    """
    sent = []
    for chat_id, message in pending:
        try:
            stdin.write(chat_encode(message) + "\n")
            stdin.flush()
        except (BrokenPipeError, ValueError, OSError):
            break
        sent.append(chat_id)
    return sent


def chat_close(stdin) -> None:
    """Close the CLI's stdin. The common case is the process having already
    exited, which must not raise back into the caller."""
    try:
        stdin.close()
    except (BrokenPipeError, ValueError, OSError):
        pass


def _chat_pump_loop(stdin, chat_source, chat_delivered, stop_pump, poll_interval) -> None:
    """Runs on its own thread for the lifetime of one CLI process: polls
    `chat_source()` for newly queued messages and writes them to `stdin` in
    order via `chat_pump`, acknowledging each batch through `chat_delivered`.
    The first poll happens immediately, before any wait, so messages already
    queued when the process starts are not lost. Always closes `stdin` on
    the way out, however the loop ends, so the CLI sees a clean EOF instead
    of an abandoned pipe.
    """
    try:
        while not stop_pump.is_set():
            try:
                pending = list(chat_source() or [])
            except Exception as exc:
                print(f"  [claude] chat source failed: {exc!r}")
                pending = []
            if pending:
                sent = chat_pump(stdin, pending)
                if sent and chat_delivered:
                    try:
                        chat_delivered(sent)
                    except Exception as exc:
                        print(f"  [claude] chat delivery ack failed: {exc!r}")
                if len(sent) < len(pending):
                    break  # stdin is gone; nothing left to do
            stop_pump.wait(poll_interval)
    finally:
        chat_close(stdin)


class ClaudeExecutor:
    """Real executor: runs the Claude CLI inside the board's checkout.

    The CLI authenticates from whatever local configuration this PC already
    has — a `claude login` session, or the operator's own ANTHROPIC_API_KEY.
    The cluster neither stores nor forwards a key.
    """

    name = "claude"

    def __init__(self, allowed_tools: str = DEFAULT_ALLOWED_TOOLS, progress_batch: int = 4,
                chat_poll_seconds: float = 0.5):
        self.allowed_tools = allowed_tools
        # How many stream-json turns to buffer before flushing a progress_cb
        # call — batches a chatty run into a handful of comments instead of
        # one per line.
        self.progress_batch = max(1, progress_batch)
        # How often the chat pump thread checks for newly queued messages.
        self.chat_poll_seconds = chat_poll_seconds

    def run(self, ticket, board=None, directory=None, session_id=None,
            progress_cb=None, should_kill=None,
            chat_source=None, chat_delivered=None, log_cb=None, profile=None):
        board = board or {}
        name = board.get("name", "?")
        should_kill = should_kill or (lambda: False)
        if not directory:
            return False, (
                f"This PC has no folder configured for board '{name}'. Set one "
                f"with: kanban-worker --set-path \"{name}=<path to the repo>\""
            )
        if not Path(directory).is_dir():
            return False, (
                f"The configured folder for board '{name}' no longer exists: "
                f"{directory}. Fix it with --set-path."
            )

        # Resolve the CLI ourselves rather than passing shell=True. On Windows
        # shell=True re-parses the argument list through cmd.exe, which cuts the
        # multi-line prompt off at its first newline — the agent then saw the
        # title and none of the ticket body. shutil.which finds the .CMD/.EXE
        # shim that shell=True was there for.
        exe = shutil.which("claude")
        if not exe:
            return False, "`claude` CLI not found on this PC's PATH."

        # profile (resolved by worker.resolve_profile: ticket beats board)
        # supplies the tool allowlist, model and system prompt for this run.
        # A falsy allowed_tools (no profile, or a profile with an empty
        # list — should not happen given the column is NOT NULL, but never
        # trust it blindly) falls back to this executor's own default rather
        # than ever launching the CLI with no tool grant at all.
        profile = profile or {}
        allowed_tools = profile.get("allowed_tools") or self.allowed_tools
        model = profile.get("model")
        system_prompt = profile.get("system_prompt")

        prompt = build_agent_prompt(ticket, board, directory)
        cmd = [exe, "-p", prompt, "--allowedTools", allowed_tools,
               "--input-format", "stream-json",
               "--output-format", "stream-json", "--verbose"]
        if session_id:
            cmd += ["--session-id", session_id]
        if model:
            cmd += ["--model", model]
        if system_prompt:
            cmd += ["--append-system-prompt", system_prompt]
        print(f"  [claude] running in {directory} for ticket #{ticket['id']}")

        def emit(text):
            if text and progress_cb:
                try:
                    progress_cb(text)
                except Exception as exc:
                    print(f"  [claude] progress callback failed: {exc!r}")

        try:
            # what makes streaming possible at all. Reading happens on a
            # background thread so this method can still poll should_kill()
            # and the overall timeout while a chatty (or silent) child is
            # in-flight — a plain blocking `for raw_line in proc.stdout`
            # can't be interrupted once it's waiting on the pipe. stdin is a
            # pipe too — --input-format stream-json accepts further user
            # turns on it for as long as the process is alive, which is what
            # lets chat messages reach a running agent at all.
            proc = subprocess.Popen(cmd, cwd=directory, stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, bufsize=1)
        except FileNotFoundError:
            return False, "`claude` CLI not found on this PC's PATH."

        # Best-effort local copy of the raw stream, mirroring the local
        # `.kanban` tool's per-run log file. Never fatal to the run: a full
        # disk or unwritable folder just means this PC has no local copy —
        # the durable, browser-visible transcript is `ticket_log` (log_cb
        # below), not this file.
        log_file = None
        try:
            logs_dir = app_data_logs_dir()
            logs_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            log_file = open(logs_dir / f"{ticket['id']}-{ts}.log", "w", encoding="utf-8")
        except OSError as exc:
            print(f"  [claude] could not open local run log: {exc!r}")

        def write_log(raw_line):
            if log_file is not None:
                try:
                    log_file.write(raw_line)
                    log_file.flush()
                except OSError:
                    pass

        stop_pump = threading.Event()
        pump_thread = None
        if chat_source is not None:
            pump_thread = threading.Thread(
                target=_chat_pump_loop,
                args=(proc.stdin, chat_source, chat_delivered, stop_pump,
                      self.chat_poll_seconds),
                daemon=True,
            )
            pump_thread.start()
        else:
            # Nothing will ever write here — close it up front so the CLI
            # sees EOF instead of a pipe held open for no reason.
            chat_close(proc.stdin)

        seen_lines = []
        pending = []
        reader_errors = []

        def _read_stream():
            try:
                for raw_line in proc.stdout:
                    seen_lines.append(raw_line)
                    write_log(raw_line)
                    role, text = _stream_json_turn(raw_line)
                    if text:
                        pending.append(text)
                        if log_cb:
                            try:
                                log_cb(role, text)
                            except Exception as exc:
                                print(f"  [claude] log callback failed: {exc!r}")
                        if len(pending) >= self.progress_batch:
                            emit("\n".join(pending))
                            pending.clear()
            except Exception as exc:
                reader_errors.append(exc)

        reader = threading.Thread(target=_read_stream, daemon=True)
        reader.start()

        # Polled (not a single blocking join) so should_kill() and the
        # timeout both get a chance to fire while the child is still running;
        # reader.join with a short timeout is the same live-poll shape
        # ClaudeExecutor uses to wait on the subprocess itself.
        def _stop_pump_and_close_stdout():
            stop_pump.set()
            if pump_thread is not None:
                pump_thread.join(timeout=5)
            try:
                proc.stdout.close()
            except Exception:
                pass
            if log_file is not None:
                try:
                    log_file.close()
                except OSError:
                    pass

        deadline = time.monotonic() + AGENT_TIMEOUT_SECONDS
        while reader.is_alive():
            reader.join(timeout=KILL_POLL_SECONDS)
            if not reader.is_alive():
                break
            # Only ever consulted while still waiting on the child: once it
            # has exited above, a kill_requested flag that arrives too late
            # to matter is never even read, let alone acted on.
            if should_kill():
                emit("\n".join(pending))
                _terminate_process_tree(proc)
                reader.join(timeout=5)
                _stop_pump_and_close_stdout()
                raise KilledByRequest("Killed by request.")
            if time.monotonic() >= deadline:
                emit("\n".join(pending))
                _terminate_process_tree(proc)
                reader.join(timeout=5)
                _stop_pump_and_close_stdout()
                return False, "Claude CLI timed out after 30 minutes."

        _stop_pump_and_close_stdout()

        if reader_errors:
            emit("\n".join(pending))
            try:
                proc.kill()
                proc.wait()
            except Exception:
                pass
            partial = _render_stream_lines(seen_lines)
            crash_msg = f"Claude CLI crashed while streaming output: {reader_errors[0]!r}"
            comment = f"{crash_msg}\n\n{partial}" if partial else crash_msg
            return False, comment[:10000]

        emit("\n".join(pending))
        returncode = proc.wait()
        output = _render_stream_lines(seen_lines)
        if returncode != 0:
            return False, f"Claude CLI exited {returncode}: {output[:2000]}"
        return True, output[:10000] or "(no output)"


# ---------- config & enrollment ----------

def load_config() -> dict | None:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return None


def save_config(cfg: dict) -> None:
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    except OSError as e:
        print(f"Cannot write config at {CONFIG_PATH}: {e}\n"
              "Move the exe to a writable folder and run it again.")
        raise
    print(f"Saved worker config to {CONFIG_PATH}")


# ---------- per-PC board paths ----------
#
# Which folder a board's code lives in is this machine's business: the same
# board is worked by several PCs with different layouts, so the mapping lives
# here rather than in the database.

def board_paths(cfg: dict) -> dict:
    """{board_id_as_str: absolute path} for the boards this PC can work."""
    return cfg.get("boards") or {}


def configured_board_ids(cfg: dict) -> list:
    """Board ids this PC has a checkout for, as ints for the claim query."""
    out = []
    for key in board_paths(cfg):
        try:
            out.append(int(key))
        except (TypeError, ValueError):
            continue  # a hand-edited config should not stop the worker
    return out


def list_cluster_boards(conn, cluster_id: int) -> list:
    with conn.cursor() as cur:
        cur.execute("SELECT id, name FROM boards WHERE cluster_id=%s ORDER BY id",
                    (cluster_id,))
        return [{"id": r[0], "name": r[1]} for r in cur.fetchall()]


def resolve_board(conn, cluster_id: int, token: str):
    """Map a board id or name to (id, name). Names are case-insensitive.

    A name that matches no board, or more than one, is rejected rather than
    guessed — picking one silently would point an agent at the wrong repo.
    """
    rows = [(b["id"], b["name"]) for b in list_cluster_boards(conn, cluster_id)]
    token = (token or "").strip()
    if token.isdigit():
        for bid, name in rows:
            if bid == int(token):
                return bid, name
        raise ValueError(f"no board with id {token} in this cluster")
    matches = [r for r in rows if r[1].lower() == token.lower()]
    if len(matches) > 1:
        raise ValueError(f"'{token}' matches {len(matches)} boards; use the id instead")
    if not matches:
        known = ", ".join(f"{b}:{n}" for b, n in rows) or "(none)"
        raise ValueError(f"no board named '{token}'. Known boards: {known}")
    return matches[0]


def parse_set_path(arg: str):
    """Split a --set-path value into (board token, path) on the FIRST '='.

    Splitting on the first one keeps Windows paths containing '=' intact.
    """
    if "=" not in (arg or ""):
        raise ValueError("--set-path needs <board-id-or-name>=<path>")
    token, path = arg.split("=", 1)
    if not token.strip() or not path.strip():
        raise ValueError("--set-path needs <board-id-or-name>=<path>")
    return token.strip(), path.strip()


def apply_set_path(conn, cfg: dict, arg: str) -> dict:
    """Validate and record one board->path mapping; saves and returns cfg."""
    token, path = parse_set_path(arg)
    board_id, name = resolve_board(conn, cfg["cluster_id"], token)
    resolved = Path(path).expanduser()
    if not resolved.is_dir():
        raise ValueError(f"{resolved} does not exist (or is not a directory)")
    cfg.setdefault("boards", {})[str(board_id)] = str(resolved)
    save_config(cfg)
    print(f"Board {board_id} ({name}) -> {resolved}")
    return cfg


def prompt_for_board_paths(conn, cfg: dict) -> dict:
    """Walk the cluster's boards after enrollment, asking for a folder for each.

    Blank input skips a board, which simply means this PC never claims its
    tickets. Skipped entirely when stdin is not a terminal, so scripted and
    service runs never hang waiting for input.
    """
    if not (sys.stdin is not None and sys.stdin.isatty()):
        return cfg
    boards = list_cluster_boards(conn, cfg["cluster_id"])
    if not boards:
        return cfg
    print("\nWhich folder on this PC holds each board's code?")
    print("Press Enter to skip a board (this PC then never claims its tickets).")
    for b in boards:
        try:
            answer = input(f"  {b['name']}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not answer:
            continue
        try:
            apply_set_path(conn, cfg, f"{b['id']}={answer}")
        except ValueError as e:
            print(f"    skipped: {e}")
    return cfg


# ---------- auto-clone: repo_url as a fallback for --set-path ----------
#
# A board with a repo_url lets a worker with no manual path mapping still
# claim its tickets: the repo is cloned once into this PC's own AppData
# folder and refreshed to the default branch before every run. An explicit
# --set-path entry always wins and is never touched by anything below —
# that folder may be an operator's own hand-tended checkout.

def app_data_boards_dir() -> Path:
    """Where auto-cloned board repos live on this PC.

    Independent of app_dir() (wherever the exe/script happens to sit) so the
    clone survives moving or re-downloading the exe. Falls back to app_dir()
    if LOCALAPPDATA is somehow unset, rather than crashing.
    """
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else app_dir()
    return root / "kanban-worker" / "boards"


def app_data_logs_dir() -> Path:
    """Where each run's raw stream-json output is written on this PC — the
    local, best-effort copy of the live transcript (the durable, browser-
    visible copy lives in the `ticket_log` table; see add_log_line). Same
    LOCALAPPDATA convention as app_data_boards_dir, and independent of it —
    a board's checkout may be an operator's own hand-tended folder that
    should never gain stray log files."""
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else app_dir()
    return root / "kanban-worker" / "logs"


# A repo_url can carry embedded credentials (https://user:token@host/...).
# resolve_directory's errors get written verbatim into a ticket comment —
# visible to every cluster member and mirrored to Discord — so any URL that
# reaches a returned message must have its userinfo stripped first, whether
# it came from board config (repo_url/origin) or from git's own stderr,
# which can echo a credentialed URL back just as easily.
_CREDENTIALED_URL_RE = re.compile(r"://[^\s/@]+@")


def _redact_url(url: str) -> str:
    """Strip `user:pass@` (or `user:token@`) userinfo from a single URL."""
    if not url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return _redact_text(url)
    if parts.netloc and "@" in parts.netloc:
        parts = parts._replace(netloc=parts.netloc.rsplit("@", 1)[-1])
        return urlunsplit(parts)
    return url


def _redact_text(text: str) -> str:
    """Strip credentials from any URL(s) embedded in arbitrary text, e.g. raw
    git stderr, which is not necessarily just a bare URL."""
    if not text:
        return text
    return _CREDENTIALED_URL_RE.sub("://", text)


GIT_TIMEOUT_SECONDS = 600


def _run_git(args: list, cwd: str | None = None) -> subprocess.CompletedProcess:
    """Run one git subcommand with no chance of hanging the slot forever.

    A repo_url that needs credentials not ambiently available can otherwise
    make git prompt on inherited stdin and block indefinitely (same class of
    risk as ClaudeExecutor.run's own timeout above). stdin is closed and the
    usual interactive-prompt env vars are disabled so git fails fast instead
    of waiting on a prompt nobody can answer; a hard timeout is the backstop
    in case some git/credential-helper combination prompts anyway.
    """
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"}
    cmd = ["git", *args]
    try:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, env=env,
                              timeout=GIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            cmd, returncode=1,
            stdout="",
            stderr=f"git command timed out after {GIT_TIMEOUT_SECONDS}s",
        )


def resolve_directory(board: dict, cfg: dict) -> tuple:
    """The folder to run this board's tickets in, cloning/refreshing it if
    needed. Returns (directory, error) — exactly one is None.

    Order: an explicit --set-path entry always wins over repo_url, and is
    used as-is with no git commands run against it at all.
    """
    board = board or {}
    board_id = board.get("id")
    name = board.get("name", "?")

    explicit = board_paths(cfg).get(str(board_id))
    if explicit:
        return explicit, None

    repo_url = (board.get("repo_url") or "").strip()
    if not repo_url:
        return None, (
            f"This PC has no folder configured for board '{name}', and the "
            "board has no Repo URL set. Fix one: "
            f'kanban-worker --set-path "{name}=<path>", or set a Repo URL '
            "in the board's Project settings."
        )

    directory = app_data_boards_dir() / str(board_id)
    if not directory.exists():
        directory.parent.mkdir(parents=True, exist_ok=True)
        print(f"  [git] cloning {name}'s repo into {directory}")
        proc = _run_git(["clone", repo_url, str(directory)])
        if proc.returncode != 0:
            stderr = _redact_text((proc.stderr or proc.stdout).strip()[:2000])
            return None, f"git clone failed for board '{name}': {stderr}"
    else:
        proc = _run_git(["remote", "get-url", "origin"], cwd=str(directory))
        if proc.returncode != 0:
            stderr = _redact_text(proc.stderr.strip()[:500])
            return None, (
                f"Could not read the git remote for board '{name}' at "
                f"{directory}: {stderr}"
            )
        origin = proc.stdout.strip()
        if origin != repo_url:
            return None, (
                f"The Repo URL configured for board '{name}' "
                f"({_redact_url(repo_url)}) does not match this PC's "
                f"existing clone's origin ({_redact_url(origin)}) at "
                f"{directory}. If this is intentional, remove that folder "
                "by hand — nothing here does that automatically."
            )
        print(f"  [git] refreshing {name}'s checkout at {directory}")

    proc = _run_git(["fetch", "origin"], cwd=str(directory))
    if proc.returncode != 0:
        stderr = _redact_text(proc.stderr.strip()[:2000])
        return None, f"git fetch failed for board '{name}': {stderr}"

    proc = _run_git(["rev-parse", "--abbrev-ref", "origin/HEAD"], cwd=str(directory))
    if proc.returncode != 0:
        stderr = _redact_text(proc.stderr.strip()[:500])
        return None, f"Could not determine the default branch for board '{name}': {stderr}"
    default_branch = proc.stdout.strip().split("/", 1)[-1]

    # --force: a dirty working tree (leftover from a previous run) must not
    # make checkout refuse and permanently wedge this board — reset --hard
    # right after is what actually cleans the tree, but only runs if checkout
    # itself succeeds first. This directory is exclusively owned by this
    # feature's auto-clone (never an operator's hand-tended checkout), so
    # forcing past local changes here is safe.
    for args in (["checkout", "--force", default_branch],
                 ["reset", "--hard", f"origin/{default_branch}"],
                 ["clean", "-fd"]):
        proc = _run_git(args, cwd=str(directory))
        if proc.returncode != 0:
            stderr = _redact_text(proc.stderr.strip()[:2000])
            return None, f"git {' '.join(args)} failed for board '{name}': {stderr}"

    return str(directory), None


# ---------- push: land a finished ticket's branch on the remote ----------
#
# The agent is told (app/prompt.py) to commit but never push — "pushing is
# handled separately" — because push credentials are this PC's own ambient
# git auth and should never be something the agent's prompt has to reason
# about. This is the "separately": after a successful run, push whatever
# branch the agent committed so the work doesn't strand itself on this PC.

def push_ticket_branch(directory: str, branch: str) -> str | None:
    """Best-effort push of `branch` to origin. Returns a note to append to
    the ticket comment, or None when there is nothing to report (no such
    branch — the agent made no commits, e.g. a no-op ticket).

    Never fails the ticket: a push failure (no network, no credentials, a
    protected branch) is surfaced in the comment instead so a human can push
    by hand, same as today, rather than losing the run's result.
    """
    proc = _run_git(["rev-parse", "--verify", "--quiet", branch], cwd=directory)
    if proc.returncode != 0:
        return None
    proc = _run_git(["push", "origin", branch], cwd=directory)
    if proc.returncode != 0:
        stderr = _redact_text((proc.stderr or proc.stdout).strip()[:1000])
        return f"\n\n(Could not push branch `{branch}` to origin: {stderr})"
    return f"\n\n(Pushed branch `{branch}` to origin.)"


def enroll(server: str, join_code: str, name: str) -> dict:
    """One-time HTTP call; the server creates this PC's Postgres role and
    returns a ready-to-use DSN."""
    req = urllib.request.Request(
        server.rstrip("/") + "/api/workers/enroll", method="POST"
    )
    req.add_header("Content-Type", "application/json")
    data = json.dumps({"join_code": join_code, "name": name}).encode()
    with urllib.request.urlopen(req, data=data, timeout=30) as resp:
        payload = json.loads(resp.read().decode())
    cfg = {
        "dsn": payload["dsn"],
        "worker_id": payload["worker_id"],
        "cluster_id": payload["cluster"]["id"],
        "name": name,
        "cluster_name": payload["cluster"]["name"],
    }
    save_config(cfg)
    print(f"Enrolled worker '{name}' in cluster '{cfg['cluster_name']}'")
    return cfg


def pause_if_frozen() -> None:
    """Double-clicked exes close their console on exit; hold it open so
    the user can read a fatal error. No-op for the script version."""
    if getattr(sys, "frozen", False) and sys.stdin is not None and sys.stdin.isatty():
        try:
            input("Press Enter to close...")
        except EOFError:
            pass


def first_run_enroll(args) -> dict | None:
    """No saved config: prompt for a join code and enroll interactively.
    Returns the saved config, or None if enrollment failed/was cancelled."""
    server = resolve_server(args)
    name = (args.name or os.environ.get("COMPUTERNAME")
            or os.environ.get("HOSTNAME") or "worker")
    print(f"No worker config found - enrolling this PC against {server}")
    print(f"Worker name: {name}")
    try:
        code = ""
        while not code:
            code = input("Cluster join code: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nEnrollment cancelled.")
        return None
    try:
        return enroll(server, code, name)
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode()).get("detail", "")
        except Exception:
            detail = ""
        print(f"Enrollment failed ({e.code}): {detail or e.reason}")
        return None
    except urllib.error.URLError as e:
        print(f"Enrollment failed: could not reach {server} ({e.reason})")
        return None
    except OSError:
        return None  # save_config already printed the fix


# ---------- direct-SQL work protocol ----------

def heartbeat(conn, worker_id: int) -> None:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            f"UPDATE workers SET last_seen={UTC_NOW} WHERE id=%s", (worker_id,)
        )


def touch_claim_heartbeat(conn, item_id: int) -> None:
    """Refresh one claim's heartbeat_at. Guarded on status='claimed' so a
    heartbeat that lands after the claim was already finished/superseded is a
    silent no-op rather than resurrecting a stale row."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            f"UPDATE work_queue SET heartbeat_at={UTC_NOW} "
            f"WHERE id=%s AND status='claimed'",
            (item_id,),
        )


def _claim_heartbeat_loop(dsn: str, item_id: int, stop_event: threading.Event,
                          interval: float = HEARTBEAT_INTERVAL_SECONDS) -> None:
    """Runs on its own thread and connection for the lifetime of one executor
    run, so a 30-minute agent invocation keeps proving to the reaper that this
    claim is still alive. A dedicated connection avoids sharing the slot's
    main connection across threads, which psycopg does not support."""
    conn = None
    while not stop_event.wait(interval):
        try:
            if conn is None or conn.closed:
                conn = psycopg.connect(dsn, connect_timeout=15)
            touch_claim_heartbeat(conn, item_id)
        except Exception:
            conn = None  # reconnect next tick; a missed beat or two is fine
    if conn is not None and not conn.closed:
        try:
            conn.close()
        except Exception:
            pass


# ---------- cluster-wide concurrency cap (gap analysis phase 2, item 5) ----------
#
# A worker's own --concurrency is a per-PC limit; nothing previously stopped
# five PCs at 3 slots each from putting 15 agents on one Claude account. The
# cap lives in cluster_settings and is enforced here, inside the same
# transaction as the claim itself, so it holds across N independent worker
# PCs with no central dispatcher — exactly like CLAIM_SQL's own SKIP LOCKED
# race-safety above.

CLUSTER_SETTINGS_LOCK_SQL = """
SELECT concurrency_cap, enabled, stop_all_requested
FROM cluster_settings WHERE cluster_id=%s
FOR UPDATE
"""

CLUSTER_CLAIMED_COUNT_SQL = (
    "SELECT count(*) FROM work_queue WHERE status='claimed' AND cluster_id=%s"
)


def cluster_claim_gate(cur, cluster_id: int) -> bool:
    """True if this cluster may claim one more item right now.

    Locks the cluster's settings row (FOR UPDATE) before counting in-flight
    claims, and holds that lock for the rest of the caller's transaction: two
    workers racing claim_next for the same cluster serialize on this check
    instead of both reading the same stale count under READ COMMITTED and
    both proceeding past the cap. The loser blocks here until the winner's
    transaction commits (claim written) or rolls back, then re-reads an
    accurate count. A cluster with no settings row (should not happen once
    app/db.run_migrations has backfilled one for every cluster, but kept as a
    defensive fallback) claims unlimited, same as before this feature existed.
    """
    cur.execute(CLUSTER_SETTINGS_LOCK_SQL, (cluster_id,))
    row = cur.fetchone()
    if row is None:
        return True
    cap, enabled, stop_all = row
    if stop_all:
        return False
    if enabled and cap is not None:
        cur.execute(CLUSTER_CLAIMED_COUNT_SQL, (cluster_id,))
        in_flight = cur.fetchone()[0]
        if in_flight >= cap:
            return False
    return True


# ---------- agent profiles ----------
#
# A profile names the tool allowlist, model and system prompt an agent run is
# launched with (gap analysis phase 5). Resolution order — ticket beats
# board beats nothing — mirrors the local `.kanban` tool: a ticket's own
# `profile` field pins the model and beats both triage and the board's
# profile.

def resolve_profile(profiles: dict, ticket_profile_id, board_default_profile_id) -> dict | None:
    """The profile a claimed ticket's agent run should use, or None to fall
    back to the worker's own --allowed-tools default (see
    ClaudeExecutor.run).

    `profiles` maps profile id -> profile dict, scoped to one cluster.
    Ticket beats board: the ticket's own `profile_id` is tried first, and
    only if it names no row in `profiles` — never chosen, or since deleted —
    does the board's `default_profile_id` get a turn. Either id "not found in
    profiles" is treated identically to "not set" rather than raising, so a
    stale or unknown reference can never make an agent run with the CLI's
    default (no) tool grant — it just falls through to the next thing in the
    order, and ultimately to the worker default.
    """
    for profile_id in (ticket_profile_id, board_default_profile_id):
        if profile_id is not None and profile_id in profiles:
            return profiles[profile_id]
    return None


def claim_next(conn, worker_id: int, cluster_id: int, board_ids=None) -> dict | None:
    """Claim the oldest eligible queued item; returns a work payload or None.

    `board_ids` limits the claim to boards this PC has a checkout for. None
    means no limit, which is what the stub executor uses — it needs no repo.

    One transaction: cluster cap gate + claim + ticket flip + board read. A
    cluster at (or over) its cap, or with stop_all_requested set, looks
    exactly like "nothing queued" to the caller — idle, not an error.
    """
    with conn.transaction(), conn.cursor() as cur:
        if not cluster_claim_gate(cur, cluster_id):
            cur.execute(
                f"UPDATE workers SET status='idle', last_seen={UTC_NOW} WHERE id=%s",
                (worker_id,),
            )
            return None
        cur.execute(CLAIM_SQL, {"wid": worker_id, "cid": cluster_id,
                                "boards": board_ids})
        row = cur.fetchone()
        if row is None:
            cur.execute(
                f"UPDATE workers SET status='idle', last_seen={UTC_NOW} WHERE id=%s",
                (worker_id,),
            )
            return None
        item_id, ticket_id = row
        cur.execute(
            f"UPDATE tickets SET status='doing', assigned_worker=%s, "
            f"attempts=COALESCE(attempts,0)+1, updated_at={UTC_NOW} "
            f"WHERE id=%s RETURNING board_id, title, body, attempts, profile_id",
            (worker_id, ticket_id),
        )
        board_id, title, body, attempts, ticket_profile_id = cur.fetchone()
        # Minted here so the id is recorded even if the worker dies mid-run:
        # `claude --resume <id>` in the board folder then replays the session.
        session_id = str(uuid.uuid4())
        cur.execute("UPDATE tickets SET session_id=%s WHERE id=%s",
                    (session_id, ticket_id))
        cur.execute(
            f"UPDATE workers SET status='working', last_seen={UTC_NOW} WHERE id=%s",
            (worker_id,),
        )
        cur.execute(
            "SELECT name, description, out_of_scope, commit_requirements, "
            "use_worktrees, repo_url, default_profile_id, auto_push "
            "FROM boards WHERE id=%s",
            (board_id,),
        )
        b = cur.fetchone()
        board_default_profile_id = b[6] if b else None
        cur.execute(
            "SELECT id, name, allowed_tools, model, system_prompt "
            "FROM profiles WHERE cluster_id=%s",
            (cluster_id,),
        )
        profiles = {
            row[0]: {"id": row[0], "name": row[1], "allowed_tools": row[2],
                      "model": row[3], "system_prompt": row[4]}
            for row in cur.fetchall()
        }
        return {
            "assignment_id": item_id,
            "session_id": session_id,
            "profile": resolve_profile(profiles, ticket_profile_id, board_default_profile_id),
            "board": {
                "id": board_id, "name": b[0], "description": b[1],
                "out_of_scope": b[2], "commit_requirements": b[3],
                "use_worktrees": bool(b[4]), "repo_url": b[5],
                "auto_push": bool(b[7]),
            } if b else None,
            "ticket": {
                "id": ticket_id, "board_id": board_id, "title": title,
                "body": body, "status": "doing", "attempts": attempts,
            },
        }


def add_progress(conn, worker_id: int, worker_name: str, ticket_id: int, message: str) -> None:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO comments (ticket_id, writer, message, created_at) "
            f"VALUES (%s, %s, %s, {UTC_NOW})",
            (ticket_id, f"worker:{worker_name}", message),
        )
        cur.execute(
            f"UPDATE workers SET last_seen={UTC_NOW} WHERE id=%s", (worker_id,)
        )


def add_log_line(conn, ticket_id: int, work_queue_id: int | None, seq: int,
                 role: str, text: str) -> None:
    """One row of the live transcript (see models.TicketLog). Called once per
    parsed stream-json turn, so a browser tailing `?since_seq=` sees output
    as it is generated rather than in the batches `add_progress` posts."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO ticket_log (ticket_id, work_queue_id, seq, role, text, created_at) "
            f"VALUES (%s, %s, %s, %s, %s, {UTC_NOW})",
            (ticket_id, work_queue_id, seq, role, text),
        )


def kill_requested(conn, item_id: int) -> bool:
    """Live read of one in-flight claim's kill flag, via the slot's own
    connection (same thread, sequential with everything else the slot does —
    no concurrent use of `conn`). A transient DB hiccup here must not abort
    an otherwise-healthy agent run, so it fails open (treated as "no kill")."""
    try:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute("SELECT kill_requested FROM work_queue WHERE id=%s", (item_id,))
            row = cur.fetchone()
            return bool(row and row[0])
    except psycopg.OperationalError:
        return False


def fetch_pending_chat(conn, ticket_id: int) -> list:
    """Undelivered `ticket_chat` rows for this ticket, oldest first — what a
    chat pump run picks up on its next poll."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, message FROM ticket_chat "
            "WHERE ticket_id=%s AND delivered_at IS NULL ORDER BY id",
            (ticket_id,),
        )
        return cur.fetchall()


def mark_chat_delivered(conn, chat_ids: list) -> None:
    if not chat_ids:
        return
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            f"UPDATE ticket_chat SET delivered_at={UTC_NOW} WHERE id = ANY(%s::int[])",
            (list(chat_ids),),
        )


def finish_work(conn, worker_id: int, worker_name: str, item_id: int,
                ticket_id: int, ok: bool, comment: str | None,
                killed: bool = False, commit_gate: dict | None = None) -> str:
    """Record the result. Mirrors v1 delegation.finish_work: success ->
    review; failure -> requeue until MAX_ATTEMPTS then failed. A kill (owner
    request, not a genuine failure) -> killed, with the claim-time attempt
    charge refunded so it does not burn the ticket's retry budget, and no
    auto-requeue (unlike a failure, restarting it is the owner's call). The
    rowcount guard on the first UPDATE preserves v1's 409-on-superseded
    semantics: if the claim was superseded while we worked — including a kill
    request that lands after this ticket already finished — nothing else is
    written, so a late kill can never masquerade as a fresh failure.

    `commit_gate`, when given, is the agent's self-reported verdict on the
    board's commit_requirements (see app/prompt.parse_commit_gate) and is
    recorded on tickets.commit_gate regardless of ok/killed, so a human can
    see why a run did not push even when the run itself succeeded.
    """
    wq_status = "done" if ok else ("killed" if killed else "failed")
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            f"UPDATE work_queue SET status=%s, finished_at={UTC_NOW}, result=%s "
            f"WHERE id=%s AND status='claimed' AND claimed_by=%s",
            (wq_status, (comment or "")[:10000], item_id, worker_id),
        )
        if cur.rowcount != 1:
            cur.execute(
                f"UPDATE workers SET status='idle', last_seen={UTC_NOW} WHERE id=%s",
                (worker_id,),
            )
            return "superseded"
        if comment:
            cur.execute(
                f"INSERT INTO comments (ticket_id, writer, message, created_at) "
                f"VALUES (%s, %s, %s, {UTC_NOW})",
                (ticket_id, f"worker:{worker_name}", comment),
            )
        if commit_gate is not None:
            cur.execute(
                "UPDATE tickets SET commit_gate=%s WHERE id=%s",
                (json.dumps(commit_gate), ticket_id),
            )
        if ok:
            ticket_status = "review"
            cur.execute(
                f"UPDATE tickets SET status='review', updated_at={UTC_NOW} WHERE id=%s",
                (ticket_id,),
            )
        elif killed:
            ticket_status = "killed"
            cur.execute(
                f"UPDATE tickets SET status='killed', "
                f"attempts=GREATEST(COALESCE(attempts,0)-1, 0), "
                f"updated_at={UTC_NOW} WHERE id=%s",
                (ticket_id,),
            )
        else:
            cur.execute("SELECT attempts, board_id FROM tickets WHERE id=%s", (ticket_id,))
            attempts, board_id = cur.fetchone()
            if (attempts or 0) < MAX_ATTEMPTS:
                ticket_status = "ready"
                cur.execute("SELECT cluster_id FROM boards WHERE id=%s", (board_id,))
                cluster_id = cur.fetchone()[0]
                cur.execute(
                    f"INSERT INTO work_queue (ticket_id, cluster_id, status, queued_at) "
                    f"VALUES (%s, %s, 'queued', {UTC_NOW})",
                    (ticket_id, cluster_id),
                )
                cur.execute(
                    f"UPDATE tickets SET status='ready', assigned_worker=NULL, "
                    f"updated_at={UTC_NOW} WHERE id=%s",
                    (ticket_id,),
                )
            else:
                ticket_status = "failed"
                cur.execute(
                    f"UPDATE tickets SET status='failed', updated_at={UTC_NOW} WHERE id=%s",
                    (ticket_id,),
                )
        cur.execute(
            f"UPDATE workers SET status='idle', last_seen={UTC_NOW} WHERE id=%s",
            (worker_id,),
        )
        return ticket_status


def raise_question(conn, worker_id: int, worker_name: str, item_id: int,
                   ticket_id: int, question: dict, comment: str | None) -> str:
    """Record an agent's escalation: park the ticket blocked and release the
    work_queue slot. Mirrors finish_work's rowcount guard against a claim that
    was superseded (e.g. a human dragged the ticket back to ready) while the
    agent was still running — in that case nothing here is written."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            f"UPDATE work_queue SET status='blocked', finished_at={UTC_NOW}, result=%s "
            f"WHERE id=%s AND status='claimed' AND claimed_by=%s",
            ((comment or "")[:10000], item_id, worker_id),
        )
        if cur.rowcount != 1:
            cur.execute(
                f"UPDATE workers SET status='idle', last_seen={UTC_NOW} WHERE id=%s",
                (worker_id,),
            )
            return "superseded"
        cur.execute(
            f"INSERT INTO ticket_questions "
            f"(ticket_id, question, type, format, options, multi, created_at) "
            f"VALUES (%s, %s, %s, %s, %s, %s, {UTC_NOW})",
            (ticket_id, question["question"], question["type"], question["format"],
             json.dumps(question["options"]) if question["options"] is not None else None,
             question["multi"]),
        )
        cur.execute(
            f"UPDATE tickets SET status='blocked', assigned_worker=NULL, "
            f"updated_at={UTC_NOW} WHERE id=%s",
            (ticket_id,),
        )
        cur.execute(
            f"UPDATE workers SET status='idle', last_seen={UTC_NOW} WHERE id=%s",
            (worker_id,),
        )
        return "blocked"


# ---------- stale-claim reaper ----------
#
# The cloud analogue of the local tool's reap_decision: if a worker PC dies
# mid-ticket, its claim never calls finish_work, so nothing would otherwise
# move the row out of 'claimed'. Every worker opportunistically reaps its own
# cluster's stale claims once per poll cycle (see main()) — no new
# infrastructure (no Render cron, no elected leader), because the existing
# kanban_worker group grant already includes UPDATE on work_queue/tickets for
# every enrolled PC, not just the one that placed a given claim. Two workers
# reaping the same cluster in the same tick is expected, not a bug: the
# threshold check in reap_stale_claims re-reads the row before touching it,
# then _reap_one's UPDATE ... WHERE status='claimed' guard (same pattern as
# finish_work's rowcount guard) means only the first one actually flips it —
# whichever reaper loses the race sees rowcount 0 and moves on.

def reap_stale_claims(conn, cluster_id: int,
                      stale_after_seconds: float = STALE_CLAIM_SECONDS,
                      now: "datetime.datetime | None" = None) -> list:
    """Flip claims whose heartbeat has gone stale back to queued/ready,
    respecting MAX_ATTEMPTS exactly like a reported failure would. Returns a
    list of (item_id, ticket_id, new_ticket_status) for whichever claims this
    call actually reaped (empty if another reaper won every race, or if
    nothing was stale).
    """
    now = now or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    cutoff = now - datetime.timedelta(seconds=stale_after_seconds)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM work_queue WHERE status='claimed' AND cluster_id=%s "
            "AND COALESCE(heartbeat_at, claimed_at) < %s",
            (cluster_id, cutoff),
        )
        candidate_ids = [row[0] for row in cur.fetchall()]
    reaped = []
    for item_id in candidate_ids:
        result = _reap_one(conn, item_id, stale_after_seconds)
        if result is not None:
            reaped.append(result)
    return reaped


# ---------- ticket_log retention ----------
#
# Kept in the DB (not a local file) is what makes the live transcript durable
# and visible from any browser, but that means it can grow indefinitely if
# nothing ever trims it. Every worker opportunistically prunes its own
# cluster's finished tickets once per poll cycle, same "no elected leader, no
# dedicated infrastructure" reasoning as reap_stale_claims above — a redundant
# DELETE from a second worker in the same tick just matches zero rows.

TICKET_LOG_RETENTION_DAYS = 30


def prune_ticket_log(conn, cluster_id: int,
                     retention_days: float = TICKET_LOG_RETENTION_DAYS,
                     now: "datetime.datetime | None" = None) -> int:
    """Delete ticket_log rows for tickets in this cluster that reached a
    terminal status more than `retention_days` ago. Returns the number of
    rows deleted."""
    now = now or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    cutoff = now - datetime.timedelta(days=retention_days)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "DELETE FROM ticket_log WHERE ticket_id IN ("
            "SELECT t.id FROM tickets t JOIN boards b ON b.id = t.board_id "
            "WHERE b.cluster_id = %s AND t.status IN ('done', 'failed', 'killed') "
            "AND t.updated_at < %s)",
            (cluster_id, cutoff),
        )
        return cur.rowcount


def _reap_one(conn, item_id: int, stale_after_seconds: float):
    """Atomically resolve one stale claim, or no-op if it was already
    resolved (by another reaper, or by the worker's own finish_work landing
    late) between the candidate scan and here."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            f"UPDATE work_queue SET status='failed', finished_at={UTC_NOW}, "
            f"result=%s WHERE id=%s AND status='claimed'",
            (f"reaped: no heartbeat for over {int(stale_after_seconds)}s "
             "(worker likely died mid-ticket)", item_id),
        )
        if cur.rowcount != 1:
            return None
        cur.execute("SELECT ticket_id FROM work_queue WHERE id=%s", (item_id,))
        ticket_id = cur.fetchone()[0]
        cur.execute("SELECT attempts, board_id FROM tickets WHERE id=%s", (ticket_id,))
        attempts, board_id = cur.fetchone()
        if (attempts or 0) < MAX_ATTEMPTS:
            cur.execute("SELECT cluster_id FROM boards WHERE id=%s", (board_id,))
            cluster_id = cur.fetchone()[0]
            cur.execute(
                f"INSERT INTO work_queue (ticket_id, cluster_id, status, queued_at) "
                f"VALUES (%s, %s, 'queued', {UTC_NOW})",
                (ticket_id, cluster_id),
            )
            cur.execute(
                f"UPDATE tickets SET status='ready', assigned_worker=NULL, "
                f"updated_at={UTC_NOW} WHERE id=%s",
                (ticket_id,),
            )
            new_status = "ready"
        else:
            cur.execute(
                f"UPDATE tickets SET status='failed', updated_at={UTC_NOW} WHERE id=%s",
                (ticket_id,),
            )
            new_status = "failed"
    return (item_id, ticket_id, new_status)


# ---------- initial triage ----------
#
# Cloud analogue of the local tool's initial triage (_real_sonnet_triage /
# apply_initial_triage): a `todo` ticket with no model yet gets one inferred,
# plus inferred dependencies on other tickets on its board, and is promoted to
# `ready`. Runs opportunistically in every worker, once per poll cycle,
# piggybacked on the same main-loop tick as reap_stale_claims — no elected
# leader, no Render cron, no new credential. See
# docs/superpowers/specs/2026-08-22-triage-design.md for why: this PC's own
# already-authenticated `claude` CLI does the inference, and the guarded
# UPDATE below (`WHERE status='todo' AND model IS NULL`) makes two workers
# racing the same ticket, or a second pass over an already-triaged one, a
# no-op for every writer but the first — same pattern _reap_one/finish_work
# already rely on.

TRIAGE_TIMEOUT_SECONDS = 120


def run_triage_cli(prompt: str) -> str:
    """Ask the local `claude` CLI for one triage reply. Raises on anything
    that keeps the CLI from producing output; the caller treats that as a
    triage failure, same as a reply that fails to parse."""
    exe = shutil.which("claude")
    if not exe:
        raise RuntimeError("`claude` CLI not found on this PC's PATH.")
    proc = subprocess.run([exe, "-p", prompt], capture_output=True, text=True,
                          timeout=TRIAGE_TIMEOUT_SECONDS)
    if proc.returncode != 0:
        raise RuntimeError(f"claude CLI exited {proc.returncode}: {proc.stderr[:500]}")
    return proc.stdout


def _triage_candidates(conn, board_id: int, ticket_id: int) -> list:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, title, status FROM tickets WHERE board_id=%s AND id != %s "
            "ORDER BY id",
            (board_id, ticket_id),
        )
        return [{"id": r[0], "title": r[1], "status": r[2]} for r in cur.fetchall()]


def _apply_triage_one(conn, ticket_id: int, cluster_id: int, model: str,
                      depends_on: list):
    """Atomically promote one triaged ticket, or no-op if it was already
    triaged/promoted (by another worker, or a stale scan) between the
    candidate scan and here. Returns (ticket_id, model, depends_on) if this
    call actually applied it, else None."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            f"UPDATE tickets SET model=%s, status='ready', assigned_worker=NULL, "
            f"updated_at={UTC_NOW} WHERE id=%s AND status='todo' AND model IS NULL",
            (model, ticket_id),
        )
        if cur.rowcount != 1:
            return None
        for dep_id in depends_on:
            cur.execute(
                "INSERT INTO ticket_deps (ticket_id, depends_on_id) VALUES (%s, %s) "
                "ON CONFLICT (ticket_id, depends_on_id) DO NOTHING",
                (ticket_id, dep_id),
            )
        cur.execute(
            f"INSERT INTO work_queue (ticket_id, cluster_id, status, queued_at) "
            f"VALUES (%s, %s, 'queued', {UTC_NOW})",
            (ticket_id, cluster_id),
        )
    return (ticket_id, model, depends_on)


def triage_todo_tickets(conn, cluster_id: int, run_llm=run_triage_cli) -> list:
    """Triage every eligible `todo` ticket in this cluster. Returns the list of
    (ticket_id, model, depends_on) actually applied — empty if there was
    nothing to triage, every candidate lost its race, or every triage attempt
    failed. A triage failure (the CLI errors, times out, or replies with
    something `parse_triage_result` rejects) leaves that ticket in `todo`
    untouched; it is retried on the next poll cycle, by this worker or another.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT t.id, t.board_id, t.title, t.body FROM tickets t "
            "JOIN boards b ON b.id = t.board_id "
            "WHERE b.cluster_id=%s AND t.status='todo' AND t.model IS NULL "
            "ORDER BY t.id",
            (cluster_id,),
        )
        rows = cur.fetchall()
    applied = []
    for ticket_id, board_id, title, body in rows:
        candidates = _triage_candidates(conn, board_id, ticket_id)
        ticket = {"id": ticket_id, "title": title, "body": body}
        prompt = build_triage_prompt(ticket, candidates)
        try:
            raw = run_llm(prompt)
        except Exception as exc:
            print(f"[triage] #{ticket_id} failed: {exc!r}")
            continue
        result = parse_triage_result(raw, [c["id"] for c in candidates], ticket_id)
        if result is None:
            print(f"[triage] #{ticket_id} failed: unparseable/invalid triage reply")
            continue
        outcome = _apply_triage_one(conn, ticket_id, cluster_id,
                                    result["model"], result["depends_on"])
        if outcome is not None:
            print(f"[triage] #{ticket_id} -> ready (model={result['model']}, "
                  f"depends_on={result['depends_on']})")
            applied.append(outcome)
    return applied


# ---------- argument parser & executor selection ----------

# ---------- concurrency slots ----------
#
# How much this PC runs at once is the PC's own business: the server never
# schedules, it only publishes queued work. Each slot is an independent
# claim->run->finish loop with its own connection, so a slot sitting in a
# 30-minute agent run never holds up its neighbors.

_SLOT_LOCK = threading.Lock()
_RUNNING = {"n": 0}


def running_count() -> int:
    """How many slots are inside an executor run right now."""
    with _SLOT_LOCK:
        return _RUNNING["n"]


def set_slot_counts(conn, worker_id: int, concurrency: int, running: int) -> None:
    """Publish this PC's limit and current load. Advisory only — the server
    reads these to draw the Workers panel; nothing schedules on them."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            f"UPDATE workers SET concurrency=%s, running=%s, "
            f"status=%s, last_seen={UTC_NOW} WHERE id=%s",
            (concurrency, running, "working" if running else "idle", worker_id),
        )


def fetch_worker_settings(conn, worker_id: int) -> dict:
    """This PC's current name and website-set concurrency request (ticket
    #18's `workers.desired_concurrency`; NULL there means "PC decides")."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT name, desired_concurrency FROM workers WHERE id=%s", (worker_id,)
        )
        row = cur.fetchone()
    if row is None:
        return {}
    return {"name": row[0], "desired_concurrency": row[1]}


def resolve_concurrency(args, cfg: dict, desired: int | None = None) -> int:
    """Flag beats the website-set `desired` value beats saved config beats 1.

    `desired` is None unless the site has an explicit override on file, so an
    unconfigured worker behaves exactly as before this parameter existed.
    """
    value = args.concurrency
    if value is None:
        value = desired if desired is not None else cfg.get("concurrency")
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 1


def run_slot(cfg, args, executor, stop_event, slot_no: int) -> None:
    """One independent claim->run->finish loop with its own DB connection.

    Slots share nothing but the config dict (read-only) and the stop event.
    """
    boards = None if isinstance(executor, StubExecutor) else configured_board_ids(cfg)
    paths = board_paths(cfg)
    conn = None
    while not stop_event.is_set():
        try:
            if conn is None or conn.closed:
                conn = psycopg.connect(cfg["dsn"], connect_timeout=15)
            work = claim_next(conn, cfg["worker_id"], cfg["cluster_id"], boards)
            if work:
                ticket = work["ticket"]
                with _SLOT_LOCK:
                    _RUNNING["n"] += 1
                print(f"[slot {slot_no}] claimed #{ticket['id']} '{ticket['title']}'")

                # Bound to this claim's connection/ticket by default args, so a
                # progress flush mid-run always lands on the right row even if
                # `conn`/`ticket` are reassigned by a later loop iteration.
                def progress_cb(message, _conn=conn, _ticket_id=ticket["id"]):
                    try:
                        add_progress(_conn, cfg["worker_id"], cfg["name"], _ticket_id, message)
                    except Exception as exc:
                        print(f"[slot {slot_no}] progress post failed: {exc!r}")

                # Reader-thread-only (see ClaudeExecutor.run), same as
                # progress_cb above, so sharing `conn` is safe: the main
                # thread never touches it while executor.run() is in flight.
                log_seq = {"n": 0}

                def log_cb(role, message, _conn=conn, _ticket_id=ticket["id"],
                          _wq_id=work["assignment_id"]):
                    log_seq["n"] += 1
                    try:
                        add_log_line(_conn, _ticket_id, _wq_id, log_seq["n"], role, message)
                    except Exception as exc:
                        print(f"[slot {slot_no}] log line post failed: {exc!r}")

                hb_stop = threading.Event()
                hb_thread = threading.Thread(
                    target=_claim_heartbeat_loop,
                    args=(cfg["dsn"], work["assignment_id"], hb_stop),
                    daemon=True,
                )
                hb_thread.start()
                should_kill = lambda: kill_requested(conn, work["assignment_id"])  # noqa: E731
                killed = False
                commit_gate = None

                # The chat pump runs on its own thread inside executor.run()
                # for the run's whole duration, so it gets a connection of
                # its own rather than sharing `conn` (which the main thread
                # reads/writes concurrently via progress_cb and, after the
                # run, finish_work). A connection failure here just disables
                # chat for this run instead of failing the ticket.
                try:
                    chat_conn = psycopg.connect(cfg["dsn"], connect_timeout=15)
                except Exception as exc:
                    # Any failure here just disables chat for this run rather
                    # than failing the ticket (and, since _RUNNING["n"] was
                    # already incremented above, must not escape uncaught —
                    # that would leak the counter past the try/finally below
                    # that decrements it).
                    print(f"[slot {slot_no}] chat pump connection failed: {exc!r}")
                    chat_conn = None

                def chat_source(_conn=chat_conn, _ticket_id=ticket["id"]):
                    return fetch_pending_chat(_conn, _ticket_id) if _conn is not None else []

                def chat_delivered(chat_ids, _conn=chat_conn):
                    if _conn is not None:
                        mark_chat_delivered(_conn, chat_ids)

                try:
                    if isinstance(executor, StubExecutor):
                        ok, comment = executor.run(
                            ticket, board=work.get("board"),
                            directory=paths.get(str(ticket["board_id"])),
                            session_id=work.get("session_id"),
                            progress_cb=progress_cb, should_kill=should_kill,
                            chat_source=chat_source, chat_delivered=chat_delivered,
                            log_cb=log_cb, profile=work.get("profile"))
                    else:
                        directory, resolve_error = resolve_directory(
                            work.get("board") or {}, cfg)
                        if resolve_error:
                            ok, comment = False, resolve_error
                        else:
                            ok, comment = executor.run(
                                ticket, board=work.get("board"),
                                directory=directory,
                                session_id=work.get("session_id"),
                                progress_cb=progress_cb, should_kill=should_kill,
                                chat_source=chat_source, chat_delivered=chat_delivered,
                                log_cb=log_cb, profile=work.get("profile"))
                            # A question isn't a finished attempt — nothing to
                            # push yet, and (in tests especially) `directory`
                            # may not even be a real git checkout.
                            if ok and not parse_question(comment):
                                board = work.get("board") or {}
                                commit_gate = parse_commit_gate(comment)
                                # A board with commit_requirements refuses to
                                # push unless the agent's own gate says they
                                # were met — an unreported gate (marker
                                # missing/malformed) is treated the same as an
                                # explicit False, not as "nothing to check".
                                gate_unmet = (
                                    bool(board.get("commit_requirements"))
                                    and not (commit_gate and commit_gate["requirements_met"])
                                )
                                if not board.get("auto_push"):
                                    pass  # opt-in: this board never auto-pushes
                                elif gate_unmet:
                                    comment = (comment or "") + (
                                        "\n\n(Not pushed: the commit gate did not "
                                        "report the board's commit requirements "
                                        "were met.)"
                                    )
                                else:
                                    push_note = push_ticket_branch(
                                        directory, ticket_branch_name(ticket))
                                    if push_note:
                                        comment = (comment or "") + push_note
                except KilledByRequest as exc:
                    ok, comment, killed = False, str(exc), True
                except Exception as exc:
                    ok, comment = False, f"Executor error: {exc!r}"
                finally:
                    hb_stop.set()
                    hb_thread.join(timeout=5)
                    with _SLOT_LOCK:
                        _RUNNING["n"] -= 1
                    if chat_conn is not None and not chat_conn.closed:
                        chat_conn.close()
                # A clean (ok) run whose entire reply is the escalation marker
                # is a question, not a completion — park it blocked instead of
                # landing it in review. A non-zero exit is never treated as a
                # question even if its error text happens to contain the
                # marker (e.g. echoed back from a crashed prior attempt), and
                # neither is a kill: the run was cut short, not concluded.
                question = parse_question(comment) if ok and not killed else None
                if question:
                    status = raise_question(conn, cfg["worker_id"], cfg["name"],
                                            work["assignment_id"], ticket["id"],
                                            question, comment)
                else:
                    status = finish_work(conn, cfg["worker_id"], cfg["name"],
                                         work["assignment_id"], ticket["id"], ok, comment,
                                         killed=killed, commit_gate=commit_gate)
                print(f"[slot {slot_no}] #{ticket['id']} -> {status}")
        except psycopg.OperationalError as e:
            msg = str(e)
            print(f"[slot {slot_no}] database unreachable ({msg[:150]}); retrying...")
            if "password authentication failed" in msg or "does not exist" in msg:
                print("Credentials rejected — this PC may be revoked. Re-enroll.")
                stop_event.set()  # one slot learning this is enough for all of them
                return
            conn = None
        except Exception as exc:  # a slot must never die silently
            print(f"[slot {slot_no}] unexpected error: {exc!r}")
        if args.once:
            break
        stop_event.wait(args.poll)
    if conn is not None and not conn.closed:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="kanban-cloud worker (v2 direct-DB)")
    parser.add_argument("--enroll", action="store_true",
                        help="enroll this PC (needs --join-code; --server optional)")
    parser.add_argument("--server",
                        help=f"server base URL (default {DEFAULT_SERVER})")
    parser.add_argument("--test", action="store_true",
                        help=f"enroll against a local dev server ({TEST_SERVER}) "
                             "instead of production; overridden by --server")
    parser.add_argument("--join-code", help="cluster join code (enrollment only)")
    parser.add_argument("--name", help="worker name (defaults to computer name)")
    parser.add_argument("--stub", action="store_true",
                        help="use the stub executor instead of the Claude CLI")
    # v1 flag; real execution is now the default, so this is a no-op alias
    parser.add_argument("--real", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--poll", type=float, default=POLL_SECONDS,
                        help=f"poll interval seconds (default {POLL_SECONDS})")
    parser.add_argument("--once", action="store_true",
                        help="poll a single time then exit (for testing)")
    parser.add_argument("--set-path", action="append", metavar="BOARD=PATH",
                        help="map a board (id or name) to its folder on this PC; "
                             "repeatable")
    parser.add_argument("--list-boards", action="store_true",
                        help="list this cluster's boards and their configured paths")
    parser.add_argument("--concurrency", type=int, default=None,
                        help="tickets this PC runs at once (default 1; saved to "
                             "the config so it sticks)")
    parser.add_argument("--allowed-tools", default=DEFAULT_ALLOWED_TOOLS,
                        help=f"comma-separated tools the agent may use "
                             f"(default: {DEFAULT_ALLOWED_TOOLS})")
    return parser


def pick_executor(args):
    if args.stub:
        return StubExecutor()
    return ClaudeExecutor(allowed_tools=args.allowed_tools)


def resolve_server(args) -> str:
    """--server always wins; else --test targets the local dev server;
    else production."""
    return args.server or (TEST_SERVER if args.test else DEFAULT_SERVER)


# ---------- main loop ----------

def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # cp1252 consoles must not kill us
        except (AttributeError, ValueError):
            pass

    args = build_parser().parse_args()

    if args.enroll:
        if not args.join_code:
            print("--enroll needs --join-code")
            return 2
        name = (args.name or os.environ.get("COMPUTERNAME")
                or os.environ.get("HOSTNAME") or "worker")
        enroll(resolve_server(args), args.join_code, name)
        return 0

    cfg = load_config()
    if cfg is None:
        cfg = first_run_enroll(args)
        if cfg is None:
            pause_if_frozen()
            return 2
        try:
            conn = psycopg.connect(cfg["dsn"], connect_timeout=15)
            try:
                cfg = prompt_for_board_paths(conn, cfg)
            finally:
                conn.close()
        except psycopg.OperationalError as e:
            print(f"Enrolled, but could not list boards ({str(e)[:120]}).")
            print("Set folders later with --set-path <board>=<path>.")

    if args.list_boards or args.set_path:
        conn = psycopg.connect(cfg["dsn"], connect_timeout=15)
        try:
            for arg in args.set_path or []:
                try:
                    cfg = apply_set_path(conn, cfg, arg)
                except ValueError as e:
                    print(f"--set-path {arg}: {e}")
                    return 2
            if args.list_boards:
                paths = board_paths(cfg)
                print(f"{'id':>4}  {'board':<24} path")
                for b in list_cluster_boards(conn, cfg["cluster_id"]):
                    print(f"{b['id']:>4}  {b['name']:<24} "
                          f"{paths.get(str(b['id']), '(not configured)')}")
        finally:
            conn.close()
        return 0

    executor = pick_executor(args)

    # Pick up a website-set rename/concurrency request (ticket #18) before
    # deciding how many slots to run. Best-effort: offline at startup just
    # means falling back to the local flag/config, same as before this call
    # existed.
    settings = {}
    try:
        settings_conn = psycopg.connect(cfg["dsn"], connect_timeout=15)
        try:
            settings = fetch_worker_settings(settings_conn, cfg["worker_id"])
        finally:
            settings_conn.close()
    except psycopg.OperationalError:
        pass
    if settings.get("name") and settings["name"] != cfg.get("name"):
        cfg["name"] = settings["name"]
        save_config(cfg)

    concurrency = resolve_concurrency(args, cfg, settings.get("desired_concurrency"))
    if concurrency != cfg.get("concurrency"):
        cfg["concurrency"] = concurrency
        save_config(cfg)

    if not isinstance(executor, StubExecutor) and not configured_board_ids(cfg):
        print("WARNING: no board folders configured on this PC via --set-path; "
              "only boards with a Repo URL set (auto-clone) will be claimable "
              "here.\n         Fix with: --list-boards, then "
              "--set-path <board>=<path>")

    print(f"Worker '{cfg['name']}' polling Postgres every {args.poll}s "
          f"({concurrency} slot{'s' if concurrency > 1 else ''}, "
          f"executor: {executor.name}). Ctrl+C to stop.")

    stop_event = threading.Event()
    slots = [threading.Thread(target=run_slot, name=f"slot-{i}",
                              args=(cfg, args, executor, stop_event, i),
                              daemon=True)
             for i in range(concurrency)]
    for t in slots:
        t.start()

    # The heartbeat lives on the main thread so a PC with every slot busy in a
    # 30-minute agent run still reports online — it used to ride on the claim
    # query, which is exactly what a busy worker stops issuing. The stale-claim
    # reaper and initial triage both piggyback on the same tick: opportunistic,
    # once per poll cycle, no dedicated infrastructure (see each one's own
    # docstring for why).
    hb_conn = None
    try:
        while any(t.is_alive() for t in slots):
            try:
                if hb_conn is None or hb_conn.closed:
                    hb_conn = psycopg.connect(cfg["dsn"], connect_timeout=15)
                set_slot_counts(hb_conn, cfg["worker_id"], concurrency,
                                running_count())
                reap_stale_claims(hb_conn, cfg["cluster_id"])
                prune_ticket_log(hb_conn, cfg["cluster_id"])
                triage_todo_tickets(hb_conn, cfg["cluster_id"])
            except psycopg.OperationalError:
                hb_conn = None
            except Exception:
                pass
            if args.once:
                break
            time.sleep(args.poll)
    except KeyboardInterrupt:
        print("\nStopping worker; slots will finish the ticket in hand.")
    stop_event.set()
    for t in slots:
        t.join(timeout=30 if args.once else None)
    if hb_conn is not None and not hb_conn.closed:
        hb_conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
