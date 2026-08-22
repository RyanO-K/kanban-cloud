# Design — cloud agents doing real repo work, and worker-local concurrency

Date: 2026-08-22
Status: approved, ready to plan
Context: `docs/2026-08-22-local-vs-cloud-gap-analysis.md` (Phase 1 + the per-worker
half of Phase 2)

## Problem

Two independent defects, both in the path between "a ticket is queued" and "an
agent does something useful".

**1. A cloud agent cannot do repo work.** `ClaudeExecutor.run` calls
`subprocess.run(["claude", "-p", prompt])` with no `cwd`, so the agent executes in
whatever directory `kanban-worker.exe` happens to live in. A cloud `board` is
`(id, cluster_id, name)` — there is nowhere to record which repo a board is about,
and no project context, so even the prompt cannot describe the job. The call also
passes no tool permissions, so a headless run cannot get permission to edit a file
even if it found one.

**2. A worker has no concurrency control.** `worker.py` claims one item, runs it to
completion, then claims the next. In-flight work across a cluster equals the number
of running exes; the only throttle is closing windows. There is no way to say "this
PC can handle three tickets at once" or "this laptop should only ever run one".

## Non-goals

Deliberately out of scope, tracked as later phases in the gap analysis:

- Cluster-wide `concurrency_cap`, `enabled` pause, "stop all" (Phase 2)
- Ticket dependencies (Phase 2)
- Stale-claim reaper, progress streaming, kill (Phase 3)
- `blocked` status, questions/answers, agent chat (Phase 4)
- Profiles, per-ticket model pin, triage (Phase 5)
- Auto-commit / auto-push and `commit_gate` enforcement (Phase 6)

`commit_requirements` and the git-workflow guidance ship here as **prompt text
only**. The agent is told what is expected; nothing verifies or pushes it. A
Phase 1 run therefore leaves committed-but-unpushed work on the worker PC.

## Architecture

Board-level *project* facts live on the server, where everyone shares them.
Machine-level *path* facts live on each worker, where only that PC knows them.
The prompt is composed on the worker, because it is the only place both halves
are in scope at once.

```
  browser                    server (Render)              worker PC
  ---------------            ------------------           -----------------------
  Board settings   --PATCH-> boards.description           .worker_config.json
   modal                     boards.out_of_scope            boards: {id -> path}
                             boards.commit_requirements     concurrency: N
                             boards.use_worktrees
                                     |                             |
                                     +------- direct SQL ----------+
                                                                   |
                                            build_agent_prompt(ticket, board, dir)
                                                                   |
                                            claude -p --allowedTools ... --session-id
                                                cwd = the board's local path
```

### Why the prompt is built on the worker

The alternative — rendering it server-side onto the `work_queue` row at enqueue
time — was rejected. The server does not know the claiming PC's path (it is not
even chosen until claim time, since any of N workers may win), so a server-rendered
prompt would be structurally incomplete. It would also go stale whenever the board
or ticket was edited between enqueue and claim. Auditability, the one real argument
for the server-side variant, is recovered more cheaply in Phase 3 by logging the
composed prompt into the run log.

## Components

### 1. Board project metadata — `app/models.py`, `app/db.py`, `app/main.py`

`boards` gains four nullable columns:

| Column | Type | Meaning |
|---|---|---|
| `description` | TEXT | project-context paragraph, injected into every prompt |
| `out_of_scope` | TEXT | what agents must not touch |
| `commit_requirements` | TEXT | free text, e.g. "all tests must pass before committing" |
| `use_worktrees` | BOOLEAN NOT NULL DEFAULT FALSE | selects which git guidance the prompt carries |

Migration follows the established guarded-ALTER pattern in `db.run_migrations`
(`ADD COLUMN IF NOT EXISTS` on Postgres; SQLite reaches the shape through
`create_all` on a fresh DB and through explicit `ADD COLUMN` on an existing one,
matching how the `workers` v2 columns are handled).

No new grants: `kanban_worker` already holds `SELECT` on `boards`, and new
columns inherit table-level grants.

**`directory` is deliberately not a column.** It is per-PC, and the same board is
worked by several machines.

API:

- `GET /api/clusters/{cluster_id}/boards` — response gains the four fields.
- `PATCH /api/boards/{board_id}` — new; accepts any subset of the four. Membership
  is checked the same way `board_for_user` already does it. Spectators are already
  blocked from mutations by the proxy gate.

UI: a **Board settings** modal in `app/static/index.html`, opened from a gear button
next to the board selector, owner-only (hidden in spectator mode, like the existing
create/edit affordances).

### 2. Worker-local board paths — `worker.py`

`.worker_config.json` gains a `boards` object mapping **board id (string) to
absolute path**:

```json
{
  "dsn": "postgresql://worker_c1_w3:...",
  "worker_id": 3,
  "cluster_id": 1,
  "name": "ryans-pc",
  "concurrency": 2,
  "boards": {"4": "C:/Users/ryan/Documents/Github/site-page"}
}
```

Ids, not names, are the key: a board rename must not silently orphan a path.

CLI:

- `--list-boards` — prints the cluster's boards as `id  name  configured-path`.
- `--set-path <id-or-name>=<path>` — resolves a name case-insensitively over SQL,
  rejects a name matching zero or several boards, requires the directory to exist,
  saves. Repeatable.
- First run after enrollment walks the cluster's boards interactively, prompting for
  a path per board; blank input skips. Skipped when not a TTY.

**Claim filter.** `CLAIM_SQL` gains `AND t.board_id = ANY(%(boards)s)`. A PC with no
path for a board never claims that board's tickets, so a misconfigured worker goes
idle rather than running an agent in the wrong folder. The filter applies to the real
executor only; `--stub` passes a sentinel that disables it, keeping the demo board and
the existing test suite working unchanged.

Startup prints a warning naming the fix when the real executor has zero configured
boards.

### 3. Prompt builder — `app/prompt.py` (new)

One pure function, no I/O:

```python
build_agent_prompt(ticket: dict, board: dict, directory: str) -> str
```

Sections, in order: role line; ticket id/title/detail; project description;
out-of-scope; working directory; git-workflow guidance (branch-per-ticket named
`<id>-<slug>`, or worktree guidance when `use_worktrees`); commit requirements; and
the closing instruction to reply with a concise summary. Absent fields drop their
section entirely rather than emitting an empty heading.

It lives in `app/` (not `worker.py`) so it sits under the pytest suite, and `worker.py`
imports it directly. PyInstaller follows the static import, so no spec-file change is
needed — **provided `app/prompt.py` imports nothing outside the standard library**. It
must not reach for SQLAlchemy, FastAPI or `app.models`, or the onefile exe pulls the
whole server in. A test asserts the module's imports stay stdlib-only.

### 4. Executor — `worker.py`

`ClaudeExecutor.run` gains:

- `cwd=<the board's local path>` — the core fix.
- `--allowedTools Read,Edit,Write,Bash,Grep,Glob` — the local tool's `default`
  profile list; overridable with `--allowed-tools`. Without it a headless `claude -p`
  cannot get permission to edit anything.
- `--session-id <uuid4>`, minted per attempt and written to a new `tickets.session_id`
  column, so a human can `claude --resume <id>` and take over a stuck run. This is
  `claudeSessionId` from the local tool.

### 5. Worker concurrency — `worker.py`

- `--concurrency N`, persisted as `concurrency` in the config, default **1** — the
  existing behavior exactly.
- N **slot threads**, each owning its own `psycopg` connection, each independently
  claiming, running, finishing, and claiming again. `FOR UPDATE ... SKIP LOCKED`
  already makes this race-safe; no additional locking is introduced.
- The **main thread** owns config, shutdown and a heartbeat every `POLL_SECONDS`
  regardless of slot state. This incidentally fixes the documented "worker shows
  offline during long `--real` executions" bug, because the heartbeat no longer
  rides on the claim query.
- `workers` gains `concurrency` (INT NOT NULL DEFAULT 1) and `running` (INT NOT NULL
  DEFAULT 0). The existing `status` column is left in place and still maintained, so
  already-deployed exes and the current UI keep working; the Workers panel renders
  `running/concurrency` when present.
- Shutdown: Ctrl+C sets a stop event; slots finish the ticket in hand (results must
  not be lost) and then exit. A second Ctrl+C exits immediately.

No new grants: `workers` already carries `UPDATE`.

## Data flow — one ticket, end to end

1. Owner sets the board's description / commit requirements in the Board settings modal.
2. Owner on the worker PC runs `kanban-worker.exe --set-path site-page=C:/.../site-page --concurrency 3`.
3. A ticket is moved to `ready`; `enqueue_ticket` writes a `work_queue` row.
4. A free slot's `claim_next` wins the row — the claim filter having confirmed this PC
   has a path for that board — and flips the ticket to `doing`, `attempts += 1`.
5. The slot reads the board row, calls `build_agent_prompt`, mints a session id,
   writes it to the ticket, and runs `claude -p` with `cwd` set.
6. `finish_work` records the result, comment and terminal status exactly as today;
   `running` decrements.

## Error handling

- **No path for the claimed board** — cannot occur via the filter, but is checked
  again at execution time; if it somehow happens, the attempt fails with a message
  naming `--set-path` rather than running in the wrong directory.
- **Path configured but missing on disk** (repo moved/deleted) — the attempt fails
  with a clear message; the existing retry-then-`failed` budget applies.
- **A slot thread raises** — caught per slot, recorded through the normal failure
  path, slot continues. One bad ticket never kills the worker.
- **A slot hangs** — the existing 30-minute `subprocess.run` timeout bounds it, and
  because slots are independent threads the other slots keep working.
- **DB connection drops** — per-slot reconnect, as the single-connection loop does today.
- **Revoked role** — unchanged: the worker detects the auth failure and exits.

## Testing

TDD. The existing 114 tests in this worktree's baseline must stay green.

- **Migration**: new board columns present after `run_migrations` on a pre-existing
  DB of each backend; idempotent on a second run.
- **Board settings API**: owner reads and patches; partial patches leave other fields
  alone; a board in another cluster 403s; a spectator 403s.
- **`build_agent_prompt`**: pure-function tests over each metadata combination —
  all fields, no fields, worktrees on/off — asserting sections appear and that absent
  fields emit no empty headings.
- **Config**: `--set-path` by id and by name, ambiguous-name and unknown-name
  rejection, nonexistent-directory rejection, round-trip through the config file.
- **Claim filter**: a worker with a path for board A does not claim a queued board-B
  ticket; the stub sentinel disables the filter.
- **Executor**: `subprocess.run` is invoked with the expected `cwd`, `--allowedTools`
  and `--session-id` (mocked).
- **Concurrency**: N slots run concurrently and never exceed N; each slot holds its
  own connection; one slot blocking does not prevent another from claiming; the
  heartbeat continues while every slot is busy.
- **Markup**: the Board settings modal's ids exist in `index.html`, matching the
  existing `test_frontend_markup.py` approach.
- **Packaging**: `app/prompt.py` imports only standard-library modules, so importing
  it from `worker.py` cannot drag the server into the onefile exe.

## Open risks

- **Concurrent session on `master`.** Another session is editing `app/db.py`'s
  `run_migrations` and `app/static/index.html` in the shared checkout. This work is
  isolated on the `phase1-repo-work` branch off `217adba`; the merge back must
  re-read both files rather than assume the baseline.
- **`app/prompt.py` in the frozen exe.** The `--onefile` build has no spec file and
  follows imports automatically, so this is safe only while `prompt.py` stays
  stdlib-only. The guard is a test, not the build; if a later change needs a model
  import there, the module must be split rather than the test relaxed.
