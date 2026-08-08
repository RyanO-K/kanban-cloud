"""v2 enrollment route: join-code gate, role provisioning, DSN response."""
import pytest

from app import enrollment


@pytest.fixture()
def fake_provisioning(monkeypatch):
    """Pretend the SQLite test engine can provision roles; capture calls."""
    calls = {"provisioned": [], "revoked": []}
    monkeypatch.setattr(enrollment, "can_provision", lambda engine: True)
    monkeypatch.setattr(enrollment, "ensure_worker_group", lambda engine: None)
    monkeypatch.setattr(
        enrollment, "provision_role",
        lambda engine, role: calls["provisioned"].append(role) or "fakepw",
    )
    monkeypatch.setattr(
        enrollment, "build_worker_dsn",
        lambda admin_url, role, pw: f"postgresql://{role}:{pw}@fakehost/db?sslmode=require",
    )
    monkeypatch.setattr(
        enrollment, "revoke_role",
        lambda engine, role: calls["revoked"].append(role),
    )
    return calls


def test_enroll_requires_postgres(client, cluster):
    r = client.post("/api/workers/enroll",
                    json={"join_code": cluster["join_code"], "name": "pc1"})
    assert r.status_code == 400
    assert "Postgres" in r.json()["detail"]


def test_enroll_provisions_role_and_returns_dsn(client, cluster, fake_provisioning):
    r = client.post("/api/workers/enroll",
                    json={"join_code": cluster["join_code"], "name": "pc1"})
    assert r.status_code == 200, r.text
    data = r.json()
    role = enrollment.role_name_for(cluster["id"], data["worker_id"])
    assert data["cluster"]["id"] == cluster["id"]
    assert data["dsn"] == f"postgresql://{role}:fakepw@fakehost/db?sslmode=require"
    assert fake_provisioning["provisioned"] == [role]


def test_enroll_bad_join_code_404(client, fake_provisioning):
    r = client.post("/api/workers/enroll", json={"join_code": "NOPE1234", "name": "pc1"})
    assert r.status_code == 404


def test_enroll_blank_name_400(client, cluster, fake_provisioning):
    r = client.post("/api/workers/enroll",
                    json={"join_code": cluster["join_code"], "name": "  "})
    assert r.status_code == 400


def test_reenroll_same_name_reuses_worker_and_unrevokes(client, user, cluster, fake_provisioning):
    r1 = client.post("/api/workers/enroll",
                     json={"join_code": cluster["join_code"], "name": "pc1"})
    r2 = client.post("/api/workers/enroll",
                     json={"join_code": cluster["join_code"], "name": "pc1"})
    assert r1.json()["worker_id"] == r2.json()["worker_id"]
    assert len(fake_provisioning["provisioned"]) == 2  # password rotated
    workers = client.get(f"/api/clusters/{cluster['id']}/workers",
                         headers=user["headers"]).json()
    assert workers[0]["revoked"] is False


def test_old_worker_routes_are_gone(client):
    assert client.post("/api/workers/register",
                       json={"join_code": "X", "name": "n"}).status_code in (404, 405)
    assert client.post("/api/work/poll").status_code in (404, 405)
    assert client.post("/api/work/1/result", json={"ok": True}).status_code in (404, 405)
    assert client.post("/api/work/1/progress", json={"message": "hi"}).status_code in (404, 405)
