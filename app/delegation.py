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
    or "Run now"). Idempotent on queue membership: if a *queued* item already
    exists, no duplicate is created, but the ticket is resynced to ready with
    a fresh attempt budget (covers a ticket row that drifted out of sync with
    its still-outstanding queue item). If an outstanding *claimed* item exists
    (e.g. the worker died mid-ticket), that claim is superseded so the ticket
    can be delegated again; a late result for the superseded assignment is
    discarded by the worker's own claim rowcount guard (v2: worker.py,
    ``UPDATE work_queue SET status='done'/'failed' WHERE id=... AND
    status='claimed' AND claimed_by=...`` — a rowcount of 0 means the claim
    was superseded and the result is dropped).
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
    # A fresh, user-initiated delegation gets a full retry budget. (In v2 the
    # failure-requeue path lives in worker.py's finish_work, which creates its
    # retry WorkItem directly via SQL and therefore keeps the cumulative
    # attempt count instead of resetting it here.)
    ticket.attempts = 0
    db.add(item)
    db.commit()
    return item
