# Design — board sign-in button + seeded demo board

Date: 2026-08-19
Repos: `kanban-cloud` (primary), `site-page` (companion change)

## Problem

`https://www.okeefe.work/board/` is live and correct, but a visitor arriving
there gets two bad impressions:

1. **No way in.** A spectator sees the note "viewing read-only — owner login via
   site" and nothing to click. The owner has to know to go back to the homepage,
   log in, and navigate to the board again. The site's GitHub OAuth callback
   hard-redirects to `/#projects` (`site-page/src/server.ts`), so even logging in
   from the board's context dumps you on the homepage.
2. **Nothing to look at.** `GET /board/api/session` currently returns
   `{"mode":"spectator","cluster":null}` — the prod database has no cluster at
   all, so the board renders empty. Even once the owner logs in and "Main" is
   auto-created, an empty board tells a visitor nothing about what this is.

The animated showcase at `/kanban` already establishes what a populated board
should feel like. The live board should match it.

## Scope

In scope:

- A "Sign in with GitHub" button on the board for spectators, which returns the
  user to `/board/` after OAuth.
- A seeded "Demo" board of example tickets, and spectators landing on it.

Explicitly out of scope (decided, not overlooked):

- **Durable sessions.** `site-page` keeps sessions in an in-memory `Map`, so a
  redeploy or free-tier idle sleep logs the owner out. Chosen deliberately: the
  sign-in button makes recovery one click. Nothing here makes that worse.
- **The 15s proxy cold-start timeout.** `BOARD_UPSTREAM_TIMEOUT_MS = 15_000` in
  `site-page/src/server.ts` is shorter than the measured 32.4s cold start of the
  free-tier upstream, so the first visitor after an idle period gets a 502. Real
  bug, separate change.
- Worker enrollment and everything downstream of it.

## Part A — sign in from the board

### A1. `site-page`: return-to-path through OAuth

`oauthStates` changes from `Set<string>` to `Map<string, string>` (state →
return path). The 10-minute TTL cleanup is unchanged.

`GET /auth/github` reads `?return=<path>` and stores `safeReturnPath(raw)`
against the new state. `GET /auth/callback` reads the path back out of the map
(deleting the entry as it does today) and redirects there.

`safeReturnPath(raw: unknown): string` is exported for direct unit testing and
returns `'/#projects'` — today's hardcoded destination — unless the input is a
string that:

- starts with `/`, and
- does **not** start with `//` or `/\` (protocol-relative URLs are an open
  redirect: `//evil.com` is a valid absolute URL to a browser), and
- is at most 200 characters.

Anything else falls back to the default. This is the only security-relevant
piece of the change and gets its own tests.

### A2. `kanban-cloud`: advertise the login URL

`create_app` reads a new optional env var `PROXY_LOGIN_URL` next to
`PROXY_SHARED_SECRET`. When set, the spectator branch of `GET /api/session`
includes it:

```jsonc
{"mode": "spectator", "cluster": {...}, "board": {...}, "login_url": "/auth/github?return=/board/"}
```

`login_url` is `null` when the env var is unset. Owner and local payloads are
untouched. Keeping the URL in config rather than hardcoding the site's auth
path preserves kanban-cloud as a generic app that happens to be proxied.

### A3. `kanban-cloud`: the button

`app/static/index.html` gains an anchor in the topbar beside `#spectatorNote`,
hidden by default. At boot, when `sessionMode === 'spectator'` and the session
payload carries a `login_url`, its `href` is set and it is shown. The spectator
note drops "owner login via site" (the button now says it) and reads
"viewing read-only".

The href is a root-relative site path, so the browser navigates to
`https://www.okeefe.work/auth/github?return=/board/` — out of the `/board/`
prefix and straight at the site's own OAuth route. Nothing proxies through
kanban-cloud, and local mode (no `PROXY_LOGIN_URL`) shows no button at all.

### A4. Config

`PROXY_LOGIN_URL=/auth/github?return=/board/` on the Render `kanban-cloud`
service. Ryan sets this in the dashboard; without it the button simply does not
appear, so the deploy order does not matter.

## Part B — the demo board

### B1. `scripts/seed_demo.py`

A one-off script in the shape of the existing `scripts/neon_smoke_v2.py`, run
by Ryan against the Neon DSN:

```
py scripts/seed_demo.py "<dsn>" [--board-name Demo]
```

It uses the app's SQLAlchemy models, so the same code path works against
scratch SQLite in tests and Neon in production. Steps:

1. Resolve the default cluster (lowest id — a proxy deployment is
   single-tenant). None → exit 2: "no cluster yet — log in to the board once
   first; the cluster is created on the owner's first request."
2. Resolve the owner: lowest-id user who is a member of that cluster. None →
   the same exit.
3. Find or create a board named `Demo` in that cluster.
4. **Idempotence guard:** if that board already has any tickets, print
   `already seeded (N tickets) — nothing to do` and exit 0 without writing.
   A partially-cleaned demo board therefore stays as the owner left it; only a
   completely empty one reseeds, and only when the script is run again on
   purpose.
5. Insert the tickets and comments in one transaction, then commit.
6. Print `SEED OK — board <id> "Demo": 8 tickets, 4 comments`.

### B2. What gets seeded

Eight tickets whose titles, bodies and agent comments come from the `POOL` in
`site-page/public/kanban/index.html`, so the live board reads in the same voice
as the animated showcase. Showcase `detail` becomes the ticket `body`;
showcase `comment` becomes a `comments` row whose `writer` is the model name.

| Status | Tickets |
|---|---|
| `todo` | Reduced-motion accessibility pass; Notification bell for blocked tickets |
| `ready` | Two-column board settings modal; Docker dev-workspace image |
| `doing` | Enforce orchestrator concurrency cap *(comment: `claude-opus`)* |
| `review` | Performance tab CPU rollup *(comment: `claude-haiku`)* |
| `done` | Idempotent kanban server startup; FLIP slide animation for card moves *(comments: `claude-sonnet`)* |

`created_at` is staggered backwards in hours so the ordering looks organic.

Two deliberate omissions:

- **No `workers` rows.** Fake workers would show up in the workers panel as
  permanently offline. The agent voice lives in comments instead, and
  `assigned_worker` / `target_worker` stay `NULL`.
- **No `work_queue` rows.** Claiming reads the queue, not ticket status, and
  enqueueing only happens through the API's move-to-`ready` path. Seeding rows
  directly means the two `ready` demo tickets are inert: a real enrolled worker
  can never claim one.

### B3. Spectators land on Demo

A helper `default_board(db, cluster)` in `app/main.py` returns the board named
`demo` (case-insensitive) in that cluster, else the lowest-id board, else
`None`. The spectator branch of `GET /api/session` returns it as
`"board": {"id", "name"} | null`.

`app/static/index.html` stores the boot session payload, and `loadBoards()`
selects the session's board id when the mode is spectator and that id is in the
list; otherwise it keeps today's `boards[0]`. Owner and local modes are
unchanged, so the owner still lands on Main. Either can switch with the
existing board dropdown, and the dropdown showing "Demo" is what tells a
visitor the data is illustrative — no extra badge.

## Testing

`kanban-cloud` (pytest, current baseline 47 passing):

- `/api/session` spectator carries `login_url` when `PROXY_LOGIN_URL` is set and
  `null` when it is not; owner and local payloads are unchanged from today.
- `default_board` prefers a "Demo" board over a lower-id "Main", falls back to
  lowest id, and returns `None` for a board-less cluster; the spectator session
  payload reflects it.
- Seed script against scratch SQLite: creates the board, 8 tickets and 4
  comments; a second run writes nothing and exits 0; exits non-zero with the
  guidance message when no cluster or no user exists; creates zero `work_queue`
  rows.

`site-page` (`node --test`, new `tests/auth-return.test.mjs`):

- `safeReturnPath` accepts `/board/`, and falls back to `/#projects` for
  `//evil.com`, `/\evil.com`, `https://evil.com`, `''`, `undefined`, and a
  201-character path.
- `GET /auth/github?return=/board/` responds 302 to github.com and records the
  return path against the issued state; the callback redirects to it. Where
  mocking GitHub's token exchange is disproportionate, the round trip is
  asserted at the state-map level and the pure helper carries the rest.

Manual verification after deploy: logged out, `/board/` shows a populated Demo
board and a Sign in button → click → GitHub → land back on `/board/` in owner
mode on Main.

## Rollout order

1. Merge and deploy both repos.
2. Ryan sets `PROXY_LOGIN_URL` on the kanban-cloud Render service.
3. Ryan signs in once, which auto-creates the cluster and the Main board.
4. Ryan runs `scripts/seed_demo.py` against the Neon DSN.

Steps 3 and 4 are ordered: the seed script has nothing to attach to until the
cluster exists, and it says so rather than failing obscurely.
