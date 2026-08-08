# kanban-cloud v2 — DB-centric workers

**Date:** 2026-08-08
**Status:** Approved by Ryan (design conversation, this date)

## Goal

Make Neon Postgres the single source of truth and coordination point. The web
service becomes a thin face for humans; worker PCs interact directly with the
database. Motivations (Ryan): kill the middleman (workers keep running even if
the Render service is down/asleep/redeploying), simpler architecture (one
source of truth, less worker-API code), and a trust-model change (worker PCs
are trusted; the join-code/token dance over HTTP is unnecessary ceremony — but
credentials are still issued per-PC by the server at enrollment).

## Decisions made

- **Workers talk SQL, not HTTP.** After a one-time enrollment call, a worker
  never contacts the web service again. Poll, claim, progress, result,
  heartbeat, and Claude-key read are all direct Postgres operations.
- **Per-PC Postgres roles.** Enrollment creates a dedicated Neon role +
  password for the PC and returns a ready-to-use connection string. Revoking a
  PC drops the role — revocation is enforced at the DB layer.
- **kanban-cloud stays a separate service** (not folded into site-page). It
  keeps the board UI/API for humans behind the okeefe.work reverse proxy
  (unchanged) plus exactly one worker-facing route: enrollment.

## Architecture

```
humans ── okeefe.work/board/ ──> site-page proxy ──> kanban-cloud (FastAPI, Render)
                                                          │ admin DSN (neondb_owner)
                                                          ▼
                                                   Neon Postgres  ◄── SQL ── worker PCs
                                                                        (per-PC roles)
```

## Enrollment

Command: `py worker.py --enroll --server https://kanban-cloud.onrender.com
--join-code <code> --name <pc-name>`

Server route `POST /api/workers/enroll` (proxy-exempt, join-code-gated —
successor to today's `register`):

1. Validate join code → cluster.
2. Insert/refresh the `workers` row; store the role name on it.
3. With the admin connection: `CREATE ROLE worker_<id> LOGIN PASSWORD
   '<random>'` and `GRANT kanban_worker TO worker_<id>`.
4. Return a connection string (host/db from the server's own `DATABASE_URL`,
   the new role, `sslmode=require`).

The worker saves the DSN in `.worker_config.json` and thereafter runs entirely
against Postgres.

**Revocation:** owner-only UI action → server marks the worker row revoked and
`DROP ROLE worker_<id>`. Open connections from that role die with it; new
connections fail at auth. In-flight claims are handled by the existing
supersede semantics (re-enqueue supersedes; a revoked PC can no longer write a
late result at all).

## Grants

One group role holds all permissions; per-PC roles only inherit it:

- `kanban_worker` (NOLOGIN group):
  - `SELECT` on `tickets`, `boards`, `clusters`, `cluster_settings` (Claude
    API key is read directly — the delivery-on-claim step disappears),
    `workers`, `work_queue`, `comments`.
  - `INSERT` on `comments`.
  - `UPDATE` on `work_queue`, `tickets`, `workers` (heartbeats).
  - **No** `DELETE` anywhere. **No** access to `users` or `auth_tokens`.
- Future schema changes: grant once to `kanban_worker`, every PC inherits.

## Worker loop (worker.py rewrite)

- Driver: `psycopg` (worker is no longer stdlib-only; document the one-time
  `pip install psycopg[binary]` per PC).
- Poll every `--poll-seconds` (default 10) — see Neon-compute note below.
- **Claim** (atomic, race-safe, Postgres-native):

  ```sql
  UPDATE work_queue SET status='claimed', worker_id=%s, claimed_at=now()
  WHERE id = (
    SELECT id FROM work_queue
    WHERE status='queued' AND (target_worker_id IS NULL OR target_worker_id=%s)
    ORDER BY created_at
    FOR UPDATE SKIP LOCKED LIMIT 1
  )
  RETURNING *;
  ```

- **Progress:** update the queue row / insert a comment.
- **Result:** one transaction — update `work_queue`, update the ticket status,
  insert the result comment.
- **Heartbeat:** `UPDATE workers SET last_seen=now()` each poll; UI reads
  liveness from timestamps.
- Executors unchanged: `StubExecutor` default, `ClaudeExecutor` behind
  `--real` (reads the cluster Claude key via `SELECT` from
  `cluster_settings`).

## Server slim-down

Delete: `POST /api/work/poll`, `POST /api/work/{id}/result`,
`POST /api/work/{id}/progress`, and the `X-Worker-Token` auth path. The
proxy-exempt list shrinks to the enroll route only. Server-side delegation
reduces to setting `target_worker_id` on queue rows. Board UI/API for humans
(owner/spectator via proxy headers) is unchanged.

## Error handling

- Worker loses DB connectivity → retry with backoff; claims it holds are
  recoverable via existing supersede/re-enqueue semantics.
- Enrollment partially fails (role created, insert failed or vice versa) →
  enrollment is idempotent per worker name+cluster: re-running cleans up or
  completes (drop-and-recreate the role).
- Revoked worker mid-run → its next SQL statement fails at auth; nothing it
  can do persists.

## Constraints & flagged tradeoffs

- **Neon compute:** any polling worker keeps Neon compute awake for as long
  as it runs (free tier autosuspends when idle). Acceptable because workers
  run only when started deliberately; poll interval is a flag.
- **Postgres-only worker path:** `FOR UPDATE SKIP LOCKED` and roles do not
  exist in SQLite. The server's SQLite fallback remains for the human-facing
  board only; the worker requires Postgres.

## Testing

- Pure-logic unit tests for the worker (SQL construction, config handling,
  executor selection) in the existing pytest style.
- Server tests updated: enrollment route (join-code gate, idempotency, role
  bookkeeping mocked at the DB boundary), deleted routes return 404.
- **Live Neon smoke** (scripted, like the v1 smoke): enroll a scratch worker →
  N-way concurrent claim race (exactly one winner) → progress → result →
  revoke → verify the dropped role can no longer connect. Run against the real
  DB, then wipe fixtures.

## Migration

- Schema: add `role_name` (and a revoked flag if not present) to `workers`.
  No data migration — the production DB is empty post-wipe.
- `worker.py` v1 configs are obsolete; enrollment replaces them.

## Out of scope

- LISTEN/NOTIFY push delegation (polling is sufficient; same Neon-wake cost).
- Row-level security / per-cluster isolation (single-operator trust model).
- Folding the board into site-page (explicitly declined — separate service).
