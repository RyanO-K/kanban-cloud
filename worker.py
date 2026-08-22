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
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    import psycopg
except ImportError as exc:
    sys.exit(
        "Missing Postgres driver: no working psycopg backend is installed "
        f"({exc}).\nFix: pip install \"psycopg[binary]\"\n"
        'Then re-run: py worker.py --enroll --join-code ABC12345'
    )

DEFAULT_SERVER = "https://kanban-cloud.onrender.com"


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
CLAIM_SQL = f"""
UPDATE work_queue SET status='claimed', claimed_by=%(wid)s, claimed_at={UTC_NOW}
WHERE id = (
  SELECT wq.id FROM work_queue wq
  JOIN tickets t ON t.id = wq.ticket_id
  WHERE wq.status='queued' AND wq.cluster_id=%(cid)s
    AND (t.target_worker IS NULL OR t.target_worker = %(wid)s)
  ORDER BY wq.queued_at, wq.id
  FOR UPDATE OF wq SKIP LOCKED
  LIMIT 1
)
RETURNING id, ticket_id
"""


# ---------- executors (unchanged from v1) ----------

class StubExecutor:
    """Fake executor: waits a moment and produces a canned result."""

    name = "stub"

    def run(self, ticket: dict, api_key: str | None) -> tuple[bool, str]:
        print(f"  [stub] pretending to work on ticket #{ticket['id']}: {ticket['title']}")
        time.sleep(2)
        return True, (
            f"[StubExecutor] Completed ticket '{ticket['title']}' (attempt "
            f"{ticket.get('attempts', '?')}). This is a placeholder result — "
            "run the worker without --stub to execute via the Claude CLI."
        )


class ClaudeExecutor:
    """Real executor: shells out to the Claude CLI with the cluster's API key."""

    name = "claude"

    def run(self, ticket: dict, api_key: str | None) -> tuple[bool, str]:
        if not api_key:
            return False, "No Claude API key configured for this cluster (set it in Settings)."
        prompt = (
            f"You are working a kanban ticket.\n"
            f"Title: {ticket['title']}\n\n"
            f"Details:\n{ticket.get('body') or '(no details)'}\n\n"
            f"Do the work described, then reply with a concise summary of what you did."
        )
        env = dict(os.environ)
        env["ANTHROPIC_API_KEY"] = api_key
        print(f"  [claude] running `claude -p ...` for ticket #{ticket['id']}")
        try:
            proc = subprocess.run(
                ["claude", "-p", prompt],
                env=env,
                capture_output=True,
                text=True,
                timeout=1800,
                shell=(os.name == "nt"),  # find claude.cmd shim on Windows
            )
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


def claim_next(conn, worker_id: int, cluster_id: int) -> dict | None:
    """Claim the oldest eligible queued item; returns the v1 poll payload
    shape or None. One transaction: claim + ticket flip + key read."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(CLAIM_SQL, {"wid": worker_id, "cid": cluster_id})
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
        cur.execute(
            f"UPDATE workers SET status='working', last_seen={UTC_NOW} WHERE id=%s",
            (worker_id,),
        )
        cur.execute(
            "SELECT claude_api_key FROM cluster_settings WHERE cluster_id=%s",
            (cluster_id,),
        )
        key_row = cur.fetchone()
        return {
            "assignment_id": item_id,
            "claude_api_key": key_row[0] if key_row else None,
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
    return parser


def pick_executor(args):
    return StubExecutor() if args.stub else ClaudeExecutor()


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

    executor = pick_executor(args)
    print(f"Worker '{cfg['name']}' polling Postgres every {args.poll}s "
          f"(executor: {executor.name}). Ctrl+C to stop.")

    conn = None
    while True:
        try:
            if conn is None or conn.closed:
                conn = psycopg.connect(cfg["dsn"], connect_timeout=15)
            work = claim_next(conn, cfg["worker_id"], cfg["cluster_id"])
            if work:
                ticket = work["ticket"]
                item_id = work["assignment_id"]
                print(f"Claimed ticket #{ticket['id']} '{ticket['title']}' "
                      f"(assignment {item_id})")
                try:
                    ok, comment = executor.run(ticket, work.get("claude_api_key"))
                except Exception as exc:
                    ok, comment = False, f"Executor error: {exc!r}"
                status = finish_work(conn, cfg["worker_id"], cfg["name"],
                                     item_id, ticket["id"], ok, comment)
                print(f"  reported {'success' if ok else 'FAILURE'} -> "
                      f"ticket status: {status}")
            else:
                heartbeat(conn, cfg["worker_id"])
        except psycopg.OperationalError as e:
            msg = str(e)
            print(f"Database unreachable ({msg[:200]}); retrying...")
            if "password authentication failed" in msg or "does not exist" in msg:
                print("Credentials rejected — this PC may be revoked. Re-enroll.")
                pause_if_frozen()
                return 1
            conn = None
        except KeyboardInterrupt:
            print("\nStopping worker.")
            return 0

        if args.once:
            return 0
        try:
            time.sleep(args.poll)
        except KeyboardInterrupt:
            print("\nStopping worker.")
            return 0


if __name__ == "__main__":
    sys.exit(main())
