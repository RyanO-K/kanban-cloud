# Import a local `.kanban` board into kanban-cloud — design

Date: 2026-08-22

## Goal

An **Import** button in the board header that pulls a local file-based `.kanban`
board (a folder of numbered JSON tickets) into the cloud as a new board. Folder
picker or drag-and-drop; one board per import.

## Why the browser has to do the reading

kanban-cloud runs on Render. It cannot see `C:/Users/ryan/Documents/Github/.kanban/`.
"Import from local" therefore means: the browser reads the files off the operator's
machine and POSTs them up. There is no server-side filesystem path to add.

## Division of labour

`app/static/index.html` is inline JS with no test runner. `app/` has a 73-test
pytest suite. So the split is deliberate:

- **Browser** — read the chosen folder, keep a fixed key whitelist per ticket, POST.
  No vocabulary translation, no formatting, no naming decisions.
- **Server** — every semantic decision, in a new pure module that is unit-tested.

The whitelist is also what keeps the payload small. Measured against the six live
local boards: raw folders are 0.9–2.8 MB (dominated by `history` and run logs);
after whitelisting, 33–204 KB. The site-page proxy streams request bodies
(`req.pipe(upstream)`) with no buffering or size cap, so 204 KB is unremarkable.

Whitelisted keys, per ticket file:

```
title, detail, status, comments, dependsOn, blocks, steps, files, outputs
```

Everything else is dropped in the browser and never leaves the machine:
`history`, `claudeSessionId`, `claudeSessionDir`, `runLogFile`, `commitGate`,
`completedLog`, `model`, `orchestrator`, `optional`. That is run exhaust from a
different machine and it is where all the bloat lives.

## Module: `app/importer.py`

Pure functions. No DB session, no FastAPI, no I/O.

### `map_status(local: str | None) -> str`

The two vocabularies overlap only partly.

| local | cloud | note |
|---|---|---|
| `todo`, `pending` | `todo` | |
| `ready` | `ready` | **enqueues for an agent** — see "Ready dispatches" below |
| `in_progress` | `doing` | |
| `blocked` | `todo` | cloud has no `blocked`; the original is recorded in the appendix |
| `completed` | `done` | |
| `done`, `review`, `failed`, `doing` | passthrough | already cloud vocabulary |
| anything else, missing, non-string | `todo` | |

The passthrough row is not hypothetical: the live `trump-market-impact` board has
three tickets already in `done`.

### `render_body(raw: dict, board_slug: str, local_id: str) -> str`

Returns `detail`, followed by a context appendix. The appendix always carries the
provenance line; each other subsection appears only when its source field is
present and non-empty.

```
<detail>

---
*Imported from local board `ai-kanban` #16 (local status: completed)*

**Depends on:** 12, 13
**Blocks:** 20
**Steps:**
- …
**Files:**
- …
**Outputs:**
- …
```

The provenance line is what makes `blocked` → `todo` non-lossy: the original
local status is always stated.

### `normalize_ticket(raw, board_slug) -> dict`

`{title, body, status, comments}` where `comments` is a list of
`{writer, message, created_at}`. `created_at` parses the local `timestamp`
(ISO 8601, with or without offset — stored naive UTC to match `models.utcnow`)
and falls back to import time when absent or unparseable.

Tickets with a blank or missing `title` are **skipped**, not failed — one bad file
should not lose the other forty. The count comes back in the response.

### `unique_board_name(existing: list[str], desired: str) -> str`

Returns `desired` if free, else `desired (2)`, `desired (3)`, … Comparison is
case-insensitive and whitespace-trimmed, matching how `default_board` already
compares board names.

### `sort_key(local_id: str) -> tuple`

Numeric where the id is numeric, so ticket 2 precedes ticket 10. Non-numeric ids
sort after numeric ones, lexically.

## Endpoint: `POST /api/clusters/{cluster_id}/import`

Request:

```json
{ "name": "ai-kanban", "tickets": [ { "title": "...", "detail": "...", ... } ] }
```

Response:

```json
{ "board_id": 7, "name": "ai-kanban", "imported": 40, "skipped": 1, "queued": 0 }
```

Behaviour:

- `require_member(db, user, cluster_id)` — same gate as `create_board`.
- Board name is `unique_board_name` over the cluster's existing boards. Import
  never merges into or mutates an existing board, so re-importing is always safe.
- Tickets are inserted in `sort_key` order so cloud ids follow local ids.
- Tickets that map to `ready` are enqueued via `delegation.enqueue_ticket`.
- Empty `tickets` is a 400 (`"no tickets to import"`); the board is not created.
- More than 500 tickets is a 400, to bound a pathological upload.

Spectators cannot reach it: the site-page proxy rejects non-GET in read-only mode,
and the FastAPI `proxy_gate` middleware independently rejects any non-GET without
`x-proxy-user`. Both checks already exist; the endpoint adds no new surface.

## Browser UI

An `Import` button in the header next to `+ ticket`, carrying `owner-only` so it
disappears for spectators exactly like its neighbours.

Two entry paths, converging on one `importBoard(fileList, folderName)`:

1. **Click** — a hidden `<input type="file" webkitdirectory>`. Picking a folder
   fires `change`.
2. **Drop** — the board area is a drop target. `dataTransfer.items[0].webkitGetAsEntry()`
   gives a `FileSystemDirectoryEntry`; one `readEntries` pass collects its files.
   A dashed overlay shows on `dragover`.

Validation, in the browser, before any request:

- The folder must contain `_meta.json` — the same rule the local tool uses to
  decide whether a directory is a board. If it is missing, the error names the
  subfolders that *do* look like boards, so dropping `.kanban/` itself says
  *"That's a folder of 6 boards — drop one of them: ai-kanban, discord-kanban-bot, …"*
  rather than failing silently.
- `_meta.json` itself is not imported as a ticket. Board name comes from the
  folder name, not from `_meta.json`'s `project` field, because the folder name is
  what the operator just picked and recognises.
- Files that are not `<digits>.json` are ignored.
- Files that do not parse as a JSON object are counted and reported, not fatal.

On success: toast `Imported 40 tickets into "ai-kanban"`, board list refreshes,
the new board is selected. On failure: the server's message in the toast.

## Explicitly out of scope

- **Multi-board import.** One board per import. Dropping the `.kanban/` root is a
  clear error, not a six-board bulk load.
- **Sync / re-import / dedupe.** Import is one-directional and always creates a new
  board. There is no update path, no id correlation, no conflict resolution.
- **Cloud → local export.** Not addressed.
- **The orchestration gap.** `dependsOn`/`blocks` arrive as prose in the appendix,
  not as a dependency graph — the cloud schema has no such concept. See
  `docs/2026-08-22-local-vs-cloud-gap-analysis.md`.

## Ready dispatches

Per the operator's decision, local `ready` maps to cloud `ready`, which enqueues
the ticket for an agent to claim and run. Importing a board with queued work will
hand that work to agents.

This is currently inert: across all 125 tickets in the six live local boards the
statuses are 120 `completed`, 3 `done`, 2 `blocked`. Nothing is in `ready`. The
behaviour matters only for a future board that has queued work. Reversing it is a
one-line change to the `map_status` table.

## Cold start

Import is a single request and inherits the outstanding cold-start bug: the
site-page proxy times out at 15 s while a sleeping Render service takes ~32 s to
wake, so the first import against a cold board can return *"The board is
unreachable right now"*. Retrying works. This is pre-existing and tracked
separately; the import feature neither causes nor fixes it.

## Testing

`tests/test_importer.py` — pure mapping:
- every row of the status table, plus unknown / missing / non-string
- appendix with all subsections, with none, and with each individually absent
- provenance line records the original local status, including `blocked`
- comment timestamp parsing: with offset, without, malformed, absent
- `unique_board_name` on free name, one clash, several clashes, case/whitespace
- `sort_key` orders 2 before 10; non-numeric ids sort last
- blank-title tickets are skipped

`tests/test_import_endpoint.py` — route:
- creates the board and its tickets, in local-id order
- a `ready` ticket produces a `work_queue` row; a `completed` one does not
- name clash produces `name (2)` and leaves the original board untouched
- empty ticket list → 400, no board created
- over-500 tickets → 400
- non-member → 403; spectator (proxy mode, no `x-proxy-user`) → 403
- blank-title tickets are skipped and counted in `skipped`

`tests/test_frontend_markup.py` — the Import button, the hidden directory input,
and the drop zone exist, and the button carries `owner-only`.
