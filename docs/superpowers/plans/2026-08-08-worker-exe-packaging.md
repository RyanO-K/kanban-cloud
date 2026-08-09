# Worker .exe Packaging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Package the v2 worker client as a single portable Windows exe (PyInstaller onefile) with interactive first-run enrollment, built and released by GitHub Actions on `worker-v*` tags.

**Architecture:** All runtime changes live in `worker.py` (the single worker file): exe-aware config-path resolution, a first-run join-code prompt that enrolls and drops into the polling loop, real Claude executor by default with `--stub` for testing, and a console-pause helper so double-click users can read fatal errors. A new CI workflow builds the exe on `windows-latest`, smoke-tests it, and attaches it to a GitHub Release.

**Tech Stack:** Python 3.12, PyInstaller `--onefile`, psycopg 3 (`psycopg[binary]`), GitHub Actions (`windows-latest`, `softprops/action-gh-release@v2`).

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-08-worker-exe-packaging-design.md`.
- Test command (worktrees have no venv — use the main repo's):
  `C:\Users\ryan\Documents\Github\kanban-cloud\.venv\Scripts\python.exe -m pytest tests -q`
  All 47 existing tests must stay green; no test may be deleted or skipped.
- `DEFAULT_SERVER = "https://kanban-cloud.onrender.com"` — exact value.
- Exe name: `kanban-worker.exe`. Release tag pattern: `worker-v*`.
- `worker.py` stays the single worker file — no new runtime modules.
- Real executor (`ClaudeExecutor`) is the default; `--stub` selects the stub; `--real` stays accepted as a hidden no-op alias (`help=argparse.SUPPRESS`).
- Config stays next to the exe (`Path(sys.executable).parent` when frozen) — no %APPDATA%.
- Never commit `.env` or any secret/DSN.
- The exe does NOT bundle the `claude` CLI.
- Do not touch `app/` server code — this plan is worker + CI + docs only.

---

### Task 1: Exe-aware paths, parser extraction, executor default flip

**Files:**
- Modify: `worker.py` (module constants ~line 31, `main()` argparse block ~lines 271-283, executor pick ~line 300)
- Test: `tests/test_worker_exe.py` (create)

**Interfaces:**
- Consumes: existing `worker.StubExecutor`, `worker.ClaudeExecutor`, `POLL_SECONDS`.
- Produces (Task 2 relies on these exact names): `DEFAULT_SERVER: str`, `app_dir() -> Path`, `build_parser() -> argparse.ArgumentParser` (flags: `--enroll --server --join-code --name --stub --real --poll --once`), `pick_executor(args)` returning an executor instance.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_worker_exe.py`:

```python
"""worker.py exe-packaging behavior: frozen paths, parser, executor default,
first-run enrollment prompt (first-run tests are added in Task 2)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import worker  # noqa: E402


def test_app_dir_script_mode():
    assert worker.app_dir() == Path(worker.__file__).resolve().parent


def test_app_dir_frozen(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\some\place\kanban-worker.exe")
    assert worker.app_dir() == Path(r"C:\some\place")


def test_default_server_constant():
    assert worker.DEFAULT_SERVER == "https://kanban-cloud.onrender.com"


def test_executor_default_is_real():
    args = worker.build_parser().parse_args([])
    assert isinstance(worker.pick_executor(args), worker.ClaudeExecutor)


def test_stub_flag_selects_stub():
    args = worker.build_parser().parse_args(["--stub"])
    assert isinstance(worker.pick_executor(args), worker.StubExecutor)


def test_real_alias_still_accepted():
    args = worker.build_parser().parse_args(["--real"])
    assert isinstance(worker.pick_executor(args), worker.ClaudeExecutor)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `C:\Users\ryan\Documents\Github\kanban-cloud\.venv\Scripts\python.exe -m pytest tests/test_worker_exe.py -v`
Expected: FAIL — `AttributeError: module 'worker' has no attribute 'app_dir'` (and `DEFAULT_SERVER`, `build_parser`, `pick_executor`).

- [ ] **Step 3: Implement in `worker.py`**

Replace the `CONFIG_PATH` line (currently `CONFIG_PATH = Path(__file__).parent / ".worker_config.json"`) with:

```python
DEFAULT_SERVER = "https://kanban-cloud.onrender.com"


def app_dir() -> Path:
    """Directory the worker lives in: next to the exe when frozen by
    PyInstaller (whose __file__ points into a temp dir deleted after each
    run), next to the script otherwise."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


CONFIG_PATH = app_dir() / ".worker_config.json"
```

Extract the argparse block out of `main()` into module-level functions (place them just above `main()`):

```python
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="kanban-cloud worker (v2 direct-DB)")
    parser.add_argument("--enroll", action="store_true",
                        help="enroll this PC (needs --join-code; --server optional)")
    parser.add_argument("--server",
                        help=f"server base URL (default {DEFAULT_SERVER})")
    parser.add_argument("--join-code", help="cluster join code (enrollment only)")
    parser.add_argument("--name", help="worker name (defaults to computer name)")
    parser.add_argument("--stub", action="store_true",
                        help="use the stub executor instead of the Claude CLI")
    # v1 flag; real execution is now the default, so this is a no-op alias
    parser.add_argument("--real", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--poll", type=float, default=POLL_SECONDS,
                        help=f"poll interval seconds (default {POLL_SECONDS})")
    parser.add_argument("--once", action="store_true",
                        help="poll a single time then exit (for testing)")
    return parser


def pick_executor(args):
    return StubExecutor() if args.stub else ClaudeExecutor()
```

In `main()`: replace the whole inline `parser = argparse.ArgumentParser(...)` ... `args = parser.parse_args()` block with `args = build_parser().parse_args()`, and replace `executor = ClaudeExecutor() if args.real else StubExecutor()` with `executor = pick_executor(args)`.

- [ ] **Step 4: Run the full suite**

Run: `C:\Users\ryan\Documents\Github\kanban-cloud\.venv\Scripts\python.exe -m pytest tests -q`
Expected: 53 passed (47 existing + 6 new), 0 failed.

- [ ] **Step 5: Commit**

```bash
git add worker.py tests/test_worker_exe.py
git commit -m "feat(worker): exe-aware config path, parser extraction, real executor default"
```

---

### Task 2: Interactive first-run enrollment + fatal-exit pause

**Files:**
- Modify: `worker.py` (`save_config` ~line 115, `enroll` docstring block, `main()` enrollment/config section ~lines 285-298, revoked-credential exit ~line 330)
- Test: `tests/test_worker_exe.py` (extend)

**Interfaces:**
- Consumes (from Task 1): `DEFAULT_SERVER`, `build_parser()`, `app_dir()`.
- Produces: `first_run_enroll(args) -> dict | None`, `pause_if_frozen() -> None`. `main()` behavior: no config + no `--enroll` → interactive enrollment then polling loop; `--enroll` without `--server` uses `DEFAULT_SERVER`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_worker_exe.py`:

```python
import io
import json
import urllib.error


def test_first_run_reprompts_then_enrolls(monkeypatch, capsys):
    calls = {}

    def fake_enroll(server, code, name):
        calls.update(server=server, code=code, name=name)
        return {"worker_id": 1, "dsn": "x", "cluster_id": 2,
                "name": name, "cluster_name": "Main"}

    monkeypatch.setattr(worker, "enroll", fake_enroll)
    prompts = iter(["", "   ", "  ABC123  "])  # two empties re-prompt
    monkeypatch.setattr("builtins.input", lambda prompt="": next(prompts))
    monkeypatch.setenv("COMPUTERNAME", "TESTPC")
    args = worker.build_parser().parse_args([])
    cfg = worker.first_run_enroll(args)
    assert cfg["worker_id"] == 1
    assert calls == {"server": worker.DEFAULT_SERVER, "code": "ABC123",
                     "name": "TESTPC"}


def test_first_run_server_and_name_overrides(monkeypatch):
    calls = {}
    monkeypatch.setattr(worker, "enroll",
                        lambda server, code, name: calls.update(
                            server=server, code=code, name=name) or {"worker_id": 9})
    monkeypatch.setattr("builtins.input", lambda prompt="": "JOIN1")
    args = worker.build_parser().parse_args(
        ["--server", "http://localhost:8000", "--name", "devbox"])
    assert worker.first_run_enroll(args) == {"worker_id": 9}
    assert calls == {"server": "http://localhost:8000", "code": "JOIN1",
                     "name": "devbox"}


def test_first_run_http_error_prints_detail(monkeypatch, capsys):
    err = urllib.error.HTTPError(
        "url", 404, "Not Found", {},
        io.BytesIO(json.dumps({"detail": "Invalid join code"}).encode()))

    def boom(server, code, name):
        raise err

    monkeypatch.setattr(worker, "enroll", boom)
    monkeypatch.setattr("builtins.input", lambda prompt="": "WRONG")
    args = worker.build_parser().parse_args([])
    assert worker.first_run_enroll(args) is None
    out = capsys.readouterr().out
    assert "Invalid join code" in out and "404" in out


def test_first_run_cancelled_by_eof(monkeypatch):
    def eof(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    args = worker.build_parser().parse_args([])
    assert worker.first_run_enroll(args) is None


def test_pause_if_frozen_noop_when_not_frozen(monkeypatch):
    called = []
    monkeypatch.setattr("builtins.input", lambda prompt="": called.append(1))
    monkeypatch.delattr(sys, "frozen", raising=False)
    worker.pause_if_frozen()
    assert called == []


def test_save_config_unwritable_message(monkeypatch, tmp_path, capsys):
    target = tmp_path / "no-such-dir" / "cfg.json"  # parent missing -> OSError
    monkeypatch.setattr(worker, "CONFIG_PATH", target)
    try:
        worker.save_config({"a": 1})
        raised = False
    except OSError:
        raised = True
    assert raised
    assert "writable folder" in capsys.readouterr().out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `C:\Users\ryan\Documents\Github\kanban-cloud\.venv\Scripts\python.exe -m pytest tests/test_worker_exe.py -v`
Expected: the six new tests FAIL (`no attribute 'first_run_enroll'` / `'pause_if_frozen'`; `save_config` raises without the message); Task 1's six still PASS.

- [ ] **Step 3: Implement in `worker.py`**

Replace `save_config` with:

```python
def save_config(cfg: dict) -> None:
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    except OSError as e:
        print(f"Cannot write config at {CONFIG_PATH}: {e}\n"
              "Move the exe to a writable folder and run it again.")
        raise
    print(f"Saved worker config to {CONFIG_PATH}")
```

Add below `enroll()`:

```python
def pause_if_frozen() -> None:
    """Double-clicked exes close their console on exit; hold it open so
    the user can read a fatal error. No-op for the script version."""
    if getattr(sys, "frozen", False) and sys.stdin is not None and sys.stdin.isatty():
        try:
            input("Press Enter to close...")
        except EOFError:
            pass


def first_run_enroll(args) -> dict | None:
    """No saved config: prompt for a join code and enroll interactively.
    Returns the saved config, or None if enrollment failed/was cancelled."""
    server = args.server or DEFAULT_SERVER
    name = (args.name or os.environ.get("COMPUTERNAME")
            or os.environ.get("HOSTNAME") or "worker")
    print(f"No worker config found - enrolling this PC against {server}")
    print(f"Worker name: {name}")
    try:
        code = ""
        while not code:
            code = input("Cluster join code: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nEnrollment cancelled.")
        return None
    try:
        return enroll(server, code, name)
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode()).get("detail", "")
        except Exception:
            detail = ""
        print(f"Enrollment failed ({e.code}): {detail or e.reason}")
        return None
    except urllib.error.URLError as e:
        print(f"Enrollment failed: could not reach {server} ({e.reason})")
        return None
    except OSError:
        return None  # save_config already printed the fix
```

(`urllib.error.URLError` subclasses `OSError`, so the order above matters: HTTPError → URLError → OSError-from-save_config.)

In `main()`, replace the enrollment/config section (from `if args.enroll:` through the `cfg is None` early-return) with:

```python
    if args.enroll:
        if not args.join_code:
            print("--enroll needs --join-code")
            return 2
        name = (args.name or os.environ.get("COMPUTERNAME")
                or os.environ.get("HOSTNAME") or "worker")
        enroll(args.server or DEFAULT_SERVER, args.join_code, name)
        return 0

    cfg = load_config()
    if cfg is None:
        cfg = first_run_enroll(args)
        if cfg is None:
            pause_if_frozen()
            return 2
```

In the polling loop's revoked-credential branch, add the pause before the return:

```python
            if "password authentication failed" in msg or "does not exist" in msg:
                print("Credentials rejected — this PC may be revoked. Re-enroll.")
                pause_if_frozen()
                return 1
```

- [ ] **Step 4: Run the full suite**

Run: `C:\Users\ryan\Documents\Github\kanban-cloud\.venv\Scripts\python.exe -m pytest tests -q`
Expected: 59 passed (53 + 6 new), 0 failed.

- [ ] **Step 5: Commit**

```bash
git add worker.py tests/test_worker_exe.py
git commit -m "feat(worker): interactive first-run enrollment + console pause on fatal exits"
```

---

### Task 3: GitHub Actions build-and-release workflow

**Files:**
- Create: `.github/workflows/worker-exe.yml`

**Interfaces:**
- Consumes: `worker.py` at repo root (Tasks 1-2 complete); `--help` exits 0 (argparse default).
- Produces: on `worker-v*` tag push → GitHub Release with `kanban-worker.exe` attached; on `workflow_dispatch` → build artifact only.

- [ ] **Step 1: Write the workflow**

Create `.github/workflows/worker-exe.yml`:

```yaml
name: worker-exe

on:
  push:
    tags: ["worker-v*"]
  workflow_dispatch:

permissions:
  contents: write  # create the release

jobs:
  build:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install build dependencies
        run: pip install "psycopg[binary]" pyinstaller

      - name: Build kanban-worker.exe
        run: >
          pyinstaller --onefile --name kanban-worker
          --collect-all psycopg --collect-all psycopg_binary
          worker.py

      - name: Smoke test the frozen exe
        # --help proves the exe starts and psycopg imports (import worker
        # happens before argparse), and exits 0.
        run: |
          dist\kanban-worker.exe --help
          if ($LASTEXITCODE -ne 0) { exit 1 }

      - name: Upload build artifact (manual runs)
        if: github.event_name == 'workflow_dispatch'
        uses: actions/upload-artifact@v4
        with:
          name: kanban-worker
          path: dist/kanban-worker.exe

      - name: Attach exe to GitHub Release (tag runs)
        if: startsWith(github.ref, 'refs/tags/worker-v')
        uses: softprops/action-gh-release@v2
        with:
          files: dist/kanban-worker.exe
```

- [ ] **Step 2: Validate the YAML parses**

Run: `C:\Users\ryan\Documents\Github\kanban-cloud\.venv\Scripts\python.exe -c "import yaml, pathlib; yaml.safe_load(pathlib.Path('.github/workflows/worker-exe.yml').read_text()); print('YAML OK')"`
Expected: `YAML OK`. (PyYAML ships with the venv via other deps; if the import fails, `pip install pyyaml` into the venv first.)

- [ ] **Step 3: Run the full suite (regression guard)**

Run: `C:\Users\ryan\Documents\Github\kanban-cloud\.venv\Scripts\python.exe -m pytest tests -q`
Expected: 59 passed.

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/worker-exe.yml
git commit -m "ci: build kanban-worker.exe and attach to worker-v* releases"
```

---

### Task 4: Docs — README client-PC install, worker docstring, STATUS

**Files:**
- Modify: `worker.py` (module docstring, lines 1-18)
- Modify: `README.md` (worker setup section)
- Modify: `STATUS.md` (append entry)

**Interfaces:**
- Consumes: everything above; no code changes.

- [ ] **Step 1: Update the `worker.py` module docstring**

Replace the docstring's Setup/Run sections (keep the surrounding architecture text and the Neon-compute note) with:

```python
"""kanban-cloud worker client (v2: direct Postgres).

One-time enrollment over HTTP issues this PC its own database role; after
that the worker never contacts the web service — polling, claiming,
progress, results, and heartbeats are SQL against Neon.

Client PCs (packaged exe): download kanban-worker.exe from the latest
worker-v* GitHub Release, put it in its own folder, and run it — on first
run it asks for the cluster join code, then starts polling. Real ticket
execution shells out to the `claude` CLI, which must be installed
separately.

Dev / script setup (once per PC):
    pip install "psycopg[binary]"
    py worker.py --enroll --join-code ABC12345 --name ryans-pc

Run:
    py worker.py            # real executor (Claude CLI)
    py worker.py --stub     # stub executor for testing

Note: while this worker runs, its polling keeps Neon compute awake
(free tier autosuspends only when idle). Stop the worker when not in use.
"""
```

- [ ] **Step 2: Update `README.md`**

Find the worker setup section (search for `pip install "psycopg[binary]"` / the enroll command) and restructure it so the exe is the primary path. Add this subsection above the existing script instructions (keep those, retitled as the dev path):

```markdown
### Client PC install (.exe)

1. Download `kanban-worker.exe` from the latest `worker-v*` GitHub Release
   (repo is private — download while signed in and copy it to the PC).
2. Put the exe in a folder of its own (it writes `.worker_config.json`
   next to itself).
3. Double-click it. On first run it asks for the cluster join code (shown
   in the board's workers panel), enrolls, and starts polling.
4. For real ticket execution, install the Claude CLI on the PC (`claude`
   must be on PATH). Pass `--stub` to test without it.

First-run notes: Windows SmartScreen will warn about the unsigned exe
(More info → Run anyway). To decommission a PC: click **Revoke** in the
board UI, then delete the exe's folder (the config, including the DB
credential, lives there).

Releasing a new version: `git tag worker-vX.Y.Z && git push origin
worker-vX.Y.Z` — CI builds the exe and attaches it to the release. Tag
pushes do not trigger the Render deploy.
```

Also update any remaining `--real` references in README to reflect the new default (real by default, `--stub` for testing).

- [ ] **Step 3: Append to `STATUS.md`**

Add a dated entry at the top of the log section:

```markdown
## Worker .exe packaging (2026-08-09)

`worker.py` is now packageable as a portable single-file Windows exe:
PyInstaller onefile via `.github/workflows/worker-exe.yml` (tag
`worker-v*` → GitHub Release with `kanban-worker.exe`; manual
`workflow_dispatch` → artifact). Runtime changes: config resolves next to
the exe when frozen; first run with no config prompts for the join code
and enrolls against https://kanban-cloud.onrender.com; the real Claude
executor is now the default (`--stub` for testing, `--real` kept as a
hidden no-op alias); fatal exits pause for Enter when frozen so
double-click users can read the error. Caveats: exe is unsigned
(SmartScreen warning), `claude` CLI not bundled, config holds the DB
credential next to the exe. Spec:
`docs/superpowers/specs/2026-08-08-worker-exe-packaging-design.md`.
```

- [ ] **Step 4: Run the full suite (docstring edit touches worker.py)**

Run: `C:\Users\ryan\Documents\Github\kanban-cloud\.venv\Scripts\python.exe -m pytest tests -q`
Expected: 59 passed.

- [ ] **Step 5: Commit**

```bash
git add worker.py README.md STATUS.md
git commit -m "docs: client PC .exe install path (README, worker docstring, STATUS)"
```
