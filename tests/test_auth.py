def test_register_and_me(client):
    r = client.post("/api/register", json={"email": "A@x.com", "password": "secret"})
    assert r.status_code == 200
    tok = r.json()["token"]
    me = client.get("/api/me", headers={"Authorization": f"Bearer {tok}"})
    assert me.status_code == 200
    assert me.json()["email"] == "a@x.com"  # normalized lowercase


def test_duplicate_email_rejected(client):
    client.post("/api/register", json={"email": "a@x.com", "password": "secret"})
    r = client.post("/api/register", json={"email": "a@x.com", "password": "other"})
    assert r.status_code == 409


def test_login_good_and_bad_password(client):
    client.post("/api/register", json={"email": "a@x.com", "password": "secret"})
    ok = client.post("/api/login", json={"email": "a@x.com", "password": "secret"})
    assert ok.status_code == 200 and ok.json()["token"]
    bad = client.post("/api/login", json={"email": "a@x.com", "password": "wrong"})
    assert bad.status_code == 401


def test_protected_routes_require_token(client):
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/clusters").status_code == 401
    assert client.get("/api/me", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_mask_secret_never_reveals_short_values():
    from app.auth import mask_secret

    assert mask_secret(None) is None
    assert mask_secret("") is None
    assert mask_secret("abcd") == "•" * 8          # short: fully hidden
    assert mask_secret("12345678") == "•" * 8      # boundary: fully hidden
    assert mask_secret("sk-ant-api03-xyz-abcd") == "•" * 8 + "abcd"


def test_password_hash_roundtrip():
    from app.auth import hash_password, verify_password

    h = hash_password("hunter2")
    assert h != "hunter2" and h.startswith("pbkdf2$")
    assert verify_password("hunter2", h)
    assert not verify_password("hunter3", h)
    assert not verify_password("hunter2", "garbage")
