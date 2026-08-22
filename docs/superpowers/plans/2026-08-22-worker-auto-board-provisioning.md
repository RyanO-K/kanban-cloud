# Worker Auto Board Provisioning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A worker PC with no `--set-path` entry for a board can still claim and run that board's tickets, by auto-cloning the board's `repo_url` into `%LOCALAPPDATA%\kanban-worker\boards\<board_id>` and refreshing it to the default branch before every ticket run.

**Architecture:** Add an optional `repo_url` field to `boards` (server + UI, mirroring the existing `description`/`out_of_scope`/`use_worktrees` fields exactly). On the worker side, add a `resolve_directory(board, cfg)` function that tries an explicit `--set-path` entry first, then falls back to cloning/refreshing `repo_url` under a fixed AppData path; wire it into `run_slot` in place of today's plain path lookup, guarded so `StubExecutor` (which needs no repo) skips it entirely.

**Tech Stack:** Python (FastAPI + SQLAlchemy server, stdlib-only `worker.py`), vanilla JS/HTML frontend, `git` CLI shelled out via `subprocess`, pytest.

**Spec:** `docs/superpowers/specs/2026-08-22-worker-auto-board-provisioning-design.md`

## Global Constraints

- Never clone, fetch, reset, or clean a directory the operator configured via `--set-path` — only the auto-managed AppData path is ever touched this way.
- Clone auth is ambient (this PC's existing git credentials) — no secrets stored or passed through the cluster.
- The auto-clone directory is named by plain numeric board id (`<board_id>`), never a slug — nothing persists a resolved path, so it must be reconstructible identically after a board rename.
- A `repo_url` that doesn't match an existing clone's `origin` is a hard error, never an automatic re-clone or re-point.
- `worker.py` stays stdlib + `psycopg` only (it is frozen into a single-file exe by PyInstaller) — no new third-party imports.

---

### Task 1: `boards.repo_url` data column

**Files:**
- Modify: `app/models.py:87-105` (`Board` class + docstring)
- Modify: `app/db.py:20-28` (`_PHASE1_COLUMNS`)
- Modify: `schema.sql:39-48` (`boards` table + comment)
- Test: `tests/test_migrations.py`

**Interfaces:**
- Produces: `Board.repo_url: str | None` — a nullable column later tasks read via `board_json`, `claim_next`, and the frontend.

- [ ] **Step 1: Write the failing test**

Edit `tests/test_migrations.py`'s `test_models_have_phase1_columns` (around line 89-97) to also assert the new column:

```python
def test_models_have_phase1_columns():
    from app.models import Board, Ticket
    board_cols = {c.name for c in Board.__table__.columns}
    assert {"description", "out_of_scope", "commit_requirements",
            "use_worktrees", "repo_url"} <= board_cols
    assert "directory" not in board_cols  # per-PC, never a server column
    assert "session_id" in {c.name for c in Ticket.__table__.columns}
    assert {"concurrency", "running"} <= {c.name for c in Worker.__table__.columns}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_migrations.py::test_models_have_phase1_columns -v`
Expected: FAIL — `repo_url` missing from `board_cols`.

- [ ] **Step 3: Write minimal implementation**

In `app/models.py`, update the `Board` class (replace lines 87-105):

```python
class Board(Base):
    """A project. The metadata below is the context every agent working one of
    this board's tickets is given (see app/prompt.build_agent_prompt).

    `repo_url` is the shared clone source for every worker on this board —
    where its code lives on any given PC is a different thing: that folder is
    per-PC (the same board is worked by several machines with different
    layouts) and is either set by hand (`--set-path`) or derived from
    `repo_url` under the worker's own AppData folder. Neither the folder nor
    that derivation lives in this table.
    """
    __tablename__ = "boards"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cluster_id: Mapped[int] = mapped_column(ForeignKey("clusters.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    out_of_scope: Mapped[str | None] = mapped_column(Text, nullable=True)
    commit_requirements: Mapped[str | None] = mapped_column(Text, nullable=True)
    use_worktrees: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="0"
    )
    repo_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
```

In `app/db.py`, append to `_PHASE1_COLUMNS` (line 20-28):

```python
_PHASE1_COLUMNS = [
    ("boards", "description TEXT"),
    ("boards", "out_of_scope TEXT"),
    ("boards", "commit_requirements TEXT"),
    ("boards", "use_worktrees BOOLEAN NOT NULL DEFAULT FALSE"),
    ("boards", "repo_url TEXT"),
    ("tickets", "session_id VARCHAR(64)"),
    ("workers", "concurrency INTEGER NOT NULL DEFAULT 1"),
    ("workers", "running INTEGER NOT NULL DEFAULT 0"),
]
```

In `schema.sql`, update the `boards` table comment and add the column (replace lines 35-48):

```sql
-- description/out_of_scope/commit_requirements/use_worktrees are the project
-- context injected into every agent prompt built for this board. repo_url is
-- the git clone URL a worker with no --set-path entry auto-clones under its
-- own AppData folder. The folder the code actually lives in on a given PC is
-- deliberately NOT here: it is per-PC and lives in each worker's own
-- .worker_config.json (or is derived from repo_url).
CREATE TABLE IF NOT EXISTS boards (
    id                  SERIAL PRIMARY KEY,
    cluster_id          INTEGER NOT NULL REFERENCES clusters(id),
    name                VARCHAR(255) NOT NULL,
    description         TEXT,
    out_of_scope        TEXT,
    commit_requirements TEXT,
    use_worktrees       BOOLEAN NOT NULL DEFAULT FALSE,
    repo_url            TEXT,
    created_at          TIMESTAMP NOT NULL DEFAULT now()
);
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_migrations.py -v`
Expected: PASS — including `test_migration_adds_phase1_columns_to_an_existing_db` and `test_phase1_boolean_defaults_are_postgres_legal`, which iterate `_PHASE1_COLUMNS` generically and now cover `repo_url` for free.

- [ ] **Step 5: Commit**

```bash
git add app/models.py app/db.py schema.sql tests/test_migrations.py
git commit -m "feat(db): add boards.repo_url for worker auto-clone"
```

---

### Task 2: API wiring for `repo_url`

**Files:**
- Modify: `app/main.py:86-96` (`BoardPatch`), `app/main.py:299-307` (`board_json`), `app/main.py:522-541` (`patch_board`)
- Test: `tests/test_board_settings.py`

**Interfaces:**
- Consumes: `Board.repo_url` from Task 1.
- Produces: `board_json(board)["repo_url"]`; `PATCH /api/boards/{id}` accepts `{"repo_url": "..."}`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_board_settings.py`:

```python
def test_list_boards_includes_repo_url(client, user, cluster):
    boards = client.get(f"/api/clusters/{cluster['id']}/boards",
                        headers=user["headers"]).json()
    assert boards[0]["repo_url"] is None


def test_patch_board_sets_repo_url(client, user, cluster):
    r = client.patch(f"/api/boards/{cluster['board_id']}", headers=user["headers"],
                     json={"repo_url": "https://github.com/org/repo.git"})
    assert r.status_code == 200, r.text
    assert r.json()["repo_url"] == "https://github.com/org/repo.git"


def test_patch_board_repo_url_is_partial_like_the_other_fields(client, user, cluster):
    bid = cluster["board_id"]
    client.patch(f"/api/boards/{bid}", headers=user["headers"],
                 json={"repo_url": "https://github.com/org/repo.git"})
    client.patch(f"/api/boards/{bid}", headers=user["headers"],
                 json={"use_worktrees": True})
    r = client.get(f"/api/clusters/{cluster['id']}/boards",
                   headers=user["headers"]).json()[0]
    assert r["repo_url"] == "https://github.com/org/repo.git"
    assert r["use_worktrees"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_board_settings.py -v -k repo_url`
Expected: FAIL — `KeyError: 'repo_url'` (missing from `board_json`'s dict, and `BoardPatch` rejects/ignores the field).

- [ ] **Step 3: Write minimal implementation**

In `app/main.py`, update `BoardPatch` (line 90-96):

```python
class BoardPatch(BaseModel):
    """A board's project metadata. Every field is optional so patches are
    partial — one panel saving must not blank a field it did not send."""
    description: str | None = None
    out_of_scope: str | None = None
    commit_requirements: str | None = None
    use_worktrees: bool | None = None
    repo_url: str | None = None
```

Update `board_json` (line 299-307):

```python
def board_json(b: Board) -> dict:
    return {
        "id": b.id,
        "name": b.name,
        "description": b.description,
        "out_of_scope": b.out_of_scope,
        "commit_requirements": b.commit_requirements,
        "use_worktrees": bool(b.use_worktrees),
        "repo_url": b.repo_url,
    }
```

Update the field tuple in `patch_board` (line 535-536):

```python
        for field in ("description", "out_of_scope", "commit_requirements",
                      "use_worktrees", "repo_url"):
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_board_settings.py -v`
Expected: PASS (all board-settings tests, including the new ones).

- [ ] **Step 5: Commit**

```bash
git add app/main.py tests/test_board_settings.py
git commit -m "feat(api): expose boards.repo_url through board_json and PATCH"
```

---

### Task 3: Settings-modal UI field

**Files:**
- Modify: `app/static/index.html` (board settings modal markup ~line 182-203, `openBoardSettings`/`saveBoardSettings` JS ~line 356-378)
- Test: `tests/test_frontend_markup.py`

**Interfaces:**
- Consumes: `board.repo_url` from Task 2's API.
- Produces: an editable `repo_url` round-trip through the Settings modal — no new interface for later tasks (worker-side code reads the DB directly, not the UI).

- [ ] **Step 1: Write the failing test**

Extend `tests/test_frontend_markup.py`'s `test_board_settings_modal_markup_present` (line 72-76):

```python
def test_board_settings_modal_markup_present(markup):
    for token in ("boardSettingsBtn", "boardOverlay", "bDescription",
                  "bOutOfScope", "bCommitReq", "bUseWorktrees", "bRepoUrl",
                  "openBoardSettings", "saveBoardSettings"):
        assert token in markup, token


def test_board_settings_wires_repo_url_on_open_and_save(markup):
    assert 'document.getElementById("bRepoUrl").value = b.repo_url || ""' in markup
    assert 'repo_url: document.getElementById("bRepoUrl").value' in markup
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_frontend_markup.py -v -k board_settings`
Expected: FAIL — `bRepoUrl` not present anywhere in the markup.

- [ ] **Step 3: Write minimal implementation**

In `app/static/index.html`, replace the board settings modal block (line 182-203):

```html
<!-- board project settings modal -->
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
    <label>Repo URL</label>
    <input id="bRepoUrl" placeholder="https://github.com/org/repo.git">
    <p class="muted" style="font-size:12px">A worker PC with no
      <code>--set-path</code> entry for this board clones this URL automatically
      into its own AppData folder and keeps it up to date. Setting
      <code>kanban-worker --set-path</code> on a PC always overrides this.</p>
    <div class="row">
      <button onclick="saveBoardSettings()">Save</button>
      <button class="ghost" onclick="document.getElementById('boardOverlay').classList.remove('show')">Cancel</button>
    </div>
  </div>
</div>
```

Update `openBoardSettings()` and `saveBoardSettings()` (line 356-378):

```javascript
function openBoardSettings() {
  const b = boards.find(x => x.id === Number(document.getElementById("boardSel").value));
  if (!b) return;
  document.getElementById("bDescription").value = b.description || "";
  document.getElementById("bOutOfScope").value = b.out_of_scope || "";
  document.getElementById("bCommitReq").value = b.commit_requirements || "";
  document.getElementById("bUseWorktrees").checked = !!b.use_worktrees;
  document.getElementById("bRepoUrl").value = b.repo_url || "";
  document.getElementById("boardOverlay").classList.add("show");
}

async function saveBoardSettings() {
  const id = Number(document.getElementById("boardSel").value);
  if (!id) return;
  await api("PATCH", `./api/boards/${id}`, {
    description: document.getElementById("bDescription").value,
    out_of_scope: document.getElementById("bOutOfScope").value,
    commit_requirements: document.getElementById("bCommitReq").value,
    use_worktrees: document.getElementById("bUseWorktrees").checked,
    repo_url: document.getElementById("bRepoUrl").value,
  });
  document.getElementById("boardOverlay").classList.remove("show");
  await refreshClusterBits();
  toast("Project settings saved");
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_frontend_markup.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/static/index.html tests/test_frontend_markup.py
git commit -m "feat(ui): add Repo URL field to board project settings"
```

---

### Task 4: `claim_next` returns `repo_url`

**Files:**
- Modify: `worker.py:399-417` (`claim_next`)
- Test: `tests/test_worker_paths.py`

**Interfaces:**
- Consumes: `board.repo_url` column from Task 1.
- Produces: `claim_next(...)["board"]["repo_url"]`, which Task 6's `resolve_directory` call reads via `work.get("board")`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_worker_paths.py`. This needs a `FakeCursor`/`FakeConn` that can return a fixed row from `fetchone()` for the board SELECT — extend the existing fakes rather than duplicating them:

```python
class FakeCursorWithBoardRow(FakeCursor):
    """Like FakeCursor, but claim_next's several fetchone() calls need to
    return a fixed sequence: the claim row, the ticket-flip row, then the
    board row."""

    def __init__(self, fetchone_sequence):
        super().__init__(rows=())
        self._sequence = list(fetchone_sequence)

    def fetchone(self):
        return self._sequence.pop(0) if self._sequence else None


class FakeConnWithBoardRow(FakeConn):
    def __init__(self, fetchone_sequence):
        super().__init__()
        self._sequence = fetchone_sequence

    def cursor(self):
        cur = FakeCursorWithBoardRow(self._sequence)
        self.cursors.append(cur)
        return cur


def test_claim_next_returns_repo_url_on_the_board_dict():
    conn = FakeConnWithBoardRow([
        (5, 9),                                             # claim: item_id, ticket_id
        (2, "Fix the thing", "Details.", 1),                 # ticket flip: board_id, title, body, attempts
        ("site-page", "Desc", None, None, False, "https://github.com/org/repo.git"),  # board row
    ])
    work = worker.claim_next(conn, worker_id=1, cluster_id=1, board_ids=None)
    assert work["board"]["repo_url"] == "https://github.com/org/repo.git"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_worker_paths.py -v -k repo_url`
Expected: FAIL — `KeyError: 'repo_url'` (the board dict doesn't have that key yet), or the fake board row is one column short of what the current SELECT expects (5 columns vs. the test's 6 — that mismatch is the point: the test is written for the post-change SELECT).

- [ ] **Step 3: Write minimal implementation**

In `worker.py`, update `claim_next` (line 399-417):

```python
        cur.execute(
            "SELECT name, description, out_of_scope, commit_requirements, "
            "use_worktrees, repo_url FROM boards WHERE id=%s",
            (board_id,),
        )
        b = cur.fetchone()
        return {
            "assignment_id": item_id,
            "session_id": session_id,
            "board": {
                "id": board_id, "name": b[0], "description": b[1],
                "out_of_scope": b[2], "commit_requirements": b[3],
                "use_worktrees": bool(b[4]), "repo_url": b[5],
            } if b else None,
            "ticket": {
                "id": ticket_id, "board_id": board_id, "title": title,
                "body": body, "status": "doing", "attempts": attempts,
            },
        }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_worker_paths.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add worker.py tests/test_worker_paths.py
git commit -m "feat(worker): carry repo_url through claim_next's board dict"
```

---

### Task 5: `resolve_directory` — the auto-clone/refresh core

**Files:**
- Modify: `worker.py` (new section after `prompt_for_board_paths`, before `enroll` — i.e. after line 289 in the current file)
- Test: `tests/test_worker_auto_clone.py` (new)

**Interfaces:**
- Consumes: `board_paths(cfg)` (existing), `app_dir()` (existing).
- Produces:
  - `app_data_boards_dir() -> Path`
  - `resolve_directory(board: dict, cfg: dict) -> tuple[str | None, str | None]` — `(directory, error)`; exactly one is `None`. Task 6 calls this directly.

- [ ] **Step 1: Write the failing test**

Create `tests/test_worker_auto_clone.py`:

```python
"""Auto-clone: a board with a repo_url needs no manual --set-path.

Every git interaction goes through subprocess.run, so it's faked the same
way tests/test_executor.py fakes the Claude CLI call — no real git process
runs in this suite.
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import worker  # noqa: E402

BOARD = {"id": 9, "name": "site-page", "repo_url": "https://github.com/org/repo.git"}


class FakeGit:
    """Records every git invocation; returns canned output keyed by subcommand.

    `outputs` maps a subcommand (args[1], e.g. "clone", "fetch", "remote") to
    (returncode, stdout, stderr). Missing entries default to success/"".
    """

    def __init__(self, outputs=None):
        self.outputs = outputs or {}
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        assert cmd[0] == "git"
        returncode, stdout, stderr = self.outputs.get(cmd[1], (0, "", ""))
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)


def test_explicit_set_path_wins_and_touches_no_git(monkeypatch, tmp_path):
    fake = FakeGit()
    monkeypatch.setattr(subprocess, "run", fake)
    cfg = {"boards": {"9": str(tmp_path / "manual-checkout")}}
    directory, error = worker.resolve_directory(BOARD, cfg)
    assert directory == str(tmp_path / "manual-checkout")
    assert error is None
    assert fake.calls == []


def test_clones_when_the_appdata_directory_is_missing(monkeypatch, tmp_path):
    boards_root = tmp_path / "appdata" / "boards"
    monkeypatch.setattr(worker, "app_data_boards_dir", lambda: boards_root)
    fake = FakeGit(outputs={"rev-parse": (0, "origin/main\n", "")})
    monkeypatch.setattr(subprocess, "run", fake)
    directory, error = worker.resolve_directory(BOARD, {})
    assert error is None
    assert directory == str(boards_root / "9")
    subcommands = [c[0][1] for c in fake.calls]
    assert subcommands == ["clone", "fetch", "rev-parse", "checkout", "reset", "clean"]
    clone_cmd = fake.calls[0][0]
    assert clone_cmd[2] == BOARD["repo_url"]
    assert clone_cmd[3] == str(boards_root / "9")


def test_refreshes_an_existing_clone_instead_of_recloning(monkeypatch, tmp_path):
    boards_root = tmp_path / "appdata" / "boards"
    existing = boards_root / "9"
    existing.mkdir(parents=True)
    monkeypatch.setattr(worker, "app_data_boards_dir", lambda: boards_root)
    fake = FakeGit(outputs={
        "remote": (0, BOARD["repo_url"] + "\n", ""),
        "rev-parse": (0, "origin/main\n", ""),
    })
    monkeypatch.setattr(subprocess, "run", fake)
    directory, error = worker.resolve_directory(BOARD, {})
    assert error is None
    assert directory == str(existing)
    subcommands = [c[0][1] for c in fake.calls]
    assert "clone" not in subcommands
    assert subcommands == ["remote", "fetch", "rev-parse", "checkout", "reset", "clean"]


def test_default_branch_is_read_from_origin_head(monkeypatch, tmp_path):
    boards_root = tmp_path / "appdata" / "boards"
    monkeypatch.setattr(worker, "app_data_boards_dir", lambda: boards_root)
    fake = FakeGit(outputs={"rev-parse": (0, "origin/develop\n", "")})
    monkeypatch.setattr(subprocess, "run", fake)
    worker.resolve_directory(BOARD, {})
    checkout_cmd = next(c[0] for c in fake.calls if c[0][1] == "checkout")
    reset_cmd = next(c[0] for c in fake.calls if c[0][1] == "reset")
    assert checkout_cmd[2] == "develop"
    assert reset_cmd[3] == "origin/develop"


def test_origin_mismatch_is_a_hard_error_not_a_recreate(monkeypatch, tmp_path):
    boards_root = tmp_path / "appdata" / "boards"
    existing = boards_root / "9"
    existing.mkdir(parents=True)
    monkeypatch.setattr(worker, "app_data_boards_dir", lambda: boards_root)
    fake = FakeGit(outputs={"remote": (0, "https://github.com/org/other-repo.git\n", "")})
    monkeypatch.setattr(subprocess, "run", fake)
    directory, error = worker.resolve_directory(BOARD, {})
    assert directory is None
    assert "does not match" in error
    subcommands = [c[0][1] for c in fake.calls]
    assert subcommands == ["remote"]  # never proceeds to fetch/reset


def test_clone_failure_is_reported_not_raised(monkeypatch, tmp_path):
    boards_root = tmp_path / "appdata" / "boards"
    monkeypatch.setattr(worker, "app_data_boards_dir", lambda: boards_root)
    fake = FakeGit(outputs={"clone": (128, "", "fatal: Authentication failed")})
    monkeypatch.setattr(subprocess, "run", fake)
    directory, error = worker.resolve_directory(BOARD, {})
    assert directory is None
    assert "Authentication failed" in error


def test_neither_set_path_nor_repo_url_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(subprocess, "run", FakeGit())
    directory, error = worker.resolve_directory(
        {"id": 9, "name": "site-page", "repo_url": None}, {})
    assert directory is None
    assert "--set-path" in error
    assert "Repo URL" in error
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_worker_auto_clone.py -v`
Expected: FAIL — `AttributeError: module 'worker' has no attribute 'resolve_directory'` (and `app_data_boards_dir`).

- [ ] **Step 3: Write minimal implementation**

In `worker.py`, insert this new section after `prompt_for_board_paths` (after line 289, before the `enroll` function):

```python
# ---------- auto-clone: repo_url as a fallback for --set-path ----------
#
# A board with a repo_url lets a worker with no manual path mapping still
# claim its tickets: the repo is cloned once into this PC's own AppData
# folder and refreshed to the default branch before every run. An explicit
# --set-path entry always wins and is never touched by anything below —
# that folder may be an operator's own hand-tended checkout.

def app_data_boards_dir() -> Path:
    """Where auto-cloned board repos live on this PC.

    Independent of app_dir() (wherever the exe/script happens to sit) so the
    clone survives moving or re-downloading the exe. Falls back to app_dir()
    if LOCALAPPDATA is somehow unset, rather than crashing.
    """
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else app_dir()
    return root / "kanban-worker" / "boards"


def _run_git(args: list, cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def resolve_directory(board: dict, cfg: dict) -> tuple:
    """The folder to run this board's tickets in, cloning/refreshing it if
    needed. Returns (directory, error) — exactly one is None.

    Order: an explicit --set-path entry always wins over repo_url, and is
    used as-is with no git commands run against it at all.
    """
    board = board or {}
    board_id = board.get("id")
    name = board.get("name", "?")

    explicit = board_paths(cfg).get(str(board_id))
    if explicit:
        return explicit, None

    repo_url = (board.get("repo_url") or "").strip()
    if not repo_url:
        return None, (
            f"This PC has no folder configured for board '{name}', and the "
            "board has no Repo URL set. Fix one: "
            f'kanban-worker --set-path "{name}=<path>", or set a Repo URL '
            "in the board's Project settings."
        )

    directory = app_data_boards_dir() / str(board_id)
    if not directory.exists():
        directory.parent.mkdir(parents=True, exist_ok=True)
        proc = _run_git(["clone", repo_url, str(directory)])
        if proc.returncode != 0:
            return None, (
                f"git clone failed for board '{name}': "
                f"{(proc.stderr or proc.stdout).strip()[:2000]}"
            )
    else:
        proc = _run_git(["remote", "get-url", "origin"], cwd=str(directory))
        if proc.returncode != 0:
            return None, (
                f"Could not read the git remote for board '{name}' at "
                f"{directory}: {proc.stderr.strip()[:500]}"
            )
        origin = proc.stdout.strip()
        if origin != repo_url:
            return None, (
                f"The Repo URL configured for board '{name}' ({repo_url}) "
                f"does not match this PC's existing clone's origin "
                f"({origin}) at {directory}. If this is intentional, remove "
                "that folder by hand — nothing here does that automatically."
            )

    proc = _run_git(["fetch", "origin"], cwd=str(directory))
    if proc.returncode != 0:
        return None, f"git fetch failed for board '{name}': {proc.stderr.strip()[:2000]}"

    proc = _run_git(["rev-parse", "--abbrev-ref", "origin/HEAD"], cwd=str(directory))
    if proc.returncode != 0:
        return None, (
            f"Could not determine the default branch for board '{name}': "
            f"{proc.stderr.strip()[:500]}"
        )
    default_branch = proc.stdout.strip().split("/", 1)[-1]

    for args in (["checkout", default_branch],
                 ["reset", "--hard", f"origin/{default_branch}"],
                 ["clean", "-fd"]):
        proc = _run_git(args, cwd=str(directory))
        if proc.returncode != 0:
            return None, (
                f"git {' '.join(args)} failed for board '{name}': "
                f"{proc.stderr.strip()[:2000]}"
            )

    return str(directory), None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_worker_auto_clone.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add worker.py tests/test_worker_auto_clone.py
git commit -m "feat(worker): auto-clone/refresh a board's repo_url under AppData"
```

---

### Task 6: Wire `resolve_directory` into `run_slot`

**Files:**
- Modify: `worker.py:531-577` (`run_slot`)
- Test: `tests/test_concurrency.py`

**Interfaces:**
- Consumes: `resolve_directory(board, cfg)` from Task 5.
- Produces: no new interface — this is the integration point; `run_slot`'s external behavior (signature, threading model) is unchanged.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_concurrency.py`:

```python
def test_real_slots_auto_clone_when_no_set_path_is_configured(monkeypatch, tmp_path):
    """No --set-path entry, but the board carries a repo_url: resolve_directory
    must be consulted and its directory handed to the executor."""
    monkeypatch.setattr(worker, "CONFIG_PATH", tmp_path / "cfg.json")
    seen = {}

    def fake_claim(conn, wid, cid, boards):
        return {"assignment_id": 1, "session_id": "s",
                "board": {"id": 9, "name": "site-page",
                          "repo_url": "https://github.com/org/repo.git"},
                "ticket": {"id": 1, "board_id": 9, "title": "t", "body": "",
                           "status": "doing", "attempts": 1}}

    class RecordingExecutor:
        name = "recording"

        def run(self, ticket, board=None, directory=None, session_id=None):
            seen["directory"] = directory
            return True, "ok"

    monkeypatch.setattr(worker.psycopg, "connect", _fake_connect())
    monkeypatch.setattr(worker, "claim_next", fake_claim)
    monkeypatch.setattr(worker, "finish_work", lambda *a, **k: "review")
    monkeypatch.setattr(worker, "resolve_directory",
                        lambda board, cfg: (str(tmp_path / "cloned"), None))

    cfg = {"dsn": "x", "worker_id": 1, "cluster_id": 1, "name": "pc"}
    args = worker.build_parser().parse_args(["--poll", "0.01", "--once"])
    worker.run_slot(cfg, args, RecordingExecutor(), threading.Event(), 0)
    assert seen["directory"] == str(tmp_path / "cloned")


def test_resolve_error_skips_the_executor_entirely(monkeypatch, tmp_path):
    """When neither --set-path nor repo_url resolves, don't waste a Claude
    CLI invocation on a directory that doesn't exist."""
    monkeypatch.setattr(worker, "CONFIG_PATH", tmp_path / "cfg.json")
    finished = {}

    def fake_claim(conn, wid, cid, boards):
        return {"assignment_id": 1, "session_id": "s",
                "board": {"id": 9, "name": "site-page", "repo_url": None},
                "ticket": {"id": 1, "board_id": 9, "title": "t", "body": "",
                           "status": "doing", "attempts": 1}}

    class Unreachable:
        name = "unreachable"

        def run(self, *a, **k):
            raise AssertionError("executor.run must not be called")

    def fake_finish(conn, wname, wid, item_id, ticket_id, ok, comment):
        finished["ok"], finished["comment"] = ok, comment
        return "failed"

    monkeypatch.setattr(worker.psycopg, "connect", _fake_connect())
    monkeypatch.setattr(worker, "claim_next", fake_claim)
    monkeypatch.setattr(worker, "finish_work", fake_finish)
    monkeypatch.setattr(worker, "resolve_directory",
                        lambda board, cfg: (None, "no folder configured, no repo_url"))

    cfg = {"dsn": "x", "worker_id": 1, "cluster_id": 1, "name": "pc"}
    args = worker.build_parser().parse_args(["--poll", "0.01", "--once"])
    worker.run_slot(cfg, args, Unreachable(), threading.Event(), 0)
    assert finished["ok"] is False
    assert finished["comment"] == "no folder configured, no repo_url"


def test_stub_slots_never_call_resolve_directory(monkeypatch, tmp_path):
    """--stub needs no repo at all; it must not trigger a clone attempt."""
    monkeypatch.setattr(worker, "CONFIG_PATH", tmp_path / "cfg.json")
    monkeypatch.setattr(worker.time, "sleep", lambda s: None)  # skip StubExecutor's 2s delay

    def fake_claim(conn, wid, cid, boards):
        return {"assignment_id": 1, "session_id": "s",
                "board": {"id": 9, "name": "b", "repo_url": "https://x/y.git"},
                "ticket": {"id": 1, "board_id": 9, "title": "t", "body": "",
                           "status": "doing", "attempts": 1}}

    def boom(board, cfg):
        raise AssertionError("resolve_directory must not run for --stub")

    monkeypatch.setattr(worker.psycopg, "connect", _fake_connect())
    monkeypatch.setattr(worker, "claim_next", fake_claim)
    monkeypatch.setattr(worker, "finish_work", lambda *a, **k: "review")
    monkeypatch.setattr(worker, "resolve_directory", boom)

    cfg = {"dsn": "x", "worker_id": 1, "cluster_id": 1, "name": "pc"}
    args = worker.build_parser().parse_args(["--poll", "0.01", "--once"])
    worker.run_slot(cfg, args, worker.StubExecutor(), threading.Event(), 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_concurrency.py -v -k "auto_clone or resolve_error or never_call_resolve"`
Expected: FAIL — today `run_slot` uses `paths.get(...)` unconditionally and never calls `resolve_directory`, so `seen["directory"]` is `None` in the first test and the second test's `Unreachable.run` actually gets called and raises.

- [ ] **Step 3: Write minimal implementation**

In `worker.py`, replace the body of `run_slot` (line 531-577) with:

```python
def run_slot(cfg, args, executor, stop_event, slot_no: int) -> None:
    """One independent claim->run->finish loop with its own DB connection.

    Slots share nothing but the config dict (read-only) and the stop event.
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
                    if isinstance(executor, StubExecutor):
                        ok, comment = executor.run(
                            ticket, board=work.get("board"),
                            directory=paths.get(str(ticket["board_id"])),
                            session_id=work.get("session_id"))
                    else:
                        directory, resolve_error = resolve_directory(
                            work.get("board") or {}, cfg)
                        if resolve_error:
                            ok, comment = False, resolve_error
                        else:
                            ok, comment = executor.run(
                                ticket, board=work.get("board"),
                                directory=directory,
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
                stop_event.set()  # one slot learning this is enough for all of them
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

(Only the inner `try/except`/`finally` around `executor.run` changed — everything else in the function is unchanged.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_concurrency.py -v`
Expected: PASS — all existing concurrency tests plus the three new ones (existing tests keep passing because they all set `cfg["boards"]` explicitly, which `resolve_directory` still honors first, or use `StubExecutor`, which skips `resolve_directory` entirely).

- [ ] **Step 5: Commit**

```bash
git add worker.py tests/test_concurrency.py
git commit -m "feat(worker): resolve_directory drives run_slot's board directory"
```

---

### Task 7: Full suite + manual smoke check

**Files:** none (verification only)

- [ ] **Step 1: Run the full test suite**

Run: `pytest -q`
Expected: PASS, no regressions. (Ignore anything under `.venv/` or `.claude/worktrees/` if pytest is misconfigured to scan them — the project's own tests live in `tests/`.)

- [ ] **Step 2: Manual smoke check on this PC**

This PC's own `.worker_config.json` (worker id 6, cluster 3, board id unknown yet) has no `boards` mapping — it's the machine that motivated this whole feature. Once a board in cluster 3 has a `repo_url` set (via the UI), run:

```bash
py worker.py --once
```

Expected: worker log shows a `git clone` (first run) or `git fetch`/`reset`/`clean` (subsequent runs) against `%LOCALAPPDATA%\kanban-worker\boards\<id>`, then a Claude CLI invocation in that directory — no `--set-path` required.

- [ ] **Step 3: Commit (only if Step 1 or 2 needed a fix)**

```bash
git add -A
git commit -m "fix: address issues found in full-suite/smoke verification"
```
