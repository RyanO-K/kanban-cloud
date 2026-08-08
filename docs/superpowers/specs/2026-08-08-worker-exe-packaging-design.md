# Worker .exe packaging — design

**Date:** 2026-08-08
**Status:** Approved by Ryan (design conversation, this date)

## Goal

Turn the v2 worker client (`worker.py`, direct-Postgres) into a single
Windows executable that client PCs can download and run without installing
Python or pip-installing psycopg. Replace the current per-PC setup
(`pip install "psycopg[binary]"` + `py worker.py --enroll ...`) with:
download `kanban-worker.exe` → double-click → paste join code → working.

## Decisions made

- **Package shape: portable single .exe** (PyInstaller `--onefile`,
  console app). No installer, no Start Menu, no auto-start. The exe and
  its `.worker_config.json` travel together as a portable pair.
- **First-run UX: interactive enrollment.** No config present → prompt for
  the join code on stdin, default the name to the computer name and the
  server to `https://kanban-cloud.onrender.com`, enroll, then drop
  straight into the polling loop. CLI flags still work for scripted setup.
- **Executor default flips to real.** `ClaudeExecutor` is the default;
  new `--stub` flag selects the stub. `--real` remains as a hidden
  no-op alias so existing commands don't break.
- **Build & ship: GitHub Actions release.** Tag `worker-v*` → Windows CI
  job builds the exe, smoke-tests it, attaches it to a GitHub Release.

## Packaging approach

PyInstaller `--onefile` (chosen over Nuitka — slower, fussier CI, overkill
for a ~350-line script — and over an embeddable-Python folder, which isn't
a single file). The build bundles Python + psycopg's binary wheel
(libpq DLL included). psycopg 3 hides its C extension in a separate
`psycopg_binary` package, so the build uses `--collect-all psycopg
--collect-all psycopg_binary` to be safe.

## worker.py changes (exe-aware, minimal)

1. **Config path.** Under `--onefile`, `__file__` points into a temp
   extraction dir (`_MEIPASS`) deleted after each run — config written
   there vanishes. New resolution:

   ```python
   def app_dir() -> Path:
       if getattr(sys, "frozen", False):   # PyInstaller
           return Path(sys.executable).parent
       return Path(__file__).parent

   CONFIG_PATH = app_dir() / ".worker_config.json"
   ```

   No %APPDATA% fallback — portable convention, config next to the exe.
   If the exe sits somewhere unwritable, `save_config` fails with a clear
   message telling the user to move the exe to a writable folder.

2. **Interactive first run.** Today, no config + no `--enroll` prints an
   error and exits 2. New behavior: prompt instead.

   - `DEFAULT_SERVER = "https://kanban-cloud.onrender.com"` module
     constant; `--server` overrides it (for local testing).
   - Prompt for the join code (`input("Join code: ")`, strip whitespace,
     re-prompt on empty). Name defaults to `COMPUTERNAME`/`HOSTNAME` as
     today; `--name` overrides.
   - On successful enrollment, continue directly into the polling loop
     (no second launch needed). On enrollment failure (HTTP error, bad
     join code) print the server's error detail and exit non-zero.
   - Explicit `--enroll` keeps today's semantics (enroll then exit 0) but
     `--server` becomes optional (defaults to `DEFAULT_SERVER`).

3. **Executor default.** `--stub` flag added; executor selection becomes
   `StubExecutor() if args.stub else ClaudeExecutor()`. `--real` stays
   accepted (`action="store_true"`, `help=argparse.SUPPRESS`) and is
   ignored — real is already the default. Missing `claude` CLI keeps its
   existing per-ticket failure message; docs tell client PCs to install
   the Claude CLI for real execution.

4. **Double-click ergonomics.** On fatal exit paths (enrollment failure,
   revoked credentials, unwritable config) when frozen and stdin is a
   TTY, pause with `input("Press Enter to close...")` before returning,
   so the console window doesn't vanish before the error is read.
   Wrapped in try/except (EOFError) for non-interactive contexts.

## Build workflow

`.github/workflows/worker-exe.yml`:

- **Triggers:** push of tags matching `worker-v*`, plus
  `workflow_dispatch` (manual test builds → uploaded as a workflow
  artifact, no release).
- **Job (windows-latest):** checkout → setup Python 3.12 →
  `pip install "psycopg[binary]" pyinstaller` →
  `pyinstaller --onefile --name kanban-worker --collect-all psycopg
  --collect-all psycopg_binary worker.py` → smoke:
  `dist/kanban-worker.exe --help` must exit 0 (proves the frozen exe
  starts and psycopg imports) → on tag builds, create/update the GitHub
  Release for the tag with `dist/kanban-worker.exe` attached
  (`softprops/action-gh-release`).
- **Ship flow:** `git tag worker-v1.0.0 && git push origin worker-v1.0.0`.
  Tag pushes do not trigger the Render deploy (master-branch only).
  The repo is private: release downloads require Ryan's GitHub auth —
  he downloads the exe and hands it to client PCs.

## Caveats (documented, not solved)

- Unsigned exe → SmartScreen "Windows protected your PC" on first run
  (More info → Run anyway). PyInstaller onefile exes occasionally trip
  AV false-positives. Code signing is out of scope.
- `.worker_config.json` next to the exe holds the per-PC DB credential —
  same trust model as the script version.
- The `claude` CLI is not bundled (Node binary); each PC installs it
  separately for `--real`-style execution. Without it, tickets fail with
  the existing clear message.

## Testing

- Unit tests (existing pytest style, in `tests/test_worker.py` or a new
  `tests/test_worker_exe.py`):
  - `app_dir()` returns the exe dir when `sys.frozen` is set (monkeypatch
    `sys.frozen`/`sys.executable`) and the script dir otherwise.
  - First-run flow: no config + monkeypatched `input` → enrolls with
    entered join code, `DEFAULT_SERVER`, computer-name default, then
    proceeds to the loop (enrollment HTTP call mocked).
  - Empty join-code input re-prompts.
  - Executor selection: default → `ClaudeExecutor`; `--stub` →
    `StubExecutor`; `--real` accepted and still → `ClaudeExecutor`.
- CI smoke: the built exe runs `--help` with exit 0 on the runner.
- Full end-to-end (download exe → enroll against prod → claim a ticket)
  is a manual check on the first release.

## Docs

README "Client PC install (.exe)" section: download `kanban-worker.exe`
from the latest `worker-v*` GitHub Release → put it in a folder of its
own → double-click → paste the cluster join code (from the board's
workers panel) → it polls. Install the Claude CLI for real execution;
`--stub` for testing. Decommission a PC: click Revoke in the UI and
delete the exe's folder (config included). Update the existing
pip-install setup instructions to present the exe as the primary path
and the Python script as the dev path.

## Out of scope

- Code signing / SmartScreen reputation.
- Auto-start (service, scheduled task) — an always-on worker would keep
  Neon compute awake 24/7.
- Auto-update; new version = download the new release exe.
- Bundling the Claude CLI.
- macOS/Linux binaries (script path still works there).
