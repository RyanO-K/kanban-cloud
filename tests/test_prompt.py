"""The agent prompt: pure, stdlib-only, and the only place project context and
the machine's local path meet."""
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.prompt import (  # noqa: E402
    COMMIT_GATE_MARKER,
    build_agent_prompt,
    parse_commit_gate,
    slugify,
)

TICKET = {"id": 12, "title": "Fix the footer", "body": "It overlaps on mobile."}
BOARD = {"name": "site-page", "description": "The portfolio site.",
         "out_of_scope": "Do not touch billing.",
         "commit_requirements": "All tests must pass.", "use_worktrees": False}
BARE = {"name": "b", "description": None, "out_of_scope": None,
        "commit_requirements": None, "use_worktrees": False}


def test_includes_ticket_and_directory():
    p = build_agent_prompt(TICKET, BOARD, r"C:\repos\site-page")
    assert "Fix the footer" in p
    assert "It overlaps on mobile." in p
    assert "#12" in p
    assert r"C:\repos\site-page" in p


def test_includes_project_context():
    p = build_agent_prompt(TICKET, BOARD, "/repo")
    assert "The portfolio site." in p
    assert "Do not touch billing." in p
    assert "All tests must pass." in p


def test_absent_fields_emit_no_empty_sections():
    p = build_agent_prompt(TICKET, BARE, "/repo")
    assert "Out of scope" not in p
    assert "Before you commit" not in p
    assert "Project background" not in p
    assert "Fix the footer" in p       # the ticket itself still survives
    assert "/repo" in p                # and so does the directory


def test_worktree_guidance_switches_on_the_flag():
    off = build_agent_prompt(TICKET, {**BOARD, "use_worktrees": False}, "/repo")
    on = build_agent_prompt(TICKET, {**BOARD, "use_worktrees": True}, "/repo")
    assert "git worktree add" in on
    assert "git worktree add" not in off
    assert "12-fix-the-footer" in on
    assert "12-fix-the-footer" in off


def test_never_instructs_a_push():
    """Phase 1 has no credentials story; pushing is explicitly out of scope."""
    p = build_agent_prompt(TICKET, BOARD, "/repo")
    assert "do not push" in p.lower()


def test_empty_body_is_tolerated():
    p = build_agent_prompt({"id": 1, "title": "T", "body": ""}, BOARD, "/repo")
    assert "(no details provided)" in p


def test_missing_body_key_is_tolerated():
    p = build_agent_prompt({"id": 1, "title": "T"}, BARE, "/repo")
    assert "(no details provided)" in p


def test_whitespace_only_fields_count_as_absent():
    board = {**BARE, "description": "   \n  "}
    assert "Project background" not in build_agent_prompt(TICKET, board, "/repo")


def test_slugify():
    assert slugify("Fix the footer!") == "fix-the-footer"
    assert slugify("  Multiple   spaces  ") == "multiple-spaces"
    assert slugify("") == "ticket"
    assert slugify("!!!") == "ticket"


def test_slugify_caps_length():
    assert slugify("one two three four five six seven eight") == \
        "one-two-three-four-five-six"


def test_commit_requirements_ask_for_a_gate_verdict():
    p = build_agent_prompt(TICKET, BOARD, "/repo")
    assert COMMIT_GATE_MARKER in p
    assert "requirements_met" in p


def test_no_commit_requirements_means_no_gate_instruction():
    p = build_agent_prompt(TICKET, BARE, "/repo")
    assert COMMIT_GATE_MARKER not in p


# ---------- parse_commit_gate ----------

def test_parse_commit_gate_extracts_marker_json():
    text = (f'All tests passed.\n\n{COMMIT_GATE_MARKER} '
            '{"requirements_met": true, "summary": "Ran the suite, all green."}')
    gate = parse_commit_gate(text)
    assert gate == {"requirements_met": True, "summary": "Ran the suite, all green."}


def test_parse_commit_gate_reports_unmet_requirements():
    text = (f'Could not get tests passing.\n\n{COMMIT_GATE_MARKER} '
            '{"requirements_met": false, "summary": "Two tests still fail."}')
    gate = parse_commit_gate(text)
    assert gate == {"requirements_met": False, "summary": "Two tests still fail."}


def test_parse_commit_gate_returns_none_without_marker():
    assert parse_commit_gate("Done, all good.") is None


def test_parse_commit_gate_returns_none_on_empty_text():
    assert parse_commit_gate("") is None
    assert parse_commit_gate(None) is None


def test_parse_commit_gate_returns_none_on_malformed_json():
    assert parse_commit_gate(f"{COMMIT_GATE_MARKER} not json") is None


def test_parse_commit_gate_returns_none_when_requirements_met_is_missing():
    """No verdict is not the same as a reported one — must not be silently
    treated as met."""
    gate = parse_commit_gate(f'{COMMIT_GATE_MARKER} {{"summary": "checked it"}}')
    assert gate is None


def test_parse_commit_gate_returns_none_when_requirements_met_is_not_a_bool():
    gate = parse_commit_gate(f'{COMMIT_GATE_MARKER} {{"requirements_met": "yes"}}')
    assert gate is None


def test_parse_commit_gate_summary_defaults_to_empty_string():
    gate = parse_commit_gate(f'{COMMIT_GATE_MARKER} {{"requirements_met": true}}')
    assert gate == {"requirements_met": True, "summary": ""}


def test_parse_commit_gate_uses_the_last_marker_occurrence():
    """The guidance text itself contains the marker as an example; a real
    verdict must win over any incidental earlier mention."""
    text = (f'Earlier I might write {COMMIT_GATE_MARKER} {{"requirements_met": false}} '
            f'but the real verdict is: {COMMIT_GATE_MARKER} {{"requirements_met": true}}')
    assert parse_commit_gate(text)["requirements_met"] is True


def test_module_is_stdlib_only():
    """It rides into the PyInstaller onefile exe via worker.py; a SQLAlchemy
    import here would drag the entire server in with it."""
    src = (Path(__file__).resolve().parents[1] / "app" / "prompt.py").read_text()
    third_party = {"sqlalchemy", "fastapi", "pydantic", "psycopg", "app"}
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            names = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [(node.module or "").split(".")[0]]
        else:
            continue
        assert not (set(names) & third_party), names
