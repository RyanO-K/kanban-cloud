"""Unit tests for the local -> cloud ticket mapping.

The local .kanban tool and kanban-cloud have overlapping but different status
vocabularies, and local tickets carry fields the cloud has no column for. All of
that translation lives in app/importer.py as pure functions so it can be tested
without a database or a browser.
"""
import datetime

from app import importer


def test_map_status_local_vocabulary():
    assert importer.map_status("todo") == "todo"
    assert importer.map_status("pending") == "todo"
    assert importer.map_status("ready") == "ready"
    assert importer.map_status("in_progress") == "doing"
    assert importer.map_status("blocked") == "todo"
    assert importer.map_status("completed") == "done"


def test_map_status_passes_through_cloud_vocabulary():
    """The live trump-market-impact board already has tickets in 'done'."""
    for status in ("todo", "ready", "doing", "done", "failed", "killed"):
        assert importer.map_status(status) == status


def test_map_status_folds_the_retired_review_status_into_done():
    """`review` was a cloud status until the five-column rework; a board
    exported before then can still carry it. It must land where app/db.py's
    migration put the rows that already had it, not fall through the
    unknown-status default and silently reopen finished work."""
    assert "review" not in importer.TICKET_STATUSES
    assert importer.map_status("review") == "done"


def test_map_status_defaults_unknown_to_todo():
    assert importer.map_status("banana") == "todo"
    assert importer.map_status(None) == "todo"
    assert importer.map_status(42) == "todo"
    assert importer.map_status("") == "todo"


def test_map_status_is_trimmed_and_case_insensitive():
    assert importer.map_status("  Ready  ") == "ready"
    assert importer.map_status("COMPLETED") == "done"
    assert importer.map_status("In_Progress") == "doing"


def test_render_body_records_provenance_including_original_status():
    """Mapping blocked -> todo loses information unless we say so in the body."""
    body = importer.render_body(
        {"detail": "Do the thing", "status": "blocked"}, "ai-kanban", "16"
    )
    assert body.startswith("Do the thing")
    assert "local board `ai-kanban` #16" in body
    assert "local status: blocked" in body


def test_render_body_emits_only_present_sections():
    body = importer.render_body(
        {
            "detail": "d",
            "status": "todo",
            "dependsOn": ["12", "13"],
            "steps": ["a", "b"],
        },
        "b",
        "1",
    )
    assert "**Depends on:** 12, 13" in body
    assert "**Steps:**" in body
    assert "- a" in body
    assert "- b" in body
    assert "**Blocks:**" not in body
    assert "**Files:**" not in body
    assert "**Outputs:**" not in body


def test_render_body_ignores_empty_sections():
    body = importer.render_body(
        {"detail": "d", "status": "todo", "dependsOn": [], "steps": None, "files": ""},
        "b",
        "1",
    )
    assert "**Depends on:**" not in body
    assert "**Steps:**" not in body
    assert "**Files:**" not in body


def test_render_body_with_no_extras_is_detail_plus_provenance():
    body = importer.render_body({"detail": "just this", "status": "done"}, "b", "3")
    assert "just this" in body
    assert "local status: done" in body
    assert "**" not in body


def test_render_body_survives_missing_detail():
    body = importer.render_body({"status": "todo"}, "b", "1")
    assert "local board `b` #1" in body


def test_render_body_renders_all_four_list_sections():
    body = importer.render_body(
        {
            "detail": "d",
            "status": "todo",
            "blocks": ["20"],
            "files": ["a.py"],
            "outputs": ["out.json"],
            "steps": ["s"],
        },
        "b",
        "1",
    )
    assert "**Blocks:** 20" in body
    assert "**Files:**" in body
    assert "- a.py" in body
    assert "**Outputs:**" in body
    assert "- out.json" in body


def test_normalize_ticket_maps_core_fields():
    out = importer.normalize_ticket(
        {"title": "  T  ", "detail": "d", "status": "completed"}, "b", "1"
    )
    assert out["title"] == "T"
    assert out["status"] == "done"
    assert out["body"].startswith("d")


def test_normalize_ticket_parses_comment_timestamps():
    raw = {
        "title": "T",
        "status": "completed",
        "comments": [
            {"writer": "ryan", "message": "m1", "timestamp": "2026-07-04T01:30:19+00:00"},
            {"writer": "bot", "message": "m2", "timestamp": "2026-07-04T01:30:19"},
            {"writer": "bot", "message": "m3", "timestamp": "not a date"},
            {"writer": "bot", "message": "m4"},
        ],
    }
    out = importer.normalize_ticket(raw, "b", "1")
    stamps = [c["created_at"] for c in out["comments"]]
    assert len(stamps) == 4
    # Offset-aware input is normalised to naive UTC, matching models.utcnow().
    assert stamps[0] == datetime.datetime(2026, 7, 4, 1, 30, 19)
    assert stamps[0].tzinfo is None
    assert stamps[1] == datetime.datetime(2026, 7, 4, 1, 30, 19)
    # Unparseable and absent timestamps fall back to import time, not None.
    assert all(s is not None for s in stamps)
    assert [c["message"] for c in out["comments"]] == ["m1", "m2", "m3", "m4"]


def test_normalize_ticket_converts_offset_to_utc():
    out = importer.normalize_ticket(
        {
            "title": "T",
            "comments": [{"writer": "w", "message": "m", "timestamp": "2026-07-04T03:30:19+02:00"}],
        },
        "b",
        "1",
    )
    assert out["comments"][0]["created_at"] == datetime.datetime(2026, 7, 4, 1, 30, 19)


def test_normalize_ticket_skips_blank_titles():
    assert importer.normalize_ticket({"title": "   ", "status": "todo"}, "b", "1") is None
    assert importer.normalize_ticket({"status": "todo"}, "b", "1") is None
    assert importer.normalize_ticket("not a dict", "b", "1") is None
    assert importer.normalize_ticket(None, "b", "1") is None


def test_normalize_ticket_drops_malformed_comments():
    out = importer.normalize_ticket(
        {"title": "T", "comments": ["nope", {"message": "no writer"}, {"writer": "w"}]},
        "b",
        "1",
    )
    assert out["comments"] == []


def test_normalize_ticket_tolerates_non_list_comments():
    out = importer.normalize_ticket({"title": "T", "comments": "nope"}, "b", "1")
    assert out["comments"] == []


def test_unique_board_name():
    assert importer.unique_board_name([], "ai-kanban") == "ai-kanban"
    assert importer.unique_board_name(["other"], "ai-kanban") == "ai-kanban"
    assert importer.unique_board_name(["ai-kanban"], "ai-kanban") == "ai-kanban (2)"
    assert (
        importer.unique_board_name(["ai-kanban", "ai-kanban (2)"], "ai-kanban")
        == "ai-kanban (3)"
    )


def test_unique_board_name_compares_loosely():
    """default_board() already compares board names trimmed + lowercased."""
    assert importer.unique_board_name([" AI-Kanban "], "ai-kanban") == "ai-kanban (2)"


def test_unique_board_name_falls_back_for_blank_input():
    assert importer.unique_board_name([], "   ") == "Imported board"
    assert importer.unique_board_name([], "") == "Imported board"


def test_sort_key_orders_numerically():
    ids = ["10", "2", "1", "abc", "3"]
    assert sorted(ids, key=importer.sort_key) == ["1", "2", "3", "10", "abc"]


def test_sort_key_tolerates_missing_ids():
    assert sorted(["2", None, "1"], key=importer.sort_key) == ["1", "2", None]
