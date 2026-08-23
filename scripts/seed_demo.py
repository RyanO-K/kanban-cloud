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
    # The Blocked column's showcase card: a run that finished but never landed
    # its branch, which is what stops a ticket short of done.
    (
        "blocked",
        "Performance tab CPU rollup",
        "Rolled each Claude session's subprocess-tree CPU/memory into a live "
        "5-minute graph on the Performance tab, with a whole-tree kill button.",
        "claude-haiku",
        "Rolled up subprocess CPU over a 5-min window. psutil path ok.\n\n"
        "(Not pushed: auto-push is off for this board, so the branch is only "
        "on the worker PC. Push it by hand to close the ticket.)",
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
            "no cluster yet - sign in to the board once first; the cluster is "
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
            "cluster has no members - sign in to the board once first; the "
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
        return f"already seeded ({existing} tickets) - nothing to do"

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
        f'SEED OK - board {board.id} "{board.name}": '
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
