# STATUS — kanban-cloud MVP

Last updated: 2026-08-03

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
