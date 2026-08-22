"""The agent prompt: pure, stdlib-only, and the only place project context and
the machine's local path meet."""
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.prompt import build_agent_prompt, build_resume_prompt, slugify  # noqa: E402

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


# ---------- resume prompt ----------

def test_resume_prompt_carries_question_and_answer():
    p = build_resume_prompt({"question": "Which branch?", "answer_value": "main",
                             "answer_notes": None})
    assert "Which branch?" in p
    assert "main" in p


def test_resume_prompt_includes_notes_when_present():
    p = build_resume_prompt({"question": "Delete the old table?",
                             "answer_value": "yes", "answer_notes": "It's unused."})
    assert "It's unused." in p


def test_resume_prompt_omits_notes_section_when_blank():
    p = build_resume_prompt({"question": "Q", "answer_value": "A", "answer_notes": ""})
    assert "Notes:" not in p


def test_resume_prompt_is_much_shorter_than_a_fresh_prompt():
    """The whole point: the resumed session already has the ticket and repo
    context loaded, so this should not re-send build_agent_prompt's bulk."""
    fresh = build_agent_prompt(TICKET, BOARD, "/repo")
    resumed = build_resume_prompt({"question": "Q", "answer_value": "A",
                                   "answer_notes": None})
    assert len(resumed) < len(fresh)


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
