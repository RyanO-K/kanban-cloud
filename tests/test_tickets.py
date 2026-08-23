import pytest
from conftest import make_ticket


def test_ticket_crud(client, user, cluster):
    t = make_ticket(client, user, cluster["board_id"], title="First")
    assert t["status"] == "todo" and t["title"] == "First"

    # list
    lst = client.get(f"/api/boards/{cluster['board_id']}/tickets", headers=user["headers"]).json()
    assert [x["id"] for x in lst] == [t["id"]]

    # edit + move
    r = client.patch(f"/api/tickets/{t['id']}", json={"title": "Renamed", "status": "doing"},
                     headers=user["headers"])
    assert r.status_code == 200
    assert r.json()["title"] == "Renamed" and r.json()["status"] == "doing"

    # bad status rejected
    assert client.patch(f"/api/tickets/{t['id']}", json={"status": "bogus"},
                        headers=user["headers"]).status_code == 400

    # comment
    r = client.post(f"/api/tickets/{t['id']}/comments", json={"message": "hi"},
                    headers=user["headers"])
    assert r.json()["comments"][0]["writer"] == user["email"]

    # delete
    assert client.delete(f"/api/tickets/{t['id']}", headers=user["headers"]).json()["ok"] is True
    lst = client.get(f"/api/boards/{cluster['board_id']}/tickets", headers=user["headers"]).json()
    assert lst == []


def test_cluster_scoping_blocks_outsiders(client, user, cluster):
    t = make_ticket(client, user, cluster["board_id"])
    # second user, not a member of the cluster
    r = client.post("/api/register", json={"email": "other@x.com", "password": "pass1234"})
    other = {"Authorization": f"Bearer {r.json()['token']}"}
    assert client.get(f"/api/boards/{cluster['board_id']}/tickets", headers=other).status_code == 403
    assert client.patch(f"/api/tickets/{t['id']}", json={"status": "done"}, headers=other).status_code == 403
    assert client.get(f"/api/clusters/{cluster['id']}/workers", headers=other).status_code == 403

    # joining with the code grants access
    j = client.post("/api/clusters/join", json={"join_code": cluster["join_code"]}, headers=other)
    assert j.status_code == 200
    assert client.get(f"/api/boards/{cluster['board_id']}/tickets", headers=other).status_code == 200


def test_create_ticket_honors_requested_status(client, user, cluster):
    t = make_ticket(client, user, cluster["board_id"], title="Started elsewhere", status="doing")
    assert t["status"] == "doing"

    t2 = make_ticket(client, user, cluster["board_id"], title="Queue me", status="ready")
    assert t2["status"] == "ready"
    q = client.get(f"/api/clusters/{cluster['id']}/queue", headers=user["headers"]).json()
    assert [i["ticket_id"] for i in q] == [t2["id"]]  # only the ready one queued

    r = client.post(f"/api/boards/{cluster['board_id']}/tickets",
                    json={"title": "bad", "status": "bogus"}, headers=user["headers"])
    assert r.status_code == 400


def test_create_ticket_rejects_foreign_target_worker(client, user, cluster):
    from sqlalchemy import text

    from app.models import utcnow

    c2 = client.post("/api/clusters", json={"name": "Other"}, headers=user["headers"]).json()
    engine = client.app.state.engine
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO workers (cluster_id, name, revoked, status, last_seen, created_at) "
            "VALUES (:c, 'other-pc', 0, 'idle', :n, :n)"
        ), {"c": c2["id"], "n": utcnow()})
        foreign_id = conn.execute(
            text("SELECT id FROM workers WHERE cluster_id = :c AND name = 'other-pc'"),
            {"c": c2["id"]},
        ).scalar_one()
    r = client.post(f"/api/boards/{cluster['board_id']}/tickets",
                    json={"title": "x", "target_worker": foreign_id},
                    headers=user["headers"])
    assert r.status_code == 400


# ---------- commit_gate round trip (ticket #15) ----------
# The real write is worker.finish_work's raw SQL (Postgres-only, covered
# against a fake cursor in tests/test_commit_gate.py); simulated here with
# direct SQL against the test DB, same convention test_blocked_endpoint.py
# uses for a worker's raise_question.

def set_commit_gate(client, ticket_id, requirements_met, summary):
    import json

    from sqlalchemy import text

    engine = client.app.state.engine
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE tickets SET commit_gate=:g WHERE id=:t"),
            {"g": json.dumps({"requirements_met": requirements_met, "summary": summary}),
             "t": ticket_id},
        )


def test_ticket_has_no_commit_gate_by_default(client, user, cluster):
    t = make_ticket(client, user, cluster["board_id"])
    assert t["commit_gate"] is None


def test_commit_gate_round_trips_to_the_ticket_api(client, user, cluster):
    t = make_ticket(client, user, cluster["board_id"])
    set_commit_gate(client, t["id"], True, "Ran the suite, all green.")
    listed = client.get(f"/api/boards/{cluster['board_id']}/tickets",
                        headers=user["headers"]).json()
    fresh = [x for x in listed if x["id"] == t["id"]][0]
    assert fresh["commit_gate"] == {"requirements_met": True,
                                    "summary": "Ran the suite, all green."}


def test_commit_gate_round_trips_an_unmet_verdict(client, user, cluster):
    t = make_ticket(client, user, cluster["board_id"])
    set_commit_gate(client, t["id"], False, "Two tests still fail.")
    listed = client.get(f"/api/boards/{cluster['board_id']}/tickets",
                        headers=user["headers"]).json()
    fresh = [x for x in listed if x["id"] == t["id"]][0]
    assert fresh["commit_gate"] == {"requirements_met": False,
                                    "summary": "Two tests still fail."}


