"""Enqueue-side delegation semantics (v2: claiming itself moved into worker.py SQL).

A 'claimed' state is simulated with a direct UPDATE against the test DB —
exactly what a v2 worker does, minus the Postgres-only SKIP LOCKED wrapper.
"""
from sqlalchemy import text

from tests.conftest import make_ticket


def queue_rows(client, user, cluster_id):
    return client.get(f"/api/clusters/{cluster_id}/queue", headers=user["headers"]).json()


def mark_claimed(client, item_id, worker_id=999):
    engine = client.app.state.engine
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE work_queue SET status='claimed', claimed_by=:w WHERE id=:i"
        ), {"w": worker_id, "i": item_id})


def test_ready_status_enqueues(client, user, cluster):
    t = make_ticket(client, user, cluster["board_id"], status="ready")
    rows = queue_rows(client, user, cluster["id"])
    assert len(rows) == 1
    assert rows[0]["ticket_id"] == t["id"]
    assert rows[0]["status"] == "queued"


def test_enqueue_is_idempotent(client, user, cluster):
    t = make_ticket(client, user, cluster["board_id"], status="ready")
    client.post(f"/api/tickets/{t['id']}/run", headers=user["headers"])
    assert len(queue_rows(client, user, cluster["id"])) == 1


def test_reenqueue_supersedes_orphaned_claim(client, user, cluster):
    t = make_ticket(client, user, cluster["board_id"], status="ready")
    item = queue_rows(client, user, cluster["id"])[0]
    mark_claimed(client, item["id"])  # worker dies mid-ticket
    client.post(f"/api/tickets/{t['id']}/run", headers=user["headers"])
    rows = queue_rows(client, user, cluster["id"])
    by_id = {r["id"]: r for r in rows}
    assert by_id[item["id"]]["status"] == "failed"  # superseded
    assert sum(1 for r in rows if r["status"] == "queued") == 1


def test_rerun_resets_attempts(client, user, cluster):
    t = make_ticket(client, user, cluster["board_id"], status="ready")
    engine = client.app.state.engine
    with engine.begin() as conn:
        conn.execute(text("UPDATE tickets SET attempts=2, status='failed' WHERE id=:i"),
                     {"i": t["id"]})
    client.post(f"/api/tickets/{t['id']}/run", headers=user["headers"])
    r = client.get(f"/api/boards/{cluster['board_id']}/tickets", headers=user["headers"])
    fresh = [x for x in r.json() if x["id"] == t["id"]][0]
    assert fresh["attempts"] == 0
    assert fresh["status"] == "ready"
