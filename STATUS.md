# STATUS — kanban-cloud MVP

Last updated: 2026-08-22

## Ticket dependencies gate claiming (2026-08-22)

Phase 2 of the gap analysis (`docs/2026-08-22-local-vs-cloud-gap-analysis.md`,
§8 item 4): a new `ticket_deps(ticket_id, depends_on_id)` table, cluster-scoped
via each ticket's board (no `cluster_id` of its own). `blocks` is derived by
querying the reverse edge rather than stored.

The gate lives in `worker.CLAIM_SQL` itself (`worker.py`), not a Python check
after claiming — a queued ticket is skipped unless every dependency has
reached `done` or `review`. The predicate (`worker.DEPS_MET_SQL`) is plain
ANSI SQL split out from the rest of `CLAIM_SQL`, which needs Postgres-only
`SKIP LOCKED`/array syntax; being portable let it be proven behaviorally
against a scratch SQLite DB in `tests/test_worker.py` instead of only
string-matched, which is all the pre-existing board-filter predicate gets.

`PATCH /api/tickets/{id}` takes a `depends_on` list (ticket editor, owner
only): validates every id exists and shares the ticket's cluster, then
rejects the save outright if it would create a cycle (BFS over existing
edges from each candidate dependency back to the ticket being edited — a
self-dependency is the one-node case of this, no special-casing needed).
Deleting a ticket clears both its own edges and any edge pointing at it.
Ticket JSON gained `depends_on`, `blocks`, and a derived `blocked` bool; the
board view shows a red "blocked" badge on cards with an unmet dependency, and
the ticket modal grew a multi-select picker with a matching note.

`kanban_worker`'s SELECT grant (`app/enrollment.py`) picked up `ticket_deps`
so the claim query's join can read it; `ensure_worker_group` re-applies
grants on every startup, so an already-provisioned Neon DB picks this up
without a manual step.

Tests: 201 → 217 (9 dependency-graph + display, 4 claim-gating, 3 markup).

Not yet done — the remaining phases: concurrency cap enforced in the claim
transaction, `tickets.order`, stale-claim reaper, progress streaming, kill,
human-in-the-loop, profiles/triage, git/worktree isolation.

## Agents do real repo work, and PCs limit their own concurrency (2026-08-22)

Cloud agents could not do repo work at all: `ClaudeExecutor` ran `claude -p`
with no `cwd`, no tool grant, and a prompt built from the ticket title and
body alone. It has now got all three. Spec:
`docs/superpowers/specs/2026-08-22-agents-do-real-repo-work-design.md`, plan:
`docs/superpowers/plans/2026-08-22-agents-do-real-repo-work.md`. This is
Phase 1 of the local-vs-cloud gap analysis in
`docs/2026-08-22-local-vs-cloud-gap-analysis.md`.

The design splits "where does this agent run" in two, because the two halves
have different owners. Project facts — description, out-of-scope,
commit requirements, worktree preference — are board columns on the server,
since every PC working that board needs the same ones. The *folder* is not:
the same board is worked by machines with different layouts, so it lives in
each worker's own `.worker_config.json`, keyed by board id, set with
`--set-path`. The prompt is therefore composed on the worker, by
`app/prompt.py`, which is stdlib-only by contract — `worker.py` imports it and
PyInstaller follows static imports into the onefile exe, so a SQLAlchemy
import there would drag the whole server in. A test enforces that.

`CLAIM_SQL` grew a board predicate so a PC with no folder for a board goes
idle instead of claiming a ticket it would immediately have to fail. `--stub`
passes NULL to opt out; it needs no repo.

Concurrency is a worker-local setting (`--concurrency N`, saved to the
config), not a server one — nothing schedules, so N slot threads each running
their own claim/run/report loop is the whole mechanism. `SKIP LOCKED` already
makes concurrent claims race-safe, so slots coordinate on nothing but a stop
event. The heartbeat moved to the main thread: it used to ride on the claim
query, which is exactly what a fully-busy worker stops issuing, so a PC
working flat out reported offline.

Found and fixed along the way: `subprocess.run(..., shell=True)` on Windows
re-parses the argument list through cmd.exe, truncating the multi-line prompt
at its first newline. Every cloud agent to date had received the ticket title
and none of its body. `shutil.which` finds the `.CMD` shim that `shell=True`
was there for, with `shell=False`. Regression-tested.

Tests: 117 → 181 (3 migrations, 8 board settings, 3 markup, 11 prompt,
18 worker paths, 10 executor, 11 concurrency).

Not yet done — the remaining phases of the gap analysis: ticket dependencies,
a cluster-wide cap, a reaper for dead claims, live progress streaming, kill,
agent questions and chat, agent profiles, and triage.

## Workers authenticate with local Claude Code config, not a cloud key (2026-08-22)

The cluster no longer stores or forwards a Claude API key. `ClaudeExecutor`
now shells out to `claude -p ...` inheriting the worker process's own
environment, so it authenticates however that PC already does (a `claude
login` session, or the operator's own `ANTHROPIC_API_KEY`). This removes the
`cluster_settings` table, the Settings panel, the `GET/PUT
/api/clusters/{id}/settings` endpoints, and `cluster_settings` from the
`kanban_worker` group's SELECT grant — closing the "any enrolled worker can
read the key via SQL" caveat entirely, since there is no longer a key to
read. `app/db.py:run_migrations` drops a pre-existing `cluster_settings`
table (verified idempotent against a scratch SQLite DB with the old table
present) so already-deployed DBs pick this up on next startup.

Operator note: each worker PC must run `claude login` (or otherwise have
`claude` CLI auth configured) before real ticket execution works — this is
no longer provisioned centrally.

Tests: 117 passed (removed the API-key-masking and settings-endpoint tests
that no longer apply; `test_cluster_scoping_blocks_outsiders` and the
proxy-mode owner/spectator tests now exercise the `/workers` endpoint in the
settings tests' place).

## Import a local `.kanban` board (2026-08-22)

An owner-only **Import** button in the header pulls a board out of the local
file-based `.kanban` tool: pick a folder or drop one on the board. Spec:
`docs/superpowers/specs/2026-08-22-import-local-board-design.md`, plan:
`docs/superpowers/plans/2026-08-22-import-local-board.md`. See README,
"Importing a local `.kanban` board", for the user-facing rules.

Shape of the work: the server is on Render and cannot read the operator's
disk, so the browser reads the folder and posts it up. The browser does nothing
but key-whitelisting; every semantic decision — status vocabulary, the body
appendix, board naming, ordering — lives in `app/importer.py` as pure
functions, because `app/static/index.html` has no JS test runner and
`app/` has a pytest suite. Import always creates a new board and never merges,
which removes sync/dedupe/conflict handling from the problem entirely.

Tests: 73 → 116 (21 mapping, 17 endpoint, 5 markup).

The browser half has no unit tests, so it was verified by extracting the
import module out of the shipped `index.html` and running it under Node
against all six live local boards, posting to a real dev server: folder
detection, the `.kanban`-root refusal (names the six boards it found), the
40-ticket read, the whitelist, name-clash suffixing, and comment timestamps
surviving as their original June/July dates rather than import time. The
mapper was also dry-run over all 125 real local tickets — 125 mapped, 0
skipped, largest body 6 KB.

Two things deliberately left as they are:

- **Imported `ready` tickets dispatch to agents.** The operator chose this. It
  is currently inert — all 125 live local tickets are `completed`, `done` or
  `blocked`, none `ready`. Reversing it is one line in `importer.STATUS_MAP`.
- **`dependsOn`/`blocks` arrive as prose**, not as a dependency graph. The
  cloud has no such concept; see `docs/2026-08-22-local-vs-cloud-gap-analysis.md`.

Note on history: commit `8248341`, whose message describes only the importer
module, also contains an unrelated dark-mode/account-menu change to
`index.html`. A concurrent session staged that file between this session's
`git add` and `git commit`. The work is intact, only mis-attributed; history
was left alone rather than rewritten under a live session.

## Public board polish (2026-08-19)

Spectators arriving at `https://www.okeefe.work/board/` now get a way in and
something to look at. Spec:
`docs/superpowers/specs/2026-08-19-board-signin-and-demo-board-design.md`,
plan: `docs/superpowers/plans/2026-08-19-board-signin-and-demo-board.md`.

- **Sign-in button**: new optional env `PROXY_LOGIN_URL` is echoed to the
  browser as `login_url` on `GET /api/session`; the spectator UI turns it into
  a "Sign in with GitHub" button. In `site-page`, `/auth/github?return=<path>`
  carries a validated same-site path through the OAuth state so `/auth/callback`
  returns the browser to `/board/` instead of `/#projects`. Validation is an
  RFC 3986 character allowlist plus a URL-parser same-origin check; anything
  else (`//evil.com`, absolute URLs, over 200 chars) falls back to the old
  destination. The first cut denylisted `//` and `/\` prefixes only, which a
  code review broke with `?return=/%09/evil.com`: browsers strip ASCII tab and
  newline before parsing, so the tab-smuggled form resolved to `https://evil.com`.
  Fixed in site-page 3da9beb before anyone could reach it — `PROXY_LOGIN_URL`
  was still unset, so nothing had ever sent a `return=` parameter.
- **Demo board**: `GET /api/session` also returns the `board` a spectator should
  land on — the board named `Demo` when one exists, else the first board — and
  `scripts/seed_demo.py "<dsn>"` populates it with eight example tickets and
  four agent comments lifted from the animated `/kanban` showcase. The seeder is
  idempotent and writes no `work_queue` rows, so its two `ready` tickets are
  inert and no enrolled worker can claim them.
- **Tests**: 73 passed (59 baseline → +6 session, +5 seeder, +3 markup). One
  pre-existing assertion in `test_session_modes` was updated: the spectator
  payload gained `board` and `login_url`. Verified end to end against a local
  uvicorn in proxy mode — gate 403s without the secret, owner provisions the
  cluster, spectator sees `login_url`, the seeder reports
  `SEED OK - board 2 "Demo": 8 tickets, 4 comments` and is a no-op on rerun,
  and the spectator session then points at the Demo board.
- **Not done here**: `site-page` sessions are still an in-memory `Map` (a
  redeploy or idle sleep logs the owner out — deliberate), and the 15s
  `BOARD_UPSTREAM_TIMEOUT_MS` in the proxy is still shorter than the ~32s
  free-tier cold start, so the first visitor after an idle period gets a 502.
- **Operator steps** (not automated): set `PROXY_LOGIN_URL=/auth/github?return=/board/`
  on the Render `kanban-cloud` service, sign in once to create the cluster, then
  run the seeder against the Neon DSN.

## Worker .exe packaging (2026-08-09)

`worker.py` is now packageable as a portable single-file Windows exe:
PyInstaller onefile via `.github/workflows/worker-exe.yml` (tag
`worker-v*` → GitHub Release with `kanban-worker.exe`; manual
`workflow_dispatch` → artifact). Runtime changes: config resolves next to
the exe when frozen; first run with no config prompts for the join code
and enrolls against https://kanban-cloud.onrender.com; the real Claude
executor is now the default (`--stub` for testing, `--real` kept as a
hidden no-op alias); fatal exits pause for Enter when frozen so
double-click users can read the error. Caveats: exe is unsigned
(SmartScreen warning), `claude` CLI not bundled, config holds the DB
credential next to the exe. Spec:
`docs/superpowers/specs/2026-08-08-worker-exe-packaging-design.md`.

## 2026-08-08 — v2: DB-centric workers

Reworked worker/server interaction from HTTP polling to direct SQL. Design +
plan: `docs/superpowers/specs/2026-08-08-db-centric-workers-design.md` and
`docs/superpowers/plans/2026-08-08-db-centric-workers.md`.

- **What changed**: `worker.py` no longer polls `/api/work/poll` with an
  `X-Worker-Token`; it makes one HTTP call ever — `POST /api/workers/enroll`
  (`--enroll --server <url> --join-code <code> [--name <n>]`) — which
  provisions this PC its own Postgres **login role** and returns a DSN, saved
  to `.worker_config.json`. Every subsequent poll/claim/heartbeat/progress/
  result (`py worker.py [--real] [--poll N] [--once]`, default
  `POLL_SECONDS=10`) is direct SQL against Neon via `psycopg[binary]`
  (`pip install "psycopg[binary]"` now required on worker PCs). The claim
  query uses `FOR UPDATE ... SKIP LOCKED` instead of the v1
  `UPDATE ... WHERE status='queued'` rowcount guard.
- **Grant model** (`app/enrollment.py`): all per-PC roles inherit one shared
  `NOLOGIN` group role, `kanban_worker` — `SELECT` on tickets, boards,
  clusters, cluster_settings, workers, work_queue, comments; `INSERT` on
  comments and work_queue; `UPDATE` on work_queue, tickets, workers; `USAGE,
  SELECT` on all sequences. No `DELETE` anywhere; `users` and `auth_tokens`
  are untouched by any worker grant.
- **HTTP surface deleted**: `/api/workers/register`, `/api/work/poll`,
  `/api/work/{id}/result`, and `/api/work/{id}/progress` are gone. The only
  worker-facing HTTP route left is `POST /api/workers/enroll`
  (proxy-exempt — it's the sole entry in `WORKER_EXEMPT_RE` now, down from
  four routes). Revocation is `POST /api/workers/{id}/revoke`: the owner UI
  button drops the Postgres role and terminates live sessions; re-enrolling
  the same PC recreates the role and restores access.
- **Tests**: 47 passed, 0 skipped (up from the 35-test v1 baseline this
  worktree started from).
- **Live Neon smoke: PASSED (2026-08-08)**. Deployed to Render (commit
  124c6cd, startup ran the v2 migrations + `kanban_worker` group creation
  against prod cleanly), then `scripts/neon_smoke_v2.py` ran against the
  real Neon DB: enrolled 2 workers with live per-PC roles → 8-way
  concurrent claim race with exactly 1 winner → progress comment →
  result recorded (ticket → review) → worker role correctly denied
  `SELECT` on `users` → revoked worker's dropped role could no longer
  connect. `SMOKE PASS`, fixtures cleaned.
- **v1 caveats that still hold, unchanged by this rework**:
  - The cluster Claude API key is still stored **plaintext in the DB**. In
    v2 it is also readable, in plaintext, by **any enrolled worker's
    Postgres role** via direct `SELECT` on `cluster_settings` (not just the
    worker that claims a given ticket) — that's an intentional consequence
    of the grant model (any worker may need the key to run `--real`), not a
    regression, but it does widen the caveat: a compromised worker PC's DB
    credentials now expose the key directly, no HTTP layer in between.
  - **No stale-claim reaper.** A dead worker's `claimed` `work_queue` row
    still doesn't time out on its own; the supersede-on-re-enqueue path
    (drag back to `ready` / "Run now") covers it manually, same as v1 — it
    flips the stale item to `failed` ("superseded") and queues a fresh one,
    so if the dead worker's `finish_work` call ever does land, its rowcount
    guard (`WHERE status='claimed' AND claimed_by=...`) finds nothing to
    update and reports "superseded" rather than corrupting the fresh claim.
  - A running worker still keeps the Neon compute endpoint awake (autosuspend
    only fires when nothing is querying it) — same tradeoff as v1, just via
    a different wire protocol.

## Reverse-proxy mode (2026-08-08)

Prep for being reverse-proxied by the portfolio site at `/board` behind its
GitHub auth, with a public read-only spectator view (commits `4a8af23`,
`7bc0d6f`, `c349a92`, docs commit after). Code only — not deployed.

- **Relative frontend URLs**: every fetch in `app/static/index.html` is now
  `./api/...` (all 22 call sites audited), so the UI works under a `/board/`
  path prefix through a proxy with zero config.
- **Proxy gate**: with env `PROXY_SHARED_SECRET` set, all routes except the
  four worker-facing ones (`POST /api/workers/register`, `/api/work/poll`,
  `/api/work/{id}/result`, `/api/work/{id}/progress` — they keep their own
  token auth; workers connect directly) require `X-Proxy-Secret`
  (hmac.compare_digest, 403 otherwise). Env unset → fully unchanged local
  behavior (verified: legacy 24-test suite untouched, conftest clears the var).
- **Proxy identities**: `X-Proxy-User: <login>` → auto-provisioned
  full-rights user `<login>@proxy.user` (unusable password hash), auto-joined
  to the default cluster (oldest; "Main" + board auto-created on first owner
  request). No user header or `X-Proxy-Readonly: 1` → spectator: whitelisted
  safe GETs only (boards/tickets/workers/queue/health/session/index), 403 on
  all mutations and on sensitive GETs (`/api/clusters` leaks join codes;
  settings leaks key state). `GET /api/session` returns
  `{mode: 'local'|'owner'|'spectator', ...}` (owner: `user{id,email}`;
  spectator: `cluster{id,name}|null`).
- **Spectator UI**: boot asks `./api/session`; spectator renders the board
  live (5s polling kept) with create/edit/drag/comments/settings/join-code
  hidden or disabled and a "viewing read-only — owner login via site" note;
  owner mode skips the login UI entirely.
- **Tests**: 11 new in `tests/test_proxy.py` — gate on/off/wrong-secret,
  worker-route exemption incl. a full no-secret register→poll→progress→result
  flow, owner provisioning + full rights + no password login, session modes,
  readonly override, spectator reads OK + every mutation 403s.
- **Live smoke** (uvicorn :8951, scratch SQLite, `PROXY_SHARED_SECRET` set):
  no/wrong secret → 403; owner session provisioned `ryano-k@proxy.user` and
  created a ticket; spectator read the board/tickets but got 403 on PATCH and
  settings GET; worker register without the secret hit its own 404, not the
  gate.

```
$ .venv/Scripts/python -m pytest tests/ -q
35 passed, 1 warning in 9.21s
```


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
