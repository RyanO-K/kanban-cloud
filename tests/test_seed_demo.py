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
