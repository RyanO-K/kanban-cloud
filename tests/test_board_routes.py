"""Per-board routes: refreshing /b/{board_id} (or a ticket link) must not
lose your place — GET /api/boards/{id} tells the frontend which cluster a
URL-supplied board id belongs to, and /b/{id} serves the same SPA shell as
'/'."""
from pathlib import Path

INDEX = (Path(__file__).resolve().parents[1] / "app" / "static" / "index.html").read_text(
    encoding="utf-8"
)


def test_board_route_serves_the_same_shell_as_root(client, user, cluster):
    root = client.get("/")
    routed = client.get(f"/b/{cluster['board_id']}")
    assert routed.status_code == 200
    assert routed.text == root.text


def test_board_route_serves_the_shell_even_for_an_unknown_board(client):
    """Client-side routing decides what to render; the server doesn't 404
    just because no board with that id exists (yet, or ever)."""
    r = client.get("/b/99999")
    assert r.status_code == 200
    assert "kanban-cloud" in r.text


def test_get_board_returns_metadata_and_cluster_id(client, user, cluster):
    r = client.get(f"/api/boards/{cluster['board_id']}", headers=user["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == cluster["board_id"]
    assert body["cluster_id"] == cluster["id"]
    assert body["name"] == "Main"


def test_get_board_requires_membership(client, user, cluster):
    other = client.post("/api/register", json={"email": "z@x.co", "password": "pass1234"}).json()
    headers = {"Authorization": f"Bearer {other['token']}"}
    r = client.get(f"/api/boards/{cluster['board_id']}", headers=headers)
    assert r.status_code == 403


def test_get_unknown_board_404s(client, user):
    r = client.get("/api/boards/99999", headers=user["headers"])
    assert r.status_code == 404


def test_get_board_requires_auth(client, cluster):
    r = client.get(f"/api/boards/{cluster['board_id']}")
    assert r.status_code == 401


# ---------- frontend markup: routing wiring ----------

def test_frontend_parses_board_and_ticket_from_the_url():
    assert "ROUTE_MATCH" in INDEX
    assert "pendingBoardId" in INDEX
    assert "pendingTicketId" in INDEX


def test_frontend_syncs_the_address_bar_on_board_and_ticket_changes():
    assert "function syncRoute()" in INDEX
    assert "history.replaceState" in INDEX
    body = INDEX[INDEX.index("function openTicketModal"):]
    assert "syncRoute()" in body[:body.index("function closeTicketModal")]


def test_frontend_resolves_a_direct_board_link_to_its_cluster():
    assert "resolveClusterForBoard" in INDEX
    assert "./api/boards/${boardId}" in INDEX


def test_api_fetches_resolve_against_app_root_not_raw_location():
    """A '/b/{id}' route changes location.pathname, so relative './api/...'
    fetches must NOT resolve against it directly or they'd break behind the
    reverse-proxy prefix (e.g. '/board/b/5' + './api/x' -> '/board/b/api/x')."""
    assert "APP_ROOT" in INDEX
    body = INDEX[INDEX.index("async function api("):]
    body = body[:body.index("\n}")]
    assert "APP_ROOT" in body
