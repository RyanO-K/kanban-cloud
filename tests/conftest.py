import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import create_app  # noqa: E402


@pytest.fixture()
def client(tmp_path):
    """TestClient backed by a fresh SQLite database per test."""
    app = create_app(f"sqlite:///{tmp_path / 'test.db'}")
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def user(client):
    """A registered user; returns dict with auth headers."""
    r = client.post("/api/register", json={"email": "ryan@example.com", "password": "pass1234"})
    assert r.status_code == 200, r.text
    tok = r.json()["token"]
    return {"headers": {"Authorization": f"Bearer {tok}"}, "email": "ryan@example.com"}


@pytest.fixture()
def cluster(client, user):
    """A cluster (with default board) created by `user`."""
    r = client.post("/api/clusters", json={"name": "Team"}, headers=user["headers"])
    assert r.status_code == 200, r.text
    c = r.json()
    boards = client.get(f"/api/clusters/{c['id']}/boards", headers=user["headers"]).json()
    c["board_id"] = boards[0]["id"]
    return c


def register_worker(client, join_code, name):
    r = client.post("/api/workers/register", json={"join_code": join_code, "name": name})
    assert r.status_code == 200, r.text
    data = r.json()
    data["headers"] = {"X-Worker-Token": data["worker_token"]}
    return data


def make_ticket(client, user, board_id, title="Do a thing", **kw):
    payload = {"title": title, "body": "details", **kw}
    r = client.post(f"/api/boards/{board_id}/tickets", json=payload, headers=user["headers"])
    assert r.status_code == 200, r.text
    return r.json()
