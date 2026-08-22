"""tickets.order lets a human rank a ticket ahead of others regardless of when
it was queued. CLAIM_SQL itself is Postgres-only (SKIP LOCKED, ::int[] casts)
and can't run against the SQLite test DB (see test_worker_paths.py), so these
tests run worker.CLAIM_ORDER_BY -- the exact fragment CLAIM_SQL uses -- as a
plain SELECT against real queued rows, proving the two can't drift apart.
"""
import sys
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import worker  # noqa: E402

from tests.conftest import make_ticket


def set_order(client, ticket_id, order):
    engine = client.app.state.engine
    with engine.begin() as conn:
        conn.execute(text('UPDATE tickets SET "order"=:o WHERE id=:i'),
                     {"o": order, "i": ticket_id})


def claim_order(client, cluster_id):
    """The ticket_ids CLAIM_SQL would hand out, in the order it would hand
    them out, using the real ORDER BY fragment worker.py claims with."""
    engine = client.app.state.engine
    with engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT wq.ticket_id FROM work_queue wq "
            "JOIN tickets t ON t.id = wq.ticket_id "
            f"WHERE wq.status='queued' AND wq.cluster_id=:cid "
            f"ORDER BY {worker.CLAIM_ORDER_BY}"
        ), {"cid": cluster_id}).all()
    return [r[0] for r in rows]


def test_lower_order_is_claimed_first_despite_being_queued_later(client, user, cluster):
    a = make_ticket(client, user, cluster["board_id"], title="A", status="ready")
    b = make_ticket(client, user, cluster["board_id"], title="B", status="ready")
    assert claim_order(client, cluster["id"]) == [a["id"], b["id"]]  # FIFO by default

    set_order(client, b["id"], -1)  # rank B ahead despite arriving second
    assert claim_order(client, cluster["id"]) == [b["id"], a["id"]]


def test_ties_fall_back_to_queued_at(client, user, cluster):
    a = make_ticket(client, user, cluster["board_id"], title="A", status="ready")
    b = make_ticket(client, user, cluster["board_id"], title="B", status="ready")
    # Both default to order=0: earlier queued_at (A, queued first) must win.
    assert claim_order(client, cluster["id"]) == [a["id"], b["id"]]


def test_new_ticket_defaults_to_order_zero(client, user, cluster):
    t = make_ticket(client, user, cluster["board_id"])
    assert t["order"] == 0


def test_reorder_endpoint_persists_rank_within_a_column(client, user, cluster):
    a = make_ticket(client, user, cluster["board_id"], title="A", status="ready")
    b = make_ticket(client, user, cluster["board_id"], title="B", status="ready")

    r = client.patch(f"/api/boards/{cluster['board_id']}/reorder",
                      json={"status": "ready", "ticket_ids": [b["id"], a["id"]]},
                      headers=user["headers"])
    assert r.status_code == 200

    lst = client.get(f"/api/boards/{cluster['board_id']}/tickets", headers=user["headers"]).json()
    by_id = {x["id"]: x for x in lst}
    assert by_id[b["id"]]["order"] < by_id[a["id"]]["order"]
    assert claim_order(client, cluster["id"]) == [b["id"], a["id"]]


def test_reorder_rejects_a_mismatched_ticket_set(client, user, cluster):
    a = make_ticket(client, user, cluster["board_id"], title="A", status="ready")
    r = client.patch(f"/api/boards/{cluster['board_id']}/reorder",
                      json={"status": "ready", "ticket_ids": [a["id"], 9999]},
                      headers=user["headers"])
    assert r.status_code == 400
