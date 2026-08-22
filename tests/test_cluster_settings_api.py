"""Cluster-wide settings API (gap analysis phase 2, item 5): the Settings
panel's cap + stop-all + in-flight count. Enforcement itself lives in
worker.cluster_claim_gate; this covers the read/write surface only.
"""
from sqlalchemy import text

from tests.conftest import make_ticket


def test_new_cluster_gets_default_settings(client, user, cluster):
    r = client.get(f"/api/clusters/{cluster['id']}/settings", headers=user["headers"])
    assert r.status_code == 200, r.text
    assert r.json() == {
        "enabled": False, "concurrency_cap": None,
        "stop_all_requested": False, "in_flight": 0,
    }


def test_put_settings_round_trips(client, user, cluster):
    r = client.put(f"/api/clusters/{cluster['id']}/settings", headers=user["headers"],
                   json={"enabled": True, "concurrency_cap": 5, "stop_all_requested": False})
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is True
    assert r.json()["concurrency_cap"] == 5

    r = client.get(f"/api/clusters/{cluster['id']}/settings", headers=user["headers"])
    assert r.json() == {
        "enabled": True, "concurrency_cap": 5,
        "stop_all_requested": False, "in_flight": 0,
    }


def test_put_settings_can_clear_the_cap(client, user, cluster):
    client.put(f"/api/clusters/{cluster['id']}/settings", headers=user["headers"],
              json={"enabled": True, "concurrency_cap": 5, "stop_all_requested": False})
    r = client.put(f"/api/clusters/{cluster['id']}/settings", headers=user["headers"],
                   json={"enabled": False, "concurrency_cap": None, "stop_all_requested": False})
    assert r.status_code == 200, r.text
    assert r.json()["concurrency_cap"] is None


def test_put_settings_rejects_a_non_positive_cap(client, user, cluster):
    r = client.put(f"/api/clusters/{cluster['id']}/settings", headers=user["headers"],
                   json={"enabled": True, "concurrency_cap": 0, "stop_all_requested": False})
    assert r.status_code == 400


def test_put_settings_can_set_stop_all(client, user, cluster):
    r = client.put(f"/api/clusters/{cluster['id']}/settings", headers=user["headers"],
                   json={"enabled": False, "concurrency_cap": None, "stop_all_requested": True})
    assert r.status_code == 200, r.text
    assert r.json()["stop_all_requested"] is True


def test_settings_reports_the_current_in_flight_count(client, user, cluster):
    t1 = make_ticket(client, user, cluster["board_id"], status="ready")
    make_ticket(client, user, cluster["board_id"], status="ready")
    engine = client.app.state.engine
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE work_queue SET status='claimed', claimed_by=999 WHERE ticket_id=:t"
        ), {"t": t1["id"]})
    r = client.get(f"/api/clusters/{cluster['id']}/settings", headers=user["headers"])
    assert r.json()["in_flight"] == 1


def test_settings_are_scoped_to_membership(client, user, cluster):
    other = client.post("/api/register", json={"email": "z@x.co", "password": "pass1234"}).json()
    headers = {"Authorization": f"Bearer {other['token']}"}
    r = client.get(f"/api/clusters/{cluster['id']}/settings", headers=headers)
    assert r.status_code == 403
    r = client.put(f"/api/clusters/{cluster['id']}/settings", headers=headers,
                   json={"enabled": True, "concurrency_cap": 1, "stop_all_requested": False})
    assert r.status_code == 403


def test_unknown_cluster_settings_404s(client, user):
    r = client.get("/api/clusters/99999/settings", headers=user["headers"])
    assert r.status_code == 404
