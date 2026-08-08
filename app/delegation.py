"""Enqueue-side delegation. Claiming and result handling moved into worker.py
(direct SQL) in v2.
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    AGENT_READY_STATUS,
    Board,
    Ticket,
    WorkItem,
    utcnow,
)


def enqueue_ticket(db: Session, ticket: Ticket) -> WorkItem | None:
    """Queue a ticket for agent execution (explicit user intent: move to ready
    or "Run now"). No-op if already queued. If an outstanding *claimed* item
    exists (e.g. the worker died mid-ticket), the claim is superseded so the
    ticket can be delegated again; a late result for the old assignment is
    rejected with 409 by the result endpoint.
    """
    existing = db.scalar(
        select(WorkItem).where(
            WorkItem.ticket_id == ticket.id,
            WorkItem.status.in_(["queued", "claimed"]),
        )
    )
    if existing is not None and existing.status == "queued":
        # Already queued: don't duplicate the WorkItem, but resync the ticket
        # record (a "Run now" can follow direct edits that left the ticket's
        # status/attempts stale relative to the still-outstanding queue item).
        ticket.status = AGENT_READY_STATUS
        ticket.assigned_worker = None
        ticket.attempts = 0
        db.commit()
        return None
    if existing is not None:
        # claimed: supersede the (possibly dead) claim.
        existing.status = "failed"
        existing.finished_at = utcnow()
        existing.result = "superseded: ticket re-queued while claim outstanding"
    board = db.get(Board, ticket.board_id)
    item = WorkItem(ticket_id=ticket.id, cluster_id=board.cluster_id, status="queued")
    ticket.status = AGENT_READY_STATUS
    ticket.assigned_worker = None
    # A fresh, user-initiated delegation gets a full retry budget. (The
    # internal failure-requeue path in finish_work creates its WorkItem
    # directly and therefore keeps the cumulative attempt count.)
    ticket.attempts = 0
    db.add(item)
    db.commit()
    return item
