"""Phase 4: blocked status + agent questions, server-side HTTP surface.

A worker raising a question is simulated with direct SQL against the test DB
— the same convention test_claim.py uses to simulate a claim, since the real
raise_question SQL is Postgres-only (see tests/test_blocked.py for its own
coverage against a fake cursor).
"""
import datetime

from sqlalchemy import text

from tests.conftest import make_ticket


def queue_rows(client, user, cluster_id):
    return client.get(f"/api/clusters/{cluster_id}/queue", headers=user["headers"]).json()


def block_ticket(client, ticket_id, question="Which library should I use?", **extra):
    """Simulate what worker.raise_question does: park the ticket blocked with
    an open question, and (if a queued/claimed work_queue row exists for it)
    move that row out of 'queued'/'claimed' the same way raise_question does."""
    engine = client.app.state.engine
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE work_queue SET status='blocked' WHERE ticket_id=:t "
            "AND status IN ('queued', 'claimed')"
        ), {"t": ticket_id})
        conn.execute(text("UPDATE tickets SET status='blocked' WHERE id=:t"),
                    {"t": ticket_id})
        row = conn.execute(text(
            "INSERT INTO ticket_questions (ticket_id, question, type, format, "
            "options, multi, created_at) VALUES (:t, :q, :ty, :f, :o, :m, :c) RETURNING id"
        ), {"t": ticket_id, "q": question, "ty": extra.get("type", "input"),
            "f": extra.get("format"), "o": extra.get("options"),
            "m": extra.get("multi", False), "c": datetime.datetime.utcnow()})
        return row.scalar_one()


def test_blocking_takes_a_ticket_out_of_the_claimable_queue(client, user, cluster):
    """A blocked ticket is never claimed: its work_queue row must leave
    'queued' and never return to it on its own."""
    t = make_ticket(client, user, cluster["board_id"], status="ready")
    block_ticket(client, t["id"])
    rows = queue_rows(client, user, cluster["id"])
    assert len(rows) == 1
    assert rows[0]["status"] != "queued"
    assert rows[0]["status"] == "blocked"


def test_ticket_surfaces_its_open_question(client, user, cluster):
    t = make_ticket(client, user, cluster["board_id"], status="ready")
    qid = block_ticket(client, t["id"], question="Postgres or SQLite?",
                       type="choice", options='["Postgres", "SQLite"]')
    listed = client.get(f"/api/boards/{cluster['board_id']}/tickets",
                        headers=user["headers"]).json()
    fresh = [x for x in listed if x["id"] == t["id"]][0]
    assert fresh["status"] == "blocked"
    assert fresh["question"]["id"] == qid
    assert fresh["question"]["question"] == "Postgres or SQLite?"
    assert fresh["question"]["type"] == "choice"
    assert fresh["question"]["options"] == ["Postgres", "SQLite"]
    assert fresh["question"]["answered_at"] is None


def test_answering_requeues_the_ticket_exactly_once(client, user, cluster):
    t = make_ticket(client, user, cluster["board_id"], status="ready")
    qid = block_ticket(client, t["id"])

    r = client.post(f"/api/tickets/{t['id']}/questions/{qid}/answer",
                    json={"value": "Postgres", "notes": "prod parity"},
                    headers=user["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ready"
    assert body["question"] is None  # answered -> no longer the open question

    rows = queue_rows(client, user, cluster["id"])
    queued = [row for row in rows if row["status"] == "queued"]
    assert len(queued) == 1
    assert queued[0]["ticket_id"] == t["id"]
    # Flagged so worker.py's claim_next continues the agent's prior session
    # instead of restarting the ticket from scratch — see gap analysis phase
    # 7 item 19 / ticket #16.
    assert queued[0]["resume"] is True

    # answering a second time must not enqueue a second work item
    r2 = client.post(f"/api/tickets/{t['id']}/questions/{qid}/answer",
                     json={"value": "SQLite"}, headers=user["headers"])
    assert r2.status_code == 409
    rows_after = queue_rows(client, user, cluster["id"])
    assert len([row for row in rows_after if row["status"] == "queued"]) == 1


def test_answer_is_recorded_on_the_question(client, user, cluster):
    t = make_ticket(client, user, cluster["board_id"], status="ready")
    qid = block_ticket(client, t["id"])
    client.post(f"/api/tickets/{t['id']}/questions/{qid}/answer",
               json={"value": "Postgres", "notes": "prod parity"},
               headers=user["headers"])
    engine = client.app.state.engine
    with engine.begin() as conn:
        row = conn.execute(text(
            "SELECT answer_value, answer_notes, answered_at FROM ticket_questions WHERE id=:q"
        ), {"q": qid}).fetchone()
    assert row.answer_value == "Postgres"
    assert row.answer_notes == "prod parity"
    assert row.answered_at is not None


def test_answer_rejects_a_question_from_another_ticket(client, user, cluster):
    t1 = make_ticket(client, user, cluster["board_id"], status="ready")
    t2 = make_ticket(client, user, cluster["board_id"], title="Other")
    qid = block_ticket(client, t1["id"])
    r = client.post(f"/api/tickets/{t2['id']}/questions/{qid}/answer",
                    json={"value": "x"}, headers=user["headers"])
    assert r.status_code == 404


def test_blocked_endpoint_lists_open_questions_across_boards(client, user, cluster):
    t1 = make_ticket(client, user, cluster["board_id"], status="ready")
    board2 = client.post(f"/api/clusters/{cluster['id']}/boards",
                         json={"name": "Second"}, headers=user["headers"]).json()
    t2 = client.post(f"/api/boards/{board2['id']}/tickets",
                     json={"title": "Elsewhere", "status": "ready"},
                     headers=user["headers"]).json()
    block_ticket(client, t1["id"], question="Q1")
    block_ticket(client, t2["id"], question="Q2")

    r = client.get(f"/api/clusters/{cluster['id']}/blocked", headers=user["headers"])
    assert r.status_code == 200
    by_ticket = {row["ticket_id"]: row for row in r.json()}
    assert set(by_ticket) == {t1["id"], t2["id"]}
    assert by_ticket[t1["id"]]["question"]["question"] == "Q1"
    assert by_ticket[t2["id"]]["board_id"] == board2["id"]


def test_blocked_endpoint_omits_resolved_tickets(client, user, cluster):
    t = make_ticket(client, user, cluster["board_id"], status="ready")
    qid = block_ticket(client, t["id"])
    client.post(f"/api/tickets/{t['id']}/questions/{qid}/answer",
               json={"value": "x"}, headers=user["headers"])
    r = client.get(f"/api/clusters/{cluster['id']}/blocked", headers=user["headers"])
    assert r.json() == []


def test_blocked_endpoint_lists_a_ticket_with_no_question(client, user, cluster):
    """Since the five-column rework a ticket also lands blocked when its run
    finished without pushing — no question to answer, but still a ticket
    waiting on a human, so the bell has to count it."""
    t = make_ticket(client, user, cluster["board_id"])
    client.patch(f"/api/tickets/{t['id']}", json={"status": "blocked"},
                 headers=user["headers"])

    rows = client.get(f"/api/clusters/{cluster['id']}/blocked",
                      headers=user["headers"]).json()
    assert [row["ticket_id"] for row in rows] == [t["id"]]
    assert rows[0]["question"] is None


def test_an_ordinary_ready_delegation_is_not_flagged_resume(client, user, cluster):
    """Only an unblock-requeue sets resume=True; a plain move-to-ready must
    start the agent fresh, same as before this feature existed."""
    make_ticket(client, user, cluster["board_id"], status="ready")
    rows = queue_rows(client, user, cluster["id"])
    assert len(rows) == 1
    assert rows[0]["resume"] is False


def test_blocked_status_is_a_valid_manual_status(client, user, cluster):
    """The status itself is a first-class value in the generic status
    validation, independent of the escalation machinery."""
    t = make_ticket(client, user, cluster["board_id"])
    r = client.patch(f"/api/tickets/{t['id']}", json={"status": "blocked"},
                     headers=user["headers"])
    assert r.status_code == 200
    assert r.json()["status"] == "blocked"
