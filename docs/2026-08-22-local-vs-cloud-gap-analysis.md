# Gap analysis — `.kanban` (local) vs `kanban-cloud`

Date: 2026-08-22

Sources read: `.kanban/CLAUDE.md`, `.kanban/app/{kanban_server,orchestrator,orchestrator_core,perf_monitor,layrr_launcher,cpu_limiter}.py`,
`.kanban/static/*`, `.kanban/config/*.json`, `.kanban/_orchestrator/state.json`, board `_meta.json` + live ticket JSON;
`kanban-cloud/{schema.sql,app/models.py,app/main.py,app/delegation.py,worker.py,app/static/index.html,STATUS.md,README.md}`.

## Executive summary

The two systems are **not the same product with different storage**. The local tool is
~11k LOC of *agent orchestration* with a kanban UI attached; the cloud tool is ~1.6k LOC
of *multi-tenant kanban CRUD* with a thin pull-based work queue attached.

The cloud has essentially **none of the orchestration layer**. Concretely:

- **No dependency graph.** `dependsOn`/`blocks` do not exist in the cloud schema. Nothing
  gates a ticket from being worked before its prerequisites finish.
- **No concurrency limit.** In the cloud, in-flight work = number of running worker
  processes. There is no `concurrencyCap`, no per-board cap, no `enabled` pause switch and
  no "stop all". The only way to throttle is to stop worker exes by hand.
- **No triage, no profiles, no model selection.** Every cloud ticket is executed by the
  same hardcoded `claude -p <prompt>` — no system prompt, no `--allowedTools`, no
  `--model`, no session id.
- **No project context.** A cloud `board` is `(id, cluster_id, name)`. There is no
  `directory`, so `ClaudeExecutor` shells out **with no `cwd`** — the agent runs in
  whatever folder `kanban-worker.exe` happens to sit in. It cannot reliably find a repo,
  and there is no git workflow, branch, worktree, commit gate or auto-commit at all.
- **No human-in-the-loop.** No `blocked` status, no `question`/`answer`, no notification
  inbox, no agent chat, no progress streaming.

Below, every local feature is listed with its cloud status.

---

## 1. Ticket data model

Local ticket fields (observed live on the `ai-kanban` board) vs the cloud `tickets` table.

| Local field | Purpose | Cloud |
|---|---|---|
| `id`, `title`, `detail`, `status`, `createdAt` | core | Present (`body` = `detail`) |
| `comments[]` `{writer,message,timestamp}` | discussion / agent summary | Present (`comments` table) |
| **`dependsOn[]` / `blocks[]`** | **dependency graph; gates dispatch** | **MISSING entirely** |
| **`order`** | priority within the Ready queue (UI up/down buttons) | Missing — queue is FIFO on `queued_at` |
| **`model`** | per-ticket model pin; beats triage and profile | Missing |
| `steps[]`, `files[]`, `outputs[]` | agent plan / checklist arrays | Missing |
| `history[]` | append-only `status_change` audit with `sessionId` | Missing — only `updated_at` |
| `optional` | mark ticket non-blocking | Missing |
| `claudeSessionId` / `claudeSessionDir` | `claude --resume <id>` handoff to a human | Missing |
| `runLogFile` / `completedLog` | per-run transcript pointer + archived log | Missing (`work_queue.result`, 10k chars, terminal only) |
| `commitGate {requirementsMet, summary}` | agent's self-report that the board's test gate passed | Missing |
| `outputBranch` / `mergeBranch` | branch the work landed on / merge target | Missing |
| `orchestrator{}` marker (`state`, `profile`, `model`, `pid`, `sessionId`, `cwd`, `dispatchedAt`, `killRequested`, `logFile`, `containerName`) | in-flight state, with strict field ownership between loop and agent | Partial: `assigned_worker` + `work_queue.status/claimed_by` |
| `orchestrator.question` (`type` input\|choice, `format`, `options`, `multi`, `answer{value,notes}`) | human escalation | Missing |
| `pendingChat[]` | mid-run human messages not yet delivered | Missing |
| `_kanbanGuide` | inline convention doc stamped on every ticket | Missing |

## 2. Status vocabulary

| Local | Cloud |
|---|---|
| `todo`, `ready`, `in_progress`, `blocked`, `pending`, `completed` | `todo`, `ready`, `doing`, `review`, `done`, `failed` |

- **`blocked` has no cloud equivalent** — that is the human-in-the-loop state, and its
  absence is why none of the question/answer machinery exists.
- Names diverge (`in_progress`/`doing`, `completed`/`done`), so a port needs an explicit
  mapping decision. `review` and `failed` are cloud-only and worth keeping.

## 3. Board / project metadata — the largest single gap

Local `_meta.json` carries the whole project contract. Cloud `boards` has `name` only.

| Local `_meta.json` key | What it drives | Cloud |
|---|---|---|
| **`directory`** | the repo the agent `cd`s into | **Missing — agent runs with no cwd** |
| `project`, `context{}`, `openQuestions[]`, `outOfScope[]` | context injected into triage and agent prompts | Missing |
| **`commitRequirements`** | free-text gate ("all tests must pass") the agent must satisfy, and that auto-commit checks via `commitGate` | Missing |
| **`useWorktrees`** | branch + `git worktree add` isolation vs in-place work | Missing |
| **`useDocker`** + per-board Dockerfile + dispatch preflight | run the agent inside a container on `/workspace` | Missing |
| `envVars{}` (non-secret, `--env-file`) | container config | Missing |
| `passthroughEnv[]` (secret **names** only, `docker run -e NAME`) | secret forwarding without persisting values | Missing |
| `showMergeBranch` | per-board UI toggle for the branch field | Missing |
| `layrr{targetPort,projectRoot,baseBranch,model}` | point-and-click live editing | Missing |
| `updated` | board freshness | Derivable |

**Cloud-specific wrinkle:** `directory` is per-PC. The same cloud board is worked by
several enrolled PCs, so this cannot be a single board column — it needs a
`worker_board_paths (worker_id, board_id, directory)` mapping, or a per-worker local
config keyed by board.

## 4. Orchestration — absent

The cloud has no orchestrator process. Work moves only because a worker polls
`work_queue` every 10s and claims the oldest queued row (`FOR UPDATE ... SKIP LOCKED`).
Everything below exists locally and has no cloud counterpart.

| # | Local capability | Where it lives | Cloud |
|---|---|---|---|
| 1 | **`concurrencyCap`** — global in-flight cap; `free = cap - in_flight` drives how many are dispatched per tick | `state.json`, `orchestrator.tick` | Missing |
| 2 | **`enabled` pause / `stopAllRequested`** — pause new dispatch (reaping continues), kill everything in flight | `state.json`, Orchestrator tab | Missing |
| 3 | **Tick loop** (`tickSeconds`) + **Nudge** (`POST /api/orchestrator/nudge`, run one tick now) | `orchestrator.run_loop` | Missing |
| 4 | **Dispatch triage (Opus)** — picks which eligible tickets to run, which profile, which model, with a reason | `_real_opus_triage`, `validate_triage` | Missing |
| 5 | **Initial triage (Sonnet)** on `todo` — assigns a model and infers `dependsOn`, then promotes to `ready`; never clobbers a user-set value | `_real_sonnet_triage`, `apply_initial_triage`, `promotable_tickets` | Missing |
| 6 | **Profiles** — `{name, displayName, whenToUse, model, allowedTools[], systemPrompt, enabled}` with CRUD API + UI tab; 8 live profiles today | `config/*.json`, `/api/profiles` | Missing — one hardcoded prompt |
| 7 | **Backfill dispatch** — fills leftover free slots greedily so the cap, not triage's output length, sets the count | `backfill_dispatch` | Missing |
| 8 | **Eligibility gate** — dispatch only from `ready`; per-**board**-scoped dependency resolution (ids collide across boards); `blocked` + answered re-dispatches | `eligible_tickets`, `_dep_met_fn` | Missing |
| 9 | **Ready-queue priority** — `order` ascending, UI reorder buttons | `update_task_order` | Missing |
| 10 | **Reap / stall detection** — idle-based (log-file growth, `idleSeconds`) plus absolute `maxAgentSeconds`; crash vs completed vs needs-human by exit code; adopted-agent inference from what it wrote | `reap_decision`, `note_log_growth`, `agent_left_signal` | Missing — **and no stale-claim reaper at all**; a dead cloud worker leaves the row `claimed` forever (already logged in `STATUS.md`) |
| 11 | **Kill** — per-ticket kill, instant by PID (`taskkill /T` / `killpg`) or queued via `killRequested`; `docker kill` by container name | `orch_kill`, `kill_pid` | Missing |
| 12 | **Usage-limit handling** — parses "usage/session limit" + reset epoch from the transcript, parks dispatch until reset, resumes automatically; also detects `/login` errors | `parse_usage_limit`, `set_usage_pause` | Missing |
| 13 | **Activity feed** — `dispatch` / `promote` / `reap` / `skip` / `error` / `usage_limit` / `chat_requeue` events | `activity.json`, `/api/orchestrator/activity` | Partial — "Recent delegations" (queue rows) only |
| 14 | **Per-run logs + live streaming** — `_orchestrator/runs/<id>-<ts>.log`, stream-json parsed into turns with tool-call summaries and hoverable result previews, polled live in the side panel | `parse_log_turns`, `GET /api/board/<b>/task/<id>/log` | Missing — fire-and-forget, no progress until the run ends |
| 15 | **Auto-commit / auto-push** — on completion, kanban-only changes commit to master; other changes publish to `ticket/<id>-<slug>`; resolves *which sub-repo* the changed paths belong to | `publish_output_branch`, `discover_changed_paths`, `repo_dir_for_paths` | Missing |
| 16 | **Commit-requirements gate** — the orchestrator refuses to auto-commit unless `commitGate.requirementsMet` | `commit_requirements_met` | Missing |
| 17 | **Worktrees** — branch naming `<id>-Feature-Name`, `EnterWorktree`, worktree discovery/cleanup, board-path anchoring guidance in the prompt | `_git_workflow_guidance`, `worktree_path` | Missing |
| 18 | **Docker sandbox** — per-board image build, `/workspace` mount, host→container path translation, env-file + secret passthrough, in-container git identity, preflight that blocks rather than silently falling back | `_docker_dispatch`, `docker_preflight` | Missing |
| 19 | **Session resume** — an unblocked ticket resumes its own `--resume <session>` with a short "you were unblocked, here is the answer" prompt instead of restarting | `_build_resume_prompt`, `resume_session_id` | Missing |
| 20 | **`--allowedTools` restriction, superpowers plugin args, Fable availability probe/fallback** | `superpowers_args`, `resolve_model` | Missing |
| 21 | **Single-instance lock** (`acquire_lock`, PID-checked) | `orchestrator_core` | N/A today; relevant once a cloud orchestrator exists |
| 22 | **Rich agent prompt** — profile system prompt + ticket file path + prior Q&A + pending chat + git-workflow guidance + commit-gate guidance + escalation guidance | `_build_agent_prompt` | Missing — 5-line generic prompt |

## 5. Human-in-the-loop — absent

| Local | Cloud |
|---|---|
| Agent sets `status:blocked` + `orchestrator.question` (`input` or `choice`, with `format`, `options`, `multi`) | Missing |
| Answer shape `{value, notes}`; notes are authoritative and fed back into the prompt | Missing |
| **Notification bell** with badge count + "Needs attention" modal, draft preservation across polls | Missing |
| Answering auto-re-dispatches on the next tick, resuming the same session | Missing |
| **Agent chat** — `POST /api/orchestrator/chat/<board>/<id>`, per-run JSONL inbox, stdin pump relays each message as a stream-json user turn (queued as the *next* turn, never interrupting), delivered/undelivered split via a byte-offset sidecar, `pendingChat` requeue so nothing is lost at run end | Missing |
| Mid-run progress comments | Missing — executor is fire-and-forget with a 30-minute timeout |

## 6. UI

| Local | Cloud |
|---|---|
| Views: **Boards / Setup (Orchestrator + Profiles + Activity) / Performance** | Boards only, plus Workers / Settings / Recent-delegations side panels |
| `__all__` combined cross-board view | Missing — one board at a time |
| Board **Settings** modal (everything in §3) | Missing |
| Ticket side panel: markdown-rendered detail, inline title/detail edit, **live agent log**, **chat box**, history, attached spec docs, model picklist, branch field | Modal with title, body, status, target worker, comments |
| Unread/reviewed tracking + "Mark all read" | Missing |
| Drag-and-drop with FLIP animation, drop placeholder, reorder buttons | Drag-and-drop present; no ordering |
| "Clear done" | Missing |
| Model picklist fed by live `/api/models` discovery | Missing |
| Spec/doc index — `docs/specs/*` auto-attached to tickets, `/api/doc/<path>` viewer | Missing |
| **Layrr live edit** (proxy overlay files tickets on the board, status widget, multi-instance chips) | Missing |
| **Performance tab** — psutil rollup of every `claude.exe` tree on the PC including orphans, 5-minute CPU/mem graphs, kill-tree button | Missing |
| Server CPU cap (Windows Job Object, live-editable) | Missing |
| Incremental polling via `?since=<mtime>` | Full refetch every 5s |
| Status pill (server up/down, orchestrator live / paused / usage-limited) | Worker online dots only |
| Toast, skeletons, dock-to-sidebar create form | Partial |

## 7. Where the cloud is ahead — do not regress these

Multi-user auth (PBKDF2 + token table); clusters, join codes and membership; per-PC
Postgres login roles under a least-privilege `kanban_worker` group; one-call enrollment
plus one-click revoke that drops the role and terminates live sessions; reverse-proxy mode
with owner/spectator identities behind the site's GitHub auth; public read-only board and
demo seeder; packaged single-file `kanban-worker.exe` built by GitHub Actions;
`review`/`failed` statuses; `target_worker` routing (local has no notion of "which
machine"); attempt budget with retry-then-fail.

---

## 8. Recommended sequencing

Ordered so each phase unblocks the next.

**Phase 1 — make cloud agents able to do real work (prerequisite for everything else)**

1. `boards`: add project metadata (`description`, `context`, `commit_requirements`,
   `use_worktrees`, `show_merge_branch`, ...).
2. New `worker_board_paths(worker_id, board_id, directory)` — per-PC repo location. Pass
   `cwd=` into `ClaudeExecutor`. Without this, nothing else matters.
3. Rich prompt builder mirroring `_build_agent_prompt` (ticket, project context, git
   guidance, commit-gate guidance, escalation guidance).

**Phase 2 — dependencies and concurrency (the two called out explicitly)**

4. `ticket_deps(ticket_id, depends_on_id)` table, with derived `blocks`. Enforce it in the
   claim SQL: only claim a queued item whose deps are all in `('done','review')`, scoped
   per board, matching `_dep_met_fn`.
5. `cluster_settings` / `board_settings`: `concurrency_cap`, `enabled`,
   `stop_all_requested`. Enforce the cap **inside the claim transaction**
   (`SELECT count(*) FROM work_queue WHERE status='claimed' AND cluster_id=...` in the same
   statement) so it holds across N independent workers with no central dispatcher.
6. `tickets.order` + UI reorder; use it in `CLAIM_SQL`'s `ORDER BY` ahead of `queued_at`.

**Phase 3 — reliability**

7. Stale-claim reaper: add `heartbeat_at` to `work_queue`; any worker (or a Render cron)
   flips claims whose worker went stale back to `queued`. This closes the long-standing
   `STATUS.md` gap and is the cloud analogue of `reap_decision`.
8. Progress streaming: the worker posts periodic comments or rows to a `run_log` table; the
   UI live-tails it. The local log viewer is one of the highest-value UI features.
9. Kill: a `work_queue.kill_requested` flag the worker polls.

**Phase 4 — human-in-the-loop**

10. `blocked` status + `ticket_questions` table (`type`, `format`, `options`, `multi`,
    `answer_value`, `answer_notes`), notification bell, auto-requeue on answer.
11. Agent chat: a `ticket_chat` table replacing the JSONL inbox; the worker pumps it to the
    CLI's stdin exactly as `_chat_pump` does. The `chat_*` helpers in
    `orchestrator_core.py` are already pure and unit-tested — reuse them.

**Phase 5 — quality of dispatch**

12. `profiles` table (cluster-scoped) + Profiles UI + `--allowedTools` / `--model` /
    system prompt on the executor; per-ticket `model` pin.
13. Triage. Decide **where it runs** — the one genuinely new architectural question.
    Options: (a) a scheduled job inside the FastAPI app (it already stores the cluster API
    key); (b) an elected "orchestrator worker" per cluster; (c) a Render cron job. (a) is
    simplest and keeps triage off user PCs.
14. Initial triage: auto-promote `todo` to `ready` with an inferred model and deps.

**Phase 6 — git and isolation**

15. `commit_gate` on tickets + auto-commit/auto-push from the worker, gated by the board's
    `commit_requirements`. Push credentials stay on the worker PC.
16. Worktrees, then Docker mode — the largest and least urgent, since the cloud already
    isolates by running on someone else's PC.

**Phase 7 — parity polish**

17. History/audit table, unread tracking, clear-done, `__all__` view, model discovery, spec
    attachment, usage-limit pause, activity feed, session resume.
18. Performance tab and Layrr are arguably *local-only by nature* — they inspect and serve
    the local machine. If wanted, they belong in the worker reporting telemetry upward, not
    in the web app.

## 9. Design tensions worth deciding early

- **Push vs pull.** Local is a central dispatcher deciding *what runs where*. Cloud is
  workers pulling greedily. Concurrency caps, triage and target routing all pull toward a
  dispatcher. Recommendation: keep the pull/claim model — it is the source of the cloud's
  race safety and worker autonomy — and express caps, deps and priority as **predicates
  inside the claim SQL**, with triage as an out-of-band annotator that only sets
  `profile` / `model` / `order` on queued rows.
- **Status vocabulary.** Pick one. Suggest keeping the cloud names and adding `blocked`:
  `review`/`failed` are genuinely useful, and renaming breaks already-enrolled exes.
- **Where the API key lives.** Any server-side triage uses the stored plaintext cluster
  key. That is already a documented caveat; making the server *use* it raises the stakes,
  so encryption at rest or per-user keys is worth doing before Phase 5.
- **Per-PC vs per-board config.** `directory`, `useWorktrees` and `envVars` are board-level
  locally because there is one PC. In the cloud, `directory` must be per-(worker, board);
  the rest can stay board-level.
