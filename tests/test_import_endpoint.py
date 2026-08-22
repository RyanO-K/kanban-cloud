"""POST /api/clusters/{id}/import — bulk-create a board from local .kanban JSON.

The browser reads the operator's board folder and posts a key-whitelisted
payload; this route turns it into a board. Import never merges into an existing
board, so re-running it is always safe.
"""
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import WorkItem

SECRET = "test-proxy-secret"
OWNER = {"X-Proxy-Secret": SECRET, "X-Proxy-User": "Ryan"}
SPECTATOR = {"X-Proxy-Secret": SECRET}


def do_import(client, user, cluster_id, name="ai-kanban", tickets=None):
    return client.post(
        f"/api/clusters/{cluster_id}/import",
        json={"name": name, "tickets": tickets if tickets is not None else []},
        headers=user["headers"],
    )


def board_tickets(client, user, board_id):
    return client.get(f"/api/boards/{board_id}/tickets", headers=user["headers"]).json()


def test_import_creates_board_and_tickets_in_local_id_order(client, user, cluster):
    r = do_import(
        client, user, cluster["id"],
        tickets=[
            {"id": "10", "title": "Tenth", "status": "completed"},
            {"id": "2", "title": "Second", "status": "completed"},
            {"id": "1", "title": "First", "status": "completed"},
        ],
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["name"] == "ai-kanban"
    assert out["imported"] == 3 and out["skipped"] == 0

    titles = [t["title"] for t in board_tickets(client, user, out["board_id"])]
    assert titles == ["First", "Second", "Tenth"]


def test_import_maps_statuses(client, user, cluster):
    r = do_import(
        client, user, cluster["id"],
        tickets=[
            {"id": "1", "title": "a", "status": "completed"},
            {"id": "2", "title": "b", "status": "in_progress"},
            {"id": "3", "title": "c", "status": "blocked"},
        ],
    )
    got = {t["title"]: t["status"] for t in board_tickets(client, user, r.json()["board_id"])}
    assert got == {"a": "done", "b": "doing", "c": "todo"}


def test_import_appends_context_appendix(client, user, cluster):
    r = do_import(
        client, user, cluster["id"],
        tickets=[{"id": "16", "title": "a", "status": "blocked", "detail": "the detail",
                  "dependsOn": ["12"]}],
    )
    body = board_tickets(client, user, r.json()["board_id"])[0]["body"]
    assert "the detail" in body
    # Demoting blocked -> todo stays visible because the appendix says so.
    assert "local status: blocked" in body
    assert "**Depends on:** 12" in body


def test_import_carries_comments(client, user, cluster):
    r = do_import(
        client, user, cluster["id"],
        tickets=[{"id": "1", "title": "a", "comments": [
            {"writer": "ryan", "message": "first", "timestamp": "2026-07-04T01:30:19+00:00"},
            {"writer": "bot", "message": "second"},
        ]}],
    )
    comments = board_tickets(client, user, r.json()["board_id"])[0]["comments"]
    assert [(c["writer"], c["message"]) for c in comments] == [("ryan", "first"), ("bot", "second")]


def test_import_ready_ticket_is_queued(client, user, cluster):
    r = do_import(
        client, user, cluster["id"],
        tickets=[
            {"id": "1", "title": "queued one", "status": "ready"},
            {"id": "2", "title": "not queued", "status": "completed"},
        ],
    )
    assert r.json()["queued"] == 1
    q = client.get(f"/api/clusters/{cluster['id']}/queue", headers=user["headers"]).json()
    assert len(q) == 1
    # The queue exposes ticket_id, not the title — correlate through the board.
    queued_id = q[0]["ticket_id"]
    by_id = {t["id"]: t["title"] for t in board_tickets(client, user, r.json()["board_id"])}
    assert by_id[queued_id] == "queued one"


def test_import_without_ready_queues_nothing(client, user, cluster):
    """The six live local boards have no 'ready' tickets, so this is the norm."""
    r = do_import(
        client, user, cluster["id"],
        tickets=[{"id": str(i), "title": f"t{i}", "status": "completed"} for i in range(1, 6)],
    )
    assert r.json()["queued"] == 0
    assert client.get(f"/api/clusters/{cluster['id']}/queue",
                      headers=user["headers"]).json() == []


def test_import_name_clash_suffixes_and_leaves_original_alone(client, user, cluster):
    first = do_import(client, user, cluster["id"],
                      tickets=[{"id": "1", "title": "original"}]).json()
    second = do_import(client, user, cluster["id"],
                       tickets=[{"id": "1", "title": "newer"}]).json()

    assert second["name"] == "ai-kanban (2)"
    assert second["board_id"] != first["board_id"]
    # The first board is untouched: import creates, never merges.
    assert [t["title"] for t in board_tickets(client, user, first["board_id"])] == ["original"]


def test_import_skips_blank_titles(client, user, cluster):
    r = do_import(
        client, user, cluster["id"],
        tickets=[
            {"id": "1", "title": "keeper"},
            {"id": "2", "title": "   "},
            {"id": "3"},
            {"id": "4", "title": None},
        ],
    )
    out = r.json()
    assert out["imported"] == 1 and out["skipped"] == 3
    assert [t["title"] for t in board_tickets(client, user, out["board_id"])] == ["keeper"]


def test_import_rejects_empty_ticket_list(client, user, cluster):
    before = client.get(f"/api/clusters/{cluster['id']}/boards", headers=user["headers"]).json()
    r = do_import(client, user, cluster["id"], tickets=[])
    assert r.status_code == 400
    after = client.get(f"/api/clusters/{cluster['id']}/boards", headers=user["headers"]).json()
    assert len(after) == len(before), "no board should be created for an empty import"


def test_import_rejects_a_board_of_only_blank_titles(client, user, cluster):
    """Every ticket skipped is an empty import, not a board of nothing."""
    before = client.get(f"/api/clusters/{cluster['id']}/boards", headers=user["headers"]).json()
    r = do_import(client, user, cluster["id"], tickets=[{"id": "1"}, {"id": "2"}])
    assert r.status_code == 400
    after = client.get(f"/api/clusters/{cluster['id']}/boards", headers=user["headers"]).json()
    assert len(after) == len(before)


def test_import_rejects_oversized_payload(client, user, cluster):
    tickets = [{"id": str(i), "title": f"t{i}"} for i in range(501)]
    r = do_import(client, user, cluster["id"], tickets=tickets)
    assert r.status_code == 400
    assert "500" in r.json()["detail"]


def test_import_requires_membership(client, user, cluster):
    r = client.post("/api/register", json={"email": "other@x.com", "password": "pass1234"})
    other = {"headers": {"Authorization": f"Bearer {r.json()['token']}"}}
    assert do_import(client, other, cluster["id"],
                     tickets=[{"id": "1", "title": "a"}]).status_code == 403


def test_import_requires_auth(client, cluster):
    r = client.post(f"/api/clusters/{cluster['id']}/import",
                    json={"name": "x", "tickets": [{"id": "1", "title": "a"}]})
    assert r.status_code == 401


@pytest.fixture()
def pclient(tmp_path):
    app = create_app(f"sqlite:///{tmp_path / 'proxy.db'}", proxy_secret=SECRET)
    with TestClient(app) as c:
        yield c


def test_import_rejected_for_spectator(pclient):
    """Spectators are stopped by the proxy gate before the route is reached."""
    pclient.get("/api/session", headers=OWNER)  # provision owner + cluster
    cluster_id = pclient.get("/api/clusters", headers=OWNER).json()[0]["id"]

    r = pclient.post(f"/api/clusters/{cluster_id}/import",
                     json={"name": "x", "tickets": [{"id": "1", "title": "a"}]},
                     headers=SPECTATOR)
    assert r.status_code == 403
    assert "read-only" in r.json()["detail"].lower()


def test_import_works_for_proxy_owner(pclient):
    pclient.get("/api/session", headers=OWNER)
    cluster_id = pclient.get("/api/clusters", headers=OWNER).json()[0]["id"]

    r = pclient.post(f"/api/clusters/{cluster_id}/import",
                     json={"name": "ai-kanban", "tickets": [{"id": "1", "title": "a"}]},
                     headers=OWNER)
    assert r.status_code == 200, r.text
    assert r.json()["imported"] == 1
