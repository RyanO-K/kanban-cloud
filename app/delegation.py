"""Delegation core: enqueue, atomic claim, and result handling.

Kept free of FastAPI so the claim/assignment logic is directly unit-testable.
"""
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from .models import (
    AGENT_READY_STATUS,
    MAX_ATTEMPTS,
    Board,
    ClusterSettings,
    Comment,
    Ticket,
    WorkItem,
    Worker,
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
    if existing is not None:
        if existing.status == "queued":
            return None
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


def claim_next(db: Session, worker: Worker) -> dict | None:
    """Atomically claim the oldest eligible queued work item for this worker.

    Eligibility: item is queued, in the worker's cluster, and the ticket's
    target_worker is NULL ("any") or equals this worker.

    Double-claim guard: the claim is a transactional
    ``UPDATE work_queue SET status='claimed' ... WHERE id=? AND status='queued'``;
    a rowcount of 0 means another worker won the race and we try the next
    candidate.
    """
    worker.last_seen = utcnow()

    candidates = db.execute(
        select(WorkItem.id, WorkItem.ticket_id)
        .join(Ticket, Ticket.id == WorkItem.ticket_id)
        .where(
            WorkItem.status == "queued",
            WorkItem.cluster_id == worker.cluster_id,
            or_(Ticket.target_worker.is_(None), Ticket.target_worker == worker.id),
        )
        .order_by(WorkItem.queued_at.asc(), WorkItem.id.asc())
    ).all()

    for item_id, ticket_id in candidates:
        res = db.execute(
            update(WorkItem)
            .where(WorkItem.id == item_id, WorkItem.status == "queued")
            .values(status="claimed", claimed_by=worker.id, claimed_at=utcnow())
        )
        if res.rowcount != 1:
            continue  # lost the race for this item
        ticket = db.get(Ticket, ticket_id)
        ticket.status = "doing"
        ticket.assigned_worker = worker.id
        ticket.attempts = (ticket.attempts or 0) + 1
        worker.status = "working"
        settings = db.get(ClusterSettings, worker.cluster_id)
        api_key = settings.claude_api_key if settings else None
        db.commit()
        return {
            "assignment_id": item_id,
            "claude_api_key": api_key,  # delivered ONLY to the claiming worker
            "ticket": {
                "id": ticket.id,
                "board_id": ticket.board_id,
                "title": ticket.title,
                "body": ticket.body,
                "status": ticket.status,
                "attempts": ticket.attempts,
            },
        }

    worker.status = "idle"
    db.commit()
    return None


def finish_work(
    db: Session, worker: Worker, item: WorkItem, ok: bool, comment: str | None
) -> dict:
    """Record a work result. Success -> ticket to review; failure -> requeue
    until MAX_ATTEMPTS, then failed."""
    ticket = db.get(Ticket, item.ticket_id)
    now = utcnow()
    item.finished_at = now
    item.result = (comment or "")[:10000]
    worker.status = "idle"
    worker.last_seen = now

    if comment:
        db.add(Comment(ticket_id=ticket.id, writer=f"worker:{worker.name}", message=comment))

    if ok:
        item.status = "done"
        ticket.status = "review"
    else:
        item.status = "failed"
        if (ticket.attempts or 0) < MAX_ATTEMPTS:
            # back to queue for another try
            db.add(WorkItem(ticket_id=ticket.id, cluster_id=item.cluster_id, status="queued"))
            ticket.status = AGENT_READY_STATUS
            ticket.assigned_worker = None
        else:
            ticket.status = "failed"
    db.commit()
    return {"ticket_status": ticket.status, "item_status": item.status}
