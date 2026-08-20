# Board Sign-In + Demo Board Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give anonymous visitors to `https://www.okeefe.work/board/` a working "Sign in with GitHub" button that returns them to the board, and a populated "Demo" board to look at instead of an empty one.

**Architecture:** Two repos. `site-page` learns to carry a validated return path through the GitHub OAuth state so the callback can send the browser back to `/board/` instead of the homepage. `kanban-cloud` learns two config-driven facts it reports through `GET /api/session` — a `login_url` (which becomes the button) and the `board` a spectator should land on (the demo board when one exists) — plus a one-off seed script that writes the demo tickets into Neon.

**Tech Stack:** TypeScript + Node's built-in test runner (`node --test`) in `site-page`; Python 3, FastAPI, SQLAlchemy and pytest in `kanban-cloud`; plain ES5-ish JS in `kanban-cloud/app/static/index.html`.

## Global Constraints

- Spec: `kanban-cloud/docs/superpowers/specs/2026-08-19-board-signin-and-demo-board-design.md`.
- Repo roots are siblings: `C:\Users\ryan\Documents\Github\kanban-cloud` and `C:\Users\ryan\Documents\Github\site-page`. Run every command from inside the relevant repo.
- `kanban-cloud` work happens on branch `board-signin-and-demo-board` (already created, spec committed). `site-page` work happens on a new branch `board-signin`.
- **Do not push.** Pushes and production database writes are the user's to run. Commit locally only.
- **Do not run anything against the production Neon database.** Every test uses scratch SQLite.
- Behaviour with no proxy env set (local dev mode) must stay byte-identical. `kanban-cloud`'s existing baseline is 47 passing pytest tests; `site-page`'s is `npm test` green.
- New env var name, exactly: `PROXY_LOGIN_URL`. Production value, exactly: `/auth/github?return=/board/`.
- The demo board is named exactly `Demo`. Matching it is case-insensitive.
- Default post-login destination stays exactly `/#projects` (today's hardcoded value).

---

### Task 1: `site-page` — carry a validated return path through OAuth

**Files:**
- Create: `site-page/src/auth-return.ts`
- Create: `site-page/tests/auth-return.test.mjs`
- Modify: `site-page/src/server.ts` (imports at line 1-6; `oauthStates` at line 18; `/auth/github` at lines 243-249; `/auth/callback` at lines 251-271)

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: `safeReturnPath(raw: unknown): string` and `DEFAULT_RETURN: string` exported from `src/auth-return.ts` (compiled to `dist/auth-return.js`). Nothing later in this plan imports them — Task 4's button just needs the route to honour `?return=`.

Why a separate module: `src/server.ts` calls `server.listen()` at import time, so it cannot be imported into a unit test. The existing tests spawn `dist/server.js` as a child process. Putting the pure validation in its own file makes it directly importable.

- [ ] **Step 1: Create the branch**

```bash
cd /c/Users/ryan/Documents/Github/site-page
git checkout -b board-signin
```

- [ ] **Step 2: Write the failing test**

Create `tests/auth-return.test.mjs`:

```javascript
// Unit tests for the post-login return path validator. The rejection cases are
// open-redirect defence: a browser treats "//evil.com" and "/\evil.com" as
// absolute URLs to another origin, even though both start with a slash.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { safeReturnPath, DEFAULT_RETURN } from '../dist/auth-return.js';

test('accepts a same-site absolute path', () => {
  assert.equal(safeReturnPath('/board/'), '/board/');
  assert.equal(safeReturnPath('/board/?x=1'), '/board/?x=1');
  assert.equal(safeReturnPath('/'), '/');
});

test('rejects protocol-relative and absolute URLs', () => {
  assert.equal(safeReturnPath('//evil.com'), DEFAULT_RETURN);
  assert.equal(safeReturnPath('/\\evil.com'), DEFAULT_RETURN);
  assert.equal(safeReturnPath('https://evil.com'), DEFAULT_RETURN);
  assert.equal(safeReturnPath('evil.com'), DEFAULT_RETURN);
});

test('rejects missing, non-string and over-long input', () => {
  assert.equal(safeReturnPath(undefined), DEFAULT_RETURN);
  assert.equal(safeReturnPath(null), DEFAULT_RETURN);
  assert.equal(safeReturnPath(''), DEFAULT_RETURN);
  assert.equal(safeReturnPath(42), DEFAULT_RETURN);
  assert.equal(safeReturnPath('/' + 'a'.repeat(200)), DEFAULT_RETURN);
});

test('the default is the homepage projects anchor', () => {
  assert.equal(DEFAULT_RETURN, '/#projects');
});
```

- [ ] **Step 3: Run it to verify it fails**

```bash
npm test
```

Expected: FAIL — `Cannot find module '../dist/auth-return.js'`.

- [ ] **Step 4: Implement the validator**

Create `src/auth-return.ts`:

```typescript
/** Where /auth/callback sends the browser when no valid return path was given. */
export const DEFAULT_RETURN = '/#projects';

/** Longest post-login path we will echo back into a Location header. */
const MAX_RETURN_LENGTH = 200;

/**
 * Validate a caller-supplied post-login destination.
 *
 * Only same-site absolute paths are allowed. "//evil.com" and "/\evil.com"
 * start with a slash but browsers resolve them as protocol-relative URLs to
 * another origin, so accepting them would make /auth/callback an open redirect.
 */
export function safeReturnPath(raw: unknown): string {
  if (typeof raw !== 'string') return DEFAULT_RETURN;
  if (raw.length === 0 || raw.length > MAX_RETURN_LENGTH) return DEFAULT_RETURN;
  if (!raw.startsWith('/')) return DEFAULT_RETURN;
  if (raw.startsWith('//') || raw.startsWith('/\\')) return DEFAULT_RETURN;
  return raw;
}
```

- [ ] **Step 5: Run the test to verify it passes**

```bash
npm test
```

Expected: the four `auth-return` tests PASS; every pre-existing test still passes.

- [ ] **Step 6: Wire it into the OAuth routes**

In `src/server.ts`, add to the import block at the top (after the `./store` import on line 6):

```typescript
import { safeReturnPath, DEFAULT_RETURN } from './auth-return';
```

Change line 18 from a Set to a Map of state → return path:

```typescript
const oauthStates = new Map<string, string>();
```

Replace the `/auth/github` handler body so it records the return path against the state:

```typescript
    if (method === 'GET' && urlPath === '/auth/github') {
      const state = randomBytes(16).toString('hex');
      oauthStates.set(state, safeReturnPath(params.get('return')));
      setTimeout(() => oauthStates.delete(state), 10 * 60 * 1000);
      res.writeHead(302, { Location: `https://github.com/login/oauth/authorize?client_id=${GITHUB_CLIENT_ID}&scope=read:user&state=${state}` });
      res.end(); return;
    }
```

In the `/auth/callback` handler, read the path back out immediately after the state check (the `oauthStates.delete(state)` line already there becomes the second of these two lines):

```typescript
      const returnTo = oauthStates.get(state) ?? DEFAULT_RETURN;
      oauthStates.delete(state);
```

and change the success redirect at the end of that handler from `Location: '/#projects'` to:

```typescript
      res.writeHead(302, { Location: returnTo });
```

Leave `oauthStates.has(state)` in the guard as-is — `Map` has the same method.

- [ ] **Step 7: Run the full suite**

```bash
npm test
```

Expected: all tests pass, including `tests/board-proxy.test.mjs`. `npm test` runs `tsc` first, so a type error here fails the run.

- [ ] **Step 8: Commit**

```bash
git add src/auth-return.ts src/server.ts tests/auth-return.test.mjs
git commit -m "feat(auth): return to a validated same-site path after GitHub login"
```

---

### Task 2: `kanban-cloud` — report a login URL and a landing board in `/api/session`

**Files:**
- Modify: `kanban-cloud/app/main.py` (module docstring lines 1-7; `create_app` signature and proxy config at lines 118-129; near `default_cluster` at lines 169-172; `session_info` at lines 296-313)
- Modify: `kanban-cloud/tests/test_proxy.py` (append)
- Modify: `kanban-cloud/README.md` (the reverse-proxy section)

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces:
  - `create_app(db_url=None, proxy_secret=None, proxy_login_url=None) -> FastAPI` — third keyword argument is new.
  - `default_board(db: Session, cluster: Cluster | None) -> Board | None`, defined inside `create_app` next to `default_cluster`.
  - Spectator `GET /api/session` payload gains two keys, consumed by Task 4's frontend: `"login_url": str | None` and `"board": {"id": int, "name": str} | None`.

- [ ] **Step 1: Write the failing tests**

Append to `kanban-cloud/tests/test_proxy.py`:

```python
# ---------- session: login_url + landing board ----------

def test_session_reports_login_url_when_configured(tmp_path):
    app = create_app(
        f"sqlite:///{tmp_path / 'login.db'}",
        proxy_secret=SECRET,
        proxy_login_url="/auth/github?return=/board/",
    )
    with TestClient(app) as c:
        s = c.get("/api/session", headers=SPECTATOR).json()
        assert s["mode"] == "spectator"
        assert s["login_url"] == "/auth/github?return=/board/"


def test_session_login_url_is_null_when_unconfigured(pclient):
    s = pclient.get("/api/session", headers=SPECTATOR).json()
    assert s["login_url"] is None


def test_owner_and_local_session_payloads_unchanged(pclient, client):
    """The new keys are spectator-only; owner and local shapes are untouched."""
    owner = pclient.get("/api/session", headers=OWNER).json()
    assert set(owner) == {"mode", "user"}
    assert client.get("/api/session").json() == {"mode": "local"}


def test_spectator_lands_on_the_demo_board(pclient):
    """A board named Demo wins over the lower-id default board."""
    seed = owner_seed(pclient)
    cluster_id = seed["cluster"]["id"]
    r = pclient.post(f"/api/clusters/{cluster_id}/boards", json={"name": "Demo"}, headers=OWNER)
    assert r.status_code == 200, r.text
    listing = pclient.get(f"/api/clusters/{cluster_id}/boards", headers=OWNER).json()
    demo_id = next(b["id"] for b in listing if b["name"] == "Demo")
    assert demo_id != seed["board"]["id"]

    s = pclient.get("/api/session", headers=SPECTATOR).json()
    assert s["board"] == {"id": demo_id, "name": "Demo"}


def test_spectator_board_falls_back_to_first_board(pclient):
    seed = owner_seed(pclient)
    s = pclient.get("/api/session", headers=SPECTATOR).json()
    assert s["board"]["id"] == seed["board"]["id"]


def test_spectator_board_is_null_without_a_cluster(pclient):
    s = pclient.get("/api/session", headers=SPECTATOR).json()
    assert s["cluster"] is None
    assert s["board"] is None
```

- [ ] **Step 2: Run them to verify they fail**

```bash
cd /c/Users/ryan/Documents/Github/kanban-cloud
.venv/Scripts/python -m pytest tests/test_proxy.py -q
```

Expected: FAIL — `create_app() got an unexpected keyword argument 'proxy_login_url'` and `KeyError: 'login_url'`.

- [ ] **Step 3: Implement**

In `app/main.py`, extend the module docstring env list (after the `PROXY_SHARED_SECRET` line):

```
      PROXY_LOGIN_URL (optional; a site-relative URL the spectator UI offers as
      a "Sign in with GitHub" button — e.g. /auth/github?return=/board/).
```

Change the `create_app` signature and add the config read next to the existing `proxy_secret` lines:

```python
def create_app(
    db_url: str | None = None,
    proxy_secret: str | None = None,
    proxy_login_url: str | None = None,
) -> FastAPI:
```

```python
    # Where the spectator UI points its sign-in button. Site-specific, so it is
    # configuration rather than a hardcoded path: kanban-cloud does not know or
    # care that the proxy in front of it is a portfolio site.
    proxy_login_url = (
        proxy_login_url
        if proxy_login_url is not None
        else os.environ.get("PROXY_LOGIN_URL")
    ) or None
```

Add `default_board` immediately after `default_cluster`:

```python
    def default_board(db: Session, cluster: Cluster | None) -> Board | None:
        """The board a visitor should land on: the demo board when one exists,
        otherwise the cluster's first board."""
        if cluster is None:
            return None
        boards = db.scalars(
            select(Board).where(Board.cluster_id == cluster.id).order_by(Board.id)
        ).all()
        for board in boards:
            if board.name.strip().lower() == "demo":
                return board
        return boards[0] if boards else None
```

Replace the spectator return in `session_info`:

```python
        cluster = default_cluster(db)
        board = default_board(db, cluster)
        return {
            "mode": "spectator",
            "cluster": {"id": cluster.id, "name": cluster.name} if cluster else None,
            "board": {"id": board.id, "name": board.name} if board else None,
            "login_url": proxy_login_url,
        }
```

Update that function's docstring contract line for spectators to:

```
        - {"mode": "spectator", "cluster": {...}|null, "board": {...}|null,
          "login_url": str|null} — read-only; `board` is the board to render
          (the demo board if there is one) and `login_url`, when set, is where
          the sign-in button sends the visitor.
```

- [ ] **Step 4: Run the full suite**

```bash
.venv/Scripts/python -m pytest tests/ -q
```

Expected: PASS — 47 pre-existing plus the 6 new tests.

- [ ] **Step 5: Document the env var**

In `README.md`, in the reverse-proxy section, after the `PROXY_SHARED_SECRET` description, add:

```markdown
`PROXY_LOGIN_URL` (optional) is a site-relative URL the spectator UI turns into
a "Sign in with GitHub" button — for the portfolio deployment,
`/auth/github?return=/board/`. Unset means no button is shown, which is the
right behaviour for a local run where there is no site in front. It is returned
to the browser as `login_url` on `GET /api/session`.
```

Update the `GET /api/session` example block in that same section so the spectator line reads:

```jsonc
{"mode": "spectator", "cluster": {"id": 1, "name": "Main"}, "board": {"id": 2, "name": "Demo"}, "login_url": "/auth/github?return=/board/"}
```

- [ ] **Step 6: Commit**

```bash
git add app/main.py tests/test_proxy.py README.md
git commit -m "feat: report login_url and the spectator landing board from /api/session"
```

---

### Task 3: `kanban-cloud` — seed the demo board

**Files:**
- Create: `kanban-cloud/scripts/seed_demo.py`
- Create: `kanban-cloud/tests/test_seed_demo.py`
- Modify: `kanban-cloud/README.md` (add a "Demo board" subsection to the reverse-proxy section)

**Interfaces:**
- Consumes: `default_board`'s naming rule from Task 2 — the board this script creates must be named `Demo` for spectators to land on it.
- Produces: `scripts/seed_demo.py` exposing `DEMO_TICKETS: list[tuple[str, str, str, str | None, str | None]]`, `class SeedError(RuntimeError)`, `seed(db: Session, board_name: str) -> str`, and `main(argv: list[str] | None = None) -> int`. Nothing else imports these.

- [ ] **Step 1: Write the failing tests**

Create `kanban-cloud/tests/test_seed_demo.py`:

```python
"""The one-off demo-board seeder: idempotence, preconditions, inertness."""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from app.db import make_engine  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import Board, Comment, Ticket, WorkItem  # noqa: E402

import seed_demo  # noqa: E402

SECRET = "test-proxy-secret"
OWNER = {"X-Proxy-Secret": SECRET, "X-Proxy-User": "Ryan"}


@pytest.fixture()
def dsn(tmp_path):
    """A SQLite DSN whose schema exists and whose cluster/owner are provisioned."""
    url = f"sqlite:///{tmp_path / 'seed.db'}"
    app = create_app(url, proxy_secret=SECRET)
    with TestClient(app) as c:
        assert c.get("/api/session", headers=OWNER).json()["mode"] == "owner"
    return url


@pytest.fixture()
def empty_dsn(tmp_path):
    """Schema only — no owner has ever signed in, so there is no cluster."""
    url = f"sqlite:///{tmp_path / 'empty.db'}"
    create_app(url, proxy_secret=SECRET)
    return url


def run(url, board_name="Demo"):
    with Session(make_engine(url)) as db:
        return seed_demo.seed(db, board_name)


def test_seed_creates_the_demo_board(dsn):
    message = run(dsn)
    assert message.startswith("SEED OK")
    with Session(make_engine(dsn)) as db:
        board = db.scalar(select(Board).where(Board.name == "Demo"))
        assert board is not None
        tickets = db.scalars(select(Ticket).where(Ticket.board_id == board.id)).all()
        assert len(tickets) == len(seed_demo.DEMO_TICKETS)
        assert {t.status for t in tickets} == {"todo", "ready", "doing", "review", "done"}
        assert db.scalar(select(func.count()).select_from(Comment)) == sum(
            1 for row in seed_demo.DEMO_TICKETS if row[3]
        )


def test_seed_leaves_nothing_claimable(dsn):
    """Demo tickets never enter the work queue, so no real worker can claim one."""
    run(dsn)
    with Session(make_engine(dsn)) as db:
        assert db.scalar(select(func.count()).select_from(WorkItem)) == 0
        assert all(
            t.assigned_worker is None and t.target_worker is None
            for t in db.scalars(select(Ticket)).all()
        )


def test_seed_is_idempotent(dsn):
    run(dsn)
    with Session(make_engine(dsn)) as db:
        before = db.scalar(select(func.count()).select_from(Ticket))
    message = run(dsn)
    assert message.startswith("already seeded")
    with Session(make_engine(dsn)) as db:
        assert db.scalar(select(func.count()).select_from(Ticket)) == before


def test_seed_refuses_without_a_cluster(empty_dsn):
    with pytest.raises(seed_demo.SeedError) as exc:
        run(empty_dsn)
    assert "sign in to the board once first" in str(exc.value)


def test_main_exit_codes(dsn, empty_dsn, capsys):
    assert seed_demo.main([dsn]) == 0
    assert "SEED OK" in capsys.readouterr().out
    assert seed_demo.main([empty_dsn]) == 2
```

- [ ] **Step 2: Run them to verify they fail**

```bash
.venv/Scripts/python -m pytest tests/test_seed_demo.py -q
```

Expected: FAIL — `ModuleNotFoundError: No module named 'seed_demo'`.

- [ ] **Step 3: Implement the script**

Create `kanban-cloud/scripts/seed_demo.py`:

```python
"""Seed the "Demo" board that anonymous visitors land on.

Usage:  py scripts/seed_demo.py "<dsn>" [--board-name Demo]

Idempotent: if the demo board already holds any ticket, nothing is written, so
tickets deleted by hand stay deleted. Requires the owner to have signed in to
the board at least once — that first request is what creates the cluster.

The content mirrors the animated showcase at site-page/public/kanban/, so the
live board reads in the same voice as the mockup that advertises it.
"""
import argparse
import datetime
import sys
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import make_engine  # noqa: E402
from app.models import (  # noqa: E402
    Board,
    Cluster,
    ClusterMember,
    Comment,
    Ticket,
    User,
    utcnow,
)

# (status, title, body, comment_writer, comment_message)
# Ordered oldest first: the finished work reads as the earliest.
DEMO_TICKETS: list[tuple[str, str, str, str | None, str | None]] = [
    (
        "done",
        "Idempotent kanban server startup",
        "Duplicate server instances piled up and co-bound port 8745, wedging the "
        "API so boards failed to render. Startup now kills stale listeners before "
        "binding.",
        "claude-sonnet",
        "Added a single-instance guard; port 8745 no longer wedges. 262 tests pass.",
    ),
    (
        "done",
        "FLIP slide animation for card moves",
        "Card moves jumped between columns. Reworked them as FLIP transitions so "
        "only transform/opacity animate — smooth and GPU-friendly.",
        "claude-sonnet",
        "Transforms only touch the GPU; slides land at 180ms. Suite green.",
    ),
    (
        "review",
        "Performance tab CPU rollup",
        "Rolled each Claude session's subprocess-tree CPU/memory into a live "
        "5-minute graph on the Performance tab, with a whole-tree kill button.",
        "claude-haiku",
        "Rolled up subprocess CPU over a 5-min window. psutil path ok.",
    ),
    (
        "doing",
        "Enforce orchestrator concurrency cap",
        "The headless loop could dispatch more sub-agents than the configured "
        "cap. Reap now runs before dispatch and stops once the in-flight count "
        "hits the limit.",
        "claude-opus",
        "Cap honoured in orchestrator_core; 3 agents max. Unit tests pass.",
    ),
    (
        "ready",
        "Two-column board settings modal",
        "The board-settings form was a tall single column. Split it into two "
        "columns to cut the modal height on smaller screens.",
        None,
        None,
    ),
    (
        "ready",
        "Docker dev-workspace image",
        "Run each dispatched agent inside a per-repo container. Build a "
        "node:20-slim + Python image with the Claude CLI and pytest; secrets pass "
        "through by name.",
        None,
        None,
    ),
    (
        "todo",
        "Notification bell for blocked tickets",
        "Blocked tickets awaiting a human answer have no entry point. Add a "
        "topbar bell whose badge counts pending questions and hides at zero.",
        None,
        None,
    ),
    (
        "todo",
        "Reduced-motion accessibility pass",
        "Honour prefers-reduced-motion across the board UI so nonessential "
        "animation is disabled for visitors who ask for it.",
        None,
        None,
    ),
]


class SeedError(RuntimeError):
    """A precondition the operator has to fix before seeding can work."""


def seed(db: Session, board_name: str = "Demo") -> str:
    """Create and populate the demo board. Returns a one-line report."""
    cluster = db.scalar(select(Cluster).order_by(Cluster.id).limit(1))
    if cluster is None:
        raise SeedError(
            "no cluster yet — sign in to the board once first; the cluster is "
            "created on the owner's first request"
        )

    owner = db.scalar(
        select(User)
        .join(ClusterMember, ClusterMember.user_id == User.id)
        .where(ClusterMember.cluster_id == cluster.id)
        .order_by(User.id)
        .limit(1)
    )
    if owner is None:
        raise SeedError(
            "cluster has no members — sign in to the board once first; the "
            "owner account is provisioned on that request"
        )

    board = db.scalar(
        select(Board).where(
            Board.cluster_id == cluster.id,
            func.lower(Board.name) == board_name.lower(),
        )
    )
    if board is None:
        board = Board(cluster_id=cluster.id, name=board_name)
        db.add(board)
        db.flush()

    existing = db.scalar(
        select(func.count()).select_from(Ticket).where(Ticket.board_id == board.id)
    )
    if existing:
        return f"already seeded ({existing} tickets) — nothing to do"

    now = utcnow()
    comments = 0
    for index, (status, title, body, writer, message) in enumerate(DEMO_TICKETS):
        # Stagger backwards so the board's history looks organic rather than
        # like eight rows written in the same millisecond.
        created = now - datetime.timedelta(hours=3 * (len(DEMO_TICKETS) - index))
        ticket = Ticket(
            board_id=board.id,
            title=title,
            body=body,
            status=status,
            created_by=owner.id,
            created_at=created,
            updated_at=created,
        )
        db.add(ticket)
        db.flush()
        if writer:
            db.add(
                Comment(
                    ticket_id=ticket.id,
                    writer=writer,
                    message=message,
                    created_at=created + datetime.timedelta(minutes=45),
                )
            )
            comments += 1

    db.commit()
    return (
        f'SEED OK — board {board.id} "{board.name}": '
        f"{len(DEMO_TICKETS)} tickets, {comments} comments"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed the demo kanban board.")
    parser.add_argument("dsn", help="SQLAlchemy DSN, e.g. the Neon connection string")
    parser.add_argument("--board-name", default="Demo")
    args = parser.parse_args(argv)

    with Session(make_engine(args.dsn)) as db:
        try:
            print(seed(db, args.board_name))
        except SeedError as err:
            print(f"error: {err}", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the tests**

```bash
.venv/Scripts/python -m pytest tests/test_seed_demo.py -q
```

Expected: PASS — 5 tests.

- [ ] **Step 5: Run the full suite**

```bash
.venv/Scripts/python -m pytest tests/ -q
```

Expected: PASS — the Task 2 total plus 5.

- [ ] **Step 6: Document it**

Add to `README.md`, in the reverse-proxy section after the `PROXY_LOGIN_URL` paragraph:

```markdown
### Demo board

Anonymous visitors land on a board named `Demo` when one exists (see
`login_url`/`board` on `GET /api/session`), so the public view shows example
work instead of an empty column set. Populate it once, after signing in for
the first time:

    py scripts/seed_demo.py "<neon-dsn>"

The script is idempotent — if the demo board already holds a ticket it writes
nothing, so cards you delete stay deleted. Seeded tickets never enter the work
queue, so an enrolled worker cannot claim one even though two of them sit in
`ready`.
```

- [ ] **Step 7: Commit**

```bash
git add scripts/seed_demo.py tests/test_seed_demo.py README.md
git commit -m "feat: one-off seeder for the public demo board"
```

---

### Task 4: `kanban-cloud` — sign-in button and demo landing in the board UI

**Files:**
- Modify: `kanban-cloud/app/static/index.html` (header markup around line 77; `refreshClusterBits` around line 238; module state around line 162; `boot` around lines 432-448)
- Create: `kanban-cloud/tests/test_frontend_markup.py`

**Interfaces:**
- Consumes: the `login_url` and `board` keys added to the spectator `GET /api/session` payload in Task 2.
- Produces: no server-side interface. `#signInBtn` is the element the manual verification step looks for.

There is no JS test harness in this repo, so the automated coverage here is a markup smoke test that catches typos in the ids the boot code drives; the real check is the manual step.

- [ ] **Step 1: Write the failing test**

Create `kanban-cloud/tests/test_frontend_markup.py`:

```python
"""Cheap guards on the static UI: the ids boot() drives must actually exist.

There is no JS runner in this repo, so these assertions only catch typos and
accidental deletions — behaviour is verified by hand against the deployment.
"""
from pathlib import Path

import pytest

INDEX = Path(__file__).resolve().parents[1] / "app" / "static" / "index.html"


@pytest.fixture(scope="module")
def markup():
    return INDEX.read_text(encoding="utf-8")


def test_sign_in_button_exists_and_starts_hidden(markup):
    assert 'id="signInBtn"' in markup
    assert "Sign in with GitHub" in markup


def test_boot_consumes_the_new_session_keys(markup):
    assert "login_url" in markup
    assert "sessionBoardId" in markup


def test_spectator_note_no_longer_tells_you_to_leave(markup):
    """The button replaced the 'log in via the site' instruction."""
    assert "owner login via site" not in markup
```

- [ ] **Step 2: Run it to verify it fails**

```bash
.venv/Scripts/python -m pytest tests/test_frontend_markup.py -q
```

Expected: FAIL on all three — the button does not exist and the old note text is still present.

- [ ] **Step 3: Add the button to the header**

In `app/static/index.html`, replace the spectator note line in the `<header>`:

```html
    <span id="spectatorNote">viewing read-only</span>
    <button class="small" id="signInBtn" style="display:none">Sign in with GitHub</button>
```

A `<button>` rather than an anchor so it inherits the existing `.small` styling with no new CSS.

- [ ] **Step 4: Track the landing board and drive the button in boot**

Add next to the other module-level state (beside `let sessionMode = "local";`):

```javascript
// Board the server told a spectator to land on (the demo board when one
// exists). Null in owner/local mode, where the first board still wins.
let sessionBoardId = null;
```

In `boot()`, inside the `if (sessionMode === "spectator")` branch, immediately after `document.body.classList.add("spectator");`:

```javascript
    sessionBoardId = s.board ? s.board.id : null;
    if (s.login_url) {
      const signIn = document.getElementById("signInBtn");
      signIn.onclick = function () { location.href = s.login_url; };
      signIn.style.display = "";
    }
```

`s.login_url` is a root-relative site path, so this navigates out of the `/board/` prefix to the site's own OAuth route — deliberately not a `./api/` call.

- [ ] **Step 5: Land on that board**

In `refreshClusterBits`, replace the board-selection line:

```javascript
  currentBoard = boards.find(b => b.id === sessionBoardId) || boards[0] || null;
```

- [ ] **Step 6: Run the tests**

```bash
.venv/Scripts/python -m pytest tests/ -q
```

Expected: PASS — the Task 3 total plus 3.

- [ ] **Step 7: Verify by hand against a local instance**

```bash
.venv/Scripts/python -m pytest tests/ -q   # confirm green first
PROXY_SHARED_SECRET=local-secret PROXY_LOGIN_URL=/auth/github?return=/board/ \
  .venv/Scripts/python -m uvicorn app.main:app --port 8951
```

In a second shell, provision an owner, a Demo board and its tickets, then read the spectator session back:

```bash
curl -s -H "X-Proxy-Secret: local-secret" -H "X-Proxy-User: ryan" http://127.0.0.1:8951/api/session
curl -s -H "X-Proxy-Secret: local-secret" http://127.0.0.1:8951/api/session
```

Expected: the first prints `"mode":"owner"`; the second prints `"mode":"spectator"` with `"login_url":"/auth/github?return=/board/"` and a `"board"` object.

Note that you cannot check this in a browser locally: with `PROXY_SHARED_SECRET` set, every request without the secret header — including the browser's — is 403ed by the gate, and that header is exactly what only the real proxy adds. Whether the button actually renders is verified on the deployment (handoff step 5). Stop the server afterwards.

- [ ] **Step 8: Commit**

```bash
git add app/static/index.html tests/test_frontend_markup.py
git commit -m "feat(ui): sign-in button for spectators and land them on the demo board"
```

---

### Task 5: `kanban-cloud` — record the change in STATUS.md

**Files:**
- Modify: `kanban-cloud/STATUS.md` (new section at the top, above "Worker .exe packaging (2026-08-09)")

**Interfaces:**
- Consumes: the finished behaviour of Tasks 1-4.
- Produces: nothing consumed by code.

- [ ] **Step 1: Write the entry**

Insert directly under the `Last updated:` line (and change that line to `Last updated: 2026-08-19`):

```markdown
## Public board polish (2026-08-19)

Spectators arriving at `https://www.okeefe.work/board/` now get a way in and
something to look at. Spec:
`docs/superpowers/specs/2026-08-19-board-signin-and-demo-board-design.md`.

- **Sign-in button**: new optional env `PROXY_LOGIN_URL` is echoed to the
  browser as `login_url` on `GET /api/session`; the spectator UI turns it into
  a "Sign in with GitHub" button. In `site-page`, `/auth/github?return=<path>`
  carries a validated same-site path through the OAuth state so `/auth/callback`
  returns the browser to `/board/` instead of `/#projects`. Rejected return
  values (`//evil.com`, `/\evil.com`, absolute URLs, anything over 200 chars)
  fall back to the old destination.
- **Demo board**: `GET /api/session` also returns the `board` a spectator should
  land on — the board named `Demo` when one exists, else the first board — and
  `scripts/seed_demo.py "<dsn>"` populates it with eight example tickets and
  four agent comments lifted from the animated `/kanban` showcase. The seeder is
  idempotent and writes no `work_queue` rows, so its two `ready` tickets are
  inert and no enrolled worker can claim them.
- **Not done here**: `site-page` sessions are still an in-memory `Map` (a
  redeploy or idle sleep logs the owner out — deliberate), and the 15s
  `BOARD_UPSTREAM_TIMEOUT_MS` in the proxy is still shorter than the ~32s
  free-tier cold start, so the first visitor after an idle period gets a 502.
- **Operator steps** (not automated): set `PROXY_LOGIN_URL=/auth/github?return=/board/`
  on the Render `kanban-cloud` service, sign in once to create the cluster, then
  run the seeder against the Neon DSN.
```

- [ ] **Step 2: Run the full suite one more time**

```bash
.venv/Scripts/python -m pytest tests/ -q
```

Expected: PASS, 61 tests (47 baseline + 6 from Task 2 + 5 from Task 3 + 3 from Task 4).

- [ ] **Step 3: Commit**

```bash
git add STATUS.md
git commit -m "docs: STATUS - spectator sign-in button and seeded demo board"
```

---

## Handoff to the user

Nothing in this plan pushes or touches production. When the branches are green, the user runs:

1. `git push` on both branches and merges (`site-page` ships from `main`; `kanban-cloud` from `master`).
2. Render → `kanban-cloud` → Environment → add `PROXY_LOGIN_URL=/auth/github?return=/board/`.
3. Sign in at `https://www.okeefe.work/` and open `/board/` once — this creates the cluster and the Main board.
4. `py scripts/seed_demo.py "<neon-dsn>"` — expect `SEED OK — board <id> "Demo": 8 tickets, 4 comments`.
5. Open `/board/` in a private window: the Demo board should be populated and the sign-in button visible.
