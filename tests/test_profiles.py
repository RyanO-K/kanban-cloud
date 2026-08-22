"""Agent profiles: cluster-scoped presets for the tool allowlist, model and
system prompt an agent run is launched with, plus a per-board default and a
per-ticket override (gap analysis phase 5)."""
from conftest import make_ticket


def create_profile(client, user, cluster_id, **kw):
    payload = {"name": "default", "allowed_tools": "Read,Edit,Write,Bash,Grep,Glob", **kw}
    r = client.post(f"/api/clusters/{cluster_id}/profiles", json=payload,
                    headers=user["headers"])
    assert r.status_code == 200, r.text
    return r.json()


# ---------- profile CRUD ----------

def test_list_profiles_starts_empty(client, user, cluster):
    r = client.get(f"/api/clusters/{cluster['id']}/profiles", headers=user["headers"])
    assert r.status_code == 200
    assert r.json() == []


def test_create_profile(client, user, cluster):
    p = create_profile(client, user, cluster["id"], name="restricted",
                       allowed_tools="Read,Grep", model="claude-opus-5",
                       system_prompt="Be terse.")
    assert p["name"] == "restricted"
    assert p["allowed_tools"] == "Read,Grep"
    assert p["model"] == "claude-opus-5"
    assert p["system_prompt"] == "Be terse."
    listed = client.get(f"/api/clusters/{cluster['id']}/profiles",
                        headers=user["headers"]).json()
    assert [x["name"] for x in listed] == ["restricted"]


def test_create_profile_requires_name_and_allowed_tools(client, user, cluster):
    r = client.post(f"/api/clusters/{cluster['id']}/profiles",
                    json={"name": "", "allowed_tools": "Read"}, headers=user["headers"])
    assert r.status_code == 400
    r = client.post(f"/api/clusters/{cluster['id']}/profiles",
                    json={"name": "x", "allowed_tools": ""}, headers=user["headers"])
    assert r.status_code == 400


def test_create_profile_duplicate_name_in_same_cluster_conflicts(client, user, cluster):
    create_profile(client, user, cluster["id"], name="dup")
    r = client.post(f"/api/clusters/{cluster['id']}/profiles",
                    json={"name": "dup", "allowed_tools": "Read"}, headers=user["headers"])
    assert r.status_code == 409


def test_patch_profile_is_partial(client, user, cluster):
    p = create_profile(client, user, cluster["id"], name="a", model="claude-sonnet-5")
    r = client.patch(f"/api/profiles/{p['id']}", json={"allowed_tools": "Read"},
                     headers=user["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["allowed_tools"] == "Read"
    assert r.json()["model"] == "claude-sonnet-5"  # untouched
    assert r.json()["name"] == "a"  # untouched


def test_patch_profile_can_clear_model_and_system_prompt(client, user, cluster):
    p = create_profile(client, user, cluster["id"], model="x", system_prompt="y")
    r = client.patch(f"/api/profiles/{p['id']}", json={"model": "", "system_prompt": ""},
                     headers=user["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["model"] == ""
    assert r.json()["system_prompt"] == ""


def test_patch_profile_rejects_blank_allowed_tools(client, user, cluster):
    p = create_profile(client, user, cluster["id"])
    r = client.patch(f"/api/profiles/{p['id']}", json={"allowed_tools": "   "},
                     headers=user["headers"])
    assert r.status_code == 400


def test_patch_unknown_profile_404s(client, user):
    r = client.patch("/api/profiles/99999", json={"name": "x"}, headers=user["headers"])
    assert r.status_code == 404


def test_patch_profile_in_another_cluster_is_forbidden(client, user, cluster):
    p = create_profile(client, user, cluster["id"])
    other = client.post("/api/register", json={"email": "z@x.co", "password": "pass1234"}).json()
    headers = {"Authorization": f"Bearer {other['token']}"}
    r = client.patch(f"/api/profiles/{p['id']}", json={"name": "stolen"}, headers=headers)
    assert r.status_code == 403


def test_delete_profile(client, user, cluster):
    p = create_profile(client, user, cluster["id"])
    r = client.delete(f"/api/profiles/{p['id']}", headers=user["headers"])
    assert r.status_code == 200
    assert client.get(f"/api/clusters/{cluster['id']}/profiles",
                      headers=user["headers"]).json() == []


# ---------- board default profile ----------

def test_list_boards_includes_default_profile_id(client, user, cluster):
    boards = client.get(f"/api/clusters/{cluster['id']}/boards",
                        headers=user["headers"]).json()
    assert boards[0]["default_profile_id"] is None


def test_patch_board_sets_default_profile(client, user, cluster):
    p = create_profile(client, user, cluster["id"])
    r = client.patch(f"/api/boards/{cluster['board_id']}", headers=user["headers"],
                     json={"default_profile_id": p["id"]})
    assert r.status_code == 200, r.text
    assert r.json()["default_profile_id"] == p["id"]


def test_patch_board_default_profile_must_be_in_the_same_cluster(client, user, cluster):
    other_cluster = client.post("/api/clusters", json={"name": "Other"},
                                headers=user["headers"]).json()
    p = create_profile(client, user, other_cluster["id"])
    r = client.patch(f"/api/boards/{cluster['board_id']}", headers=user["headers"],
                     json={"default_profile_id": p["id"]})
    assert r.status_code == 400


def test_patch_board_can_clear_default_profile(client, user, cluster):
    p = create_profile(client, user, cluster["id"])
    client.patch(f"/api/boards/{cluster['board_id']}", headers=user["headers"],
                json={"default_profile_id": p["id"]})
    r = client.patch(f"/api/boards/{cluster['board_id']}", headers=user["headers"],
                     json={"clear_default_profile": True})
    assert r.status_code == 200, r.text
    assert r.json()["default_profile_id"] is None


def test_patch_board_default_profile_is_partial_like_other_fields(client, user, cluster):
    p = create_profile(client, user, cluster["id"])
    bid = cluster["board_id"]
    client.patch(f"/api/boards/{bid}", headers=user["headers"],
                json={"default_profile_id": p["id"]})
    client.patch(f"/api/boards/{bid}", headers=user["headers"],
                json={"description": "unrelated change"})
    r = client.get(f"/api/clusters/{cluster['id']}/boards",
                   headers=user["headers"]).json()[0]
    assert r["default_profile_id"] == p["id"]
    assert r["description"] == "unrelated change"


# ---------- per-ticket profile override ----------

def test_ticket_json_includes_profile_id(client, user, cluster):
    t = make_ticket(client, user, cluster["board_id"])
    assert t["profile_id"] is None


def test_create_ticket_with_profile(client, user, cluster):
    p = create_profile(client, user, cluster["id"])
    t = make_ticket(client, user, cluster["board_id"], profile_id=p["id"])
    assert t["profile_id"] == p["id"]


def test_create_ticket_with_profile_from_another_cluster_400s(client, user, cluster):
    other_cluster = client.post("/api/clusters", json={"name": "Other"},
                                headers=user["headers"]).json()
    p = create_profile(client, user, other_cluster["id"])
    r = client.post(f"/api/boards/{cluster['board_id']}/tickets",
                    json={"title": "t", "profile_id": p["id"]}, headers=user["headers"])
    assert r.status_code == 400


def test_patch_ticket_sets_and_clears_profile(client, user, cluster):
    p = create_profile(client, user, cluster["id"])
    t = make_ticket(client, user, cluster["board_id"])
    r = client.patch(f"/api/tickets/{t['id']}", headers=user["headers"],
                     json={"profile_id": p["id"]})
    assert r.status_code == 200, r.text
    assert r.json()["profile_id"] == p["id"]
    r = client.patch(f"/api/tickets/{t['id']}", headers=user["headers"],
                     json={"clear_profile": True})
    assert r.status_code == 200, r.text
    assert r.json()["profile_id"] is None


def test_patch_ticket_profile_must_be_in_the_same_cluster(client, user, cluster):
    other_cluster = client.post("/api/clusters", json={"name": "Other"},
                                headers=user["headers"]).json()
    p = create_profile(client, user, other_cluster["id"])
    t = make_ticket(client, user, cluster["board_id"])
    r = client.patch(f"/api/tickets/{t['id']}", headers=user["headers"],
                     json={"profile_id": p["id"]})
    assert r.status_code == 400


# ---------- deleting a referenced profile leaves a dangling id, on purpose ----------
# This is the API-level half of "unknown profile falls back" — the DB-level
# half (worker.resolve_profile actually falling back) is covered directly in
# tests/test_worker.py.

def test_deleting_a_profile_leaves_dangling_references_instead_of_nulling_them(client, user, cluster):
    p = create_profile(client, user, cluster["id"])
    client.patch(f"/api/boards/{cluster['board_id']}", headers=user["headers"],
                json={"default_profile_id": p["id"]})
    t = make_ticket(client, user, cluster["board_id"], profile_id=p["id"])

    client.delete(f"/api/profiles/{p['id']}", headers=user["headers"])

    board = client.get(f"/api/clusters/{cluster['id']}/boards",
                       headers=user["headers"]).json()[0]
    ticket = client.get(f"/api/boards/{cluster['board_id']}/tickets",
                        headers=user["headers"]).json()[0]
    assert board["default_profile_id"] == p["id"]
    assert ticket["id"] == t["id"]
    assert ticket["profile_id"] == p["id"]
