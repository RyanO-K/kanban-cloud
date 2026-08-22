"""Website-side worker control (ticket #18): rename a PC and set the
concurrency limit it should run at, from the UI rather than only on the PC."""
from sqlalchemy import text


def _insert_worker(client, cluster_id, name="pc"):
    engine = client.app.state.engine
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO workers (cluster_id, name, revoked, status, concurrency,"
            " running, last_seen, created_at) VALUES"
            " (:c,:n,0,'idle',1,0,'2030-01-01','2030-01-01')"
        ), {"c": cluster_id, "n": name})


def _worker_id(client, user, cluster_id, name):
    workers = client.get(f"/api/clusters/{cluster_id}/workers", headers=user["headers"]).json()
    return next(w["id"] for w in workers if w["name"] == name)


def test_workers_list_includes_desired_concurrency_defaulting_to_none(client, user, cluster):
    _insert_worker(client, cluster["id"], "pc")
    w = client.get(f"/api/clusters/{cluster['id']}/workers", headers=user["headers"]).json()[0]
    assert w["desired_concurrency"] is None


def test_patch_worker_renames_it(client, user, cluster):
    _insert_worker(client, cluster["id"], "pc")
    wid = _worker_id(client, user, cluster["id"], "pc")
    r = client.patch(f"/api/workers/{wid}", headers=user["headers"], json={"name": "build-box"})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "build-box"
    workers = client.get(f"/api/clusters/{cluster['id']}/workers", headers=user["headers"]).json()
    assert workers[0]["name"] == "build-box"


def test_patch_worker_sets_desired_concurrency(client, user, cluster):
    _insert_worker(client, cluster["id"], "pc")
    wid = _worker_id(client, user, cluster["id"], "pc")
    r = client.patch(f"/api/workers/{wid}", headers=user["headers"],
                     json={"desired_concurrency": 4})
    assert r.status_code == 200, r.text
    assert r.json()["desired_concurrency"] == 4


def test_patch_worker_can_clear_desired_concurrency(client, user, cluster):
    _insert_worker(client, cluster["id"], "pc")
    wid = _worker_id(client, user, cluster["id"], "pc")
    client.patch(f"/api/workers/{wid}", headers=user["headers"], json={"desired_concurrency": 4})
    r = client.patch(f"/api/workers/{wid}", headers=user["headers"],
                     json={"clear_desired_concurrency": True})
    assert r.status_code == 200, r.text
    assert r.json()["desired_concurrency"] is None


def test_patch_worker_is_partial(client, user, cluster):
    """Renaming must not blank a previously-set concurrency request."""
    _insert_worker(client, cluster["id"], "pc")
    wid = _worker_id(client, user, cluster["id"], "pc")
    client.patch(f"/api/workers/{wid}", headers=user["headers"], json={"desired_concurrency": 2})
    client.patch(f"/api/workers/{wid}", headers=user["headers"], json={"name": "renamed"})
    r = client.get(f"/api/clusters/{cluster['id']}/workers", headers=user["headers"]).json()[0]
    assert r["name"] == "renamed"
    assert r["desired_concurrency"] == 2


def test_patch_worker_rejects_a_concurrency_below_one(client, user, cluster):
    _insert_worker(client, cluster["id"], "pc")
    wid = _worker_id(client, user, cluster["id"], "pc")
    r = client.patch(f"/api/workers/{wid}", headers=user["headers"],
                     json={"desired_concurrency": 0})
    assert r.status_code == 400


def test_patch_worker_rejects_a_blank_name(client, user, cluster):
    _insert_worker(client, cluster["id"], "pc")
    wid = _worker_id(client, user, cluster["id"], "pc")
    r = client.patch(f"/api/workers/{wid}", headers=user["headers"], json={"name": "   "})
    assert r.status_code == 400


def test_patch_worker_rejects_a_name_collision_within_the_cluster(client, user, cluster):
    _insert_worker(client, cluster["id"], "pc-1")
    _insert_worker(client, cluster["id"], "pc-2")
    wid = _worker_id(client, user, cluster["id"], "pc-2")
    r = client.patch(f"/api/workers/{wid}", headers=user["headers"], json={"name": "pc-1"})
    assert r.status_code == 400


def test_patch_worker_in_another_cluster_is_forbidden(client, user, cluster):
    _insert_worker(client, cluster["id"], "pc")
    wid = _worker_id(client, user, cluster["id"], "pc")
    other = client.post("/api/register",
                        json={"email": "z@x.co", "password": "pass1234"}).json()
    headers = {"Authorization": f"Bearer {other['token']}"}
    r = client.patch(f"/api/workers/{wid}", headers=headers, json={"name": "mine now"})
    assert r.status_code == 403


def test_patch_unknown_worker_404s(client, user):
    r = client.patch("/api/workers/99999", headers=user["headers"], json={"name": "x"})
    assert r.status_code == 404
