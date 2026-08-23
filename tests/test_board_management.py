"""Board management (ticket #23): mark a board default, and delete a board.

Both are board-scoped settings actions rather than project metadata, so they
live here instead of in test_board_settings.py.
"""
from sqlalchemy import text

from tests.conftest import make_ticket


def other_user(client, email="rival@example.co"):
    """A registered user with a cluster of their own — the 403 counterparty."""
    r = client.post("/api/register", json={"email": email, "password": "pass1234"})
    token = r.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    cluster = client.post("/api/clusters", json={"name": "Theirs"}, headers=headers).json()
    boards = client.get(f"/api/clusters/{cluster['id']}/boards", headers=headers).json()
    return {"headers": headers, "cluster": cluster, "board_id": boards[0]["id"]}


def boards_of(client, user, cluster_id):
    return client.get(f"/api/clusters/{cluster_id}/boards", headers=user["headers"]).json()


def add_board(client, user, cluster_id, name):
    r = client.post(f"/api/clusters/{cluster_id}/boards", json={"name": name},
                    headers=user["headers"])
    assert r.status_code == 200, r.text
    return r.json()


# ---------- mark a board as default ----------

def test_a_board_is_not_default_until_it_is_marked(client, user, cluster):
    assert boards_of(client, user, cluster["id"])[0]["is_default"] is False


def test_create_board_returns_the_default_flag(client, user, cluster):
    assert add_board(client, user, cluster["id"], "second")["is_default"] is False


def test_patch_board_marks_it_default(client, user, cluster):
    r = client.patch(f"/api/boards/{cluster['board_id']}", headers=user["headers"],
                     json={"is_default": True})
    assert r.status_code == 200, r.text
    assert r.json()["is_default"] is True
    assert boards_of(client, user, cluster["id"])[0]["is_default"] is True


def test_marking_a_board_default_unmarks_the_previous_one(client, user, cluster):
    """At most one default per cluster — otherwise "the" default is ambiguous."""
    second = add_board(client, user, cluster["id"], "second")
    client.patch(f"/api/boards/{cluster['board_id']}", headers=user["headers"],
                 json={"is_default": True})
    client.patch(f"/api/boards/{second['id']}", headers=user["headers"],
                 json={"is_default": True})
    marked = [b["id"] for b in boards_of(client, user, cluster["id"]) if b["is_default"]]
    assert marked == [second["id"]]


def test_marking_a_board_default_leaves_another_cluster_alone(client, user, cluster):
    """The "only one" rule is per cluster, not per instance."""
    theirs = other_user(client)
    client.patch(f"/api/boards/{theirs['board_id']}", headers=theirs["headers"],
                 json={"is_default": True})
    client.patch(f"/api/boards/{cluster['board_id']}", headers=user["headers"],
                 json={"is_default": True})
    assert boards_of(client, theirs, theirs["cluster"]["id"])[0]["is_default"] is True


def test_patch_board_can_unmark_the_default(client, user, cluster):
    """No board marked is a legal state — it falls back to the old rule."""
    bid = cluster["board_id"]
    client.patch(f"/api/boards/{bid}", headers=user["headers"], json={"is_default": True})
    r = client.patch(f"/api/boards/{bid}", headers=user["headers"],
                     json={"is_default": False})
    assert r.json()["is_default"] is False
    assert not any(b["is_default"] for b in boards_of(client, user, cluster["id"]))


def test_is_default_is_partial_like_the_other_fields(client, user, cluster):
    """Saving the Project panel must not silently unmark the default board."""
    bid = cluster["board_id"]
    client.patch(f"/api/boards/{bid}", headers=user["headers"], json={"is_default": True})
    client.patch(f"/api/boards/{bid}", headers=user["headers"],
                 json={"description": "unrelated edit"})
    assert boards_of(client, user, cluster["id"])[0]["is_default"] is True


def test_marking_a_board_in_another_cluster_default_is_forbidden(client, user, cluster):
    theirs = other_user(client)
    r = client.patch(f"/api/boards/{theirs['board_id']}", headers=user["headers"],
                     json={"is_default": True})
    assert r.status_code == 403


# ---------- delete a board ----------

def test_delete_board_removes_it_from_the_listing(client, user, cluster):
    second = add_board(client, user, cluster["id"], "second")
    r = client.delete(f"/api/boards/{second['id']}", headers=user["headers"])
    assert r.status_code == 200, r.text
    assert [b["id"] for b in boards_of(client, user, cluster["id"])] == [cluster["board_id"]]


def test_deleted_board_404s_afterwards(client, user, cluster):
    second = add_board(client, user, cluster["id"], "second")
    client.delete(f"/api/boards/{second['id']}", headers=user["headers"])
    assert client.get(f"/api/boards/{second['id']}",
                      headers=user["headers"]).status_code == 404


def test_delete_board_takes_its_tickets_with_it(client, user, cluster):
    second = add_board(client, user, cluster["id"], "second")
    doomed = make_ticket(client, user, second["id"], title="goes away")
    kept = make_ticket(client, user, cluster["board_id"], title="stays")
    client.delete(f"/api/boards/{second['id']}", headers=user["headers"])
    # /log resolves the ticket first, so it 404s once the ticket is gone.
    assert client.get(f"/api/tickets/{doomed['id']}/log",
                      headers=user["headers"]).status_code == 404
    still = client.get(f"/api/boards/{cluster['board_id']}/tickets",
                       headers=user["headers"]).json()
    assert [t["id"] for t in still] == [kept["id"]]


def test_delete_board_clears_every_row_hanging_off_its_tickets(client, user, cluster):
    """Comments, queue rows, deps, questions, chat and log all key on a ticket
    id: leaving any behind orphans rows Postgres' foreign keys would refuse."""
    second = add_board(client, user, cluster["id"], "second")
    ticket = make_ticket(client, user, second["id"])
    dep = make_ticket(client, user, second["id"], title="dependency")
    client.post(f"/api/tickets/{ticket['id']}/comments", headers=user["headers"],
                json={"message": "a note"})
    client.patch(f"/api/tickets/{ticket['id']}", headers=user["headers"],
                 json={"depends_on": [dep["id"]]})
    client.patch(f"/api/tickets/{ticket['id']}", headers=user["headers"],
                 json={"status": "ready"})  # queues a work_queue row

    engine = client.app.state.engine
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO ticket_questions (ticket_id, question, type, multi, created_at)"
            " VALUES (:t, 'why?', 'input', 0, '2030-01-01')"
        ), {"t": ticket["id"]})
        conn.execute(text(
            "INSERT INTO ticket_chat (ticket_id, sender, message, created_at)"
            " VALUES (:t, 'ryan', 'hi', '2030-01-01')"
        ), {"t": ticket["id"]})
        conn.execute(text(
            "INSERT INTO ticket_log (ticket_id, seq, role, text, created_at)"
            " VALUES (:t, 1, 'assistant', 'thinking', '2030-01-01')"
        ), {"t": ticket["id"]})

    r = client.delete(f"/api/boards/{second['id']}", headers=user["headers"])
    assert r.status_code == 200, r.text

    with engine.begin() as conn:
        for table in ("comments", "work_queue", "ticket_deps", "ticket_questions",
                      "ticket_chat", "ticket_log", "tickets"):
            left = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
            assert left == 0, f"{table} still has {left} row(s)"


def test_delete_board_leaves_another_boards_tickets_alone(client, user, cluster):
    second = add_board(client, user, cluster["id"], "second")
    kept = make_ticket(client, user, cluster["board_id"])
    client.post(f"/api/tickets/{kept['id']}/comments", headers=user["headers"],
                json={"message": "still here"})
    client.delete(f"/api/boards/{second['id']}", headers=user["headers"])
    still = client.get(f"/api/boards/{cluster['board_id']}/tickets",
                       headers=user["headers"]).json()
    assert [c["message"] for c in still[0]["comments"]] == ["still here"]


def test_delete_board_in_another_cluster_is_forbidden(client, user, cluster):
    theirs = other_user(client)
    r = client.delete(f"/api/boards/{theirs['board_id']}", headers=user["headers"])
    assert r.status_code == 403
    assert boards_of(client, theirs, theirs["cluster"]["id"])


def test_delete_unknown_board_404s(client, user):
    assert client.delete("/api/boards/99999",
                         headers=user["headers"]).status_code == 404


def test_deleting_the_last_board_is_allowed(client, user, cluster):
    """The picker always offers "+ new board", so an empty cluster is
    recoverable — no need to refuse."""
    r = client.delete(f"/api/boards/{cluster['board_id']}", headers=user["headers"])
    assert r.status_code == 200, r.text
    assert boards_of(client, user, cluster["id"]) == []
