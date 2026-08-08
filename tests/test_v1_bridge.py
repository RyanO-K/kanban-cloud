"""Test that v1 worker HTTP API routes return 410 Gone."""


def test_worker_register_returns_410(client):
    r = client.post("/api/workers/register", json={"join_code": "CODE1234", "name": "pc"})
    assert r.status_code == 410
    assert "Gone" in r.json()["detail"]


def test_work_poll_returns_410(client):
    r = client.post("/api/work/poll")
    assert r.status_code == 410
    assert "Gone" in r.json()["detail"]


def test_work_result_returns_410(client):
    r = client.post("/api/work/1/result", json={"ok": True})
    assert r.status_code == 410
    assert "Gone" in r.json()["detail"]


def test_work_progress_returns_410(client):
    r = client.post("/api/work/1/progress", json={"message": "hi"})
    assert r.status_code == 410
    assert "Gone" in r.json()["detail"]
