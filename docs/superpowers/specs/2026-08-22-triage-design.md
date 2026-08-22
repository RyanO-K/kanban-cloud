# Design — initial triage (Phase 5, gap analysis items 13-14)

Date: 2026-08-22
Status: approved, ready to implement
Context: `docs/2026-08-22-local-vs-cloud-gap-analysis.md`, §8 Phase 5, items 13-14 —
"the one genuinely new architectural question in the whole gap analysis."

## The question: where does triage run?

The gap analysis frames three options and calls out (a) as simplest "because the
app already holds the cluster API key." That premise is no longer true: the
"Workers authenticate with local Claude Code config, not a cloud key" change
(STATUS.md, 2026-08-09) deleted `cluster_settings` and the plaintext API key
entirely. The server holds **no Claude credential of any kind** today — every
`claude` CLI invocation runs on a worker PC, authenticated by whatever that PC
already has (`claude login` session or its own `ANTHROPIC_API_KEY`). Re-evaluating
the three options against that reality:

**(a) Scheduled job inside the FastAPI app.** Would need its own new credential —
an operator-owned Anthropic key set as a Render env var on the server, unrelated
to any cluster/user key. That reintroduces exactly the class of problem the
no-cloud-key change just closed (a single stored secret usable by anything that
can reach the server process), for a feature that doesn't need it. Also needs new
always-on infrastructure (APScheduler or similar) inside the request-serving
process.

**(c) A Render cron job.** Same new-credential problem as (a), plus a second
deployable service to build, configure, and keep in sync with schema changes —
new infrastructure for a job this cheap, and the design tensions section of the
gap analysis already flags "push vs pull" as a tension: introducing the first
piece of centrally-scheduled infrastructure cuts against the pull/claim model
that gives the cloud's claim path its race-safety today.

**(b) An elected orchestrator worker per cluster** — as literally specified
(leader election, one PC responsible) adds real complexity: a consensus/lease
mechanism, a story for what happens when the elected PC goes offline, and a
cluster with zero online workers never gets triaged even though nothing else
about "todo" tickets depends on a worker being up.

## Decision: opportunistic triage in every worker, no election

This is a variant of (b) with the election removed, and it is exactly the
precedent this codebase already set for the stale-claim reaper (STATUS.md,
"Stale-claim reaper", 2026-08-22): **every worker triages opportunistically once
per poll cycle**, piggybacked on the same main-loop tick that already calls
`reap_stale_claims` and `set_slot_counts`. No new infrastructure, no new
credential, no election:

- Triage needs an authenticated `claude` CLI to do the actual inference. Workers
  already have exactly that (the same one `ClaudeExecutor` uses); the server
  never has and, per the no-cloud-key decision, is not supposed to.
- The `kanban_worker` group role already grants every enrolled PC `UPDATE` on
  `tickets` and `INSERT` on `ticket_deps`/`work_queue` cluster-wide, not scoped to
  work that PC itself claimed — the same grant the reaper already relies on.
- Multiple workers racing to triage the same `todo` ticket is expected, not a
  bug, and is handled the same way `_reap_one`/`finish_work` already handle their
  own races: the row-applying `UPDATE` is guarded on `WHERE status='todo' AND
  model IS NULL`, so only the first writer's update has any rows to affect; every
  later one (a second pass by the same worker, or a concurrent one) sees rowcount
  0 and no-ops. That guard is also exactly what makes a second pass over an
  already-triaged ticket change nothing, which is the idempotency this ticket's
  acceptance criteria require.
- A cluster with no worker online simply doesn't triage new tickets yet — no
  worse than today, where such a cluster can't run any ticket at all either.
- Tradeoff, accepted: every online worker in a cluster calls the LLM once per
  poll cycle per un-triaged ticket it sees, so N workers means N redundant
  triage calls for the same ticket before the first one's UPDATE lands. Poll
  intervals are 10s+ and clusters are small, so this is not expected to be a
  meaningful cost; it can be revisited (e.g. a random jitter/skip) if it becomes
  one.

## What triage actually does

Mirrors the local tool's initial triage (`_real_sonnet_triage` /
`apply_initial_triage`): given one `todo` ticket with no `model` set yet, infer
(1) a model tier and (2) which other tickets on the same board it depends on,
then promote it to `ready`. Implemented as a new pure module, `app/triage.py`
(stdlib-only, like `app/prompt.py`, so it can be imported by `worker.py` without
dragging SQLAlchemy into the PyInstaller exe):

- `build_triage_prompt(ticket, candidates)` — composes a short instruction asking
  the CLI to reply with exactly one line of JSON: `{"model": "...", "depends_on":
  [...]}`, given the ticket's title/body and the id/title/status of every other
  ticket on its board as dependency candidates.
- `parse_triage_result(text, candidate_ids, ticket_id)` — strict validation: the
  reply must parse as JSON with `model` in `{"haiku", "sonnet", "opus"}` and
  `depends_on` a list of ints, each a real candidate id and not the ticket's own
  id. Anything else (malformed JSON, wrong types, an unknown model name, a
  self-dependency, an id from another board) returns `None`.

`worker.py` gains `triage_todo_tickets(conn, cluster_id, run_llm=...)`, called
from the main loop next to `reap_stale_claims`. For each eligible ticket it
builds the prompt, calls the (injectable, for testing) `run_llm` — the real
implementation shells `claude -p <prompt>` exactly like `ClaudeExecutor`, just
without tool grants or streaming, since this is a single JSON answer, not an
agentic run. Any exception from `run_llm`, or a `None` from `parse_triage_result`,
is a **triage failure**: the ticket is left in `todo` and nothing is written —
satisfying this ticket's other acceptance criterion directly, with no separate
failure-handling path to get wrong. A successful parse applies atomically:
`tickets.model`/`status='ready'` (guarded `WHERE status='todo' AND model IS
NULL`), `ticket_deps` rows for the inferred dependencies (`ON CONFLICT DO
NOTHING`), and a fresh `work_queue` row so the promoted ticket actually reaches
the claim queue — the same three things a human promoting a ticket by hand
already causes via `delegation.enqueue_ticket`.

## Schema

One nullable column, no migration risk: `tickets.model VARCHAR(32)`. `NULL` means
"not yet triaged" (or a pre-triage-era row) — the same convention `session_id`
and other Phase 1 columns already use.

## Non-goals

- Profiles (`gap analysis item 12`) — separate ticket, not blocked on this one.
- Dispatch-time triage (choosing *which ready ticket* to run next, gap analysis
  item 4 in the orchestration table) — this ticket is only the *initial* triage
  that promotes `todo` → `ready`.
- Re-triaging a ticket whose model/deps were set by mistake, or backing off
  retries on repeated triage failure — a ticket that keeps failing triage stays
  in `todo` and gets retried every poll cycle by every online worker, forever.
  Acceptable for now (same "no throttle yet" cut the gap analysis already
  documents for other Phase 5 items); worth revisiting if a bad ticket body
  turns into a standing LLM-call cost.
