"""kanban-cloud worker client (v2: direct Postgres).

One-time enrollment over HTTP issues this PC its own database role; after
that the worker never contacts the web service — polling, claiming,
progress, results, and heartbeats are SQL against Neon.

Client PCs (packaged exe): download kanban-worker.exe from the latest
worker-v* GitHub Release, put it in its own folder, and run it — on first
run it asks for the cluster join code, then starts polling. Real ticket
execution shells out to the `claude` CLI, which must be installed
separately.

Dev / script setup (once per PC):
    pip install "psycopg[binary]"
    py worker.py --enroll --join-code ABC12345 --name ryans-pc

Run:
    py worker.py            # real executor (Claude CLI)
    py worker.py --stub     # stub executor for testing

Note: while this worker runs, its polling keeps Neon compute awake
(free tier autosuspends only when idle). Stop the worker when not in use.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import psycopg

from app.prompt import build_agent_prompt

DEFAULT_SERVER = "https://kanban-cloud.onrender.com"

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

UTC_NOW = "(now() at time zone 'utc')"

# Atomic, race-safe claim: SKIP LOCKED means concurrent workers never block
# or double-claim; the subquery orders by queue age and honors target_worker.
# The board filter keeps a PC from claiming work it cannot do: a ticket whose
# board has no folder configured here would otherwise be claimed and then fail.
# A NULL %(boards)s disables it, which is how --stub (no repo needed) opts out.
CLAIM_SQL = f"""
UPDATE work_queue SET status='claimed', claimed_by=%(wid)s, claimed_at={UTC_NOW}
WHERE id = (
  SELECT wq.id FROM work_queue wq
  JOIN tickets t ON t.id = wq.ticket_id
  WHERE wq.status='queued' AND wq.cluster_id=%(cid)s
    AND (t.target_worker IS NULL OR t.target_worker = %(wid)s)
    AND (%(boards)s::int[] IS NULL OR t.board_id = ANY(%(boards)s::int[]))
  ORDER BY wq.queued_at, wq.id
  FOR UPDATE OF wq SKIP LOCKED
  LIMIT 1
)
RETURNING id, ticket_id
"""


# ---------- executors ----------

class StubExecutor:
    """Fake executor: waits a moment and produces a canned result."""

    name = "stub"

    def run(self, ticket, api_key, board=None, directory=None, session_id=None):
        print(f"  [stub] pretending to work on ticket #{ticket['id']}: {ticket['title']}")
        time.sleep(2)
        return True, (
            f"[StubExecutor] Completed ticket '{ticket['title']}' (attempt "
            f"{ticket.get('attempts', '?')}). This is a placeholder result — "
            "run the worker without --stub to execute via the Claude CLI."
        )


class ClaudeExecutor:
    """Real executor: runs the Claude CLI inside the board's checkout."""

    name = "claude"

    def __init__(self, allowed_tools: str = DEFAULT_ALLOWED_TOOLS):
        self.allowed_tools = allowed_tools

    def run(self, ticket, api_key, board=None, directory=None, session_id=None):
        board = board or {}
        name = board.get("name", "?")
        if not api_key:
            return False, ("No Claude API key configured for this cluster "
                           "(set it in Settings).")
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

        prompt = build_agent_prompt(ticket, board, directory)
        env = dict(os.environ)
        env["ANTHROPIC_API_KEY"] = api_key
        cmd = [exe, "-p", prompt, "--allowedTools", self.allowed_tools]
        if session_id:
            cmd += ["--session-id", session_id]
        print(f"  [claude] running in {directory} for ticket #{ticket['id']}")
        try:
            proc = subprocess.run(cmd, cwd=directory, env=env,
                                  capture_output=True, text=True, timeout=1800)
        except FileNotFoundError:
            return False, "`claude` CLI not found on this PC's PATH."
        except subprocess.TimeoutExpired:
            return False, "Claude CLI timed out after 30 minutes."
        output = (proc.stdout or "").strip() or (proc.stderr or "").strip()
        if proc.returncode != 0:
            return False, f"Claude CLI exited {proc.returncode}: {output[:2000]}"
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
    server = args.server or DEFAULT_SERVER
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


def claim_next(conn, worker_id: int, cluster_id: int, board_ids=None) -> dict | None:
    """Claim the oldest eligible queued item; returns a work payload or None.

    `board_ids` limits the claim to boards this PC has a checkout for. None
    means no limit, which is what the stub executor uses — it needs no repo.

    One transaction: claim + ticket flip + board read + key read.
    """
    with conn.transaction(), conn.cursor() as cur:
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
            f"WHERE id=%s RETURNING board_id, title, body, attempts",
            (worker_id, ticket_id),
        )
        board_id, title, body, attempts = cur.fetchone()
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
            "use_worktrees FROM boards WHERE id=%s",
            (board_id,),
        )
        b = cur.fetchone()
        cur.execute(
            "SELECT claude_api_key FROM cluster_settings WHERE cluster_id=%s",
            (cluster_id,),
        )
        key_row = cur.fetchone()
        return {
            "assignment_id": item_id,
            "claude_api_key": key_row[0] if key_row else None,
            "session_id": session_id,
            "board": {
                "id": board_id, "name": b[0], "description": b[1],
                "out_of_scope": b[2], "commit_requirements": b[3],
                "use_worktrees": bool(b[4]),
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


def finish_work(conn, worker_id: int, worker_name: str, item_id: int,
                ticket_id: int, ok: bool, comment: str | None) -> str:
    """Record the result. Mirrors v1 delegation.finish_work: success ->
    review; failure -> requeue until MAX_ATTEMPTS then failed. The rowcount
    guard on the first UPDATE preserves v1's 409-on-superseded semantics:
    if the claim was superseded while we worked, nothing else is written."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            f"UPDATE work_queue SET status=%s, finished_at={UTC_NOW}, result=%s "
            f"WHERE id=%s AND status='claimed' AND claimed_by=%s",
            ("done" if ok else "failed", (comment or "")[:10000], item_id, worker_id),
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
        if ok:
            ticket_status = "review"
            cur.execute(
                f"UPDATE tickets SET status='review', updated_at={UTC_NOW} WHERE id=%s",
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


def resolve_concurrency(args, cfg: dict) -> int:
    """Flag beats saved config beats 1. Never below 1."""
    value = args.concurrency if args.concurrency is not None else cfg.get("concurrency")
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
                try:
                    ok, comment = executor.run(
                        ticket, work.get("claude_api_key"),
                        board=work.get("board"),
                        directory=paths.get(str(ticket["board_id"])),
                        session_id=work.get("session_id"))
                except Exception as exc:
                    ok, comment = False, f"Executor error: {exc!r}"
                finally:
                    with _SLOT_LOCK:
                        _RUNNING["n"] -= 1
                status = finish_work(conn, cfg["worker_id"], cfg["name"],
                                     work["assignment_id"], ticket["id"], ok, comment)
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
        enroll(args.server or DEFAULT_SERVER, args.join_code, name)
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
    concurrency = resolve_concurrency(args, cfg)
    if concurrency != cfg.get("concurrency"):
        cfg["concurrency"] = concurrency
        save_config(cfg)

    if not isinstance(executor, StubExecutor) and not configured_board_ids(cfg):
        print("WARNING: no board folders configured on this PC, so nothing will "
              "be claimed.\n         Fix with: --list-boards, then "
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
    # query, which is exactly what a busy worker stops issuing.
    hb_conn = None
    try:
        while any(t.is_alive() for t in slots):
            try:
                if hb_conn is None or hb_conn.closed:
                    hb_conn = psycopg.connect(cfg["dsn"], connect_timeout=15)
                set_slot_counts(hb_conn, cfg["worker_id"], concurrency,
                                running_count())
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
