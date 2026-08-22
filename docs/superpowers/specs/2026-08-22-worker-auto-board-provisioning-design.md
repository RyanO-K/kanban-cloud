# Worker auto board provisioning

Date: 2026-08-22

## Problem

Every worker PC currently needs a human to manually `git clone` a board's repo
somewhere and then run `kanban-worker --set-path <board>=<path>` before it can
claim that board's tickets. This is a per-PC, per-board manual step with no
cloud-side record of what to clone, and it does not scale as more PCs enroll.

Concretely, this PC's own `.worker_config.json` (worker id 6, cluster 3) has no
`boards` mapping at all, so its `ClaudeExecutor` cannot run a single ticket
yet — the exact gap this design closes.

## Goals

- A board that has a git remote configured should be usable by a newly
  enrolled worker with **zero manual repo setup** — no clone, no `--set-path`.
- The clone lives in a well-known per-machine location
  (`%LOCALAPPDATA%\kanban-worker\boards\<board_id>`), not wherever the exe
  happens to sit, so it survives moving/re-downloading the exe.
- Every ticket run starts from a clean, up-to-date checkout of the board's
  default branch.
- Never touch a directory the operator configured by hand (`--set-path`).
- No new secrets pass through the cluster; cloning relies on the same "this PC
  is already authenticated" assumption already made for the `claude` CLI.

## Non-goals

- Fixing the pre-existing shared-directory race when multiple concurrent
  slots claim the same board (exists today with `--set-path` too; unaffected
  by this change either way).
- SSH key / PAT management UI. Auth is ambient (git credential manager, SSH
  agent, `gh auth`) — whatever already lets a human `git clone` that URL on
  that PC.
- Re-pointing or deleting a clone automatically when `repo_url` changes.

## Design

### 1. Data model

Add nullable `boards.repo_url` (`TEXT`), following the exact pattern used for
`description`/`out_of_scope`/`commit_requirements`/`use_worktrees`:

- `app/db.py`: append `("boards", "repo_url TEXT")` to the migration column
  list (`_PHASE1_COLUMNS`, or a new list alongside it — implementer's call,
  naming only) so existing Neon/SQLite DBs pick it up idempotently.
- `app/models.py`: `Board.repo_url: Mapped[str | None] = mapped_column(Text, nullable=True)`.
  Update the class docstring: it currently says "what is NOT here: the folder
  the code lives in" — clarify that `repo_url` is the *clone source* (shared
  across every worker on the board), while the *folder* stays per-PC/derived,
  never stored on the board row.
- `app/main.py`: add `repo_url: str | None = None` to `BoardPatch`; add
  `"repo_url": b.repo_url` to `board_json`; add `"repo_url"` to the field
  tuple iterated in `patch_board`.
- `schema.sql`: add the column with a comment for documentation parity (the
  file is optional/reference-only; SQLAlchemy still owns table creation).

### 2. Settings UI

In `app/static/index.html`'s board settings modal (`#boardOverlay`):

- New `<label>Repo URL</label><input id="bRepoUrl" placeholder="https://github.com/org/repo.git">`
  above or below the existing fields.
- `openBoardSettings()`: seed `bRepoUrl.value` from `b.repo_url || ""`.
- `saveBoardSettings()`: include `repo_url: document.getElementById("bRepoUrl").value`
  in the PATCH body.
- Update the existing muted help text ("Each worker PC sets its own folder...")
  to explain the new default: if `repo_url` is set, a worker with no
  `--set-path` entry for this board clones it automatically; `--set-path`
  still overrides per-PC when set.

### 3. Worker: claim query

`worker.py`'s `claim_next()` SQL already selects board columns
(`name, description, out_of_scope, commit_requirements, use_worktrees`) —
add `repo_url` to that `SELECT` and to the returned `board` dict.

### 4. Worker: directory resolution

Replace the direct `paths.get(str(ticket["board_id"]))` lookup in `run_slot`
with a new function:

```python
def resolve_directory(board: dict, cfg: dict) -> tuple[Path | None, str | None]:
    """Returns (directory, error). directory is None only when error is set."""
```

Logic:

1. **Explicit `--set-path` wins.** If `cfg["boards"][str(board["id"])]` exists,
   return it unchanged. Nothing below ever runs against this path.
2. **Else, if `board.get("repo_url")` is set:**
   - `directory = app_data_boards_dir() / str(board["id"])`, where
     `app_data_boards_dir()` resolves `%LOCALAPPDATA%\kanban-worker\boards`
     (fall back sanely if `LOCALAPPDATA` is unset — unlikely on any real
     Windows PC, but don't crash; e.g. fall back to `app_dir() / "boards"`).
   - If `directory` doesn't exist: `git clone <repo_url> <directory>`. Clone
     failure (auth, network, bad URL) is a normal `(False, error)` result, not
     an exception the slot loop has to catch specially.
   - If `directory` exists: read its `origin` remote (`git -C <dir> remote get-url origin`)
     and compare to `board["repo_url"]`. Mismatch → hard error naming both
     values and telling the operator to remove the directory by hand if the
     change is intentional. **Never auto-delete or re-point.**
   - Match (or freshly cloned) → refresh:
     `git -C <dir> fetch origin` →
     resolve default branch via `git -C <dir> rev-parse --abbrev-ref origin/HEAD`
     (strip the `origin/` prefix) →
     `git -C <dir> checkout <default-branch>` →
     `git -C <dir> reset --hard origin/<default-branch>` →
     `git -C <dir> clean -fd`.
   - Any git command failing mid-sequence is a normal `(False, error)` result
     with the captured stderr, same shape as today's "folder no longer
     exists" error.
3. **Neither configured** → today's "no folder configured" error message,
   reworded to mention `repo_url` as the other option:
   `f"This PC has no folder configured for board '{name}', and the board has no repo_url set. Fix one: kanban-worker --set-path \"{name}=<path>\", or set a Repo URL in the board's Project settings."`

`ClaudeExecutor.run` calls `resolve_directory` at the top instead of trusting
its `directory` parameter blindly; `run_slot` no longer needs to pre-resolve
`paths.get(...)` before calling the executor (it still passes `board` through,
which now carries `repo_url`).

### 3.5 Safety boundaries (why this is safe to automate)

- The reset/clean sequence only ever touches the AppData path this feature
  itself creates. A `--set-path` directory — which might be an operator's
  existing, hand-tended checkout — is never cloned into, fetched, reset, or
  cleaned by this code path.
- The existing git-workflow contract already forbids committing to the
  default branch (`build_agent_prompt`'s git-workflow section: work happens on
  a ticket branch or worktree, never the default branch). So resetting the
  default branch's working tree to `origin/<default-branch>` can only ever
  discard leftover untracked build artifacts — never a ticket's committed
  work, which lives on its own branch ref regardless of what's checked out.
- A `repo_url` that no longer matches an existing clone's `origin` is
  surfaced as an error for a human to resolve, never silently "fixed" by
  deleting and re-cloning.

### 4. Tests

- `tests/test_worker_paths.py` (or a new `tests/test_worker_auto_clone.py`):
  unit-test `resolve_directory`'s branches with a fake/mocked `subprocess.run`
  for git — explicit path wins; clone-when-missing; fetch+reset-when-present;
  origin-mismatch error; neither-configured error message content.
- `tests/test_board_settings.py`: extend for `repo_url` round-tripping through
  `PATCH /api/boards/{id}` and `board_json`.
- `tests/test_migrations.py`: extend for the new column being added
  idempotently to a pre-existing DB.
- `tests/test_frontend_markup.py`: extend if it asserts on modal field ids.

## Open questions resolved during brainstorming

- Repo URL source → new `boards.repo_url` field, set via UI (not per-worker,
  not inferred from naming convention).
- Clone auth → ambient git credentials on the worker PC (same assumption as
  Claude CLI auth), not a stored token.
- Relation to `--set-path` → auto-clone is the fallback; explicit
  `--set-path` always wins and is never touched by the auto-clone/refresh
  logic.
- Freshness → fetch + hard-reset to the default branch before every run, not
  clone-once-and-leave-alone, not fetch-and-warn-only.
