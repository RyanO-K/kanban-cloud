"""POST /api/tickets/{id}/kill: flip work_queue.kill_requested for a ticket's
active claim so the worker holding it notices and stops. The actual
termination and the distinct 'killed' status are worker-side (worker.py);
this covers the API's part of the contract, including the no-op case."""
from sqlalchemy import text

from tests.conftest import make_ticket
from tests.test_claim import mark_claimed, queue_rows


def test_kill_sets_kill_requested_on_the_claimed_item(client, user, cluster):
    t = make_ticket(client, user, cluster["board_id"], status="ready")
    item = queue_rows(client, user, cluster["id"])[0]
    mark_claimed(client, item["id"])

    r = client.post(f"/api/tickets/{t['id']}/kill", headers=user["headers"])
    assert r.status_code == 200, r.text

    rows = queue_rows(client, user, cluster["id"])
    assert [row["kill_requested"] for row in rows if row["id"] == item["id"]] == [True]


def test_kill_with_no_active_claim_is_a_no_op(client, user, cluster):
    """A kill arriving after the agent already finished (or before it ever
    started) must not error or fabricate a claim — just do nothing."""
    t = make_ticket(client, user, cluster["board_id"])  # never queued at all

    r = client.post(f"/api/tickets/{t['id']}/kill", headers=user["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["status"] == t["status"]
    assert queue_rows(client, user, cluster["id"]) == []


def test_kill_ignores_an_already_finished_claim(client, user, cluster):
    """Same no-op guarantee once the claim has moved past 'claimed'."""
    t = make_ticket(client, user, cluster["board_id"], status="ready")
    item = queue_rows(client, user, cluster["id"])[0]
    engine = client.app.state.engine
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE work_queue SET status='done', claimed_by=999 WHERE id=:i"
        ), {"i": item["id"]})

    r = client.post(f"/api/tickets/{t['id']}/kill", headers=user["headers"])
    assert r.status_code == 200, r.text

    rows = queue_rows(client, user, cluster["id"])
    assert [row["kill_requested"] for row in rows if row["id"] == item["id"]] == [False]


def test_kill_requires_cluster_membership(client, user, cluster):
    t = make_ticket(client, user, cluster["board_id"])
    other = client.post("/api/register",
                        json={"email": "other@example.com", "password": "pass1234"}).json()
    headers = {"Authorization": f"Bearer {other['token']}"}
    r = client.post(f"/api/tickets/{t['id']}/kill", headers=headers)
    assert r.status_code == 403
