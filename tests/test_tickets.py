from conftest import make_ticket


def test_ticket_crud(client, user, cluster):
    t = make_ticket(client, user, cluster["board_id"], title="First")
    assert t["status"] == "todo" and t["title"] == "First"

    # list
    lst = client.get(f"/api/boards/{cluster['board_id']}/tickets", headers=user["headers"]).json()
    assert [x["id"] for x in lst] == [t["id"]]

    # edit + move
    r = client.patch(f"/api/tickets/{t['id']}", json={"title": "Renamed", "status": "doing"},
                     headers=user["headers"])
    assert r.status_code == 200
    assert r.json()["title"] == "Renamed" and r.json()["status"] == "doing"

    # bad status rejected
    assert client.patch(f"/api/tickets/{t['id']}", json={"status": "bogus"},
                        headers=user["headers"]).status_code == 400

    # comment
    r = client.post(f"/api/tickets/{t['id']}/comments", json={"message": "hi"},
                    headers=user["headers"])
    assert r.json()["comments"][0]["writer"] == user["email"]

    # delete
    assert client.delete(f"/api/tickets/{t['id']}", headers=user["headers"]).json()["ok"] is True
    lst = client.get(f"/api/boards/{cluster['board_id']}/tickets", headers=user["headers"]).json()
    assert lst == []


def test_cluster_scoping_blocks_outsiders(client, user, cluster):
    t = make_ticket(client, user, cluster["board_id"])
    # second user, not a member of the cluster
    r = client.post("/api/register", json={"email": "other@x.com", "password": "pass1234"})
    other = {"Authorization": f"Bearer {r.json()['token']}"}
    assert client.get(f"/api/boards/{cluster['board_id']}/tickets", headers=other).status_code == 403
    assert client.patch(f"/api/tickets/{t['id']}", json={"status": "done"}, headers=other).status_code == 403
    assert client.get(f"/api/clusters/{cluster['id']}/settings", headers=other).status_code == 403

    # joining with the code grants access
    j = client.post("/api/clusters/join", json={"join_code": cluster["join_code"]}, headers=other)
    assert j.status_code == 200
    assert client.get(f"/api/boards/{cluster['board_id']}/tickets", headers=other).status_code == 200


def test_api_key_masked_in_browser_responses(client, user, cluster):
    key = "sk-ant-api03-verysecretkey-abcd"
    r = client.put(f"/api/clusters/{cluster['id']}/settings", json={"claude_api_key": key},
                   headers=user["headers"])
    assert r.status_code == 200
    assert key not in r.text  # masked even in the save response

    g = client.get(f"/api/clusters/{cluster['id']}/settings", headers=user["headers"])
    body = g.json()
    assert body["has_key"] is True
    assert key not in g.text
    assert body["claude_api_key_masked"].endswith("abcd")
