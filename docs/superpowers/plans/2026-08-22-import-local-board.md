# Import a local `.kanban` board — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An Import button in the board header that loads a local file-based `.kanban` board folder into kanban-cloud as a new board.

**Architecture:** The browser reads the folder and POSTs a key-whitelisted payload; a new pure module `app/importer.py` does every vocabulary and formatting decision server-side, where it is unit-testable; a new endpoint wires the two together.

**Tech Stack:** FastAPI + SQLAlchemy + pytest; inline vanilla JS in `app/static/index.html`.

**Spec:** `docs/superpowers/specs/2026-08-22-import-local-board-design.md`

## Global Constraints

- Run tests with `.venv/Scripts/python.exe -m pytest -q` — the bare `py -3.14`
  interpreter lacks `psycopg` and fails collection on `tests/test_worker*.py`.
- Baseline is **73 passing**. Every task ends green.
- The working tree carries **uncommitted third-party work** in
  `app/static/index.html` (dark mode + account dropdown) and an untracked
  `docs/2026-08-22-local-vs-cloud-gap-analysis.md`. Never `git add -A`,
  never `git commit -a`. Stage files by explicit path, and stage `index.html`
  only via a hunk-scoped patch (Task 3).
- Console/user-facing strings: ASCII only. The Windows console mangles em dashes.
- Statuses come from `models.TICKET_STATUSES`; never hardcode the list.

---

### Task 1: `app/importer.py` — pure mapping

**Files:**
- Create: `app/importer.py`
- Test: `tests/test_importer.py`

**Interfaces:**
- Consumes: `app.models.TICKET_STATUSES`, `app.models.utcnow`
- Produces: `map_status(local) -> str`, `render_body(raw, board_slug, local_id) -> str`,
  `normalize_ticket(raw, board_slug) -> dict | None`, `unique_board_name(existing, desired) -> str`,
  `sort_key(local_id) -> tuple`, `MAX_IMPORT_TICKETS = 500`

- [ ] **Step 1: Write the failing tests**

```python
import datetime
from app import importer


def test_map_status_local_vocabulary():
    assert importer.map_status("todo") == "todo"
    assert importer.map_status("pending") == "todo"
    assert importer.map_status("ready") == "ready"
    assert importer.map_status("in_progress") == "doing"
    assert importer.map_status("blocked") == "todo"
    assert importer.map_status("completed") == "done"


def test_map_status_passes_through_cloud_vocabulary():
    for status in ("todo", "ready", "doing", "review", "done", "failed"):
        assert importer.map_status(status) == status


def test_map_status_defaults_unknown_to_todo():
    assert importer.map_status("banana") == "todo"
    assert importer.map_status(None) == "todo"
    assert importer.map_status(42) == "todo"
    assert importer.map_status("") == "todo"
    assert importer.map_status("  Ready  ") == "ready"  # trimmed + lowercased


def test_render_body_records_provenance_including_original_status():
    body = importer.render_body({"detail": "Do the thing", "status": "blocked"}, "ai-kanban", "16")
    assert body.startswith("Do the thing")
    assert "local board `ai-kanban` #16" in body
    assert "local status: blocked" in body


def test_render_body_emits_only_present_sections():
    body = importer.render_body(
        {"detail": "d", "status": "todo", "dependsOn": ["12", "13"], "steps": ["a", "b"]},
        "b", "1",
    )
    assert "**Depends on:** 12, 13" in body
    assert "**Steps:**" in body
    assert "- a" in body
    assert "**Blocks:**" not in body
    assert "**Files:**" not in body


def test_render_body_with_no_extras_is_detail_plus_provenance():
    body = importer.render_body({"detail": "just this", "status": "done"}, "b", "3")
    assert body.count("**") == 0
    assert "just this" in body
    assert "local status: done" in body


def test_render_body_survives_missing_detail():
    body = importer.render_body({"status": "todo"}, "b", "1")
    assert "local board `b` #1" in body


def test_normalize_ticket_parses_comment_timestamps():
    raw = {
        "title": "T", "status": "completed",
        "comments": [
            {"writer": "ryan", "message": "m1", "timestamp": "2026-07-04T01:30:19+00:00"},
            {"writer": "bot", "message": "m2", "timestamp": "2026-07-04T01:30:19"},
            {"writer": "bot", "message": "m3", "timestamp": "not a date"},
            {"writer": "bot", "message": "m4"},
        ],
    }
    out = importer.normalize_ticket(raw, "b", "1")
    assert out["status"] == "done"
    stamps = [c["created_at"] for c in out["comments"]]
    assert stamps[0] == datetime.datetime(2026, 7, 4, 1, 30, 19)
    assert stamps[0].tzinfo is None
    assert stamps[1] == datetime.datetime(2026, 7, 4, 1, 30, 19)
    assert all(s is not None for s in stamps)


def test_normalize_ticket_skips_blank_titles():
    assert importer.normalize_ticket({"title": "   ", "status": "todo"}, "b", "1") is None
    assert importer.normalize_ticket({"status": "todo"}, "b", "1") is None
    assert importer.normalize_ticket("not a dict", "b", "1") is None


def test_normalize_ticket_drops_malformed_comments():
    out = importer.normalize_ticket(
        {"title": "T", "comments": ["nope", {"message": "no writer"}, {"writer": "w"}]}, "b", "1"
    )
    assert out["comments"] == []


def test_unique_board_name():
    assert importer.unique_board_name([], "ai-kanban") == "ai-kanban"
    assert importer.unique_board_name(["ai-kanban"], "ai-kanban") == "ai-kanban (2)"
    assert importer.unique_board_name(["ai-kanban", "ai-kanban (2)"], "ai-kanban") == "ai-kanban (3)"
    assert importer.unique_board_name([" AI-Kanban "], "ai-kanban") == "ai-kanban (2)"
    assert importer.unique_board_name([], "   ") == "Imported board"


def test_sort_key_orders_numerically():
    ids = ["10", "2", "1", "abc", "3"]
    assert sorted(ids, key=importer.sort_key) == ["1", "2", "3", "10", "abc"]
```

- [ ] **Step 2: Run to verify failure**

`.venv/Scripts/python.exe -m pytest tests/test_importer.py -q` — expect `ModuleNotFoundError: app.importer`.

- [ ] **Step 3: Implement `app/importer.py`**

Module docstring explains the two vocabularies. `STATUS_MAP` dict for the local
vocabulary; `map_status` trims + lowercases, checks `STATUS_MAP`, then
`TICKET_STATUSES` passthrough, else `"todo"`. `_lines(label, values)` helper
renders a bullet list section, returning `""` for empty/absent. `render_body`
composes detail + `---` + provenance + sections. `_parse_ts` tries
`datetime.fromisoformat`, strips tzinfo to naive UTC, returns `utcnow()` on
failure. `normalize_ticket` returns `None` for non-dict or blank title.
`unique_board_name` compares `.strip().lower()`. `sort_key` returns
`(0, int(id))` for digit strings, `(1, str(id))` otherwise.

- [ ] **Step 4: Run tests to verify they pass**

`.venv/Scripts/python.exe -m pytest tests/test_importer.py -q` — expect all pass.

- [ ] **Step 5: Commit**

```bash
git add app/importer.py tests/test_importer.py
git commit -m "feat: pure mapping module for local .kanban board import"
```

---

### Task 2: Import endpoint

**Files:**
- Modify: `app/main.py` (new route after `create_board`; import `importer`)
- Test: `tests/test_import_endpoint.py`

**Interfaces:**
- Consumes: Task 1's `importer.*`; existing `require_member`, `delegation.enqueue_ticket`, `ImportBody` (new pydantic model)
- Produces: `POST /api/clusters/{cluster_id}/import` → `{board_id, name, imported, skipped, queued}`

- [ ] **Step 1: Write the failing tests**

Cover, using the existing `conftest.py` fixtures and the auth helpers already used
by `tests/test_tickets.py`:

- `test_import_creates_board_and_tickets_in_local_id_order` — post ids 10, 2, 1;
  assert returned tickets' titles are in 1, 2, 10 order and `imported == 3`
- `test_import_maps_statuses` — `completed` arrives as `done`, `in_progress` as `doing`
- `test_import_ready_ticket_is_queued` — a `ready` ticket creates a `WorkItem`;
  response `queued == 1`
- `test_import_completed_ticket_is_not_queued` — no `WorkItem` rows
- `test_import_carries_comments` — comment writer/message present on the created ticket
- `test_import_appends_context_appendix` — body contains the provenance line
- `test_import_name_clash_suffixes` — second import of the same name yields
  `name (2)` and the first board's ticket count is unchanged
- `test_import_rejects_empty_ticket_list` — 400, and no board was created
- `test_import_rejects_oversized_payload` — 501 tickets → 400
- `test_import_skips_blank_titles` — `skipped == 1`, `imported == 1`
- `test_import_requires_membership` — non-member → 403
- `test_import_rejected_for_spectator` — proxy app without `x-proxy-user` → 403

- [ ] **Step 2: Run to verify failure** — expect 404s on the route.

- [ ] **Step 3: Implement the route**

```python
class ImportBody(BaseModel):
    name: str
    tickets: list[dict]
```

Handler: `require_member`; reject empty and `> importer.MAX_IMPORT_TICKETS`;
`unique_board_name` over existing board names; create `Board`; iterate
`sorted(tickets, key=lambda t: importer.sort_key(t.get("id")))`, `normalize_ticket`,
skip `None`, create `Ticket` + `Comment` rows; `db.commit()`; then
`delegation.enqueue_ticket` for each ticket whose status is `AGENT_READY_STATUS`;
return the counts.

- [ ] **Step 4: Run the full suite** — `.venv/Scripts/python.exe -m pytest -q`.

- [ ] **Step 5: Commit**

```bash
git add app/main.py tests/test_import_endpoint.py
git commit -m "feat: POST /api/clusters/{id}/import endpoint"
```

---

### Task 3: Browser UI — button, folder picker, drop zone

**Files:**
- Modify: `app/static/index.html`
- Test: `tests/test_frontend_markup.py`

**Interfaces:**
- Consumes: Task 2's endpoint
- Produces: `#importBtn`, `#importInput`, `#dropZone`, `importBoard(files, folderName)`

**Staging constraint:** `index.html` also holds uncommitted third-party work.
Stage only this task's hunks:

```bash
git diff -U3 app/static/index.html > /tmp/all.patch   # inspect
# hand-assemble a patch containing ONLY the import hunks, then:
git apply --cached import-only.patch
git status --short   # index.html should show BOTH staged (M) and unstaged (M)
```

- [ ] **Step 1: Write the failing markup tests**

Append to `tests/test_frontend_markup.py`:

```python
def test_import_button_is_owner_only():
    html = INDEX.read_text(encoding="utf-8")
    assert 'id="importBtn"' in html
    assert 'class="small ghost owner-only" id="importBtn"' in html


def test_import_input_is_a_directory_picker():
    html = INDEX.read_text(encoding="utf-8")
    assert 'id="importInput"' in html
    assert "webkitdirectory" in html


def test_board_area_is_a_drop_zone():
    html = INDEX.read_text(encoding="utf-8")
    assert 'id="dropZone"' in html
    assert "webkitGetAsEntry" in html
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement**

Header, immediately after the `+ ticket` button:

```html
<button class="small ghost owner-only" id="importBtn" onclick="document.getElementById('importInput').click()">Import</button>
<input type="file" id="importInput" webkitdirectory directory multiple style="display:none" onchange="onImportPicked(event)">
```

CSS for `#dropZone.dragging` (dashed accent outline + tinted background), a
`.dropHint` overlay, and JS:

- `collectBoardFiles(fileList)` → `{metaFound, ticketFiles, boardName, subBoards}`.
  `webkitRelativePath` gives `<folder>/<file>`; board name is the first segment.
  A file is a ticket iff its basename matches `/^\d+\.json$/`. `subBoards` is the
  set of second-level directories containing a `_meta.json`, used for the
  "you dropped the root" message.
- `readDroppedDirectory(entry)` → Promise of a File list, via
  `entry.createReader().readEntries()` looped until it returns empty (the API
  returns at most 100 per call — the 41-ticket board needs the loop).
- `importBoard(files, folderName)` — validate `_meta.json` present; parse each
  ticket file with `JSON.parse`, keep the nine whitelisted keys plus `id`;
  POST to `./api/clusters/{id}/import`; on success refresh boards, select the new
  one, toast the counts.
- Drag handlers on `#dropZone`: `dragover`/`dragenter` add `.dragging` and
  `preventDefault`; `dragleave` and `drop` remove it. Guard the whole thing with
  `if (sessionMode === "spectator") return;`.

- [ ] **Step 4: Run the suite.**

- [ ] **Step 5: Manual verification against a local server**

```bash
.venv/Scripts/python.exe -m uvicorn app.main:app --factory --port 8099
```

Open `http://127.0.0.1:8099/`, register, create a cluster, then import
`C:/Users/ryan/Documents/Github/.kanban/snake-game` (2 files — smallest board).
Confirm: new board appears and is selected, its one ticket lands in `done`, and
the body carries the provenance line. Then repeat with drag-and-drop on
`trump-market-impact` (41 tickets) and confirm the `readEntries` loop got all of
them.

- [ ] **Step 6: Commit (hunk-scoped, per the staging constraint above)**

---

### Task 4: Docs and end-to-end verification

**Files:**
- Modify: `README.md`, `STATUS.md`

- [ ] **Step 1: Document the feature** — how to import, the status mapping table,
  what is dropped, the one-board-per-import rule, and the `ready`-dispatches note.
- [ ] **Step 2: Run the full suite one final time.**
- [ ] **Step 3: Commit.**
- [ ] **Step 4:** Use superpowers:finishing-a-development-branch.
