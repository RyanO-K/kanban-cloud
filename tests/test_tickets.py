import datetime

import pytest
from sqlalchemy import text

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
    assert client.get(f"/api/clusters/{cluster['id']}/workers", headers=other).status_code == 403

    # joining with the code grants access
    j = client.post("/api/clusters/join", json={"join_code": cluster["join_code"]}, headers=other)
    assert j.status_code == 200
    assert client.get(f"/api/boards/{cluster['board_id']}/tickets", headers=other).status_code == 200


def test_create_ticket_honors_requested_status(client, user, cluster):
    t = make_ticket(client, user, cluster["board_id"], title="Started elsewhere", status="doing")
    assert t["status"] == "doing"

    t2 = make_ticket(client, user, cluster["board_id"], title="Queue me", status="ready")
    assert t2["status"] == "ready"
    q = client.get(f"/api/clusters/{cluster['id']}/queue", headers=user["headers"]).json()
    assert [i["ticket_id"] for i in q] == [t2["id"]]  # only the ready one queued

    r = client.post(f"/api/boards/{cluster['board_id']}/tickets",
                    json={"title": "bad", "status": "bogus"}, headers=user["headers"])
    assert r.status_code == 400


def test_create_ticket_rejects_foreign_target_worker(client, user, cluster):
    from sqlalchemy import text

    from app.models import utcnow

    c2 = client.post("/api/clusters", json={"name": "Other"}, headers=user["headers"]).json()
    engine = client.app.state.engine
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO workers (cluster_id, name, revoked, status, last_seen, created_at) "
            "VALUES (:c, 'other-pc', 0, 'idle', :n, :n)"
        ), {"c": c2["id"], "n": utcnow()})
        foreign_id = conn.execute(
            text("SELECT id FROM workers WHERE cluster_id = :c AND name = 'other-pc'"),
            {"c": c2["id"]},
        ).scalar_one()
    r = client.post(f"/api/boards/{cluster['board_id']}/tickets",
                    json={"title": "x", "target_worker": foreign_id},
                    headers=user["headers"])
    assert r.status_code == 400


# ---------- commit_gate round trip (ticket #15) ----------
# The real write is worker.finish_work's raw SQL (Postgres-only, covered
# against a fake cursor in tests/test_commit_gate.py); simulated here with
# direct SQL against the test DB, same convention test_blocked_endpoint.py
# uses for a worker's raise_question.

def set_commit_gate(client, ticket_id, requirements_met, summary):
    import json

    from sqlalchemy import text

    engine = client.app.state.engine
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE tickets SET commit_gate=:g WHERE id=:t"),
            {"g": json.dumps({"requirements_met": requirements_met, "summary": summary}),
             "t": ticket_id},
        )


def test_ticket_has_no_commit_gate_by_default(client, user, cluster):
    t = make_ticket(client, user, cluster["board_id"])
    assert t["commit_gate"] is None


def test_commit_gate_round_trips_to_the_ticket_api(client, user, cluster):
    t = make_ticket(client, user, cluster["board_id"])
    set_commit_gate(client, t["id"], True, "Ran the suite, all green.")
    listed = client.get(f"/api/boards/{cluster['board_id']}/tickets",
                        headers=user["headers"]).json()
    fresh = [x for x in listed if x["id"] == t["id"]][0]
    assert fresh["commit_gate"] == {"requirements_met": True,
                                    "summary": "Ran the suite, all green."}


def test_commit_gate_round_trips_an_unmet_verdict(client, user, cluster):
    t = make_ticket(client, user, cluster["board_id"])
    set_commit_gate(client, t["id"], False, "Two tests still fail.")
    listed = client.get(f"/api/boards/{cluster['board_id']}/tickets",
                        headers=user["headers"]).json()
    fresh = [x for x in listed if x["id"] == t["id"]][0]
    assert fresh["commit_gate"] == {"requirements_met": False,
                                    "summary": "Two tests still fail."}


# ---------- resume command: the session fields the board UI copies ----------
#
# A human takeover is `cd '<session_dir>'; claude --resume <session_id>`, run
# on the PC that owns the session. All three halves have to reach the client,
# so ticket_json carries them; the command string itself is built in the UI.

def _record_session(client, ticket_id, worker_name="ryan-pc",
                    session_id="sess-abc", session_dir=r"C:\repos\board-1"):
    """Simulate what a worker does over a run: enroll, then report back the
    session it minted and the directory it actually ran in."""
    engine = client.app.state.engine
    with engine.begin() as conn:
        wid = conn.execute(text(
            "INSERT INTO workers (cluster_id, name, revoked, status, concurrency,"
            " running, last_seen, created_at) "
            "VALUES (1, :n, 0, 'idle', 1, 0, :now, :now) RETURNING id"
        ), {"n": worker_name, "now": datetime.datetime.utcnow()}).scalar_one()
        conn.execute(text(
            "UPDATE tickets SET session_id=:s, session_dir=:d, assigned_worker=:w "
            "WHERE id=:t"
        ), {"s": session_id, "d": session_dir, "w": wid, "t": ticket_id})
    return wid


def _reread(client, user, ticket_id):
    return client.patch(f"/api/tickets/{ticket_id}", json={},
                        headers=user["headers"]).json()


def test_ticket_json_exposes_the_session_id_and_dir(client, user, cluster):
    t = make_ticket(client, user, cluster["board_id"])
    _record_session(client, t["id"])
    fresh = _reread(client, user, t["id"])
    assert fresh["session_id"] == "sess-abc"
    assert fresh["session_dir"] == r"C:\repos\board-1"


def test_ticket_json_names_the_worker_that_owns_the_session(client, user, cluster):
    """The transcript only exists on the PC that ran it, so the UI has to say
    which one — a session id the human can't act on is not enough."""
    t = make_ticket(client, user, cluster["board_id"])
    _record_session(client, t["id"], worker_name="studio-pc")
    assert _reread(client, user, t["id"])["session_worker"] == "studio-pc"


def test_a_ticket_that_never_ran_reports_no_session(client, user, cluster):
    """Nothing to resume: the UI renders no copy button for these."""
    t = make_ticket(client, user, cluster["board_id"])
    assert t["session_id"] is None
    assert t["session_dir"] is None
    assert t["session_worker"] is None
    assert t["resume_command"] is None


# ---------- the command string itself ----------
#
# Built here rather than in the browser so its fallbacks are actually tested:
# there is no JS runner in this repo (see tests/test_frontend_markup.py), so
# anything assembled in index.html is only ever checked by eye.

def test_resume_command_pairs_the_directory_with_the_session():
    """`cd` first: the CLI resolves a session id against the working
    directory, so the id alone finds nothing from anywhere else."""
    from app.main import build_resume_command

    assert (build_resume_command("sess-abc", r"C:\repos\board-1")
            == r"cd 'C:\repos\board-1'; claude --resume sess-abc")


def test_resume_command_without_a_directory_is_still_offered():
    """A ticket whose last run predates session_dir still has a session id
    worth handing over — the human supplies the folder."""
    from app.main import build_resume_command

    assert build_resume_command("sess-abc", None) == "claude --resume sess-abc"


def test_resume_command_is_none_without_a_session():
    from app.main import build_resume_command

    assert build_resume_command(None, r"C:\repos\board-1") is None
    assert build_resume_command(None, None) is None


def test_ticket_json_carries_the_assembled_resume_command(client, user, cluster):
    t = make_ticket(client, user, cluster["board_id"])
    _record_session(client, t["id"])
    fresh = _reread(client, user, t["id"])
    assert fresh["resume_command"] == r"cd 'C:\repos\board-1'; claude --resume sess-abc"


# ---------- takeover for a board that ran over ssh ----------
#
# The worker claims and streams from here, but the CLI ran on another machine
# (worker.py --set-ssh), so the transcript is on *that* disk. The command has
# to travel or it resumes nothing.

def _record_remote_session(client, ticket_id, host="ryan@studio",
                           session_dir="/srv/site-page"):
    engine = client.app.state.engine
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE tickets SET session_id=:s, session_dir=:d, session_host=:h "
            "WHERE id=:t"
        ), {"s": "sess-abc", "d": session_dir, "h": host, "t": ticket_id})


def test_resume_command_travels_to_the_host_that_holds_the_transcript():
    """`-t` because the resumed session is interactive: without a tty the CLI
    comes up attached to a dead pipe. `&&` rather than the local spelling's
    `;` since the far end is POSIX."""
    from app.main import build_resume_command

    assert (build_resume_command("sess-abc", "/srv/site-page", "ryan@studio")
            == 'ssh ryan@studio -t "cd \'/srv/site-page\' && claude --resume sess-abc"')


def test_remote_resume_command_without_a_directory_still_travels():
    from app.main import build_resume_command

    assert (build_resume_command("sess-abc", None, "ryan@studio")
            == 'ssh ryan@studio -t "claude --resume sess-abc"')


def test_no_host_still_gives_the_plain_local_command():
    """The overwhelmingly common case, and the one every existing row is."""
    from app.main import build_resume_command

    assert (build_resume_command("sess-abc", r"C:\repos\board-1", None)
            == r"cd 'C:\repos\board-1'; claude --resume sess-abc")


def test_ticket_json_exposes_the_session_host(client, user, cluster):
    t = make_ticket(client, user, cluster["board_id"])
    _record_remote_session(client, t["id"])
    fresh = _reread(client, user, t["id"])
    assert fresh["session_host"] == "ryan@studio"
    assert fresh["resume_command"].startswith("ssh ryan@studio -t ")


def test_a_local_ticket_reports_no_session_host(client, user, cluster):
    t = make_ticket(client, user, cluster["board_id"])
    _record_session(client, t["id"])
    assert _reread(client, user, t["id"])["session_host"] is None


