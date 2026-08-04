# kanban-cloud

Multi-user hosted kanban with server-side work delegation to user PCs. Teams
("clusters") share boards; moving a ticket to **ready** queues it for an AI
agent; each member's PC runs a small worker client that polls the server,
claims tickets, executes them (stub or the real Claude CLI), and posts results
back. This is an MVP — working end-to-end over polish.

## Architecture

```
            browsers (login / boards / settings)
                        |
                        v  HTTPS + bearer token
 +---------------------------------------------------+
 |  FastAPI server (app/main.py)                     |
 |   - auth: email+password, PBKDF2, token table     |
 |   - clusters (join codes), boards, tickets        |
 |   - cluster_settings: Claude API key (masked      |
 |     to browsers; sent only to claiming worker)    |
 |   - work_queue: atomic UPDATE...WHERE claim       |
 +---------------------------------------------------+
                        |
                        v  SQLAlchemy
        DATABASE_URL (Neon Postgres, sslmode=require)
        or ./kanban_cloud.db (SQLite, zero setup)
                        ^
                        |  poll every ~4s (X-Worker-Token)
 +---------------------------------------------------+
 |  worker.py on each PC (stdlib-only)               |
 |   - registers with cluster join code              |
 |   - POST /api/work/poll = heartbeat + claim       |
 |   - StubExecutor (default) | ClaudeExecutor       |
 |     (--real: `claude -p ...` with cluster key)    |
 |   - POST /api/work/{id}/result                    |
 +---------------------------------------------------+
```

Ticket statuses (vocabulary adapted from the local `.kanban` tool):
`todo → ready → doing → review → done`, plus `failed`. Moving a ticket into
**ready** (drag, edit, or the "Run on agent now" button) enqueues it in
`work_queue`. If `target_worker` is set, only that PC can claim it; otherwise
any polling worker in the cluster wins. Claims are guarded by a transactional
`UPDATE work_queue SET status='claimed' ... WHERE status='queued'` so two
pollers can never double-claim. A failed run requeues once, then the ticket
goes to `failed`.

## Run the server

```powershell
cd kanban-cloud
py -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m uvicorn app.main:app --host 0.0.0.0 --port 8900
```

Open http://localhost:8900 — register, create a cluster (a join code is
generated), add tickets. With no `DATABASE_URL` set it just works against a
local `kanban_cloud.db` SQLite file (tables auto-created on startup).

## Point it at Neon (cross-computer storage)

1. Create a Neon Postgres database and copy its connection string.
2. Install the driver and set `DATABASE_URL` before starting:

```powershell
.venv\Scripts\pip install "psycopg[binary]"
$env:DATABASE_URL = "postgresql://user:pass@ep-xxx.neon.tech/neondb?sslmode=require"
.venv\Scripts\python -m uvicorn app.main:app --host 0.0.0.0 --port 8900
```

`postgres://` / `postgresql://` URLs are normalized to the psycopg3 dialect and
`sslmode=require` is added automatically if missing. Tables are created on
startup; `schema.sql` documents the same schema if you prefer to create them by
hand. For multiple PCs to reach the server, host it somewhere they can all see
(a small VM, Render, etc.) — the DB being in Neon means any server instance
sees the same boards.

## Start a worker on another PC

Copy `worker.py` to the PC (Python 3.10+, no pip installs needed — stdlib only):

```powershell
# first run: register into the cluster using the join code shown in the UI
py worker.py --server http://your-server:8900 --join-code ABC12345 --name ryans-pc

# later runs reuse .worker_config.json
py worker.py

# execute tickets for real via the Claude CLI (needs `claude` on PATH);
# the cluster's Claude API key (Settings panel) is delivered on claim and
# exported as ANTHROPIC_API_KEY for the CLI:
py worker.py --real
```

The workers panel in the UI shows each PC and whether it is online (heartbeat
within 30s). Per ticket, "Run on" selects a specific PC or "any worker".

## Tests

```powershell
.venv\Scripts\python -m pytest tests/ -q
```

Runs against the SQLite fallback; covers auth, ticket CRUD + cluster scoping,
API-key masking, atomic claim/double-claim guard, target-worker routing,
offline-target queuing, cross-cluster isolation, and failure/retry handling.

## Security caveats (MVP)

- **HTTP by default** — put it behind TLS (reverse proxy) before real use;
  tokens and the Claude API key transit the wire.
- Bearer/worker tokens are random 256-bit strings in the DB, but they never
  expire and there is no logout-everywhere or rate limiting.
- The cluster Claude API key is stored **in plaintext in the DB** (masked in
  API responses, delivered only to claiming workers). Anyone with a cluster
  join code can register a worker and receive the key — treat join codes as
  secrets and rotate the API key if a code leaks.
- Any cluster member can change settings or delete tickets; there are no
  roles/permissions.
- `ClaudeExecutor` runs the Claude CLI with whatever permissions that CLI has
  on the worker PC — scope what those machines can do.
