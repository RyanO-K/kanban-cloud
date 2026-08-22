# Live progress streaming to the UI

Date: 2026-08-22

## Problem

`ClaudeExecutor.run` used `subprocess.run(cmd, capture_output=True, timeout=1800)`:
the worker process blocks until the CLI exits (or 30 minutes elapse) and only
then does anything become visible — no comment, no partial output, nothing.
A ticket goes silent for up to 30 minutes and then produces one summary
comment. The local `.kanban` tool's live-tailed run log is one of its
highest-value features (gap analysis §4 item 14); the cloud has no
equivalent (gap analysis §5, "Mid-run progress comments — Missing").

## Decision: periodic comments, not a `run_log` table

Two options were on the table (gap analysis §8 item 8): periodic comments on
the existing `comments` table, or a dedicated `run_log` table the UI
live-tails separately.

**Chosen: periodic comments**, via the existing `comments` table and the
`add_progress()` helper that was already written (and exercised by
`scripts/neon_smoke_v2.py`) but never actually called from a real run. Why:

- **The delivery mechanism already exists and already works.** The board UI
  polls `GET /api/boards/{id}/tickets` every 5s and re-renders each ticket's
  `comments[]` in the side panel (`app/static/index.html`, the `setInterval`
  at the bottom of the file). Posting a comment mid-run makes it show up
  within one poll cycle — "live-tailing" falls out of infrastructure that's
  already shipped, with zero new endpoints, no new poll loop, no new schema.
- **A `run_log` table earns its keep only if comments are the wrong shape**
  for high-frequency data (thousands of tiny rows) or need different
  retention/visibility than a ticket comment. Neither is true here: an agent
  run produces a few dozen turns at most, and a run's progress *is*
  legitimately part of the ticket's discussion — it is useful after the run
  ends too, exactly like a comment.
- **A comment is naturally a chat-log entry.** `writer = "worker:<name>"`
  already gives every progress post a clear author, which is what the UI's
  comment renderer expects; a separate `run_log` viewer would need its own
  UI surface (the local tool's dedicated log panel) that this ticket is not
  scoped to build.
- **Cost is bounded by batching, not by schema.** The concern a `run_log`
  table would normally address — a chatty run flooding the ticket with
  noise — is handled instead by batching several stream-json turns into one
  comment (`ClaudeExecutor.progress_batch`, default 4) rather than posting
  one comment per CLI event.

If a future ticket wants a dedicated timeline view distinct from the
discussion thread, `run_log` is still the right add — this decision is about
what ships now, not a claim that comments are the final answer forever.

## Design

### 1. `ClaudeExecutor.run`: `Popen` + incremental reads

Replaced `subprocess.run(capture_output=True, timeout=1800)` with
`subprocess.Popen(..., stdout=PIPE, stderr=STDOUT, text=True, bufsize=1)`,
iterating `proc.stdout` line by line as the process produces it. This is
what makes streaming possible at all — `capture_output=True` cannot yield
anything until the process exits, full stop.

The CLI is now invoked with `--output-format stream-json --verbose`, the
same format the local orchestrator's log viewer already parses (one JSON
object per line: `assistant` turns carrying text/tool_use blocks, and a
final `result` summary). `_stream_json_text()` turns one such line into a
short human-readable string, or `None` for lines with nothing worth
surfacing (`system` init, `user` tool-result echoes).

Turns are buffered and flushed to `progress_cb` every `progress_batch`
lines (default 4), so a chatty run produces a handful of comments instead
of one per CLI event.

**Side effect: the 30-minute all-or-nothing timeout is gone.** There was
never a principled reason for that number — it existed only because
`capture_output=True` had no way to report partial progress, so the old
code needed *some* upper bound before giving up and losing everything. With
incremental reads, whatever the agent produced before a stall is already
visible, so an arbitrary kill-switch is no longer pulling its weight. Killing
a genuinely stuck run is left to the `work_queue.kill_requested` flag from
gap analysis §8 item 9 (a separate ticket) rather than a timer here.

### 2. Crash handling

The read loop is wrapped in `try/except`. If reading `proc.stdout` raises
(broken pipe, process killed out from under us, etc.), the executor:

1. flushes whatever turns were already buffered to `progress_cb`,
2. kills the process defensively,
3. returns `(False, "...crashed...\n\n<partial transcript>")` instead of
   letting the exception propagate.

Because `progress_cb` is invoked *before* the crash is reported, and
`run_slot`'s `progress_cb` closure calls `add_progress()` (an immediate,
committed `INSERT INTO comments`), the ticket already has the partial log
attached as comments by the time the final failure comment lands — even
though the overall run is a failure. `run_slot`'s outer `except Exception`
around the whole `executor.run()` call (for exceptions the executor itself
doesn't catch) does not need to change: it already turns an unexpected
exception into a generic failure comment, and by then the incremental
comments are already committed rows, unaffected by what the final comment
says.

### 3. Worker wiring (`run_slot`)

A `progress_cb(message)` closure is built per claimed ticket, bound to the
current `conn`/`ticket_id` via default arguments (so a later loop iteration
reassigning `conn` can't retarget an in-flight callback), and passed to
`executor.run(..., progress_cb=progress_cb)`. The closure itself never lets
a DB error escape — a transient Neon hiccup mid-run must not take down an
otherwise-healthy agent run — it logs and swallows.

`StubExecutor.run` gains the same `progress_cb=None` kwarg for signature
parity; it doesn't stream anything (there's no subprocess to stream from).

## Non-goals

- A dedicated run-log UI panel / separate live-tail endpoint — the existing
  5s ticket poll + comments feed already delivers this.
- Killing a hung run (`kill_requested`) — separate ticket, gap analysis §8
  item 9.
- Deduplicating/collapsing repeated tool-call summaries — batching bounds
  volume well enough for now; can revisit if real runs prove noisy.

## Tests

`tests/test_executor.py`, rewritten around a fake `Popen`/stdout pair since
the executor no longer calls `subprocess.run`:

- existing contract tests (cwd, `--allowedTools`, `--session-id`, no
  `shell=True`, resolving via `shutil.which`, missing-CLI/missing-directory
  short-circuits) ported to the new mock shape.
- `test_executor_requests_stream_json` — cmd carries
  `--output-format stream-json`; no `timeout` kwarg is passed to `Popen`.
- `test_partial_output_streamed_before_process_exits` — `progress_cb` is
  called more than once, and an early call's content does not include a
  later line's content, proving genuine incrementality rather than a single
  post-exit dump.
- `test_progress_batches_before_flushing` — several turns collapse into one
  `progress_cb` call when `progress_batch > 1`.
- `test_crashed_agent_leaves_partial_log` — simulates the stdout iterator
  raising mid-stream; asserts `progress_cb` already saw the partial content
  before the crash, the process is killed, and the returned failure comment
  still contains the partial transcript.
- `test_nonzero_exit_reports_partial_output`, `test_progress_callback_error_does_not_abort_the_run`.
