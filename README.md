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

## Deploying behind a reverse proxy (portfolio-site mode)

The app can run behind a trusted reverse proxy (e.g. the portfolio site
serving it at `/board/` behind its own GitHub auth) with a public read-only
spectator view. The frontend uses only **relative `./api/...` URLs**, so it
works unchanged under any path prefix — just have the proxy forward
`/board/…` → `…` (and redirect `/board` → `/board/` so relative URLs resolve).

**Env var: `PROXY_SHARED_SECRET`** — setting it (non-empty) turns proxy mode
on; unset, the app behaves exactly as before (local login/register UI). With
it set, every route **except the worker-facing four** requires the header
`X-Proxy-Secret: <secret>` (constant-time compare, 403 otherwise), so nobody
can reach the app around the proxy. The exempt worker routes, which keep
their own join-code / `X-Worker-Token` auth because worker PCs connect
directly rather than through the browser proxy:

- `POST /api/workers/register`
- `POST /api/work/poll` (doubles as the heartbeat)
- `POST /api/work/{id}/result`
- `POST /api/work/{id}/progress`

Note `GET /api/health` is *behind* the gate too — platform health checks must
send the secret header (or probe a TCP connect).

**Identity headers** (the proxy must strip any client-supplied `X-Proxy-*`
headers before injecting its own):

- `X-Proxy-User: <login>` → **owner**: a full-rights account
  `<login>@proxy.user` is auto-provisioned (no password login possible) and
  auto-joined to the *default cluster* — the oldest cluster, auto-created as
  "Main" (with a "Main" board) on the first owner request. No login UI is
  shown; the proxy is the authenticator.
- No `X-Proxy-User`, or `X-Proxy-Readonly: 1` → **spectator**: server-side
  read-only. Only safe GETs are allowed (the page itself, board list,
  tickets + comments, workers status, delegation queue, health, session);
  the cluster list (join codes!) and cluster settings are denied, and every
  non-GET returns 403. The UI renders the default cluster's board live
  (polling stays on) with all mutating controls hidden and a
  "viewing read-only — owner login via site" note.

**`GET /api/session`** tells the frontend which world it is in:

```jsonc
{"mode": "local"}                                        // no PROXY_SHARED_SECRET
{"mode": "owner", "user": {"id": 1, "email": "ryan@proxy.user"}}
{"mode": "spectator", "cluster": {"id": 1, "name": "Main"}}  // cluster null if none yet
```

Example nginx-ish proxy config:

```
location /board/ {
    # ... site's own auth decides $ghuser (empty for anonymous visitors) ...
    proxy_set_header X-Proxy-Secret "<PROXY_SHARED_SECRET>";
    proxy_set_header X-Proxy-User   $ghuser;      # omit/empty => spectator
    proxy_pass http://kanban-cloud:8900/;
}
```

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
