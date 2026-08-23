"""The five-column board (ticket #20).

Three things moved together and are pinned here: the column grouping itself
(five columns over seven statuses, `review` gone), the rule that a successful
run is only *done* when its work reached the remote, and the reorder endpoint
that now addresses a column rather than a status.

The finish_work half uses the same fake cursor/connection convention as
tests/test_commit_gate.py, since the real SQL is Postgres-only (`%s`
placeholders) and worker.py's own tests never run against a live database.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import worker  # noqa: E402
from app.models import (  # noqa: E402
    BOARD_COLUMNS,
    TICKET_STATUSES,
    column_statuses,
)
from tests.conftest import make_ticket  # noqa: E402


# ---------- the column grouping ----------

def test_review_is_gone_from_the_vocabulary():
    assert "review" not in TICKET_STATUSES
    assert TICKET_STATUSES == ["todo", "ready", "doing", "blocked", "done",
                               "failed", "killed"]


def test_columns_are_the_five_the_board_shows_in_order():
    assert [key for key, _label, _statuses in BOARD_COLUMNS] == [
        "todo", "ready", "doing", "blocked", "done"]
    assert [label for _key, label, _statuses in BOARD_COLUMNS] == [
        "TODO", "Ready", "In progress", "Blocked", "Done"]


def test_every_status_lands_in_exactly_one_column():
    """A status in no column is a ticket nobody can see; a status in two is a
    ticket that renders twice and reorders unpredictably."""
    seen = [s for _key, _label, statuses in BOARD_COLUMNS for s in statuses]
    assert sorted(seen) == sorted(TICKET_STATUSES)
    assert len(seen) == len(set(seen))


def test_failed_and_killed_share_the_blocked_column():
    """They stay distinct statuses (the retry/kill machinery reads them) but
    read to a human exactly as blocked does: this one needs you."""
    assert column_statuses("blocked") == ("blocked", "failed", "killed")


def test_an_unknown_column_key_is_read_as_a_bare_status():
    """An already-loaded browser tab mid-deploy still speaks in statuses."""
    assert column_statuses("failed") == ("failed",)
    assert column_statuses("banana") == ("banana",)


# ---------- finish_work: done means committed and pushed ----------

class FakeCursor:
    def __init__(self, rowcount):
        self.calls = []
        self.rowcount = rowcount

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchone(self):
        return (1, 6)  # (attempts, board_id) — enough for the requeue branch

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, rowcount=1):
        self.rowcount = rowcount
        self.cursors = []

    def cursor(self):
        cur = FakeCursor(self.rowcount)
        self.cursors.append(cur)
        return cur

    def transaction(self):
        class _T:
            def __enter__(self_):
                return self_

            def __exit__(self_, *a):
                return False

        return _T()


def _ticket_status_write(conn):
    """The (sql, params) of the UPDATE that moved the ticket."""
    return next(p for sql, p in conn.cursors[0].calls
                if sql.startswith("UPDATE tickets SET status=%s"))


def test_a_pushed_run_lands_the_ticket_in_done():
    conn = FakeConn()
    assert worker.finish_work(conn, 1, "pc", 5, 9, True, "Done.", pushed=True) == "done"
    assert _ticket_status_write(conn) == ("done", 9)


def test_a_finished_but_unpushed_run_lands_in_blocked():
    """The run succeeded, the work is still on one PC's disk. Somebody has to
    do something about that, which is what the Blocked column means."""
    conn = FakeConn()
    assert worker.finish_work(conn, 1, "pc", 5, 9, True, "Done.", pushed=False) == "blocked"
    assert _ticket_status_write(conn) == ("blocked", 9)


def test_omitting_pushed_does_not_quietly_mark_a_ticket_done():
    """The default has to be the cautious one: a caller that cannot say the
    work landed must not be able to claim it did by saying nothing."""
    conn = FakeConn()
    assert worker.finish_work(conn, 1, "pc", 5, 9, True, "Done.") == "blocked"


def test_pushed_is_ignored_for_a_failed_run():
    """Retry behaviour is unchanged: a failure requeues on its own budget,
    whatever git did or did not do beforehand."""
    conn = FakeConn()
    assert worker.finish_work(conn, 1, "pc", 5, 9, False, "Boom.", pushed=True) == "ready"


def test_pushed_is_ignored_for_a_kill():
    conn = FakeConn()
    status = worker.finish_work(conn, 1, "pc", 5, 9, False, "Killed.",
                                killed=True, pushed=True)
    assert status == "killed"


def test_a_superseded_claim_still_writes_nothing():
    conn = FakeConn(rowcount=0)
    assert worker.finish_work(conn, 1, "pc", 5, 9, True, "Done.", pushed=True) == "superseded"
    assert not any(sql.startswith("UPDATE tickets") for sql, _ in conn.cursors[0].calls)


# ---------- the API: a column is what a drag addresses ----------

def test_api_rejects_the_retired_status(client, user, cluster):
    t = make_ticket(client, user, cluster["board_id"])
    r = client.patch(f"/api/tickets/{t['id']}", json={"status": "review"},
                     headers=user["headers"])
    assert r.status_code == 400
    r = client.post(f"/api/boards/{cluster['board_id']}/tickets",
                    json={"title": "x", "status": "review"}, headers=user["headers"])
    assert r.status_code == 400


def test_reorder_ranks_the_whole_blocked_column_across_its_statuses(client, user, cluster):
    """Dragging inside Blocked orders what the human sees — blocked, failed
    and killed cards together — not each status separately."""
    board_id = cluster["board_id"]
    a = make_ticket(client, user, board_id, title="A", status="blocked")
    b = make_ticket(client, user, board_id, title="B", status="failed")
    c = make_ticket(client, user, board_id, title="C", status="killed")

    r = client.patch(f"/api/boards/{board_id}/reorder",
                     json={"status": "blocked", "ticket_ids": [c["id"], a["id"], b["id"]]},
                     headers=user["headers"])
    assert r.status_code == 200, r.text
    assert [t["id"] for t in r.json()] == [c["id"], a["id"], b["id"]]

    rows = client.get(f"/api/boards/{board_id}/tickets", headers=user["headers"]).json()
    assert [t["id"] for t in rows if t["status"] in ("blocked", "failed", "killed")] == [
        c["id"], a["id"], b["id"]]


def test_reorder_still_rejects_a_partial_column(client, user, cluster):
    """The guard that catches a stale board must survive the widening: a
    Blocked column reorder naming only its blocked cards is a stale tab."""
    board_id = cluster["board_id"]
    a = make_ticket(client, user, board_id, title="A", status="blocked")
    make_ticket(client, user, board_id, title="B", status="failed")

    r = client.patch(f"/api/boards/{board_id}/reorder",
                     json={"status": "blocked", "ticket_ids": [a["id"]]},
                     headers=user["headers"])
    assert r.status_code == 400
