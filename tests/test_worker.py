"""worker.py v2 pure logic: config round-trip, enroll parsing, SQL invariants.

The claim/finish SQL itself is Postgres-only (SKIP LOCKED) and is exercised
by scripts/neon_smoke_v2.py against the real database.
"""
import ast
import io
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import worker  # noqa: E402


def test_config_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "CONFIG_PATH", tmp_path / "cfg.json")
    cfg = {"dsn": "postgresql://r:p@h/db", "worker_id": 1,
           "cluster_id": 2, "name": "pc", "cluster_name": "Main"}
    worker.save_config(cfg)
    assert worker.load_config() == cfg


def test_enroll_saves_full_config(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "CONFIG_PATH", tmp_path / "cfg.json")
    response = {"worker_id": 7, "cluster": {"id": 3, "name": "Main"},
                "dsn": "postgresql://worker_c3_w7:pw@h/db?sslmode=require"}

    def fake_urlopen(req, data=None, timeout=None):
        assert req.full_url == "https://srv.example/api/workers/enroll"
        body = json.loads(data.decode())
        assert body == {"join_code": "ABC12345", "name": "pc"}
        return io.BytesIO(json.dumps(response).encode())

    monkeypatch.setattr(worker.urllib.request, "urlopen", fake_urlopen)
    cfg = worker.enroll("https://srv.example", "ABC12345", "pc")
    assert cfg == {"dsn": response["dsn"], "worker_id": 7, "cluster_id": 3,
                   "name": "pc", "cluster_name": "Main"}
    assert worker.load_config() == cfg


def test_claim_sql_is_race_safe_and_utc():
    assert "FOR UPDATE OF wq SKIP LOCKED" in worker.CLAIM_SQL
    assert "now() at time zone 'utc'" in worker.CLAIM_SQL
    assert "target_worker IS NULL OR" in worker.CLAIM_SQL
    assert "LIMIT 1" in worker.CLAIM_SQL


def test_claim_sql_gates_on_unmet_dependencies():
    """The predicate must live in the claim SQL itself, not a Python check
    run after claiming — see the DEPS_MET_SQL/CLAIM_SQL docstrings."""
    assert worker.DEPS_MET_SQL in worker.CLAIM_SQL
    assert "ticket_deps" in worker.CLAIM_SQL


def _deps_met_row(conn, ticket_id):
    """Run worker.DEPS_MET_SQL for real against a scratch SQLite DB, so the
    dependency gate is proven behaviorally rather than by string match alone.
    DEPS_MET_SQL is deliberately plain ANSI SQL (unlike the rest of CLAIM_SQL,
    which needs Postgres-only SKIP LOCKED / array syntax) so it can run here
    unmodified — this executes the exact production predicate text."""
    query = f"SELECT t.id FROM tickets t WHERE t.id = ? AND {worker.DEPS_MET_SQL}"
    return conn.execute(query, (ticket_id,)).fetchall()


def test_ticket_with_unmet_dependency_is_never_claimable():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE tickets (id INTEGER PRIMARY KEY, status TEXT);
        CREATE TABLE ticket_deps (ticket_id INTEGER, depends_on_id INTEGER);
        INSERT INTO tickets VALUES (1, 'ready'), (2, 'todo');
        INSERT INTO ticket_deps VALUES (1, 2);
        """
    )
    assert _deps_met_row(conn, 1) == []


def test_ticket_becomes_claimable_only_when_its_dependency_is_done():
    """Nothing short of done releases the dependent ticket: done means
    committed and pushed, so a dependency that merely finished (and is parked
    blocked waiting for a human to land it) has nothing the next agent could
    fetch. Walks every non-done status this predicate could see."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE tickets (id INTEGER PRIMARY KEY, status TEXT);
        CREATE TABLE ticket_deps (ticket_id INTEGER, depends_on_id INTEGER);
        INSERT INTO tickets VALUES (1, 'ready'), (2, 'todo');
        INSERT INTO ticket_deps VALUES (1, 2);
        """
    )
    for status in ("todo", "ready", "doing", "blocked", "failed", "killed"):
        conn.execute("UPDATE tickets SET status=? WHERE id=2", (status,))
        assert _deps_met_row(conn, 1) == [], status

    conn.execute("UPDATE tickets SET status='done' WHERE id=2")
    assert _deps_met_row(conn, 1) == [(1,)]


def test_deps_met_sql_and_the_server_agree_on_what_counts_as_met():
    """Two copies of one rule — the claim predicate here and
    models.DEP_MET_STATUSES on the server — so pin them to each other rather
    than letting a future status change land in only one of them."""
    from app.models import DEP_MET_STATUSES

    assert DEP_MET_STATUSES == ("done",)
    assert "dep.status <> 'done'" in worker.DEPS_MET_SQL


def test_ticket_with_no_dependencies_is_unaffected():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE tickets (id INTEGER PRIMARY KEY, status TEXT);
        CREATE TABLE ticket_deps (ticket_id INTEGER, depends_on_id INTEGER);
        INSERT INTO tickets VALUES (1, 'ready');
        """
    )
    assert _deps_met_row(conn, 1) == [(1,)]


def test_max_attempts_matches_server():
    from app.models import MAX_ATTEMPTS
    assert worker.MAX_ATTEMPTS == MAX_ATTEMPTS


def test_executor_selection():
    assert worker.StubExecutor().name == "stub"
    assert worker.ClaudeExecutor().name == "claude"


class _NullCtx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeCursor(_NullCtx):
    def __init__(self, row):
        self._row = row

    def execute(self, *a, **k):
        pass

    def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self, row):
        self._row = row

    def cursor(self):
        return _FakeCursor(self._row)

    def transaction(self):
        return _NullCtx()


def test_kill_requested_reads_the_live_flag():
    assert worker.kill_requested(_FakeConn((True,)), 1) is True
    assert worker.kill_requested(_FakeConn((False,)), 1) is False


def test_kill_requested_defaults_false_when_the_row_is_gone():
    """The claim finished (or never existed) by the time we polled — treat
    that the same as "no kill", not an error."""
    assert worker.kill_requested(_FakeConn(None), 1) is False


def test_test_flag_targets_local_server():
    args = worker.build_parser().parse_args(["--enroll", "--join-code", "X", "--test"])
    assert worker.resolve_server(args) == worker.TEST_SERVER
    assert worker.TEST_SERVER != worker.DEFAULT_SERVER


def test_explicit_server_flag_overrides_test_flag():
    args = worker.build_parser().parse_args(
        ["--enroll", "--join-code", "X", "--test", "--server", "https://custom.example"]
    )
    assert worker.resolve_server(args) == "https://custom.example"


def test_no_flags_targets_default_server():
    args = worker.build_parser().parse_args(["--enroll", "--join-code", "X"])
    assert worker.resolve_server(args) == worker.DEFAULT_SERVER


# ---------- agent profiles: resolve_profile ----------

RESTRICTED = {"id": 1, "name": "restricted", "allowed_tools": "Read",
              "model": None, "system_prompt": None}
FULL = {"id": 2, "name": "full", "allowed_tools": "Read,Edit,Write,Bash,Grep,Glob",
        "model": "claude-opus-5", "system_prompt": "Be terse."}
PROFILES = {1: RESTRICTED, 2: FULL}


def test_resolve_profile_ticket_beats_board():
    assert worker.resolve_profile(PROFILES, 1, 2) == RESTRICTED


def test_resolve_profile_falls_back_to_board_when_ticket_names_none():
    assert worker.resolve_profile(PROFILES, None, 2) == FULL


def test_resolve_profile_unknown_ticket_profile_falls_back_to_board():
    """A ticket pinned to a deleted (or otherwise unknown) profile must not
    leave the agent with no tools -- it falls through to the board's default,
    exactly as if the ticket had named no profile at all."""
    assert worker.resolve_profile(PROFILES, 999, 2) == FULL


def test_resolve_profile_unknown_everywhere_returns_none():
    """None is the "nothing resolved" signal -- ClaudeExecutor.run treats it
    as "use the worker's own --allowed-tools default", never an empty grant."""
    assert worker.resolve_profile(PROFILES, 999, 888) is None
    assert worker.resolve_profile({}, None, None) is None


def test_resolve_profile_board_default_used_when_ticket_id_is_none():
    assert worker.resolve_profile(PROFILES, None, 1) == RESTRICTED


# ---------- comments are the human conversation, not the agent transcript ----------
#
# Mid-run agent output used to be posted as ticket comments alongside the
# ticket_log stream, which buried the few comments a person actually wrote
# under hundreds of rows of tool-call chatter ("-> Bash -> Edit"). The live
# board ended up 255 agent comments to 1 human one. Only finish_work may write
# a comment now: one end-of-run summary per attempt.


def _comment_writing_functions():
    """Every top-level function in worker.py whose body inserts a comment."""
    source = Path(worker.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    writers = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        body = ast.get_source_segment(source, node) or ""
        if "INSERT INTO comments" in body:
            writers.add(node.name)
    return writers


def test_only_finish_work_writes_comments():
    assert _comment_writing_functions() == {"finish_work"}


def test_run_slot_does_not_wire_agent_output_to_comments():
    """The executor still accepts progress_cb (tested in test_executor.py) --
    run_slot just must not point it at the comments table any more."""
    source = Path(worker.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    run_slot = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "run_slot")
    body = ast.get_source_segment(source, run_slot) or ""
    assert "progress_cb=None" in body
    assert "progress_cb=progress_cb" not in body
