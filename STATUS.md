# STATUS — kanban-cloud MVP

Last updated: 2026-08-07

## Done

- **Server** (`app/main.py`): FastAPI + SQLAlchemy 2.0. `DATABASE_URL` env
  (Neon Postgres, psycopg3 dialect, auto `sslmode=require`) with automatic
  SQLite fallback `./kanban_cloud.db`; tables auto-created on startup
  (`schema.sql` documents the Postgres DDL).
- **Data model** (`app/models.py`): users (PBKDF2 password hash), auth tokens,
  clusters + join codes + members, cluster_settings (Claude API key — masked to
  browsers, delivered only to claiming worker), boards, tickets
  (todo/ready/doing/review/done/failed, `target_worker` nullable = any),
  comments, workers (last_seen/online), work_queue (doubles as assignment log).
- **Delegation** (`app/delegation.py`): moving a ticket to `ready` (or
  `POST /run`) enqueues it idempotently; claim is a transactional
  `UPDATE ... WHERE status='queued'` (rowcount guard against double-claim);
  target_worker routing; failure requeues once then `failed`; success →
  `review` with the worker's result comment.
- **Worker client** (`worker.py`): stdlib-only CLI; registers via join code,
  polls `/api/work/poll` every 4s (heartbeat), StubExecutor default,
  ClaudeExecutor behind `--real` (shells `claude -p` with ANTHROPIC_API_KEY
  from the cluster key), posts results, handles server-unreachable and 401.
- **Frontend** (`app/static/index.html`): single static page, no build step —
  login/register, cluster create/join (join code shown), board columns with
  drag-and-drop, ticket modal (edit, comments, "run on: any|worker" selector,
  Run now, delete), workers panel with online dots, settings panel for the
  cluster Claude API key, recent-delegations log, 5s auto-refresh.
- **Tests**: 19 pytest tests (auth, ticket CRUD, cluster scoping, key masking,
  atomic claim + double-claim guard, routing, offline-target queuing,
  cross-cluster isolation, retry-then-fail, re-register token rotation).
- **Smoke-tested end-to-end** against a real uvicorn server: register → cluster
  → set API key → create `ready` ticket → `worker.py --once` claimed it, stub
  ran, ticket landed in `review` with the worker's comment; UTF-8 verified
  intact in the DB.
- README with architecture sketch, Neon setup, worker setup, security caveats.

## Final test output

```
$ .venv/Scripts/python -m pytest tests/ -q
19 passed, 1 warning in 5.89s
```

(the warning is an upstream FastAPI/Starlette TestClient deprecation notice)

## In progress

- Nothing — MVP complete.

## Blockers

- None.

## Known issues / deferred (MVP cuts)

- Not deployed anywhere and no real Neon DB provisioned (by design for this
  pass); Postgres path is code-complete but only exercised via SQLite.
- Polling (worker 4s, UI 5s) instead of websockets/long-poll hold.
- No stale-claim reaper: if a worker dies mid-ticket the assignment stays
  `claimed` forever (manual fix: move ticket back to `ready`... which enqueues
  a fresh item; the orphaned work_queue row remains as log noise).
- Auth tokens never expire; no roles; any member can edit settings.
- API key plaintext in DB; join code effectively grants key access (see README
  security caveats).
- ClaudeExecutor is fire-and-forget per ticket (30-min timeout, no streaming
  progress; `/api/work/{id}/progress` endpoint exists but the executor doesn't
  use it yet).

## Wave 2 verification (2026-08-07)

Independent verification pass: fresh test run, live smoke against a real
uvicorn server, code review of the delegation/auth/masking/frontend paths, and
an offline check of the Postgres dialect wiring. Four real bugs found and
fixed (commits `c5bb31b`..`627ce17`), 5 regression tests added.

### Test output (after fixes)

```
$ .venv/Scripts/python -m pytest tests/ -q
24 passed, 1 warning in 9.13s
```

(19 pre-existing tests also passed before any change was made; the warning is
the same upstream Starlette TestClient deprecation notice.)

### Live smoke (uvicorn on :8931, scratch SQLite via DATABASE_URL)

register → create cluster (`join_code RZLDDSYE`) → PUT settings key (response
masked: `••••••••9xyz`) → create ticket with UTF-8 title
"Smoke ticket ✓ vérify UTF-8" in `ready` → `py worker.py --once`:

```
Claimed ticket #1 'Smoke ticket ? v�rify UTF-8' (assignment 2)
  [stub] pretending to work on ticket #1: ...
  reported success -> ticket status: review
```

Ticket verified in `review` with the worker's comment; title/comment UTF-8
round-tripped intact through the DB (the `?`/`�` above are console-display
replacement chars only — see bug 1). Workers panel showed the PC online/idle;
delegation log showed the done item. Server killed cleanly afterwards.

### Bugs found and fixed

1. **worker.py crashed after claiming when a ticket title wasn't cp1252-
   encodable** (`UnicodeEncodeError` printing "✓" on the default Windows
   console) — found live on the very first smoke run. The crash happened
   *after* the server-side claim, orphaning the assignment and leaving the
   ticket stuck in `doing`. stdout/stderr are now reconfigured with
   `errors="replace"`. (`c5bb31b`)
2. **An orphaned `claimed` work item permanently blocked re-delegation.**
   `enqueue_ticket` no-opped on queued *or claimed* items, so the manual
   recovery this file used to describe ("move the ticket back to ready...
   enqueues a fresh item") did not actually work — verified live: the patch
   left status `ready` with nothing queued and the worker found no work.
   Explicit re-enqueue (drag to ready / Run now) now supersedes a stale claim
   (item → `failed`, result "superseded...") and queues a fresh item; a late
   result for the superseded assignment is still rejected with 409.
   (`c843fe8`)
3. **Retry budget was cumulative across delegations.** `ticket.attempts` was
   never reset, so re-running a ticket that had already failed permanently
   went straight back to `failed` with zero retries (and a re-run of a
   reviewed ticket got one fewer). Fresh user-initiated enqueues reset
   `attempts`; the internal failure-requeue in `finish_work` keeps the count,
   so MAX_ATTEMPTS still bounds each delegation. (`c843fe8`)
4. **`create_ticket` validated `body.status` then ignored it** (hardcoded
   `todo`): creating a ticket as doing/review/done silently landed in todo.
   It also accepted a `target_worker` from another cluster (PATCH already
   rejected that), yielding a never-claimable ticket. (`4cbf2a6`)

Hardening (low severity): `mask_secret` appended the last 4 chars
unconditionally, revealing keys of ≤4 chars entirely; values ≤8 chars are now
fully masked (`b6452bc`). `idx_work_queue_claim` existed only in `schema.sql`,
not in the models, so auto-created DBs (the default path everywhere) lacked
the claim index (`627ce17`).

### Reviewed and found sound

- **Atomic claim**: `UPDATE ... WHERE id=? AND status='queued'` + rowcount
  guard is correct under Postgres READ COMMITTED (blocked UPDATE re-evaluates
  the predicate after the winner commits → rowcount 0 → next candidate) and
  under SQLite's single-writer locking. Worst case on SQLite is an occasional
  SQLITE_BUSY 500 for one poller under true concurrency, which worker.py
  already absorbs (prints, re-polls). Double-claim is not possible.
- **Heartbeat/online**: `last_seen` is naive-UTC on both write and compare;
  poll and result endpoints both refresh it; 30s window vs 4s poll is sound.
- **Token auth**: bearer + X-Worker-Token lookups correct; worker token
  rotation on re-register invalidates the old token (tested). No expiry — a
  documented MVP cut.
- **Key masking**: every browser-facing path masks (GET/PUT settings, tested);
  the delegation-log endpoint never serializes `result`; FastAPI validation
  errors echo only client-sent input, and 500s carry no detail. The full key
  goes only to the claiming worker (tested).
- **Frontend refresh**: the 5s interval skips `loadBoard()` while the ticket
  modal is open, `loadSettings()` is not on the interval, and the panels it
  rewrites contain no inputs — no form-clobbering path found.

### Postgres dialect wiring (offline — no DB contacted)

`psycopg[binary]` installed in the venv; engines constructed but never
connected (plus a stubbed `psycopg.connect` to capture kwargs):

- `postgres://...` and `postgresql://...` → `postgresql+psycopg://...`;
  explicit `postgresql+psycopg://` passes through.
- URL without sslmode → `sslmode='require'` demonstrably reaches
  `psycopg.connect`; URL with `?sslmode=verify-full` is preserved, not
  overridden. `pool_pre_ping=True` set on Postgres engines only.

### Remaining issues (unchanged MVP cuts, plus notes)

- Still no stale-claim *reaper*: a dead worker's claim now no longer blocks
  re-delegation (bug 2 fix), but requires a human to drag the ticket back to
  ready; nothing times claims out automatically.
- Worker shows offline during long `--real` executions (it only heartbeats
  via poll/progress, and ClaudeExecutor still doesn't call `/progress`).
- Tokens never expire; no roles; API key plaintext in DB; HTTP by default —
  all as documented under Security caveats.
- Postgres path still only exercised offline/SQLite; no real Neon DB yet.
  *(Resolved — see "Neon live smoke" below.)*

## Neon live smoke (2026-08-07)

First run against a **real Neon Postgres** (project `polished-glade-71097631`,
fresh/empty; connection string in gitignored `.env` — the app does not load
`.env` itself, so `DATABASE_URL` was exported in the shell before starting
uvicorn on :8912).

### Startup + auto-create

`GET /api/health` → `{"ok":true,"db":"postgresql+psycopg"}`. The
`postgresql://...?sslmode=require` URL normalized to the psycopg3 dialect and
connected over TLS (server reported `PostgreSQL 18.4 ... aarch64-linux`).
Querying `information_schema` through the app's own engine showed all 10
tables auto-created on startup:

```
tables: ['auth_tokens', 'boards', 'cluster_members', 'cluster_settings',
         'clusters', 'comments', 'tickets', 'users', 'work_queue', 'workers']
claim index present: True   (idx_work_queue_claim — the 627ce17 fix, verified live)
```

### End-to-end (all against Neon)

register `smoke-neon@example.com` → cluster `neon-smoke-cluster` (join code
`LZXAYL6J`) → PUT settings with placeholder key `sk-ant-test-000-placeholder`
(NOT a real key) → both PUT and GET responses masked (`••••••••lder`; asserted
the full key appears in neither) → board → ticket "Neon smoke ticket ✓ vérify
UTF-8" created `todo`, PATCHed to `ready` → `worker.py --once`:

```
Registered worker 'neon-smoke-pc' in cluster 'neon-smoke-cluster'
Claimed ticket #1 'Neon smoke ticket ? v�rify UTF-8' (assignment 1)
  [stub] pretending to work on ticket #1: ...
  reported success -> ticket status: review
```

Ticket verified in `review` with the worker's `[StubExecutor] Completed...`
comment; title/comment UTF-8 round-tripped intact through Neon (direct
`select` shows `'Neon smoke ticket ✓ vérify UTF-8'`; the `?`/`�` above are the
known console-display replacement chars). Delegation log endpoint and the
`work_queue` row both show `queued_at → claimed_at → finished_at`, status
`done`, `claimed_by` = worker 1; workers panel showed the PC online/idle.

### Atomic claim under real Postgres concurrency

Registered two extra workers, queued ONE ticket, then fired **8 concurrent**
`POST /api/work/poll` claim attempts (2 workers × 4 threads, barrier-released
simultaneously):

```
HTTP statuses: [200, 200, 200, 200, 200, 200, 200, 200]
winners: 1 | empty polls: 7 | errors: 0
```

Exactly one poll received the assignment (with the full — unmasked — cluster
key, as designed for the claiming worker only); no 500s, no double-claim. The
winner's result posted → ticket `review` / item `done`; a duplicate result
post was rejected with **409** as designed.

### Wrap-up

- Server killed cleanly; no local files changed except this STATUS.md
  (`.env` and `.worker_config.json` are gitignored, verified via `git status`).
- **Smoke-test data left in the Neon DB** (1 user, 1 cluster, 2 boards
  incl. the auto-default, 2 tickets in `review`, 2 done `work_queue` rows,
  3 workers, and the placeholder API key). It is harmless fixture data on an
  otherwise fresh DB; wipe with `drop schema public cascade; create schema
  public;` (or just delete the rows) before real use if desired.
- No permission-classifier blocks were hit during this pass.
