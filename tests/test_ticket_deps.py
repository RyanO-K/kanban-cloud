"""Ticket dependency graph: `depends_on`/`blocks` derivation, `blocked` display,
cluster scoping, and cycle prevention. The actual claim-time gate lives in
worker.CLAIM_SQL (Postgres-only) — see tests/test_worker.py for that half.
"""
from tests.conftest import make_ticket


def board_tickets(client, user, board_id):
    return client.get(f"/api/boards/{board_id}/tickets", headers=user["headers"]).json()


def by_id(rows, ticket_id):
    return next(t for t in rows if t["id"] == ticket_id)


def test_unmet_dependency_reports_blocked_and_the_reverse_edge(client, user, cluster):
    a = make_ticket(client, user, cluster["board_id"], title="A")
    b = make_ticket(client, user, cluster["board_id"], title="B")

    r = client.patch(f"/api/tickets/{a['id']}", json={"depends_on": [b["id"]]},
                      headers=user["headers"])
    assert r.status_code == 200, r.text
    updated = r.json()
    assert updated["depends_on"] == [b["id"]]
    assert updated["blocked"] is True

    rows = board_tickets(client, user, cluster["board_id"])
    assert by_id(rows, b["id"])["blocks"] == [a["id"]]


def test_dependency_reaching_review_unblocks(client, user, cluster):
    a = make_ticket(client, user, cluster["board_id"], title="A")
    b = make_ticket(client, user, cluster["board_id"], title="B")
    client.patch(f"/api/tickets/{a['id']}", json={"depends_on": [b["id"]]},
                 headers=user["headers"])

    client.patch(f"/api/tickets/{b['id']}", json={"status": "review"}, headers=user["headers"])

    rows = board_tickets(client, user, cluster["board_id"])
    assert by_id(rows, a["id"])["blocked"] is False


def test_dependency_reaching_done_unblocks(client, user, cluster):
    a = make_ticket(client, user, cluster["board_id"], title="A")
    b = make_ticket(client, user, cluster["board_id"], title="B")
    client.patch(f"/api/tickets/{a['id']}", json={"depends_on": [b["id"]]},
                 headers=user["headers"])

    client.patch(f"/api/tickets/{b['id']}", json={"status": "done"}, headers=user["headers"])

    rows = board_tickets(client, user, cluster["board_id"])
    assert by_id(rows, a["id"])["blocked"] is False


def test_clearing_deps_removes_block_and_reverse_edge(client, user, cluster):
    a = make_ticket(client, user, cluster["board_id"], title="A")
    b = make_ticket(client, user, cluster["board_id"], title="B")
    client.patch(f"/api/tickets/{a['id']}", json={"depends_on": [b["id"]]},
                 headers=user["headers"])

    r = client.patch(f"/api/tickets/{a['id']}", json={"depends_on": []}, headers=user["headers"])
    assert r.json()["depends_on"] == []
    assert r.json()["blocked"] is False

    rows = board_tickets(client, user, cluster["board_id"])
    assert by_id(rows, b["id"])["blocks"] == []


def test_self_dependency_rejected(client, user, cluster):
    a = make_ticket(client, user, cluster["board_id"], title="A")
    r = client.patch(f"/api/tickets/{a['id']}", json={"depends_on": [a["id"]]},
                      headers=user["headers"])
    assert r.status_code == 400


def test_dependency_cycle_rejected(client, user, cluster):
    a = make_ticket(client, user, cluster["board_id"], title="A")
    b = make_ticket(client, user, cluster["board_id"], title="B")
    c = make_ticket(client, user, cluster["board_id"], title="C")
    assert client.patch(f"/api/tickets/{a['id']}", json={"depends_on": [b["id"]]},
                         headers=user["headers"]).status_code == 200
    assert client.patch(f"/api/tickets/{b['id']}", json={"depends_on": [c["id"]]},
                         headers=user["headers"]).status_code == 200

    r = client.patch(f"/api/tickets/{c['id']}", json={"depends_on": [a["id"]]},
                      headers=user["headers"])
    assert r.status_code == 400
    assert "cycle" in r.json()["detail"]

    # The rejected save must not have partially written the edge.
    rows = board_tickets(client, user, cluster["board_id"])
    assert by_id(rows, c["id"])["depends_on"] == []


def test_unknown_dependency_id_rejected(client, user, cluster):
    a = make_ticket(client, user, cluster["board_id"], title="A")
    r = client.patch(f"/api/tickets/{a['id']}", json={"depends_on": [999999]},
                      headers=user["headers"])
    assert r.status_code == 400


def test_cross_cluster_dependency_rejected(client, user, cluster):
    a = make_ticket(client, user, cluster["board_id"], title="A")
    other = client.post("/api/clusters", json={"name": "Other"},
                         headers=user["headers"]).json()
    other_boards = client.get(f"/api/clusters/{other['id']}/boards",
                               headers=user["headers"]).json()
    outsider = make_ticket(client, user, other_boards[0]["id"], title="Outsider")

    r = client.patch(f"/api/tickets/{a['id']}", json={"depends_on": [outsider["id"]]},
                      headers=user["headers"])
    assert r.status_code == 400


def test_deleting_a_dependency_ticket_clears_the_edge(client, user, cluster):
    a = make_ticket(client, user, cluster["board_id"], title="A")
    b = make_ticket(client, user, cluster["board_id"], title="B")
    client.patch(f"/api/tickets/{a['id']}", json={"depends_on": [b["id"]]},
                 headers=user["headers"])

    r = client.delete(f"/api/tickets/{b['id']}", headers=user["headers"])
    assert r.status_code == 200

    rows = board_tickets(client, user, cluster["board_id"])
    assert by_id(rows, a["id"])["depends_on"] == []
    assert by_id(rows, a["id"])["blocked"] is False
