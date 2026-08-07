"""kanban-cloud worker client.

Registers this PC into a cluster, then polls the server for delegated tickets.
Stdlib-only (urllib) so any machine with Python 3.10+ can run it — no pip installs.

Usage:
    # First run (registers and saves .worker_config.json next to this file):
    py worker.py --server http://your-server:8900 --join-code ABC12345 --name ryans-pc

    # Later runs (reuses saved config):
    py worker.py

    # Actually run tickets through the Claude CLI instead of the stub:
    py worker.py --real

Executors:
    StubExecutor   (default) — pretends to work, posts a fake result comment.
    ClaudeExecutor (--real)  — shells out to `claude -p "<prompt>"` with
                               ANTHROPIC_API_KEY set from the cluster's key.
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

CONFIG_PATH = Path(__file__).parent / ".worker_config.json"
POLL_SECONDS = 4


# ---------- tiny HTTP client ----------

class Api:
    def __init__(self, server: str, worker_token: str | None = None):
        self.server = server.rstrip("/")
        self.worker_token = worker_token

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(self.server + path, method=method)
        req.add_header("Content-Type", "application/json")
        if self.worker_token:
            req.add_header("X-Worker-Token", self.worker_token)
        data = json.dumps(body).encode() if body is not None else None
        with urllib.request.urlopen(req, data=data, timeout=30) as resp:
            return json.loads(resp.read().decode() or "{}")


# ---------- executors ----------

class StubExecutor:
    """Fake executor: waits a moment and produces a canned result."""

    name = "stub"

    def run(self, ticket: dict, api_key: str | None) -> tuple[bool, str]:
        print(f"  [stub] pretending to work on ticket #{ticket['id']}: {ticket['title']}")
        time.sleep(2)
        return True, (
            f"[StubExecutor] Completed ticket '{ticket['title']}' (attempt "
            f"{ticket.get('attempts', '?')}). This is a placeholder result — "
            "run the worker with --real to execute via the Claude CLI."
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


# ---------- worker loop ----------

def load_config() -> dict | None:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return None


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    print(f"Saved worker config to {CONFIG_PATH}")


def register(server: str, join_code: str, name: str) -> dict:
    api = Api(server)
    resp = api.call("POST", "/api/workers/register", {"join_code": join_code, "name": name})
    cfg = {
        "server": server,
        "worker_token": resp["worker_token"],
        "worker_id": resp["worker_id"],
        "name": name,
        "cluster": resp["cluster"],
    }
    save_config(cfg)
    print(f"Registered worker '{name}' in cluster '{resp['cluster']['name']}'")
    return cfg


def main() -> int:
    # Windows consoles often default to a legacy codepage (cp1252); a ticket
    # title with any non-encodable char would crash the worker *after* it
    # claimed the ticket, orphaning the claim. Never let printing kill us.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description="kanban-cloud worker")
    parser.add_argument("--server", help="server base URL, e.g. http://host:8900")
    parser.add_argument("--join-code", help="cluster join code (registers this PC)")
    parser.add_argument("--name", help="worker name (defaults to computer name)")
    parser.add_argument("--real", action="store_true",
                        help="use the Claude CLI executor instead of the stub")
    parser.add_argument("--poll", type=float, default=POLL_SECONDS,
                        help=f"poll interval seconds (default {POLL_SECONDS})")
    parser.add_argument("--once", action="store_true",
                        help="poll a single time then exit (for testing)")
    args = parser.parse_args()

    cfg = load_config()
    if args.join_code:
        if not args.server:
            print("--server is required when registering with --join-code")
            return 2
        name = args.name or os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or "worker"
        cfg = register(args.server, args.join_code, name)
    elif cfg is None:
        print("No saved config. First run needs --server and --join-code.")
        return 2
    elif args.server:
        cfg["server"] = args.server

    executor = ClaudeExecutor() if args.real else StubExecutor()
    api = Api(cfg["server"], cfg["worker_token"])
    print(f"Worker '{cfg['name']}' polling {cfg['server']} every {args.poll}s "
          f"(executor: {executor.name}). Ctrl+C to stop.")

    while True:
        try:
            resp = api.call("POST", "/api/work/poll")
            work = resp.get("work")
            if work:
                ticket = work["ticket"]
                item_id = work["assignment_id"]
                print(f"Claimed ticket #{ticket['id']} '{ticket['title']}' (assignment {item_id})")
                try:
                    ok, comment = executor.run(ticket, work.get("claude_api_key"))
                except Exception as exc:  # executor crashed — report failure
                    ok, comment = False, f"Executor error: {exc!r}"
                result = api.call("POST", f"/api/work/{item_id}/result",
                                  {"ok": ok, "comment": comment})
                print(f"  reported {'success' if ok else 'FAILURE'} -> "
                      f"ticket status: {result.get('ticket_status')}")
        except urllib.error.HTTPError as e:
            print(f"HTTP {e.code} from server: {e.read().decode()[:200]}")
            if e.code == 401:
                print("Worker token rejected — re-register with --join-code.")
                return 1
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            print(f"Server unreachable ({e}); retrying...")
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
