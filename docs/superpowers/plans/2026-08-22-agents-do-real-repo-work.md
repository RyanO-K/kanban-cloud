# Cloud Agents Doing Real Repo Work — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give a cloud worker enough context to run an agent inside the right repo, with the right permissions, and let each PC decide how many tickets it runs at once.

**Architecture:** Board-level *project* facts (description, out-of-scope, commit requirements, worktree flag) live on the server in new `boards` columns. Machine-level *path* facts live in each worker's `.worker_config.json`, keyed by board id. The worker composes the agent prompt at claim time from both halves via a pure, stdlib-only `app/prompt.py`, then runs `claude -p` with `cwd` set to the board's local path. Concurrency is N independent slot threads inside one worker process, each with its own psycopg connection.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, psycopg3, Postgres (Neon) with SQLite fallback, vanilla JS single-page frontend, pytest.

**Spec:** `docs/superpowers/specs/2026-08-22-agents-do-real-repo-work-design.md`

## Global Constraints

- Baseline is **114 passing tests** on branch `phase1-repo-work` at `217adba`. Every task ends green; no task may reduce the count.
- Run tests with the parent repo's venv: `/c/Users/ryan/Documents/Github/kanban-cloud/.venv/Scripts/python -m pytest tests/ -q` from the worktree root.
- **`app/prompt.py` must import only the standard library.** It is pulled into the PyInstaller onefile exe through `worker.py`; a SQLAlchemy or FastAPI import there drags the whole server in.
- Migrations follow the existing guarded pattern in `app/db.py::run_migrations` — `ADD COLUMN IF NOT EXISTS` on Postgres, explicit `inspect()`-guarded `ADD COLUMN` on SQLite. Never `DROP` a column that existing exes still write.
- **No new Postgres grants.** `kanban_worker` already holds `SELECT` on `boards` and `UPDATE` on `workers`/`tickets`; new columns inherit table-level grants. If a task appears to need a grant, stop — the design is wrong.
- Default `concurrency` is **1**, preserving today's behavior for every already-deployed exe.
- `status` on `workers` stays and stays maintained. Deployed exes still write it.
- Out of scope, do not build: cluster-wide cap, ticket dependencies, stale-claim reaper, progress streaming, kill, `blocked` status, profiles, triage, auto-commit/auto-push.
- Another session is editing `app/db.py` and `app/static/index.html` on `master`. Re-read both before every edit; never assume the file matches a stale read.

---

### Task 1: All schema changes in one migration

Every column this feature needs, added together, so `run_migrations` is touched exactly once (the other session is editing that function on `master`).

**Files:**
- Modify: `app/models.py` (Board, Ticket, Worker)
- Modify: `app/db.py::run_migrations`
- Modify: `schema.sql`
- Test: `tests/test_migrations.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Board.description`, `Board.out_of_scope`, `Board.commit_requirements` (`str | None`); `Board.use_worktrees` (`bool`, default False); `Ticket.session_id` (`str | None`); `Worker.concurrency` (`int`, default 1); `Worker.running` (`int`, default 0).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_migrations.py`:

```python
def test_models_have_phase1_columns():
    from app.models import Board, Ticket, Worker
    board_cols = {c.name for c in Board.__table__.columns}
    assert {"description", "out_of_scope", "commit_requirements",
            "use_worktrees"} <= board_cols
    assert "directory" not in board_cols  # per-PC, never a server column
    assert "session_id" in {c.name for c in Ticket.__table__.columns}
    assert {"concurrency", "running"} <= {c.name for c in Worker.__table__.columns}


def test_migration_adds_phase1_columns_to_an_existing_sqlite_db(tmp_path):
    """A DB created before these columns must reach the new shape."""
    import sqlalchemy as sa
    from app.db import make_engine, run_migrations
    from app.models import Base

    url = f"sqlite:///{tmp_path / 'old.db'}"
    engine = make_engine(url)
    Base.metadata.create_all(engine)
    # Simulate the pre-Phase-1 shape by dropping the new columns back off.
    with engine.begin() as conn:
        for col in ("description", "out_of_scope", "commit_requirements",
                    "use_worktrees"):
            conn.execute(sa.text(f"ALTER TABLE boards DROP COLUMN {col}"))
        conn.execute(sa.text("ALTER TABLE tickets DROP COLUMN session_id"))
        for col in ("concurrency", "running"):
            conn.execute(sa.text(f"ALTER TABLE workers DROP COLUMN {col}"))

    run_migrations(engine)
    run_migrations(engine)  # idempotent

    insp = sa.inspect(engine)
    assert {"description", "out_of_scope", "commit_requirements",
            "use_worktrees"} <= {c["name"] for c in insp.get_columns("boards")}
    assert "session_id" in {c["name"] for c in insp.get_columns("tickets")}
    assert {"concurrency", "running"} <= {c["name"] for c in insp.get_columns("workers")}


def test_phase1_defaults_are_backward_compatible(tmp_path):
    """An old row that predates the columns reads as 1 slot / 0 running."""
    import sqlalchemy as sa
    from sqlalchemy.orm import Session
    from app.db import make_engine, run_migrations
    from app.models import Base, Cluster, User, Worker

    engine = make_engine(f"sqlite:///{tmp_path / 'd.db'}")
    Base.metadata.create_all(engine)
    run_migrations(engine)
    with Session(engine) as db:
        db.add(User(id=1, email="a@b.co", password_hash="x"))
        db.add(Cluster(id=1, name="T", join_code="J", created_by=1))
        db.commit()
        with engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO workers (id, cluster_id, name, revoked, status,"
                " last_seen, created_at) VALUES"
                " (1,1,'old-pc',0,'idle','2020-01-01','2020-01-01')"
            ))
        w = db.get(Worker, 1)
        assert (w.concurrency, w.running) == (1, 0)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `/c/Users/ryan/Documents/Github/kanban-cloud/.venv/Scripts/python -m pytest tests/test_migrations.py -q`
Expected: FAIL — `AssertionError` on the column sets (the `DROP COLUMN` in test 2 will also fail with "no such column", which is the same signal).

- [ ] **Step 3: Add the model columns**

In `app/models.py`, add to `class Board`:

```python
    # Project context, injected into every agent prompt built for this board.
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    out_of_scope: Mapped[str | None] = mapped_column(Text, nullable=True)
    commit_requirements: Mapped[str | None] = mapped_column(Text, nullable=True)
    use_worktrees: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
```

Add to `class Ticket`:

```python
    # Claude CLI session id for the most recent attempt, so a human can take
    # over a stuck run with `claude --resume <id>`.
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
```

Add to `class Worker`:

```python
    # Slots this PC will run at once, and how many are busy right now. The
    # PC owns the limit; the server only displays it.
    concurrency: Mapped[int] = mapped_column(Integer, default=1, nullable=False,
                                             server_default="1")
    running: Mapped[int] = mapped_column(Integer, default=0, nullable=False,
                                         server_default="0")
```

- [ ] **Step 4: Add the migration**

In `app/db.py::run_migrations`, inside the Postgres branch, after the existing `workers` statements:

```python
            for col, ddl in _PHASE1_COLUMNS:
                conn.execute(text(f"ALTER TABLE {col} ADD COLUMN IF NOT EXISTS {ddl}"))
```

For SQLite, add a helper and call it from the SQLite branch:

```python
def _add_missing_columns(engine) -> None:
    """SQLite flavor of the Phase 1 ADD COLUMNs (no IF NOT EXISTS support)."""
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    with engine.begin() as conn:
        for table, ddl in _PHASE1_COLUMNS:
            if table not in tables:
                continue
            column = ddl.split()[0]
            if column in {c["name"] for c in insp.get_columns(table)}:
                continue
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))
```

And the shared declaration, module level:

```python
# (table, column DDL) pairs added by Phase 1. Every entry must be nullable or
# carry a DEFAULT: existing rows are back-filled by the default, and already
# deployed workers never write these columns.
_PHASE1_COLUMNS = [
    ("boards", "description TEXT"),
    ("boards", "out_of_scope TEXT"),
    ("boards", "commit_requirements TEXT"),
    ("boards", "use_worktrees BOOLEAN NOT NULL DEFAULT FALSE"),
    ("tickets", "session_id VARCHAR(64)"),
    ("workers", "concurrency INTEGER NOT NULL DEFAULT 1"),
    ("workers", "running INTEGER NOT NULL DEFAULT 0"),
]
```

Note: SQLite spells false as `0`; `DEFAULT FALSE` is accepted by SQLite 3.23+ and by Postgres, so one string serves both.

- [ ] **Step 5: Update `schema.sql` to document the new shape**

Add the four `boards` columns, `tickets.session_id`, and the two `workers` columns to their `CREATE TABLE` statements, with the same comments as the models.

- [ ] **Step 6: Run the full suite**

Run: `/c/Users/ryan/Documents/Github/kanban-cloud/.venv/Scripts/python -m pytest tests/ -q`
Expected: PASS, 117 tests (114 + 3).

- [ ] **Step 7: Commit**

```bash
git add app/models.py app/db.py schema.sql tests/test_migrations.py
git commit -m "feat(schema): board project metadata, ticket session id, worker slots"
```

---

### Task 2: Board settings API

**Files:**
- Modify: `app/main.py` (`BoardPatch` body, `list_boards`, new `patch_board`)
- Test: `tests/test_board_settings.py` (create)

**Interfaces:**
- Consumes: Task 1's `Board` columns; existing `board_for_user(db, user, board_id)`.
- Produces: `GET /api/clusters/{id}/boards` items gain `description`, `out_of_scope`, `commit_requirements`, `use_worktrees`. `PATCH /api/boards/{board_id}` accepts any subset and returns the same shape. Helper `board_json(b) -> dict`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_board_settings.py`:

```python
"""Board-level project metadata: the context an agent needs to work a repo."""
from tests.conftest import make_ticket  # noqa: F401  (keeps import parity)


def test_list_boards_includes_project_metadata(client, user, cluster):
    boards = client.get(f"/api/clusters/{cluster['id']}/boards",
                        headers=user["headers"]).json()
    assert boards[0]["description"] is None
    assert boards[0]["out_of_scope"] is None
    assert boards[0]["commit_requirements"] is None
    assert boards[0]["use_worktrees"] is False


def test_patch_board_sets_metadata(client, user, cluster):
    r = client.patch(f"/api/boards/{cluster['board_id']}", headers=user["headers"],
                     json={"description": "The invoicing app.",
                           "commit_requirements": "All tests must pass.",
                           "use_worktrees": True})
    assert r.status_code == 200, r.text
    assert r.json()["description"] == "The invoicing app."
    assert r.json()["use_worktrees"] is True
    boards = client.get(f"/api/clusters/{cluster['id']}/boards",
                        headers=user["headers"]).json()
    assert boards[0]["commit_requirements"] == "All tests must pass."


def test_patch_board_is_partial(client, user, cluster):
    bid = cluster["board_id"]
    client.patch(f"/api/boards/{bid}", headers=user["headers"],
                 json={"description": "keep me", "out_of_scope": "not the CSS"})
    client.patch(f"/api/boards/{bid}", headers=user["headers"],
                 json={"use_worktrees": True})
    r = client.get(f"/api/clusters/{cluster['id']}/boards",
                   headers=user["headers"]).json()[0]
    assert r["description"] == "keep me"      # untouched by the second patch
    assert r["out_of_scope"] == "not the CSS"
    assert r["use_worktrees"] is True


def test_patch_board_in_another_cluster_is_forbidden(client, user, cluster):
    other = client.post("/api/register",
                        json={"email": "z@x.co", "password": "pass1234"}).json()
    headers = {"Authorization": f"Bearer {other['token']}"}
    r = client.patch(f"/api/boards/{cluster['board_id']}", headers=headers,
                     json={"description": "mine now"})
    assert r.status_code == 403


def test_patch_unknown_board_404s(client, user):
    r = client.patch("/api/boards/99999", headers=user["headers"],
                     json={"description": "x"})
    assert r.status_code == 404
```

- [ ] **Step 2: Run to verify it fails**

Run: `/c/Users/ryan/Documents/Github/kanban-cloud/.venv/Scripts/python -m pytest tests/test_board_settings.py -q`
Expected: FAIL — `KeyError: 'description'` on the first test, 405/404 on the patches.

- [ ] **Step 3: Add the request body**

In `app/main.py`, next to `BoardBody`:

```python
class BoardPatch(BaseModel):
    """Board project metadata. Every field optional: patches are partial."""
    description: str | None = None
    out_of_scope: str | None = None
    commit_requirements: str | None = None
    use_worktrees: bool | None = None
```

- [ ] **Step 4: Add `board_json` and wire it into `list_boards`**

Next to `ticket_json`:

```python
    def board_json(b: Board) -> dict:
        return {
            "id": b.id,
            "name": b.name,
            "description": b.description,
            "out_of_scope": b.out_of_scope,
            "commit_requirements": b.commit_requirements,
            "use_worktrees": bool(b.use_worktrees),
        }
```

Change `list_boards`'s return to `return [board_json(b) for b in boards]`, and
`create_board`'s to `return board_json(board)`.

- [ ] **Step 5: Add the PATCH endpoint**

Directly after `create_board`:

```python
    @app.patch("/api/boards/{board_id}")
    def patch_board(
        board_id: int,
        body: BoardPatch,
        user: User = Depends(current_user),
        db: Session = Depends(get_db),
    ):
        """Edit a board's project metadata (the context agents are given).

        Partial: only fields present in the request are written, so one panel
        saving does not blank another's value.
        """
        board = board_for_user(db, user, board_id)
        for field in ("description", "out_of_scope", "commit_requirements",
                      "use_worktrees"):
            value = getattr(body, field)
            if value is not None:
                setattr(board, field, value)
        db.commit()
        return board_json(board)
```

- [ ] **Step 6: Run the full suite**

Run: `/c/Users/ryan/Documents/Github/kanban-cloud/.venv/Scripts/python -m pytest tests/ -q`
Expected: PASS, 122 tests.

- [ ] **Step 7: Commit**

```bash
git add app/main.py tests/test_board_settings.py
git commit -m "feat(api): board project metadata read + partial PATCH"
```

---

### Task 3: Board settings UI

**Files:**
- Modify: `app/static/index.html`
- Test: `tests/test_frontend_markup.py`

**Interfaces:**
- Consumes: Task 2's `PATCH /api/boards/{id}` and the extended boards list.
- Produces: element ids `boardSettingsBtn`, `boardOverlay`, `bDescription`, `bOutOfScope`, `bCommitReq`, `bUseWorktrees`; functions `openBoardSettings()`, `saveBoardSettings()`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_frontend_markup.py` (follow the file's existing read-the-file-and-assert style):

```python
def test_board_settings_modal_markup_present():
    html = INDEX_HTML.read_text(encoding="utf-8")
    for token in ("boardSettingsBtn", "boardOverlay", "bDescription",
                  "bOutOfScope", "bCommitReq", "bUseWorktrees",
                  "openBoardSettings", "saveBoardSettings"):
        assert token in html, token


def test_board_settings_button_is_owner_only():
    """Spectators must not see the gear: it opens a mutating panel."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="boardSettingsBtn"' in html
    assert "owner-only" in html.split('id="boardSettingsBtn"')[0].rsplit("<", 1)[0] \
        or 'class="owner-only"' in html
```

If `INDEX_HTML` is not already defined in that file, add
`INDEX_HTML = Path(__file__).resolve().parents[1] / "app" / "static" / "index.html"`
with the matching `from pathlib import Path` import.

- [ ] **Step 2: Run to verify it fails**

Run: `/c/Users/ryan/Documents/Github/kanban-cloud/.venv/Scripts/python -m pytest tests/test_frontend_markup.py -q`
Expected: FAIL — `AssertionError: boardSettingsBtn`.

- [ ] **Step 3: Add the gear button**

In the header, immediately after the `boardSel` `<select>`:

```html
<button class="small owner-only" id="boardSettingsBtn" onclick="openBoardSettings()"
        title="Project settings for this board">&#9881;</button>
```

- [ ] **Step 4: Add the modal**

Alongside the existing `ticketOverlay` / `clusterOverlay` blocks:

```html
<div class="overlay" id="boardOverlay" onclick="if(event.target===this) this.classList.remove('show')">
  <div class="modal">
    <h2>Project settings</h2>
    <p class="muted" style="margin-top:0">Context every agent working this board is given.</p>
    <label>Description</label>
    <textarea id="bDescription" placeholder="One paragraph of background an agent needs before touching a ticket."></textarea>
    <label>Out of scope</label>
    <textarea id="bOutOfScope" placeholder="What agents must not touch."></textarea>
    <label>Commit requirements</label>
    <textarea id="bCommitReq" placeholder="e.g. All tests must pass before committing."></textarea>
    <label style="display:flex; align-items:center; gap:6px; margin-top:8px">
      <input type="checkbox" id="bUseWorktrees"> Use git worktrees for this project
    </label>
    <p class="muted" style="font-size:12px">Each worker PC sets its own folder for this
      board with <code>kanban-worker --set-path</code>; paths are never stored here.</p>
    <div class="row">
      <button onclick="saveBoardSettings()">Save</button>
      <button class="ghost" onclick="document.getElementById('boardOverlay').classList.remove('show')">Cancel</button>
    </div>
  </div>
</div>
```

- [ ] **Step 5: Add the two functions**

Near `loadBoard()`:

```javascript
function openBoardSettings() {
  const b = boards.find(x => x.id === Number(document.getElementById("boardSel").value));
  if (!b) return;
  document.getElementById("bDescription").value = b.description || "";
  document.getElementById("bOutOfScope").value = b.out_of_scope || "";
  document.getElementById("bCommitReq").value = b.commit_requirements || "";
  document.getElementById("bUseWorktrees").checked = !!b.use_worktrees;
  document.getElementById("boardOverlay").classList.add("show");
}

async function saveBoardSettings() {
  const id = Number(document.getElementById("boardSel").value);
  await api("PATCH", `./api/boards/${id}`, {
    description: document.getElementById("bDescription").value,
    out_of_scope: document.getElementById("bOutOfScope").value,
    commit_requirements: document.getElementById("bCommitReq").value,
    use_worktrees: document.getElementById("bUseWorktrees").checked,
  });
  document.getElementById("boardOverlay").classList.remove("show");
  await refreshClusterBits();
  toast("Project settings saved");
}
```

If the page does not already keep the boards list in a module-level `boards`
variable, assign one where the board `<select>` is populated (`boards = ...`)
so `openBoardSettings` can read the current row without a refetch.

- [ ] **Step 6: Run the full suite**

Run: `/c/Users/ryan/Documents/Github/kanban-cloud/.venv/Scripts/python -m pytest tests/ -q`
Expected: PASS, 124 tests.

- [ ] **Step 7: Commit**

```bash
git add app/static/index.html tests/test_frontend_markup.py
git commit -m "feat(ui): board project settings modal"
```

---

### Task 4: The prompt builder

**Files:**
- Create: `app/prompt.py`
- Test: `tests/test_prompt.py` (create)

**Interfaces:**
- Consumes: nothing (pure).
- Produces: `build_agent_prompt(ticket: dict, board: dict, directory: str) -> str`. `ticket` uses keys `id`, `title`, `body`; `board` uses `name`, `description`, `out_of_scope`, `commit_requirements`, `use_worktrees`. Also exports `slugify(text: str) -> str`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_prompt.py`:

```python
"""The agent prompt: pure, stdlib-only, and the only place project context and
the local path meet."""
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.prompt import build_agent_prompt, slugify  # noqa: E402

TICKET = {"id": 12, "title": "Fix the footer", "body": "It overlaps on mobile."}
BOARD = {"name": "site-page", "description": "The portfolio site.",
         "out_of_scope": "Do not touch billing.",
         "commit_requirements": "All tests must pass.", "use_worktrees": False}


def test_includes_ticket_and_directory():
    p = build_agent_prompt(TICKET, BOARD, r"C:\repos\site-page")
    assert "Fix the footer" in p
    assert "It overlaps on mobile." in p
    assert "#12" in p
    assert r"C:\repos\site-page" in p


def test_includes_project_context():
    p = build_agent_prompt(TICKET, BOARD, "/repo")
    assert "The portfolio site." in p
    assert "Do not touch billing." in p
    assert "All tests must pass." in p


def test_absent_fields_emit_no_empty_sections():
    bare = {"name": "b", "description": None, "out_of_scope": None,
            "commit_requirements": None, "use_worktrees": False}
    p = build_agent_prompt(TICKET, bare, "/repo")
    assert "Out of scope" not in p
    assert "Before you commit" not in p
    assert "Project" not in p or "site-page" not in p
    assert "Fix the footer" in p          # the ticket still survives
    assert "/repo" in p


def test_worktree_guidance_switches_on_the_flag():
    off = build_agent_prompt(TICKET, {**BOARD, "use_worktrees": False}, "/repo")
    on = build_agent_prompt(TICKET, {**BOARD, "use_worktrees": True}, "/repo")
    assert "git worktree add" in on
    assert "git worktree add" not in off
    assert "12-fix-the-footer" in on and "12-fix-the-footer" in off


def test_empty_body_is_tolerated():
    p = build_agent_prompt({"id": 1, "title": "T", "body": ""}, BOARD, "/repo")
    assert "(no details provided)" in p


def test_slugify():
    assert slugify("Fix the footer!") == "fix-the-footer"
    assert slugify("  Multiple   spaces  ") == "multiple-spaces"
    assert slugify("") == "ticket"


def test_module_is_stdlib_only():
    """It rides into the PyInstaller exe via worker.py; a SQLAlchemy import
    here would drag the whole server in."""
    src = (Path(__file__).resolve().parents[1] / "app" / "prompt.py").read_text()
    third_party = {"sqlalchemy", "fastapi", "pydantic", "psycopg", "app"}
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            names = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [(node.module or "").split(".")[0]]
        else:
            continue
        assert not (set(names) & third_party), names
```

- [ ] **Step 2: Run to verify it fails**

Run: `/c/Users/ryan/Documents/Github/kanban-cloud/.venv/Scripts/python -m pytest tests/test_prompt.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.prompt'`.

- [ ] **Step 3: Write `app/prompt.py`**

```python
"""The prompt handed to a Claude CLI agent working one cloud ticket.

Pure and stdlib-only by contract: `worker.py` imports this module, and
`worker.py` is frozen into a single-file exe by PyInstaller. Importing
SQLAlchemy or FastAPI here would pull the entire server into that exe.

This is the cloud counterpart of `orchestrator._build_agent_prompt` in the
local `.kanban` tool, trimmed to what Phase 1 supports: there are no profiles,
no prior-question context and no chat backlog yet.
"""
import re

MAX_SLUG_WORDS = 6


def slugify(text: str) -> str:
    """A branch-safe slug for a ticket title. Never returns an empty string."""
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return "-".join(words[:MAX_SLUG_WORDS]) or "ticket"


def build_agent_prompt(ticket: dict, board: dict, directory: str) -> str:
    """Compose the full instruction for one ticket.

    `directory` is this PC's checkout for the board — the agent's working
    directory. It is deliberately a parameter rather than a board field: the
    same board is worked by several machines with different layouts.

    Sections whose source field is empty are omitted entirely, so a board with
    no metadata yields a short prompt rather than a form full of blank headings.
    """
    branch = f"{ticket['id']}-{slugify(ticket.get('title', ''))}"
    parts = [
        "You are working a single kanban ticket. Keep changes minimal and "
        "focused on what the ticket asks for.",
        f"\nTicket #{ticket['id']}: {ticket.get('title', '')}",
        f"\nDetails:\n{(ticket.get('body') or '').strip() or '(no details provided)'}",
        f"\nWorking directory: {directory}\n"
        f"This is the checkout for the board \"{board.get('name', '')}\". Everything "
        "you do happens here; do not go looking for the project elsewhere.",
    ]
    if (board.get("description") or "").strip():
        parts.append(f"\nProject background:\n{board['description'].strip()}")
    if (board.get("out_of_scope") or "").strip():
        parts.append(f"\nOut of scope — do not touch:\n{board['out_of_scope'].strip()}")

    if board.get("use_worktrees"):
        parts.append(
            f"\nGit workflow: isolate your changes in a git worktree.\n"
            f"  git worktree add .claude/worktrees/ticket-{ticket['id']} -b {branch}\n"
            f"Verify .claude/worktrees/ is gitignored first. Commit there when done."
        )
    else:
        parts.append(
            f"\nGit workflow: create a branch named `{branch}` in the working "
            "directory and commit your changes to it. Do not commit to the default "
            "branch, and do not push — pushing is handled separately."
        )
    if (board.get("commit_requirements") or "").strip():
        parts.append(
            f"\nBefore you commit, this project requires:\n"
            f"{board['commit_requirements'].strip()}\n"
            "If you cannot satisfy it, stop and say so in your summary instead of "
            "committing anyway."
        )
    parts.append(
        "\nWhen you are done, reply with a concise summary of what you changed "
        "and why. That summary is posted back to the ticket as a comment."
    )
    return "\n".join(parts)
```

- [ ] **Step 4: Run the tests**

Run: `/c/Users/ryan/Documents/Github/kanban-cloud/.venv/Scripts/python -m pytest tests/test_prompt.py -q`
Expected: PASS, 7 tests.

- [ ] **Step 5: Run the full suite and commit**

Run: `/c/Users/ryan/Documents/Github/kanban-cloud/.venv/Scripts/python -m pytest tests/ -q`
Expected: PASS, 131 tests.

```bash
git add app/prompt.py tests/test_prompt.py
git commit -m "feat: pure agent-prompt builder with project context and git guidance"
```

---

### Task 5: Worker board paths — config and CLI

**Files:**
- Modify: `worker.py`
- Test: `tests/test_worker_paths.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: `board_paths(cfg) -> dict[str, str]`; `configured_board_ids(cfg) -> list[int]`; `resolve_board(conn, cluster_id, token) -> tuple[int, str]`; `parse_set_path(arg) -> tuple[str, str]`; `apply_set_path(conn, cfg, arg) -> dict`; `list_boards(conn, cluster_id) -> list[dict]`; `prompt_for_board_paths(conn, cfg) -> dict`. New CLI flags `--set-path` (repeatable), `--list-boards`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_worker_paths.py`:

```python
"""Per-PC board paths: the machine-level half of 'where does this agent run'."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import worker  # noqa: E402


class FakeCursor:
    """Minimal psycopg cursor over a canned board list."""

    def __init__(self, rows):
        self._rows = rows
        self._result = []

    def execute(self, sql, params=None):
        self._result = list(self._rows)

    def fetchall(self):
        return self._result

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return FakeCursor(self._rows)

    def transaction(self):
        class _T:
            def __enter__(self_): return self_
            def __exit__(self_, *a): return False
        return _T()


BOARDS = [(4, "site-page"), (7, "devtool-invoice")]


def test_parse_set_path_splits_on_first_equals():
    assert worker.parse_set_path("4=C:/repos/a=b") == ("4", "C:/repos/a=b")


def test_parse_set_path_rejects_missing_equals():
    with pytest.raises(ValueError):
        worker.parse_set_path("4")


def test_resolve_board_by_id():
    assert worker.resolve_board(FakeConn(BOARDS), 1, "4") == (4, "site-page")


def test_resolve_board_by_name_is_case_insensitive():
    assert worker.resolve_board(FakeConn(BOARDS), 1, "SITE-page") == (4, "site-page")


def test_resolve_board_rejects_unknown_name():
    with pytest.raises(ValueError, match="no board"):
        worker.resolve_board(FakeConn(BOARDS), 1, "nope")


def test_resolve_board_rejects_ambiguous_name():
    conn = FakeConn([(1, "dup"), (2, "DUP")])
    with pytest.raises(ValueError, match="matches 2 boards"):
        worker.resolve_board(conn, 1, "dup")


def test_apply_set_path_requires_an_existing_directory(tmp_path):
    with pytest.raises(ValueError, match="does not exist"):
        worker.apply_set_path(FakeConn(BOARDS), {"cluster_id": 1},
                              f"4={tmp_path / 'missing'}")


def test_apply_set_path_saves_by_board_id(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "CONFIG_PATH", tmp_path / "cfg.json")
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = {"cluster_id": 1, "worker_id": 2, "dsn": "x", "name": "pc"}
    out = worker.apply_set_path(FakeConn(BOARDS), cfg, f"site-page={repo}")
    assert out["boards"] == {"4": str(repo)}
    assert worker.load_config()["boards"] == {"4": str(repo)}


def test_configured_board_ids_are_ints():
    cfg = {"boards": {"4": "/a", "7": "/b"}}
    assert sorted(worker.configured_board_ids(cfg)) == [4, 7]


def test_configured_board_ids_empty_when_unset():
    assert worker.configured_board_ids({}) == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `/c/Users/ryan/Documents/Github/kanban-cloud/.venv/Scripts/python -m pytest tests/test_worker_paths.py -q`
Expected: FAIL — `AttributeError: module 'worker' has no attribute 'parse_set_path'`.

- [ ] **Step 3: Implement the helpers in `worker.py`**

Add after `save_config`:

```python
# ---------- per-PC board paths ----------

def board_paths(cfg: dict) -> dict:
    """{board_id_as_str: absolute path} for the boards this PC can work."""
    return cfg.get("boards") or {}


def configured_board_ids(cfg: dict) -> list:
    """Board ids this PC has a checkout for, as ints for the claim query."""
    out = []
    for key in board_paths(cfg):
        try:
            out.append(int(key))
        except (TypeError, ValueError):
            continue
    return out


def list_boards(conn, cluster_id: int) -> list:
    with conn.cursor() as cur:
        cur.execute("SELECT id, name FROM boards WHERE cluster_id=%s ORDER BY id",
                    (cluster_id,))
        return [{"id": r[0], "name": r[1]} for r in cur.fetchall()]


def resolve_board(conn, cluster_id: int, token: str):
    """Map a board id or name to (id, name). Names are case-insensitive.

    A name is rejected rather than guessed when it matches no board or more
    than one — silently picking one would send an agent into the wrong repo.
    """
    rows = [(b["id"], b["name"]) for b in list_boards(conn, cluster_id)]
    token = (token or "").strip()
    if token.isdigit():
        for bid, name in rows:
            if bid == int(token):
                return bid, name
        raise ValueError(f"no board with id {token} in this cluster")
    matches = [r for r in rows if r[1].lower() == token.lower()]
    if len(matches) > 1:
        raise ValueError(f"'{token}' matches {len(matches)} boards; use the id")
    if not matches:
        known = ", ".join(f"{b}:{n}" for b, n in rows) or "(none)"
        raise ValueError(f"no board named '{token}'. Known boards: {known}")
    return matches[0]


def parse_set_path(arg: str):
    """Split a --set-path value into (board token, path) on the FIRST '='.

    Splitting on the first one keeps Windows paths containing '=' intact.
    """
    if "=" not in (arg or ""):
        raise ValueError("--set-path needs <board-id-or-name>=<path>")
    token, path = arg.split("=", 1)
    if not token.strip() or not path.strip():
        raise ValueError("--set-path needs <board-id-or-name>=<path>")
    return token.strip(), path.strip()


def apply_set_path(conn, cfg: dict, arg: str) -> dict:
    """Validate and record one board->path mapping; saves and returns cfg."""
    token, path = parse_set_path(arg)
    board_id, name = resolve_board(conn, cfg["cluster_id"], token)
    resolved = Path(path).expanduser()
    if not resolved.is_dir():
        raise ValueError(f"{resolved} does not exist (or is not a directory)")
    cfg.setdefault("boards", {})[str(board_id)] = str(resolved)
    save_config(cfg)
    print(f"Board {board_id} ({name}) -> {resolved}")
    return cfg


def prompt_for_board_paths(conn, cfg: dict) -> dict:
    """Walk the cluster's boards after enrollment, asking for a path for each.

    Blank input skips a board (it simply will not be claimed here). Skipped
    entirely when stdin is not a terminal, so scripted runs never hang.
    """
    if not (sys.stdin is not None and sys.stdin.isatty()):
        return cfg
    boards = list_boards(conn, cfg["cluster_id"])
    if not boards:
        return cfg
    print("\nWhich folder on this PC holds each board's code?")
    print("Press Enter to skip a board (this PC then never claims its tickets).")
    for b in boards:
        try:
            answer = input(f"  {b['name']}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not answer:
            continue
        try:
            apply_set_path(conn, cfg, f"{b['id']}={answer}")
        except ValueError as e:
            print(f"    skipped: {e}")
    return cfg
```

Add `from pathlib import Path` if the import is not already present (it is — `app_dir` uses it).

- [ ] **Step 4: Add the CLI flags**

In `build_parser`:

```python
    parser.add_argument("--set-path", action="append", metavar="BOARD=PATH",
                        help="map a board (id or name) to its folder on this PC; "
                             "repeatable")
    parser.add_argument("--list-boards", action="store_true",
                        help="list this cluster's boards and their configured paths")
```

In `main()`, after the config is loaded and before the executor is picked:

```python
    if args.list_boards or args.set_path:
        conn = psycopg.connect(cfg["dsn"], connect_timeout=15)
        try:
            for arg in args.set_path or []:
                try:
                    cfg = apply_set_path(conn, cfg, arg)
                except ValueError as e:
                    print(f"--set-path {arg}: {e}")
                    return 2
            if args.list_boards:
                paths = board_paths(cfg)
                print(f"{'id':>4}  {'board':<24} path")
                for b in list_boards(conn, cfg["cluster_id"]):
                    print(f"{b['id']:>4}  {b['name']:<24} "
                          f"{paths.get(str(b['id']), '(not configured)')}")
        finally:
            conn.close()
        return 0
```

- [ ] **Step 5: Call the interactive walk after first-run enrollment**

In `main()`, immediately after `cfg = first_run_enroll(args)` succeeds:

```python
        try:
            conn = psycopg.connect(cfg["dsn"], connect_timeout=15)
            try:
                cfg = prompt_for_board_paths(conn, cfg)
            finally:
                conn.close()
        except psycopg.OperationalError as e:
            print(f"Enrolled, but could not list boards ({str(e)[:120]}).")
            print("Set folders later with --set-path <board>=<path>.")
```

- [ ] **Step 6: Run the full suite**

Run: `/c/Users/ryan/Documents/Github/kanban-cloud/.venv/Scripts/python -m pytest tests/ -q`
Expected: PASS, 141 tests.

- [ ] **Step 7: Commit**

```bash
git add worker.py tests/test_worker_paths.py
git commit -m "feat(worker): per-PC board paths with --set-path and --list-boards"
```

---

### Task 6: Only claim boards this PC can actually work

**Files:**
- Modify: `worker.py` (`CLAIM_SQL`, `claim_next`)
- Test: `tests/test_worker_paths.py` (extend)

**Interfaces:**
- Consumes: Task 5's `configured_board_ids`.
- Produces: `claim_next(conn, worker_id, cluster_id, board_ids)` — a fourth positional parameter. `board_ids=None` disables the filter (stub mode).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_worker_paths.py`:

```python
def test_claim_sql_filters_on_configured_boards():
    """The predicate must be in the SQL, not applied after the fact: filtering
    in Python would claim the row first and then abandon it."""
    assert "t.board_id = ANY(" in worker.CLAIM_SQL


def test_claim_next_passes_configured_boards(monkeypatch):
    captured = {}

    class Cur:
        def execute(self, sql, params=None):
            captured["sql"] = sql
            captured["params"] = params
        def fetchone(self):
            return None
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class Conn:
        def cursor(self): return Cur()
        def transaction(self):
            class _T:
                def __enter__(s): return s
                def __exit__(s, *a): return False
            return _T()

    worker.claim_next(Conn(), 3, 1, [4, 7])
    assert captured["params"]["boards"] == [4, 7]


def test_claim_next_with_none_boards_disables_the_filter():
    """--stub has no repo to work in, so it must still claim anything."""
    captured = {}

    class Cur:
        def execute(self, sql, params=None):
            captured.setdefault("params", params)
        def fetchone(self): return None
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class Conn:
        def cursor(self): return Cur()
        def transaction(self):
            class _T:
                def __enter__(s): return s
                def __exit__(s, *a): return False
            return _T()

    worker.claim_next(Conn(), 3, 1, None)
    assert captured["params"]["boards"] is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `/c/Users/ryan/Documents/Github/kanban-cloud/.venv/Scripts/python -m pytest tests/test_worker_paths.py -q`
Expected: FAIL — `assert 't.board_id = ANY(' in worker.CLAIM_SQL`.

- [ ] **Step 3: Add the predicate to `CLAIM_SQL`**

Change the `WHERE` clause inside the subquery:

```sql
  WHERE wq.status='queued' AND wq.cluster_id=%(cid)s
    AND (t.target_worker IS NULL OR t.target_worker = %(wid)s)
    AND (%(boards)s::int[] IS NULL OR t.board_id = ANY(%(boards)s::int[]))
```

The `IS NULL` arm is what lets `--stub` opt out with a single parameter instead
of a second query.

- [ ] **Step 4: Thread the parameter through `claim_next`**

```python
def claim_next(conn, worker_id: int, cluster_id: int, board_ids=None) -> dict | None:
    """Claim the oldest eligible queued item; returns a work payload or None.

    `board_ids` limits the claim to boards this PC has a checkout for. None
    means no limit — used by the stub executor, which needs no repo.
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(CLAIM_SQL, {"wid": worker_id, "cid": cluster_id,
                                "boards": board_ids})
```

The rest of the function is unchanged.

- [ ] **Step 5: Run the full suite**

Run: `/c/Users/ryan/Documents/Github/kanban-cloud/.venv/Scripts/python -m pytest tests/ -q`
Expected: PASS, 144 tests.

- [ ] **Step 6: Commit**

```bash
git add worker.py tests/test_worker_paths.py
git commit -m "feat(worker): only claim tickets for boards this PC has a path for"
```

---

### Task 7: The executor runs in the repo

**Files:**
- Modify: `worker.py` (`ClaudeExecutor.run`, `claim_next` payload, `build_parser`)
- Test: `tests/test_executor.py` (create)

**Interfaces:**
- Consumes: Task 4's `build_agent_prompt`, Task 5's `board_paths`, Task 1's `Ticket.session_id`.
- Produces: `ClaudeExecutor(allowed_tools: str)` with `run(ticket, api_key, board=None, directory=None, session_id=None)`. `DEFAULT_ALLOWED_TOOLS` constant. `claim_next` payload gains a `board` dict.

- [ ] **Step 1: Write the failing test**

Create `tests/test_executor.py`:

```python
"""The executor's contract with the Claude CLI: right folder, right permissions."""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import worker  # noqa: E402

TICKET = {"id": 3, "title": "Do it", "body": "Details.", "attempts": 1}
BOARD = {"name": "site-page", "description": "The site.", "out_of_scope": None,
         "commit_requirements": None, "use_worktrees": False}


def fake_run(captured):
    def _run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0, stdout="done", stderr="")
    return _run


def test_executor_runs_in_the_board_directory(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(subprocess, "run", fake_run(captured))
    ok, out = worker.ClaudeExecutor().run(
        TICKET, "sk-ant-x", board=BOARD, directory=str(tmp_path), session_id="sid-1")
    assert ok and out == "done"
    assert captured["kwargs"]["cwd"] == str(tmp_path)


def test_executor_passes_allowed_tools_and_session_id(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(subprocess, "run", fake_run(captured))
    worker.ClaudeExecutor().run(TICKET, "k", board=BOARD,
                                directory=str(tmp_path), session_id="sid-1")
    cmd = captured["cmd"]
    assert "--allowedTools" in cmd
    assert cmd[cmd.index("--allowedTools") + 1] == worker.DEFAULT_ALLOWED_TOOLS
    assert "--session-id" in cmd
    assert cmd[cmd.index("--session-id") + 1] == "sid-1"


def test_executor_prompt_carries_project_context(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(subprocess, "run", fake_run(captured))
    worker.ClaudeExecutor().run(TICKET, "k", board=BOARD,
                                directory=str(tmp_path), session_id="s")
    prompt = captured["cmd"][captured["cmd"].index("-p") + 1]
    assert "The site." in prompt
    assert str(tmp_path) in prompt


def test_executor_refuses_to_run_without_a_directory(monkeypatch):
    """Better to fail the attempt than run an agent in a random folder."""
    called = {}
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: called.setdefault("ran", True))
    ok, msg = worker.ClaudeExecutor().run(TICKET, "k", board=BOARD,
                                          directory=None, session_id="s")
    assert ok is False
    assert "--set-path" in msg
    assert "ran" not in called


def test_executor_fails_clearly_when_the_directory_is_gone(monkeypatch, tmp_path):
    called = {}
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: called.setdefault("ran", True))
    ok, msg = worker.ClaudeExecutor().run(
        TICKET, "k", board=BOARD, directory=str(tmp_path / "gone"), session_id="s")
    assert ok is False
    assert "no longer exists" in msg
    assert "ran" not in called


def test_custom_allowed_tools(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(subprocess, "run", fake_run(captured))
    worker.ClaudeExecutor(allowed_tools="Read,Grep").run(
        TICKET, "k", board=BOARD, directory=str(tmp_path), session_id="s")
    cmd = captured["cmd"]
    assert cmd[cmd.index("--allowedTools") + 1] == "Read,Grep"


def test_stub_executor_still_takes_the_new_kwargs():
    ok, out = worker.StubExecutor().run(TICKET, None, board=None,
                                        directory=None, session_id=None)
    assert ok and "StubExecutor" in out
```

- [ ] **Step 2: Run to verify it fails**

Run: `/c/Users/ryan/Documents/Github/kanban-cloud/.venv/Scripts/python -m pytest tests/test_executor.py -q`
Expected: FAIL — `TypeError: run() got an unexpected keyword argument 'board'`.

- [ ] **Step 3: Rewrite the executors**

Add the import and constant near the top of `worker.py`:

```python
from app.prompt import build_agent_prompt

# The local .kanban tool's `default` profile list. Without an explicit grant a
# headless `claude -p` cannot get permission to edit a file, so an agent with
# no tools looks like it ran and silently changed nothing.
DEFAULT_ALLOWED_TOOLS = "Read,Edit,Write,Bash,Grep,Glob"
```

Replace `StubExecutor.run` and `ClaudeExecutor` with:

```python
class StubExecutor:
    """Fake executor: waits a moment and produces a canned result."""

    name = "stub"

    def run(self, ticket, api_key, board=None, directory=None, session_id=None):
        print(f"  [stub] pretending to work on ticket #{ticket['id']}: {ticket['title']}")
        time.sleep(2)
        return True, (
            f"[StubExecutor] Completed ticket '{ticket['title']}' (attempt "
            f"{ticket.get('attempts', '?')}). This is a placeholder result — "
            "run the worker without --stub to execute via the Claude CLI."
        )


class ClaudeExecutor:
    """Real executor: runs the Claude CLI inside the board's checkout."""

    name = "claude"

    def __init__(self, allowed_tools: str = DEFAULT_ALLOWED_TOOLS):
        self.allowed_tools = allowed_tools

    def run(self, ticket, api_key, board=None, directory=None, session_id=None):
        if not api_key:
            return False, "No Claude API key configured for this cluster (set it in Settings)."
        if not directory:
            return False, (
                f"This PC has no folder configured for board "
                f"'{(board or {}).get('name', '?')}'. Set one with: "
                f"kanban-worker --set-path \"{(board or {}).get('name', '<board>')}"
                f"=<path to the repo>\""
            )
        if not Path(directory).is_dir():
            return False, (
                f"The configured folder for board '{(board or {}).get('name', '?')}' "
                f"no longer exists: {directory}. Fix it with --set-path."
            )
        prompt = build_agent_prompt(ticket, board or {}, directory)
        env = dict(os.environ)
        env["ANTHROPIC_API_KEY"] = api_key
        cmd = ["claude", "-p", prompt, "--allowedTools", self.allowed_tools]
        if session_id:
            cmd += ["--session-id", session_id]
        print(f"  [claude] running in {directory} for ticket #{ticket['id']}")
        try:
            proc = subprocess.run(
                cmd,
                cwd=directory,
                env=env,
                capture_output=True,
                text=True,
                timeout=1800,
                shell=(os.name == "nt"),  # find claude.cmd shim on Windows
            )
        except FileNotFoundError:
            return False, "`claude` CLI not found on this PC's PATH."
        except subprocess.TimeoutExpired:
            return False, "Claude CLI timed out after 30 minutes."
        output = (proc.stdout or "").strip() or (proc.stderr or "").strip()
        if proc.returncode != 0:
            return False, f"Claude CLI exited {proc.returncode}: {output[:2000]}"
        return True, output[:10000] or "(no output)"
```

- [ ] **Step 4: Return the board with the claim, and record the session id**

In `claim_next`, after the ticket UPDATE, fetch the board and mint a session id:

```python
        session_id = str(uuid.uuid4())
        cur.execute(
            f"UPDATE tickets SET session_id=%s WHERE id=%s", (session_id, ticket_id)
        )
        cur.execute(
            "SELECT name, description, out_of_scope, commit_requirements, "
            "use_worktrees FROM boards WHERE id=%s", (board_id,)
        )
        b = cur.fetchone()
```

and extend the returned dict with:

```python
            "session_id": session_id,
            "board": {"id": board_id, "name": b[0], "description": b[1],
                      "out_of_scope": b[2], "commit_requirements": b[3],
                      "use_worktrees": bool(b[4])} if b else None,
```

Add `import uuid` at the top.

- [ ] **Step 5: Add `--allowed-tools` and update `pick_executor`**

```python
    parser.add_argument("--allowed-tools", default=DEFAULT_ALLOWED_TOOLS,
                        help=f"comma-separated tools the agent may use "
                             f"(default: {DEFAULT_ALLOWED_TOOLS})")
```

```python
def pick_executor(args):
    if args.stub:
        return StubExecutor()
    return ClaudeExecutor(allowed_tools=args.allowed_tools)
```

- [ ] **Step 6: Update the call site in the main loop**

```python
                    ok, comment = executor.run(
                        ticket, work.get("claude_api_key"),
                        board=work.get("board"),
                        directory=board_paths(cfg).get(str(ticket["board_id"])),
                        session_id=work.get("session_id"))
```

- [ ] **Step 7: Run the full suite**

Run: `/c/Users/ryan/Documents/Github/kanban-cloud/.venv/Scripts/python -m pytest tests/ -q`
Expected: PASS, 151 tests.

- [ ] **Step 8: Commit**

```bash
git add worker.py tests/test_executor.py
git commit -m "feat(worker): run the agent in the board's repo with tools and a session id"
```

---

### Task 8: Worker concurrency slots

**Files:**
- Modify: `worker.py` (`main`, new `run_slot`, `heartbeat`)
- Test: `tests/test_concurrency.py` (create)

**Interfaces:**
- Consumes: Tasks 5-7.
- Produces: `run_slot(cfg, args, executor, stop_event, slot_no) -> None`; `set_slot_counts(conn, worker_id, concurrency, running)`; `resolve_concurrency(args, cfg) -> int`. New CLI flag `--concurrency`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_concurrency.py`:

```python
"""N slots per PC: the machine's own throttle on how much it runs at once."""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import worker  # noqa: E402


def test_resolve_concurrency_prefers_the_flag():
    args = worker.build_parser().parse_args(["--concurrency", "4"])
    assert worker.resolve_concurrency(args, {"concurrency": 2}) == 4


def test_resolve_concurrency_falls_back_to_config():
    args = worker.build_parser().parse_args([])
    assert worker.resolve_concurrency(args, {"concurrency": 3}) == 3


def test_resolve_concurrency_defaults_to_one():
    args = worker.build_parser().parse_args([])
    assert worker.resolve_concurrency(args, {}) == 1


def test_resolve_concurrency_floors_at_one():
    args = worker.build_parser().parse_args(["--concurrency", "0"])
    assert worker.resolve_concurrency(args, {}) == 1


def test_slots_run_concurrently_and_never_exceed_the_limit(monkeypatch, tmp_path):
    """Three slots, a slow executor: all three must be in flight at once, and
    a fourth ticket must wait for a slot to free up."""
    monkeypatch.setattr(worker, "CONFIG_PATH", tmp_path / "cfg.json")
    live = []
    peak = {"n": 0}
    lock = threading.Lock()
    claims = iter(range(6))

    def fake_connect(dsn, **kw):
        class C:
            closed = False
            def close(self): pass
        return C()

    def fake_claim(conn, wid, cid, boards):
        try:
            n = next(claims)
        except StopIteration:
            return None
        return {"assignment_id": n, "claude_api_key": "k", "session_id": "s",
                "board": {"id": 1, "name": "b"},
                "ticket": {"id": n, "board_id": 1, "title": "t", "body": "",
                           "status": "doing", "attempts": 1}}

    class SlowExecutor:
        name = "slow"
        def run(self, ticket, key, board=None, directory=None, session_id=None):
            with lock:
                live.append(ticket["id"])
                peak["n"] = max(peak["n"], len(live))
            time.sleep(0.2)
            with lock:
                live.remove(ticket["id"])
            return True, "ok"

    monkeypatch.setattr(worker.psycopg, "connect", fake_connect)
    monkeypatch.setattr(worker, "claim_next", fake_claim)
    monkeypatch.setattr(worker, "finish_work",
                        lambda *a, **k: "review")
    monkeypatch.setattr(worker, "set_slot_counts", lambda *a, **k: None)
    monkeypatch.setattr(worker, "heartbeat", lambda *a, **k: None)

    cfg = {"dsn": "x", "worker_id": 1, "cluster_id": 1, "name": "pc",
           "boards": {"1": str(tmp_path)}}
    args = worker.build_parser().parse_args(["--poll", "0.01"])
    stop = threading.Event()
    threads = [threading.Thread(target=worker.run_slot,
                                args=(cfg, args, SlowExecutor(), stop, i))
               for i in range(3)]
    for t in threads:
        t.start()
    time.sleep(0.5)
    stop.set()
    for t in threads:
        t.join(timeout=5)
    assert peak["n"] == 3, f"expected 3 concurrent, saw {peak['n']}"


def test_each_slot_opens_its_own_connection(monkeypatch, tmp_path):
    monkeypatch.setattr(worker, "CONFIG_PATH", tmp_path / "cfg.json")
    conns = []

    def fake_connect(dsn, **kw):
        class C:
            closed = False
            def close(self): pass
        c = C()
        conns.append(c)
        return c

    monkeypatch.setattr(worker.psycopg, "connect", fake_connect)
    monkeypatch.setattr(worker, "claim_next", lambda *a, **k: None)
    monkeypatch.setattr(worker, "set_slot_counts", lambda *a, **k: None)
    monkeypatch.setattr(worker, "heartbeat", lambda *a, **k: None)

    cfg = {"dsn": "x", "worker_id": 1, "cluster_id": 1, "name": "pc"}
    args = worker.build_parser().parse_args(["--poll", "0.01", "--once"])
    stop = threading.Event()
    threads = [threading.Thread(target=worker.run_slot,
                                args=(cfg, args, worker.StubExecutor(), stop, i))
               for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert len({id(c) for c in conns}) == 3
```

- [ ] **Step 2: Run to verify it fails**

Run: `/c/Users/ryan/Documents/Github/kanban-cloud/.venv/Scripts/python -m pytest tests/test_concurrency.py -q`
Expected: FAIL — `AttributeError: module 'worker' has no attribute 'resolve_concurrency'`.

- [ ] **Step 3: Add the slot-count writer and concurrency resolver**

```python
_SLOT_LOCK = threading.Lock()
_RUNNING = {"n": 0}


def set_slot_counts(conn, worker_id: int, concurrency: int, running: int) -> None:
    """Publish this PC's limit and current load. Advisory only — the server
    reads these to draw the Workers panel; nothing schedules on them."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            f"UPDATE workers SET concurrency=%s, running=%s, "
            f"status=%s, last_seen={UTC_NOW} WHERE id=%s",
            (concurrency, running, "working" if running else "idle", worker_id),
        )


def resolve_concurrency(args, cfg: dict) -> int:
    """Flag beats saved config beats 1. Never below 1."""
    value = args.concurrency if args.concurrency is not None else cfg.get("concurrency")
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 1
```

Add `import threading` at the top.

- [ ] **Step 4: Extract the loop body into `run_slot`**

```python
def run_slot(cfg, args, executor, stop_event, slot_no: int) -> None:
    """One independent claim->run->finish loop with its own DB connection.

    Slots share nothing but the config dict (read-only) and the stop event, so
    a slot stuck in a 30-minute agent run never blocks its neighbors.
    """
    boards = None if isinstance(executor, StubExecutor) else configured_board_ids(cfg)
    paths = board_paths(cfg)
    conn = None
    while not stop_event.is_set():
        try:
            if conn is None or conn.closed:
                conn = psycopg.connect(cfg["dsn"], connect_timeout=15)
            work = claim_next(conn, cfg["worker_id"], cfg["cluster_id"], boards)
            if work:
                ticket = work["ticket"]
                with _SLOT_LOCK:
                    _RUNNING["n"] += 1
                print(f"[slot {slot_no}] claimed #{ticket['id']} '{ticket['title']}'")
                try:
                    ok, comment = executor.run(
                        ticket, work.get("claude_api_key"),
                        board=work.get("board"),
                        directory=paths.get(str(ticket["board_id"])),
                        session_id=work.get("session_id"))
                except Exception as exc:
                    ok, comment = False, f"Executor error: {exc!r}"
                finally:
                    with _SLOT_LOCK:
                        _RUNNING["n"] -= 1
                status = finish_work(conn, cfg["worker_id"], cfg["name"],
                                     work["assignment_id"], ticket["id"], ok, comment)
                print(f"[slot {slot_no}] #{ticket['id']} -> {status}")
        except psycopg.OperationalError as e:
            msg = str(e)
            print(f"[slot {slot_no}] database unreachable ({msg[:150]}); retrying...")
            if "password authentication failed" in msg or "does not exist" in msg:
                print("Credentials rejected — this PC may be revoked. Re-enroll.")
                stop_event.set()
                return
            conn = None
        except Exception as exc:  # a slot must never die silently
            print(f"[slot {slot_no}] unexpected error: {exc!r}")
        if args.once:
            break
        stop_event.wait(args.poll)
    if conn is not None and not conn.closed:
        conn.close()
```

- [ ] **Step 5: Rewrite `main`'s loop to start slots plus a heartbeat**

Replace the existing `while True:` block with:

```python
    concurrency = resolve_concurrency(args, cfg)
    if concurrency != cfg.get("concurrency"):
        cfg["concurrency"] = concurrency
        save_config(cfg)

    if not isinstance(executor, StubExecutor) and not configured_board_ids(cfg):
        print("WARNING: no board folders configured on this PC, so nothing will "
              "be claimed.\n         Fix with: --list-boards, then "
              "--set-path <board>=<path>")

    print(f"Worker '{cfg['name']}' polling Postgres every {args.poll}s "
          f"({concurrency} slot{'s' if concurrency > 1 else ''}, "
          f"executor: {executor.name}). Ctrl+C to stop.")

    stop_event = threading.Event()
    slots = [threading.Thread(target=run_slot, name=f"slot-{i}",
                              args=(cfg, args, executor, stop_event, i),
                              daemon=True)
             for i in range(concurrency)]
    for t in slots:
        t.start()

    # The heartbeat lives on the main thread so a PC with every slot busy in a
    # 30-minute agent run still reports online — it used to ride on the claim
    # query, which is exactly what a busy worker stops issuing.
    hb_conn = None
    try:
        while any(t.is_alive() for t in slots):
            try:
                if hb_conn is None or hb_conn.closed:
                    hb_conn = psycopg.connect(cfg["dsn"], connect_timeout=15)
                with _SLOT_LOCK:
                    running = _RUNNING["n"]
                set_slot_counts(hb_conn, cfg["worker_id"], concurrency, running)
            except psycopg.OperationalError:
                hb_conn = None
            except Exception:
                pass
            if args.once:
                break
            time.sleep(args.poll)
    except KeyboardInterrupt:
        print("\nStopping worker; slots will finish the ticket in hand.")
        stop_event.set()
    stop_event.set()
    for t in slots:
        t.join(timeout=None if not args.once else 30)
    if hb_conn is not None and not hb_conn.closed:
        hb_conn.close()
    return 0
```

- [ ] **Step 6: Add the flag**

```python
    parser.add_argument("--concurrency", type=int, default=None,
                        help="tickets this PC runs at once (default 1; saved to "
                             "the config so it sticks)")
```

- [ ] **Step 7: Run the full suite**

Run: `/c/Users/ryan/Documents/Github/kanban-cloud/.venv/Scripts/python -m pytest tests/ -q`
Expected: PASS, 157 tests.

- [ ] **Step 8: Commit**

```bash
git add worker.py tests/test_concurrency.py
git commit -m "feat(worker): N concurrent slots with a main-thread heartbeat"
```

---

### Task 9: Surface slots in the Workers panel, and document it all

**Files:**
- Modify: `app/main.py` (`list_workers`)
- Modify: `app/static/index.html` (`loadWorkers`)
- Modify: `README.md`, `STATUS.md`
- Test: `tests/test_board_settings.py` (extend), `tests/test_frontend_markup.py`

**Interfaces:**
- Consumes: Task 1's `Worker.concurrency`/`running`, Task 8's writer.
- Produces: workers API items gain `concurrency` and `running`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_board_settings.py`:

```python
def test_workers_api_reports_slot_counts(client, user, cluster):
    from sqlalchemy import text
    engine = client.app.state.engine
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO workers (cluster_id, name, revoked, status, concurrency,"
            " running, last_seen, created_at) VALUES"
            " (:c,'pc',0,'working',3,2,'2030-01-01','2030-01-01')"
        ), {"c": cluster["id"]})
    w = client.get(f"/api/clusters/{cluster['id']}/workers",
                   headers=user["headers"]).json()[0]
    assert w["concurrency"] == 3
    assert w["running"] == 2
```

Add to `tests/test_frontend_markup.py`:

```python
def test_workers_panel_renders_slot_counts():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "running" in html and "concurrency" in html
```

- [ ] **Step 2: Run to verify they fail**

Run: `/c/Users/ryan/Documents/Github/kanban-cloud/.venv/Scripts/python -m pytest tests/test_board_settings.py tests/test_frontend_markup.py -q`
Expected: FAIL — `KeyError: 'concurrency'`.

- [ ] **Step 3: Add the fields to `list_workers`**

In `app/main.py::list_workers`, add to each dict:

```python
                "concurrency": w.concurrency,
                "running": w.running,
```

- [ ] **Step 4: Render them**

In `loadWorkers()` in `index.html`, where each worker row is built, replace the
bare status text with:

```javascript
const load = `${w.running || 0}/${w.concurrency || 1} running`;
```

and include `load` in the row's markup next to the online dot.

- [ ] **Step 5: Document**

In `README.md`, add a "Setting up a worker PC" subsection covering
`--list-boards`, `--set-path`, `--concurrency`, and the fact that a board with
no configured path is never claimed. In the board/settings section, document the
four project-metadata fields and that they are injected into every agent prompt.

In `STATUS.md`, add a dated entry at the top summarizing the change, the new
test count, and the two carried-forward caveats: no auto-push (work stays
committed-but-unpushed on the worker PC), and cluster-wide caps/dependencies
still absent (Phase 2).

- [ ] **Step 6: Run the full suite**

Run: `/c/Users/ryan/Documents/Github/kanban-cloud/.venv/Scripts/python -m pytest tests/ -q`
Expected: PASS, 159 tests.

- [ ] **Step 7: Commit**

```bash
git add app/main.py app/static/index.html README.md STATUS.md tests/
git commit -m "feat(ui): show worker slot load; document board settings and worker setup"
```

---

## Self-review

**Spec coverage.** §1 board metadata → Tasks 1-3. §2 worker board paths →
Tasks 5-6. §3 prompt builder → Task 4. §4 executor (cwd, allowedTools,
session-id) → Tasks 1 and 7. §5 concurrency → Tasks 1, 8, 9. Testing section →
distributed across every task, plus the stdlib-only packaging guard in Task 4.
Error-handling cases: no path (Task 7), missing directory (Task 7), slot raises
(Task 8's per-slot `except`), slot hangs (existing 30-min timeout, slots
independent), DB drop (Task 8 per-slot reconnect), revoked role (Task 8 sets the
stop event).

**Type consistency.** `build_agent_prompt(ticket, board, directory)` is defined
in Task 4 and called with exactly those three positionals in Task 7.
`claim_next(conn, worker_id, cluster_id, board_ids)` gains its fourth parameter
in Task 6 and is called with four arguments in Task 8's `run_slot`.
`configured_board_ids` / `board_paths` are defined in Task 5 and used in Tasks 6
and 8. `set_slot_counts(conn, worker_id, concurrency, running)` is defined and
called with four arguments in Task 8.

**Known gap accepted deliberately:** the `--once` flag now means "each slot polls
once" rather than "the worker polls once". Task 8's test asserts the new
behavior; `tests/test_worker_exe.py` does not exercise `--once`, so nothing
breaks, and the flag is documented as a testing aid only.
