"""Cheap guards on the static UI: the ids boot() drives must actually exist.

There is no JS runner in this repo, so these assertions only catch typos and
accidental deletions — behaviour is verified by hand against the deployment.
"""
from pathlib import Path

import pytest

INDEX = Path(__file__).resolve().parents[1] / "app" / "static" / "index.html"


@pytest.fixture(scope="module")
def markup():
    return INDEX.read_text(encoding="utf-8")


def test_sign_in_button_exists_and_starts_hidden(markup):
    assert 'id="signInBtn"' in markup
    assert "Sign in with GitHub" in markup


def test_boot_consumes_the_new_session_keys(markup):
    assert "login_url" in markup
    assert "sessionBoardId" in markup


def test_spectator_note_no_longer_tells_you_to_leave(markup):
    """The button replaced the 'log in via the site' instruction."""
    assert "owner login via site" not in markup


# ---------- import a local .kanban board ----------

def test_import_button_is_owner_only(markup):
    """Spectators must not see it: the endpoint would 403 anyway."""
    assert 'id="importBtn"' in markup
    assert 'owner-only" id="importBtn"' in markup


def test_import_input_is_a_directory_picker(markup):
    assert 'id="importInput"' in markup
    assert "webkitdirectory" in markup


def test_board_area_is_a_drop_zone(markup):
    assert 'id="dropZone"' in markup
    assert "webkitGetAsEntry" in markup


def test_import_posts_only_whitelisted_keys(markup):
    """The browser drops history/commitGate/session ids before they leave the PC.

    Asserted against the IMPORT_KEYS literal rather than the whole file, so a
    comment mentioning a dropped field doesn't fail the test.
    """
    start = markup.index("const IMPORT_KEYS")
    whitelist = markup[start:markup.index(";", start)]
    for kept in ("title", "detail", "status", "comments", "dependsOn"):
        assert f'"{kept}"' in whitelist
    for dropped in ("claudeSessionId", "commitGate", "runLogFile", "history"):
        assert dropped not in whitelist, f"{dropped} should never be sent"


def test_ticket_drag_and_folder_drop_do_not_collide(markup):
    """Columns already handle ticket drags; a file drop must not become one."""
    assert 'includes("Files")' in markup
    body = markup[markup.index("async function dropTicket"):]
    assert "isFileDrag(ev)" in body[:body.index("}")]


def test_board_settings_modal_markup_present(markup):
    for token in ("boardSettingsBtn", "boardOverlay", "bDescription",
                  "bOutOfScope", "bCommitReq", "bUseWorktrees", "bRepoUrl",
                  "openBoardSettings", "saveBoardSettings"):
        assert token in markup, token


def test_board_settings_wires_repo_url_on_open_and_save(markup):
    assert 'document.getElementById("bRepoUrl").value = b.repo_url || ""' in markup
    assert 'repo_url: document.getElementById("bRepoUrl").value' in markup


def test_board_settings_button_is_owner_only(markup):
    """The gear opens a mutating panel, so spectators must not see it."""
    assert 'owner-only" id="boardSettingsBtn"' in markup


def test_workers_panel_renders_slot_counts(markup):
    """A PC's own concurrency limit and current load are shown next to it."""
    assert "w.running" in markup
    assert "w.concurrency" in markup
