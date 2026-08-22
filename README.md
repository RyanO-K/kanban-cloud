# kanban-cloud

Multi-user hosted kanban with server-side work delegation to user PCs. Teams
("clusters") share boards; moving a ticket to **ready** queues it for an AI
agent; each member's PC runs a small worker client that talks **directly to
the Postgres database** — no HTTP work-polling API — to claim tickets,
execute them (stub or the real Claude CLI), and post results back. This is an
MVP — working end-to-end over polish.

## Architecture

Workers are direct-DB clients, not HTTP clients: each enrolled PC gets its
own Postgres **login role**, scoped by grants to exactly what a worker needs
(claim/update work, post comments, read tickets — nothing else), and talks
SQL straight to Neon for every poll/claim/heartbeat/result. The web service's
job for workers shrinks to one thing: the **enrollment desk** — a single HTTP
route that trades a cluster join code for a freshly-provisioned Postgres role
and DSN. Everything else in the FastAPI app is the human-facing surface
(login, boards, tickets, settings, the workers panel).

```
            browsers (login / boards)
                        |
                        v  HTTPS + bearer token
 +---------------------------------------------------+
 |  FastAPI server (app/main.py)                     |
 |   - auth: email+password, PBKDF2, token table     |
 |   - clusters (join codes), boards, tickets        |
 |   - work_queue: atomic claim via SQL              |
 |   - POST /api/workers/enroll: the ONLY             |
 |     worker-facing HTTP route (provisions a         |
 |     per-PC Postgres role, returns its DSN)         |
 |   - POST /api/workers/{id}/revoke: owner UI        |
 |     button drops that PC's Postgres role            |
 +---------------------------------------------------+
                        |
                        v  SQLAlchemy (admin role: neondb_owner)
        DATABASE_URL (Neon Postgres, sslmode=require)
        or ./kanban_cloud.db (SQLite, zero setup —
        worker enrollment requires Postgres, see below)
                        ^
                        |  direct SQL (per-PC login role,
                        |  poll every ~10s, no HTTP after enrollment)
 +---------------------------------------------------+
 |  worker.py on each PC (needs psycopg[binary])     |
 |   - one-time: --enroll over HTTP -> gets its own  |
 |     Postgres role + DSN, saved to                 |
 |     .worker_config.json                           |
 |   - thereafter: SQL claim (SKIP LOCKED), SQL       |
 |     heartbeat, SQL result-post — no HTTP           |
 |   - ClaudeExecutor (default, uses this PC's own   |
 |     local `claude` CLI auth) | StubExecutor        |
 |     (--stub: fake results for testing)            |
 +---------------------------------------------------+
```

### Grant model (`kanban_worker` group role)

Every per-PC role inherits one shared, `NOLOGIN` group role
(`kanban_worker`); schema changes touch its grants once instead of every PC
role individually:

- **SELECT** on `tickets`, `boards`, `clusters`, `workers`, `work_queue`,
  `comments`.
- **INSERT** on `comments`, `work_queue` (the failure-requeue path inserts
  its own retry row).
- **UPDATE** on `work_queue`, `tickets`, `workers`.
- **USAGE, SELECT** on all sequences (for the INSERTs above).
- **No DELETE** anywhere, and **no access at all** to `users` or
  `auth_tokens` — a compromised worker role cannot touch login credentials.

### Revocation

The owner-only "revoke" button in the workers panel (`POST
/api/workers/{id}/revoke`) drops that PC's Postgres role and terminates any
live session immediately — access is cut at the database, not just flagged
in a table. Re-enrolling the same PC (same `--enroll` command) recreates the
role and restores access with a fresh password.

Ticket statuses (vocabulary adapted from the local `.kanban` tool):
`todo → ready → doing → review → done`, plus `failed`. Moving a ticket into
**ready** (drag, edit, or the "Run on agent now" button) enqueues it in
`work_queue`. If `target_worker` is set, only that PC can claim it; otherwise
any polling worker in the cluster wins. Each worker claims with
`UPDATE work_queue SET status='claimed' ... WHERE id = (SELECT ... WHERE
status='queued' ... FOR UPDATE SKIP LOCKED LIMIT 1)` run directly against
Postgres, so two pollers can never double-claim and neither blocks the
other. A failed run requeues once, then the ticket goes to `failed`.

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
sees the same boards. **Worker enrollment requires a Postgres `DATABASE_URL`**
(the SQLite fallback has no role/grant system to provision into); the
`neondb_owner`-style connection string's credentials become the server's
admin role for creating and dropping per-PC worker roles.

## Start a worker on another PC

### Client PC install (.exe)

1. Download `kanban-worker.exe` from the latest `worker-v*` GitHub Release
   (repo is private — download while signed in and copy it to the PC).
2. Put the exe in a folder of its own (it writes `.worker_config.json`
   next to itself).
3. Double-click it. On first run it asks for the cluster join code (shown
   in the board's workers panel), enrolls, and starts polling.
4. For real ticket execution, install the Claude CLI on the PC (`claude`
   must be on PATH). Pass `--stub` to test without it.

First-run notes: Windows SmartScreen will warn about the unsigned exe
(More info → Run anyway). To decommission a PC: click **Revoke** in the
board UI, then delete the exe's folder (the config, including the DB
credential, lives there).

Releasing a new version: `git tag worker-vX.Y.Z && git push origin
worker-vX.Y.Z` — CI builds the exe and attaches it to the release. Tag
pushes do not trigger the Render deploy.

### Dev / script setup

Copy `worker.py` to the PC and install its one dependency (Python 3.10+):

```powershell
pip install "psycopg[binary]"

# one-time enrollment: trades the join code (shown in the UI) for this PC's
# own Postgres role + DSN, saved to .worker_config.json
py worker.py --enroll --join-code <CODE> --name my-pc

# later runs reuse .worker_config.json and talk SQL directly — no HTTP:
py worker.py            # real executor (Claude CLI)
py worker.py --stub     # stub executor for testing
```

The Claude CLI is required for real ticket execution (`claude` must be on PATH
**and already authenticated on this PC** — run `claude login` once, or set
your own `ANTHROPIC_API_KEY` in the environment the worker runs in). The
cluster does not store or forward a key; each worker uses whatever local
Claude Code configuration already exists on that machine.
Default poll interval is 10s (`--poll N` to change it, `--once` to poll a
single time and exit).

The workers panel in the UI shows each PC and whether it is online (heartbeat
within 30s), and has an owner-only **revoke** button per worker (see
"Revocation" above). Per ticket, "Run on" selects a specific PC or "any
worker".

**Neon compute note:** while a worker is running, its polling counts as
activity and keeps the database's compute awake — Neon's free-tier
autosuspend only kicks in when nothing is querying it. Stop workers you
aren't actively using if you want the DB to suspend.

## Tests

```powershell
.venv\Scripts\python -m pytest tests/ -q
```

Runs against the SQLite fallback; covers auth, ticket CRUD + cluster scoping,
atomic claim/double-claim guard, target-worker routing,
offline-target queuing, cross-cluster isolation, and failure/retry handling.

## Deploying behind a reverse proxy (portfolio-site mode)

The app can run behind a trusted reverse proxy (e.g. the portfolio site
serving it at `/board/` behind its own GitHub auth) with a public read-only
spectator view. The frontend uses only **relative `./api/...` URLs**, so it
works unchanged under any path prefix — just have the proxy forward
`/board/…` → `…` (and redirect `/board` → `/board/` so relative URLs resolve).

**Env var: `PROXY_SHARED_SECRET`** — setting it (non-empty) turns proxy mode
on; unset, the app behaves exactly as before (local login/register UI). With
it set, every route **except worker enrollment** requires the header
`X-Proxy-Secret: <secret>` (constant-time compare, 403 otherwise), so nobody
can reach the app around the proxy. The one exempt route:

- `POST /api/workers/enroll` — the only HTTP a worker PC ever calls; after
  enrollment, workers talk direct SQL to Postgres and never touch this proxy
  gate at all.

Note `GET /api/health` is *behind* the gate too — platform health checks must
send the secret header (or probe a TCP connect).

**Env var: `PROXY_LOGIN_URL`** (optional) is a site-relative URL the spectator
UI turns into a "Sign in with GitHub" button — for the portfolio deployment,
`/auth/github?return=/board/`. Unset means no button is shown, which is the
right behaviour for a local run where there is no site in front. It is returned
to the browser as `login_url` on `GET /api/session`.

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
  (polling stays on) with all mutating controls hidden, a "viewing read-only"
  note, and — when `PROXY_LOGIN_URL` is set — a sign-in button.

**`GET /api/session`** tells the frontend which world it is in:

```jsonc
{"mode": "local"}                                        // no PROXY_SHARED_SECRET
{"mode": "owner", "user": {"id": 1, "email": "ryan@proxy.user"}}
{"mode": "spectator", "cluster": {"id": 1, "name": "Main"}, "board": {"id": 2, "name": "Demo"}, "login_url": "/auth/github?return=/board/"}
// spectator: cluster/board are null if none exist yet; login_url is null
// unless PROXY_LOGIN_URL is set. `board` is the board to land on — the one
// named "Demo" when it exists, else the cluster's first board.
```

### Demo board

Anonymous visitors land on a board named `Demo` when one exists (that is what
`board` on `GET /api/session` selects), so the public view shows example work
instead of an empty column set. Populate it once, after signing in for the
first time:

    py scripts/seed_demo.py "<neon-dsn>"

The script is idempotent — if the demo board already holds a ticket it writes
nothing, so cards you delete stay deleted. Seeded tickets never enter the work
queue, so an enrolled worker cannot claim one even though two of them sit in
`ready`.

## Importing a local `.kanban` board

The **Import** button in the header (owner-only) loads a board from the local
file-based `.kanban` tool. Click it and pick a board folder, or drag the folder
onto the board area. One board per import: pick a single board folder (e.g.
`.kanban/ai-kanban`), not the `.kanban` root — dropping the root tells you which
boards it found instead of importing all of them.

Import **always creates a new board**, named after the folder, suffixed `(2)`,
`(3)` on a name clash. It never merges into or edits an existing board, so
re-importing is safe and there is no sync, dedupe, or conflict resolution to
reason about. There is no cloud-to-local export.

The server runs on Render and cannot see your disk, so the browser does the
reading and posts the tickets up. It sends nine keys per ticket — `title`,
`detail`, `status`, `comments`, `dependsOn`, `blocks`, `steps`, `files`,
`outputs` — and drops `history`, `commitGate`, `runLogFile`, `completedLog`,
`claudeSessionId` and `claudeSessionDir` before anything leaves the machine.
That is run exhaust from another computer, and it is most of the bulk: the six
live local boards are 0.9–2.8 MB on disk but 33–204 KB on the wire.

The two status vocabularies differ, so they are mapped (`app/importer.py`):

| local | cloud |
|---|---|
| `todo`, `pending`, `blocked` | `todo` |
| `ready` | `ready` |
| `in_progress` | `doing` |
| `completed` | `done` |
| already-cloud names (`done`, `review`, `failed`, `doing`) | unchanged |
| anything else | `todo` |

`blocked` has no cloud equivalent. Nothing is silently lost: every imported
ticket gets an appendix in its body recording the source board, the local
ticket number and its original local status, followed by whichever of
`dependsOn` / `blocks` / `steps` / `files` / `outputs` it had. Those arrive as
prose, not as a dependency graph — the cloud schema has no such concept.

Imported `ready` tickets **are queued for agents**, exactly as moving a ticket
to `ready` in the UI is. The response reports how many (`queued`). In practice
this is currently inert: all 125 tickets across the six live local boards are
`completed`, `done` or `blocked`.

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
  browser bearer tokens and worker enrollment DSNs (which embed a Postgres
  password) transit the wire.
- Bearer tokens are random 256-bit strings in the DB, but they never expire
  and there is no logout-everywhere or rate limiting.
- There is no cluster-stored Claude API key. Each worker PC authenticates the
  `claude` CLI with its own local configuration (a `claude login` session or
  the operator's own `ANTHROPIC_API_KEY`), so there is nothing for the
  server, the DB, or a compromised worker's Postgres role to leak — a key
  simply never crosses the wire or lands in a table.
- Any cluster member can delete tickets; there are no roles/permissions.
- No stale-claim reaper: if a worker dies mid-ticket, the `work_queue` row
  stays `claimed` forever. Nothing times it out automatically — dragging the
  ticket back to `ready` (or "Run now") supersedes the stale claim and
  enqueues a fresh item; a late result from the dead claim is rejected
  (superseded), so this is a manual-but-safe recovery path, not a real fix.
- A running worker keeps the Neon database's compute awake (see "Neon
  compute note" above) — stop workers you don't need running.
- `ClaudeExecutor` runs the Claude CLI with whatever permissions that CLI has
  on the worker PC — scope what those machines can do.
