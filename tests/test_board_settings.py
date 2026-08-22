"""Board-level project metadata: the context an agent needs to work a repo."""


def test_list_boards_includes_project_metadata(client, user, cluster):
    boards = client.get(f"/api/clusters/{cluster['id']}/boards",
                        headers=user["headers"]).json()
    assert boards[0]["description"] is None
    assert boards[0]["out_of_scope"] is None
    assert boards[0]["commit_requirements"] is None
    assert boards[0]["use_worktrees"] is False


def test_patch_board_sets_metadata(client, user, cluster):
    r = client.patch(f"/api/boards/{cluster['board_id']}", headers=user["headers"],
                     json={"description": "The invoicing app.",
                           "commit_requirements": "All tests must pass.",
                           "use_worktrees": True})
    assert r.status_code == 200, r.text
    assert r.json()["description"] == "The invoicing app."
    assert r.json()["use_worktrees"] is True
    boards = client.get(f"/api/clusters/{cluster['id']}/boards",
                        headers=user["headers"]).json()
    assert boards[0]["commit_requirements"] == "All tests must pass."


def test_patch_board_is_partial(client, user, cluster):
    """One panel saving must not blank a field it did not send."""
    bid = cluster["board_id"]
    client.patch(f"/api/boards/{bid}", headers=user["headers"],
                 json={"description": "keep me", "out_of_scope": "not the CSS"})
    client.patch(f"/api/boards/{bid}", headers=user["headers"],
                 json={"use_worktrees": True})
    r = client.get(f"/api/clusters/{cluster['id']}/boards",
                   headers=user["headers"]).json()[0]
    assert r["description"] == "keep me"
    assert r["out_of_scope"] == "not the CSS"
    assert r["use_worktrees"] is True


def test_patch_board_can_clear_a_field_with_an_empty_string(client, user, cluster):
    bid = cluster["board_id"]
    client.patch(f"/api/boards/{bid}", headers=user["headers"],
                 json={"description": "temporary"})
    client.patch(f"/api/boards/{bid}", headers=user["headers"],
                 json={"description": ""})
    r = client.get(f"/api/clusters/{cluster['id']}/boards",
                   headers=user["headers"]).json()[0]
    assert r["description"] == ""


def test_patch_board_in_another_cluster_is_forbidden(client, user, cluster):
    other = client.post("/api/register",
                        json={"email": "z@x.co", "password": "pass1234"}).json()
    headers = {"Authorization": f"Bearer {other['token']}"}
    r = client.patch(f"/api/boards/{cluster['board_id']}", headers=headers,
                     json={"description": "mine now"})
    assert r.status_code == 403


def test_patch_unknown_board_404s(client, user):
    r = client.patch("/api/boards/99999", headers=user["headers"],
                     json={"description": "x"})
    assert r.status_code == 404


def test_create_board_returns_the_metadata_shape(client, user, cluster):
    r = client.post(f"/api/clusters/{cluster['id']}/boards",
                    json={"name": "second"}, headers=user["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["use_worktrees"] is False
    assert r.json()["description"] is None


def test_workers_api_reports_slot_counts(client, user, cluster):
    from sqlalchemy import text

    engine = client.app.state.engine
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO workers (cluster_id, name, revoked, status, concurrency,"
            " running, last_seen, created_at) VALUES"
            " (:c,'pc',0,'working',3,2,'2030-01-01','2030-01-01')"
        ), {"c": cluster["id"]})
    w = client.get(f"/api/clusters/{cluster['id']}/workers",
                   headers=user["headers"]).json()[0]
    assert w["concurrency"] == 3
    assert w["running"] == 2


def test_list_boards_includes_repo_url(client, user, cluster):
    boards = client.get(f"/api/clusters/{cluster['id']}/boards",
                        headers=user["headers"]).json()
    assert boards[0]["repo_url"] is None


def test_patch_board_sets_repo_url(client, user, cluster):
    r = client.patch(f"/api/boards/{cluster['board_id']}", headers=user["headers"],
                     json={"repo_url": "https://github.com/org/repo.git"})
    assert r.status_code == 200, r.text
    assert r.json()["repo_url"] == "https://github.com/org/repo.git"


def test_patch_board_repo_url_is_partial_like_the_other_fields(client, user, cluster):
    bid = cluster["board_id"]
    client.patch(f"/api/boards/{bid}", headers=user["headers"],
                 json={"repo_url": "https://github.com/org/repo.git"})
    client.patch(f"/api/boards/{bid}", headers=user["headers"],
                 json={"use_worktrees": True})
    r = client.get(f"/api/clusters/{cluster['id']}/boards",
                   headers=user["headers"]).json()[0]
    assert r["repo_url"] == "https://github.com/org/repo.git"
    assert r["use_worktrees"] is True


# ---------- auto_push (ticket #15: commit gate / auto-commit) ----------

def test_list_boards_includes_auto_push_off_by_default(client, user, cluster):
    boards = client.get(f"/api/clusters/{cluster['id']}/boards",
                        headers=user["headers"]).json()
    assert boards[0]["auto_push"] is False


def test_patch_board_sets_auto_push(client, user, cluster):
    r = client.patch(f"/api/boards/{cluster['board_id']}", headers=user["headers"],
                     json={"auto_push": True})
    assert r.status_code == 200, r.text
    assert r.json()["auto_push"] is True
    boards = client.get(f"/api/clusters/{cluster['id']}/boards",
                        headers=user["headers"]).json()
    assert boards[0]["auto_push"] is True


def test_patch_board_auto_push_is_partial_like_the_other_fields(client, user, cluster):
    bid = cluster["board_id"]
    client.patch(f"/api/boards/{bid}", headers=user["headers"], json={"auto_push": True})
    client.patch(f"/api/boards/{bid}", headers=user["headers"],
                 json={"description": "keep auto_push as-is"})
    r = client.get(f"/api/clusters/{cluster['id']}/boards",
                   headers=user["headers"]).json()[0]
    assert r["auto_push"] is True
    assert r["description"] == "keep auto_push as-is"
