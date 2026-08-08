"""Delegation tests: enqueue, atomic claim, routing, failure handling."""
import pytest
from conftest import make_ticket, register_worker

pytestmark = pytest.mark.skip(reason="v1 worker HTTP API removed in v2 (Task 3 rewrites these)")


def poll(client, worker):
    r = client.post("/api/work/poll", headers=worker["headers"])
    assert r.status_code == 200, r.text
    return r.json()["work"]


def test_ready_enqueues_and_worker_claims(client, user, cluster):
    t = make_ticket(client, user, cluster["board_id"])
    client.patch(f"/api/tickets/{t['id']}", json={"status": "ready"}, headers=user["headers"])

    w = register_worker(client, cluster["join_code"], "pc-1")
    work = poll(client, w)
    assert work is not None
    assert work["ticket"]["id"] == t["id"]
    assert work["ticket"]["status"] == "doing"

    # board reflects the claim
    lst = client.get(f"/api/boards/{cluster['board_id']}/tickets", headers=user["headers"]).json()
    assert lst[0]["status"] == "doing"
    assert lst[0]["assigned_worker"] == w["worker_id"]

    # nothing left to claim
    assert poll(client, w) is None


def test_single_item_claimed_by_only_one_worker(client, user, cluster):
    t = make_ticket(client, user, cluster["board_id"])
    client.post(f"/api/tickets/{t['id']}/run", headers=user["headers"])

    w1 = register_worker(client, cluster["join_code"], "pc-1")
    w2 = register_worker(client, cluster["join_code"], "pc-2")

    got = [poll(client, w1), poll(client, w2)]
    claimed = [g for g in got if g is not None]
    assert len(claimed) == 1  # exactly one winner


def test_atomic_claim_update_guard(client, user, cluster):
    """The claim is UPDATE ... WHERE status='queued'; a second identical update
    (a racing worker holding a stale candidate) affects 0 rows."""
    from sqlalchemy import update
    from app.models import WorkItem

    t = make_ticket(client, user, cluster["board_id"])
    client.post(f"/api/tickets/{t['id']}/run", headers=user["headers"])

    engine = client.app.state.engine
    with engine.begin() as conn:
        first = conn.execute(
            update(WorkItem).where(WorkItem.ticket_id == t["id"], WorkItem.status == "queued")
            .values(status="claimed")
        )
        assert first.rowcount == 1
        second = conn.execute(
            update(WorkItem).where(WorkItem.ticket_id == t["id"], WorkItem.status == "queued")
            .values(status="claimed")
        )
        assert second.rowcount == 0  # double-claim guarded


def test_target_worker_routing(client, user, cluster):
    w1 = register_worker(client, cluster["join_code"], "pc-1")
    w2 = register_worker(client, cluster["join_code"], "pc-2")

    t = make_ticket(client, user, cluster["board_id"], title="Only for pc-2")
    client.patch(f"/api/tickets/{t['id']}",
                 json={"target_worker": w2["worker_id"], "status": "ready"},
                 headers=user["headers"])

    # the non-target worker never gets it
    assert poll(client, w1) is None
    # the target worker does
    work = poll(client, w2)
    assert work is not None and work["ticket"]["id"] == t["id"]


def test_offline_target_worker_item_stays_queued(client, user, cluster):
    """Ticket targeted at a PC that is not polling: other workers skip it and
    it remains queued for when the target comes online."""
    w_target = register_worker(client, cluster["join_code"], "target-pc")  # never polls
    w_other = register_worker(client, cluster["join_code"], "other-pc")

    t = make_ticket(client, user, cluster["board_id"])
    client.patch(f"/api/tickets/{t['id']}",
                 json={"target_worker": w_target["worker_id"], "status": "ready"},
                 headers=user["headers"])

    assert poll(client, w_other) is None
    q = client.get(f"/api/clusters/{cluster['id']}/queue", headers=user["headers"]).json()
    assert q[0]["status"] == "queued"

    # target comes online -> claims it
    assert poll(client, w_target)["ticket"]["id"] == t["id"]


def test_other_cluster_worker_cannot_claim(client, user, cluster):
    # second cluster with its own worker
    c2 = client.post("/api/clusters", json={"name": "Other"}, headers=user["headers"]).json()
    w_foreign = register_worker(client, c2["join_code"], "foreign-pc")

    t = make_ticket(client, user, cluster["board_id"])
    client.post(f"/api/tickets/{t['id']}/run", headers=user["headers"])

    assert poll(client, w_foreign) is None


def test_api_key_delivered_only_to_claiming_worker(client, user, cluster):
    key = "sk-ant-api03-clusterkey-wxyz"
    client.put(f"/api/clusters/{cluster['id']}/settings", json={"claude_api_key": key},
               headers=user["headers"])
    t = make_ticket(client, user, cluster["board_id"])
    client.post(f"/api/tickets/{t['id']}/run", headers=user["headers"])

    w = register_worker(client, cluster["join_code"], "pc-1")
    work = poll(client, w)
    assert work["claude_api_key"] == key  # full key goes to the worker


def test_success_result_moves_ticket_to_review_with_comment(client, user, cluster):
    t = make_ticket(client, user, cluster["board_id"])
    client.post(f"/api/tickets/{t['id']}/run", headers=user["headers"])
    w = register_worker(client, cluster["join_code"], "pc-1")
    work = poll(client, w)

    r = client.post(f"/api/work/{work['assignment_id']}/result",
                    json={"ok": True, "comment": "All done, wrote the thing."},
                    headers=w["headers"])
    assert r.status_code == 200
    assert r.json() == {"ticket_status": "review", "item_status": "done"}

    lst = client.get(f"/api/boards/{cluster['board_id']}/tickets", headers=user["headers"]).json()
    assert lst[0]["status"] == "review"
    assert lst[0]["comments"][-1]["writer"] == "worker:pc-1"
    assert "All done" in lst[0]["comments"][-1]["message"]

    # double-reporting the same assignment is rejected
    again = client.post(f"/api/work/{work['assignment_id']}/result",
                        json={"ok": True, "comment": "again"}, headers=w["headers"])
    assert again.status_code == 409


def test_failure_requeues_then_fails_permanently(client, user, cluster):
    t = make_ticket(client, user, cluster["board_id"])
    client.post(f"/api/tickets/{t['id']}/run", headers=user["headers"])
    w = register_worker(client, cluster["join_code"], "pc-1")

    # attempt 1 fails -> requeued
    work = poll(client, w)
    r = client.post(f"/api/work/{work['assignment_id']}/result",
                    json={"ok": False, "comment": "boom"}, headers=w["headers"])
    assert r.json() == {"ticket_status": "ready", "item_status": "failed"}

    # attempt 2 (fresh queue item) fails -> permanent failure
    work2 = poll(client, w)
    assert work2 is not None and work2["assignment_id"] != work["assignment_id"]
    r2 = client.post(f"/api/work/{work2['assignment_id']}/result",
                     json={"ok": False, "comment": "boom again"}, headers=w["headers"])
    assert r2.json() == {"ticket_status": "failed", "item_status": "failed"}
    assert poll(client, w) is None  # nothing requeued


def test_enqueue_is_idempotent(client, user, cluster):
    t = make_ticket(client, user, cluster["board_id"])
    client.post(f"/api/tickets/{t['id']}/run", headers=user["headers"])
    client.post(f"/api/tickets/{t['id']}/run", headers=user["headers"])
    client.patch(f"/api/tickets/{t['id']}", json={"status": "ready"}, headers=user["headers"])

    q = client.get(f"/api/clusters/{cluster['id']}/queue", headers=user["headers"]).json()
    assert len(q) == 1  # only one queued item despite three triggers


def test_reenqueue_supersedes_orphaned_claim(client, user, cluster):
    """Worker dies mid-ticket (claim stays 'claimed'): moving the ticket back
    to ready must supersede the dead claim and queue a fresh item, and a late
    result for the old assignment is rejected."""
    t = make_ticket(client, user, cluster["board_id"])
    client.post(f"/api/tickets/{t['id']}/run", headers=user["headers"])
    w = register_worker(client, cluster["join_code"], "pc-1")
    work = poll(client, w)  # claimed, then the worker "dies"

    client.patch(f"/api/tickets/{t['id']}", json={"status": "ready"}, headers=user["headers"])
    q = client.get(f"/api/clusters/{cluster['id']}/queue", headers=user["headers"]).json()
    assert [i["status"] for i in q] == ["queued", "failed"]  # fresh item + superseded claim

    work2 = poll(client, w)
    assert work2 is not None and work2["assignment_id"] != work["assignment_id"]
    assert work2["ticket"]["id"] == t["id"]

    # late result for the superseded assignment is rejected
    late = client.post(f"/api/work/{work['assignment_id']}/result",
                       json={"ok": True, "comment": "too late"}, headers=w["headers"])
    assert late.status_code == 409


def test_rerun_after_permanent_failure_gets_fresh_retry_budget(client, user, cluster):
    """A re-delegated ticket must not inherit the exhausted attempt count."""
    t = make_ticket(client, user, cluster["board_id"])
    client.post(f"/api/tickets/{t['id']}/run", headers=user["headers"])
    w = register_worker(client, cluster["join_code"], "pc-1")
    for _ in range(2):  # exhaust MAX_ATTEMPTS -> failed
        work = poll(client, w)
        client.post(f"/api/work/{work['assignment_id']}/result",
                    json={"ok": False, "comment": "boom"}, headers=w["headers"])

    client.post(f"/api/tickets/{t['id']}/run", headers=user["headers"])  # re-run
    work = poll(client, w)
    assert work is not None and work["ticket"]["attempts"] == 1  # reset, not 3
    r = client.post(f"/api/work/{work['assignment_id']}/result",
                    json={"ok": False, "comment": "boom"}, headers=w["headers"])
    assert r.json()["ticket_status"] == "ready"  # requeued, not instantly failed


def test_worker_register_and_reregister(client, user, cluster):
    bad = client.post("/api/workers/register", json={"join_code": "NOPE1234", "name": "x"})
    assert bad.status_code == 404

    w1 = register_worker(client, cluster["join_code"], "pc-1")
    w1b = register_worker(client, cluster["join_code"], "pc-1")  # same name re-registers
    assert w1b["worker_id"] == w1["worker_id"]
    assert w1b["worker_token"] != w1["worker_token"]  # token rotated

    # old token now invalid
    r = client.post("/api/work/poll", headers=w1["headers"])
    assert r.status_code == 401

    # workers panel shows the PC online after a poll
    poll(client, w1b)
    panel = client.get(f"/api/clusters/{cluster['id']}/workers", headers=user["headers"]).json()
    assert panel == [
        {"id": w1["worker_id"], "name": "pc-1", "status": panel[0]["status"],
         "online": True, "last_seen": panel[0]["last_seen"]}
    ]
