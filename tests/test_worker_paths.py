"""Per-PC board paths: the machine-level half of "where does this agent run".

The server never learns these paths — the same board is worked by machines with
different layouts, so the folder is this PC's business.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import worker  # noqa: E402


class FakeCursor:
    """Minimal psycopg cursor over a canned board list."""

    def __init__(self, rows):
        self._rows = rows
        # Every execute, in order: claim_next issues several per call and the
        # later ones would otherwise clobber the one under test.
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, rows=()):
        self._rows = rows
        self.cursors = []

    def cursor(self):
        cur = FakeCursor(self._rows)
        self.cursors.append(cur)
        return cur

    def transaction(self):
        class _T:
            def __enter__(self_):
                return self_

            def __exit__(self_, *a):
                return False

        return _T()


BOARDS = [(4, "site-page"), (7, "devtool-invoice")]


# ---------- parsing ----------

def test_parse_set_path_splits_on_the_first_equals():
    """Windows paths can contain '=', so only the first one separates."""
    assert worker.parse_set_path("4=C:/repos/a=b") == ("4", "C:/repos/a=b")


def test_parse_set_path_rejects_missing_equals():
    with pytest.raises(ValueError):
        worker.parse_set_path("4")


def test_parse_set_path_rejects_empty_halves():
    with pytest.raises(ValueError):
        worker.parse_set_path("=C:/repos")
    with pytest.raises(ValueError):
        worker.parse_set_path("4=")


# ---------- board resolution ----------

def test_resolve_board_by_id():
    assert worker.resolve_board(FakeConn(BOARDS), 1, "4") == (4, "site-page")


def test_resolve_board_by_name_is_case_insensitive():
    assert worker.resolve_board(FakeConn(BOARDS), 1, "SITE-page") == (4, "site-page")


def test_resolve_board_rejects_unknown_id():
    with pytest.raises(ValueError, match="no board with id"):
        worker.resolve_board(FakeConn(BOARDS), 1, "99")


def test_resolve_board_rejects_unknown_name_and_lists_the_real_ones():
    with pytest.raises(ValueError, match="site-page"):
        worker.resolve_board(FakeConn(BOARDS), 1, "nope")


def test_resolve_board_rejects_ambiguous_name():
    """Guessing would send an agent into the wrong repo."""
    with pytest.raises(ValueError, match="matches 2 boards"):
        worker.resolve_board(FakeConn([(1, "dup"), (2, "DUP")]), 1, "dup")


# ---------- saving ----------

def test_apply_set_path_requires_an_existing_directory(tmp_path):
    with pytest.raises(ValueError, match="does not exist"):
        worker.apply_set_path(FakeConn(BOARDS), {"cluster_id": 1},
                              f"4={tmp_path / 'missing'}")


def test_apply_set_path_saves_keyed_by_board_id(tmp_path, monkeypatch):
    """Keyed by id, not name: renaming a board must not orphan the path."""
    monkeypatch.setattr(worker, "CONFIG_PATH", tmp_path / "cfg.json")
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = {"cluster_id": 1, "worker_id": 2, "dsn": "x", "name": "pc"}
    out = worker.apply_set_path(FakeConn(BOARDS), cfg, f"site-page={repo}")
    assert out["boards"] == {"4": str(repo)}
    assert worker.load_config()["boards"] == {"4": str(repo)}


def test_apply_set_path_overwrites_an_existing_mapping(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "CONFIG_PATH", tmp_path / "cfg.json")
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    cfg = {"cluster_id": 1, "boards": {"4": str(a)}}
    out = worker.apply_set_path(FakeConn(BOARDS), cfg, f"4={b}")
    assert out["boards"] == {"4": str(b)}


# ---------- reading ----------

def test_configured_board_ids_are_ints():
    assert sorted(worker.configured_board_ids({"boards": {"4": "/a", "7": "/b"}})) == [4, 7]


def test_configured_board_ids_empty_when_unset():
    assert worker.configured_board_ids({}) == []


def test_configured_board_ids_skips_junk_keys():
    assert worker.configured_board_ids({"boards": {"4": "/a", "oops": "/b"}}) == [4]


def test_board_paths_defaults_to_empty():
    assert worker.board_paths({}) == {}


# ---------- claim filter ----------

def test_claim_sql_filters_on_configured_boards():
    """The predicate must live in the SQL. Filtering after the fact would claim
    the row first and then have to abandon it."""
    assert "t.board_id = ANY(" in worker.CLAIM_SQL


def test_claim_sql_also_admits_boards_with_a_repo_url():
    """A board with no --set-path entry anywhere but a repo_url configured must
    still be claimable, via an EXISTS check against the boards table — this is
    a construction-level assertion on the SQL text only (same limitation as
    test_claim_sql_filters_on_configured_boards above): CLAIM_SQL is Postgres
    syntax (SKIP LOCKED, ::int[] casts) that can't run against the SQLite/
    FakeCursor test harness in this file, so we can't execute it end-to-end
    here."""
    assert "repo_url" in worker.CLAIM_SQL
    assert "EXISTS" in worker.CLAIM_SQL.upper()


def _boards_param(cursor):
    """The `boards` param passed to CLAIM_SQL, wherever it lands among this
    cursor's calls — claim_next now issues the cluster cap gate's queries
    first (see cluster_claim_gate), so CLAIM_SQL is no longer necessarily the
    first execute() call. FakeCursor.fetchone() always returns None, so the
    gate sees "no settings row" and falls through to CLAIM_SQL unconditionally."""
    return next(params["boards"] for _, params in cursor.calls if params and "boards" in params)


def test_claim_next_passes_configured_boards():
    conn = FakeConn()
    worker.claim_next(conn, 3, 1, [4, 7])
    assert _boards_param(conn.cursors[0]) == [4, 7]


def test_claim_next_with_none_boards_disables_the_filter():
    """--stub needs no repo, so it must still be able to claim anything."""
    conn = FakeConn()
    worker.claim_next(conn, 3, 1, None)
    assert _boards_param(conn.cursors[0]) is None


# ---------- claim_next board row ----------


class FakeCursorWithBoardRow(FakeCursor):
    """Like FakeCursor, but claim_next's several fetchone() calls need to
    return a fixed sequence: the claim row, the ticket-flip row, then the
    board row."""

    def __init__(self, fetchone_sequence):
        super().__init__(rows=())
        self._sequence = list(fetchone_sequence)

    def fetchone(self):
        return self._sequence.pop(0) if self._sequence else None


class FakeConnWithBoardRow(FakeConn):
    def __init__(self, fetchone_sequence):
        super().__init__()
        self._sequence = fetchone_sequence

    def cursor(self):
        cur = FakeCursorWithBoardRow(self._sequence)
        self.cursors.append(cur)
        return cur


def test_claim_next_returns_repo_url_on_the_board_dict():
    conn = FakeConnWithBoardRow([
        None,                                                 # cap gate: no settings row -> unlimited
        (5, 9),                                             # claim: item_id, ticket_id
        (2, "Fix the thing", "Details.", 1),                 # ticket flip: board_id, title, body, attempts
        ("site-page", "Desc", None, None, False, "https://github.com/org/repo.git"),  # board row
    ])
    work = worker.claim_next(conn, worker_id=1, cluster_id=1, board_ids=None)
    assert work["board"]["repo_url"] == "https://github.com/org/repo.git"
