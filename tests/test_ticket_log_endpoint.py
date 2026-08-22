"""GET /api/tickets/{id}/log and GET/POST /api/tickets/{id}/chat: the
server-side half of the live agent view. The worker's actual writes
(add_log_line, ticket_chat delivery) are Postgres-only and covered against a
fake cursor in tests/test_ticket_log.py and tests/test_chat.py; here rows are
inserted with plain SQL (same convention as tests/test_blocked_endpoint.py's
block_ticket) to exercise the read/write API surface a browser actually hits.
"""
from sqlalchemy import text

from tests.conftest import make_ticket
from tests.test_claim import mark_claimed, queue_rows


def insert_log_line(client, ticket_id, work_queue_id, seq, role, text_val):
    engine = client.app.state.engine
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO ticket_log (ticket_id, work_queue_id, seq, role, text, created_at) "
            "VALUES (:t, :w, :s, :r, :x, :c)"
        ), {"t": ticket_id, "w": work_queue_id, "s": seq, "r": role, "x": text_val,
            "c": "2026-08-22 00:00:00"})


def test_log_endpoint_returns_lines_in_seq_order(client, user, cluster):
    t = make_ticket(client, user, cluster["board_id"], status="ready")
    item = queue_rows(client, user, cluster["id"])[0]
    mark_claimed(client, item["id"])
    insert_log_line(client, t["id"], item["id"], 2, "assistant", "second")
    insert_log_line(client, t["id"], item["id"], 1, "assistant", "first")

    r = client.get(f"/api/tickets/{t['id']}/log", headers=user["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert [l["text"] for l in body["lines"]] == ["first", "second"]


def test_log_endpoint_since_seq_only_returns_newer_lines(client, user, cluster):
    t = make_ticket(client, user, cluster["board_id"], status="ready")
    item = queue_rows(client, user, cluster["id"])[0]
    mark_claimed(client, item["id"])
    insert_log_line(client, t["id"], item["id"], 1, "assistant", "old")
    insert_log_line(client, t["id"], item["id"], 2, "assistant", "new")

    r = client.get(f"/api/tickets/{t['id']}/log?since_seq=1", headers=user["headers"])
    lines = r.json()["lines"]
    assert [l["text"] for l in lines] == ["new"]


def test_log_endpoint_only_shows_the_latest_attempt(client, user, cluster):
    """A retried ticket gets a fresh work_queue row; the log endpoint follows
    the latest attempt, not the first one."""
    t = make_ticket(client, user, cluster["board_id"], status="ready")
    first_item = queue_rows(client, user, cluster["id"])[0]
    insert_log_line(client, t["id"], first_item["id"], 1, "assistant", "attempt one")

    engine = client.app.state.engine
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO work_queue (ticket_id, cluster_id, status, queued_at) "
            "VALUES (:t, :c, 'queued', '2026-08-22 00:00:00')"
        ), {"t": t["id"], "c": cluster["id"]})
    second_item = [row for row in queue_rows(client, user, cluster["id"])
                  if row["id"] != first_item["id"]][0]
    insert_log_line(client, t["id"], second_item["id"], 1, "assistant", "attempt two")

    r = client.get(f"/api/tickets/{t['id']}/log", headers=user["headers"])
    assert [l["text"] for l in r.json()["lines"]] == ["attempt two"]


def test_log_endpoint_reports_running_only_while_doing(client, user, cluster):
    t = make_ticket(client, user, cluster["board_id"])
    r = client.get(f"/api/tickets/{t['id']}/log", headers=user["headers"])
    assert r.json()["running"] is False
    assert r.json()["status"] == "todo"

    client.patch(f"/api/tickets/{t['id']}", json={"status": "doing"}, headers=user["headers"])
    r = client.get(f"/api/tickets/{t['id']}/log", headers=user["headers"])
    assert r.json()["running"] is True


def test_log_endpoint_requires_cluster_membership(client, user, cluster):
    t = make_ticket(client, user, cluster["board_id"])
    other = client.post("/api/register",
                        json={"email": "other@example.com", "password": "pass1234"}).json()
    headers = {"Authorization": f"Bearer {other['token']}"}
    r = client.get(f"/api/tickets/{t['id']}/log", headers=headers)
    assert r.status_code == 403


# ---------- chat ----------

def test_send_and_list_chat(client, user, cluster):
    t = make_ticket(client, user, cluster["board_id"], status="ready")
    client.patch(f"/api/tickets/{t['id']}", json={"status": "doing"}, headers=user["headers"])

    r = client.post(f"/api/tickets/{t['id']}/chat", json={"message": "use option B"},
                    headers=user["headers"])
    assert r.status_code == 200, r.text
    posted = r.json()
    assert posted["sender"] == user["email"]
    assert posted["delivered"] is False

    listed = client.get(f"/api/tickets/{t['id']}/chat", headers=user["headers"]).json()
    assert len(listed) == 1
    assert listed[0]["message"] == "use option B"


def test_chat_rejects_a_finished_ticket(client, user, cluster):
    t = make_ticket(client, user, cluster["board_id"], status="ready")
    client.patch(f"/api/tickets/{t['id']}", json={"status": "done"}, headers=user["headers"])
    r = client.post(f"/api/tickets/{t['id']}/chat", json={"message": "hi"},
                    headers=user["headers"])
    assert r.status_code == 409


def test_chat_rejects_an_empty_message(client, user, cluster):
    t = make_ticket(client, user, cluster["board_id"])
    r = client.post(f"/api/tickets/{t['id']}/chat", json={"message": "   "},
                    headers=user["headers"])
    assert r.status_code == 400


def test_chat_shows_delivered_once_the_worker_marks_it(client, user, cluster):
    t = make_ticket(client, user, cluster["board_id"], status="ready")
    client.post(f"/api/tickets/{t['id']}/chat", json={"message": "hi"}, headers=user["headers"])
    chat_id = client.get(f"/api/tickets/{t['id']}/chat", headers=user["headers"]).json()[0]["id"]

    engine = client.app.state.engine
    with engine.begin() as conn:
        conn.execute(text("UPDATE ticket_chat SET delivered_at='2026-08-22 00:00:00' WHERE id=:i"),
                    {"i": chat_id})

    listed = client.get(f"/api/tickets/{t['id']}/chat", headers=user["headers"]).json()
    assert listed[0]["delivered"] is True


def test_chat_requires_cluster_membership(client, user, cluster):
    t = make_ticket(client, user, cluster["board_id"])
    other = client.post("/api/register",
                        json={"email": "other@example.com", "password": "pass1234"}).json()
    headers = {"Authorization": f"Bearer {other['token']}"}
    r = client.post(f"/api/tickets/{t['id']}/chat", json={"message": "hi"}, headers=headers)
    assert r.status_code == 403
