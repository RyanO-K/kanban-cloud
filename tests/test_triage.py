"""Initial triage's pure functions: prompt building and reply validation.
Stdlib-only, like app/prompt.py, since worker.py imports it into the
PyInstaller onefile exe."""
import ast
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.triage import build_triage_prompt, parse_triage_result  # noqa: E402

TICKET = {"id": 5, "title": "Add retry logic", "body": "Retries should back off."}
CANDIDATES = [
    {"id": 1, "title": "Set up the queue", "status": "done"},
    {"id": 2, "title": "Add metrics", "status": "todo"},
]


def test_prompt_includes_ticket_and_candidates():
    p = build_triage_prompt(TICKET, CANDIDATES)
    assert "Add retry logic" in p
    assert "Retries should back off." in p
    assert "#5" in p
    assert "1: Set up the queue [done]" in p
    assert "2: Add metrics [todo]" in p


def test_prompt_with_no_candidates_says_so():
    p = build_triage_prompt(TICKET, [])
    assert "no other tickets" in p.lower()


def test_prompt_asks_for_one_line_json():
    p = build_triage_prompt(TICKET, CANDIDATES)
    assert "depends_on" in p
    assert "haiku" in p and "sonnet" in p and "opus" in p


def test_parse_valid_reply():
    text = json.dumps({"model": "sonnet", "depends_on": [1]})
    result = parse_triage_result(text, [1, 2], ticket_id=5)
    assert result == {"model": "sonnet", "depends_on": [1]}


def test_parse_empty_depends_on():
    text = json.dumps({"model": "haiku", "depends_on": []})
    assert parse_triage_result(text, [1, 2], ticket_id=5) == {
        "model": "haiku", "depends_on": []}


def test_parse_missing_depends_on_defaults_to_empty():
    text = json.dumps({"model": "opus"})
    assert parse_triage_result(text, [1, 2], ticket_id=5) == {
        "model": "opus", "depends_on": []}


def test_parse_dedupes_depends_on():
    text = json.dumps({"model": "sonnet", "depends_on": [1, 1, 2]})
    result = parse_triage_result(text, [1, 2], ticket_id=5)
    assert result["depends_on"] == [1, 2]


def test_rejects_malformed_json():
    assert parse_triage_result("not json at all", [1, 2], ticket_id=5) is None


def test_rejects_non_object_json():
    assert parse_triage_result("[1, 2, 3]", [1, 2], ticket_id=5) is None


def test_rejects_unknown_model():
    text = json.dumps({"model": "gpt-5", "depends_on": []})
    assert parse_triage_result(text, [1, 2], ticket_id=5) is None


def test_rejects_missing_model():
    text = json.dumps({"depends_on": []})
    assert parse_triage_result(text, [1, 2], ticket_id=5) is None


def test_rejects_self_dependency():
    text = json.dumps({"model": "sonnet", "depends_on": [5]})
    assert parse_triage_result(text, [1, 2, 5], ticket_id=5) is None


def test_rejects_unknown_dependency_id():
    text = json.dumps({"model": "sonnet", "depends_on": [999]})
    assert parse_triage_result(text, [1, 2], ticket_id=5) is None


def test_rejects_non_list_depends_on():
    text = json.dumps({"model": "sonnet", "depends_on": "1,2"})
    assert parse_triage_result(text, [1, 2], ticket_id=5) is None


def test_rejects_non_int_depends_on_entries():
    text = json.dumps({"model": "sonnet", "depends_on": ["1"]})
    assert parse_triage_result(text, [1, 2], ticket_id=5) is None


def test_rejects_bool_depends_on_entries():
    """bool is an int subclass in Python; must not slip through as an id."""
    text = json.dumps({"model": "sonnet", "depends_on": [True]})
    assert parse_triage_result(text, [1, 2], ticket_id=5) is None


def test_rejects_empty_text():
    assert parse_triage_result("", [1, 2], ticket_id=5) is None
    assert parse_triage_result(None, [1, 2], ticket_id=5) is None


def test_module_is_stdlib_only():
    """It rides into the PyInstaller onefile exe via worker.py; a SQLAlchemy
    import here would drag the entire server in with it."""
    src = (Path(__file__).resolve().parents[1] / "app" / "triage.py").read_text()
    third_party = {"sqlalchemy", "fastapi", "pydantic", "psycopg", "app"}
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            names = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [(node.module or "").split(".")[0]]
        else:
            continue
        assert not (set(names) & third_party), names
