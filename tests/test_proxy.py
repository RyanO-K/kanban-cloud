"""Reverse-proxy mode: shared-secret gate, proxy identities, spectator lockdown.

Worker-exempt routes (own token auth, bypass the gate):
  POST /api/workers/register
  POST /api/work/poll
  POST /api/work/{id}/result
  POST /api/work/{id}/progress
"""
import pytest
from fastapi.testclient import TestClient

from app.main import create_app

SECRET = "test-proxy-secret"

OWNER = {"X-Proxy-Secret": SECRET, "X-Proxy-User": "Ryan"}
SPECTATOR = {"X-Proxy-Secret": SECRET}


@pytest.fixture()
def pclient(tmp_path):
    """TestClient with the proxy gate enabled (fresh SQLite per test)."""
    app = create_app(f"sqlite:///{tmp_path / 'proxy.db'}", proxy_secret=SECRET)
    with TestClient(app) as c:
        yield c


def owner_seed(pclient):
    """Provision the owner (auto-creates default cluster + board), return ids."""
    s = pclient.get("/api/session", headers=OWNER).json()
    assert s["mode"] == "owner"
    clusters = pclient.get("/api/clusters", headers=OWNER).json()
    boards = pclient.get(f"/api/clusters/{clusters[0]['id']}/boards", headers=OWNER).json()
    return {"session": s, "cluster": clusters[0], "board": boards[0]}


# ---------- the gate ----------

def test_gate_requires_secret(pclient):
    assert pclient.get("/api/health").status_code == 403
    assert pclient.get("/").status_code == 403
    assert pclient.get("/api/health", headers={"X-Proxy-Secret": "wrong"}).status_code == 403
    assert pclient.get("/api/health", headers=SPECTATOR).status_code == 200
    assert pclient.get("/api/health", headers=OWNER).status_code == 200


def test_gate_off_behavior_unchanged(client):
    """No PROXY_SHARED_SECRET -> local dev flow exactly as before."""
    assert client.get("/api/health").status_code == 200
    r = client.post("/api/register", json={"email": "dev@example.com", "password": "pass1234"})
    assert r.status_code == 200
    tok = r.json()["token"]
    r = client.post("/api/login", json={"email": "dev@example.com", "password": "pass1234"})
    assert r.status_code == 200
    me = client.get("/api/me", headers={"Authorization": f"Bearer {tok}"})
    assert me.status_code == 200
    # Injected proxy headers are ignored when the gate is off.
    assert client.get("/api/me", headers={"X-Proxy-User": "mallory"}).status_code == 401


@pytest.mark.skip(reason="v1 worker HTTP API removed in v2 (Task 3 rewrites these)")
def test_worker_routes_exempt_from_gate(pclient):
    # No proxy secret on any of these: they must reach their own auth, not 403.
    r = pclient.post("/api/workers/register", json={"join_code": "NOPE1234", "name": "pc"})
    assert r.status_code == 404  # bad join code, NOT the gate's 403
    assert pclient.post("/api/work/poll").status_code == 401  # missing worker token
    assert pclient.post("/api/work/1/result", json={"ok": True}).status_code == 401
    assert pclient.post("/api/work/1/progress", json={"message": "hi"}).status_code == 401


@pytest.mark.skip(reason="v1 worker HTTP API removed in v2 (Task 3 rewrites these)")
def test_worker_full_flow_without_proxy_secret(pclient):
    seed = owner_seed(pclient)
    join_code = seed["cluster"]["join_code"]
    r = pclient.post("/api/workers/register", json={"join_code": join_code, "name": "pc-1"})
    assert r.status_code == 200
    wh = {"X-Worker-Token": r.json()["worker_token"]}

    t = pclient.post(
        f"/api/boards/{seed['board']['id']}/tickets",
        json={"title": "Proxied job", "status": "ready"},
        headers=OWNER,
    )
    assert t.status_code == 200

    claim = pclient.post("/api/work/poll", headers=wh).json()["work"]
    assert claim and claim["ticket"]["id"] == t.json()["id"]
    item_id = claim["assignment_id"]
    assert pclient.post(
        f"/api/work/{item_id}/progress", json={"message": "50%"}, headers=wh
    ).status_code == 200
    r = pclient.post(
        f"/api/work/{item_id}/result", json={"ok": True, "comment": "done"}, headers=wh
    )
    assert r.status_code == 200
    assert r.json()["ticket_status"] == "review"


# ---------- owner identity ----------

def test_owner_header_provisions_user_with_full_rights(pclient):
    s = pclient.get("/api/session", headers=OWNER).json()
    assert s["mode"] == "owner"
    assert s["user"]["email"] == "ryan@proxy.user"

    # Auto-provisioned into an auto-created default cluster with a board.
    clusters = pclient.get("/api/clusters", headers=OWNER).json()
    assert [c["name"] for c in clusters] == ["Main"]
    boards = pclient.get(f"/api/clusters/{clusters[0]['id']}/boards", headers=OWNER).json()
    assert [b["name"] for b in boards] == ["Main"]

    # Full rights: tickets, comments, settings, workers listing.
    t = pclient.post(
        f"/api/boards/{boards[0]['id']}/tickets", json={"title": "hi"}, headers=OWNER
    )
    assert t.status_code == 200
    tid = t.json()["id"]
    assert pclient.patch(f"/api/tickets/{tid}", json={"status": "doing"}, headers=OWNER).status_code == 200
    assert pclient.post(f"/api/tickets/{tid}/comments", json={"message": "note"}, headers=OWNER).status_code == 200
    assert pclient.put(
        f"/api/clusters/{clusters[0]['id']}/settings",
        json={"claude_api_key": "sk-ant-test-0000000000"},
        headers=OWNER,
    ).status_code == 200
    assert pclient.get(f"/api/clusters/{clusters[0]['id']}/workers", headers=OWNER).status_code == 200

    # Same login -> same user, no duplicate provisioning; bearer not required.
    s2 = pclient.get("/api/session", headers=OWNER).json()
    assert s2["user"]["id"] == s["user"]["id"]
    assert pclient.get("/api/me", headers=OWNER).json()["id"] == s["user"]["id"]


def test_proxy_account_has_no_usable_password(pclient):
    owner_seed(pclient)
    r = pclient.post(
        "/api/login",
        json={"email": "ryan@proxy.user", "password": "proxy-auth"},
        headers=OWNER,
    )
    assert r.status_code == 401


# ---------- spectator ----------

def test_session_modes(client, pclient):
    assert client.get("/api/session").json() == {"mode": "local"}
    # Spectator before anything exists: cluster is null.
    s = pclient.get("/api/session", headers=SPECTATOR).json()
    assert s == {"mode": "spectator", "cluster": None}
    seed = owner_seed(pclient)
    s = pclient.get("/api/session", headers=SPECTATOR).json()
    assert s["mode"] == "spectator"
    assert s["cluster"] == {"id": seed["cluster"]["id"], "name": "Main"}
    # join_code must NOT be in the spectator session payload.
    assert "join_code" not in str(s)


def test_readonly_header_forces_spectator(pclient):
    owner_seed(pclient)
    ro = dict(OWNER, **{"X-Proxy-Readonly": "1"})
    assert pclient.get("/api/session", headers=ro).json()["mode"] == "spectator"
    assert pclient.post("/api/clusters", json={"name": "x"}, headers=ro).status_code == 403


def test_spectator_can_read_board_live(pclient):
    seed = owner_seed(pclient)
    cid, bid = seed["cluster"]["id"], seed["board"]["id"]
    t = pclient.post(
        f"/api/boards/{bid}/tickets", json={"title": "Visible ticket"}, headers=OWNER
    ).json()
    pclient.post(f"/api/tickets/{t['id']}/comments", json={"message": "public note"}, headers=OWNER)

    assert pclient.get("/", headers=SPECTATOR).status_code == 200
    boards = pclient.get(f"/api/clusters/{cid}/boards", headers=SPECTATOR)
    assert boards.status_code == 200 and boards.json()[0]["id"] == bid
    tickets = pclient.get(f"/api/boards/{bid}/tickets", headers=SPECTATOR)
    assert tickets.status_code == 200
    assert tickets.json()[0]["title"] == "Visible ticket"
    assert tickets.json()[0]["comments"][0]["message"] == "public note"
    assert pclient.get(f"/api/clusters/{cid}/workers", headers=SPECTATOR).status_code == 200
    assert pclient.get(f"/api/clusters/{cid}/queue", headers=SPECTATOR).status_code == 200


def test_spectator_sensitive_gets_403(pclient):
    seed = owner_seed(pclient)
    cid = seed["cluster"]["id"]
    # Cluster list leaks join codes; settings leaks key state; /api/me is not needed.
    assert pclient.get("/api/clusters", headers=SPECTATOR).status_code == 403
    assert pclient.get(f"/api/clusters/{cid}/settings", headers=SPECTATOR).status_code == 403
    assert pclient.get("/api/me", headers=SPECTATOR).status_code == 403


def test_spectator_every_mutation_403(pclient):
    seed = owner_seed(pclient)
    cid, bid = seed["cluster"]["id"], seed["board"]["id"]
    tid = pclient.post(
        f"/api/boards/{bid}/tickets", json={"title": "t"}, headers=OWNER
    ).json()["id"]

    attempts = [
        ("POST", "/api/register", {"email": "x@y.z", "password": "pass1234"}),
        ("POST", "/api/login", {"email": "x@y.z", "password": "pass1234"}),
        ("POST", "/api/clusters", {"name": "evil"}),
        ("POST", "/api/clusters/join", {"join_code": "AAAA1111"}),
        ("POST", f"/api/clusters/{cid}/boards", {"name": "evil"}),
        ("PUT", f"/api/clusters/{cid}/settings", {"claude_api_key": "sk-steal"}),
        ("POST", f"/api/boards/{bid}/tickets", {"title": "evil"}),
        ("PATCH", f"/api/tickets/{tid}", {"status": "done"}),
        ("POST", f"/api/tickets/{tid}/run", None),
        ("POST", f"/api/tickets/{tid}/comments", {"message": "spam"}),
        ("DELETE", f"/api/tickets/{tid}", None),
    ]
    for method, path, body in attempts:
        r = pclient.request(method, path, json=body, headers=SPECTATOR)
        assert r.status_code == 403, f"{method} {path} -> {r.status_code}"

    # Board state untouched.
    tickets = pclient.get(f"/api/boards/{bid}/tickets", headers=SPECTATOR).json()
    assert [t["id"] for t in tickets] == [tid]
    assert tickets[0]["status"] == "todo"
    assert tickets[0]["comments"] == []
