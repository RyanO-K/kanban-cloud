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


def test_cluster_settings_modal_markup_present(markup):
    for token in ("clusterSettingsBtn", "clusterSettingsOverlay", "csEnabled",
                  "csCap", "csStopAll", "csInFlight",
                  "openClusterSettings", "saveClusterSettings"):
        assert token in markup, token


def test_cluster_settings_button_is_owner_only(markup):
    assert 'owner-only" id="clusterSettingsBtn"' in markup


def test_cluster_settings_saves_all_three_fields(markup):
    body = markup[markup.index("async function saveClusterSettings"):]
    body = body[:body.index("\n}")]
    assert "enabled:" in body
    assert "concurrency_cap:" in body
    assert "stop_all_requested:" in body


def test_workers_panel_renders_slot_counts(markup):
    """A PC's own concurrency limit and current load are shown next to it."""
    assert "w.running" in markup
    assert "w.concurrency" in markup


# ---------- website-side worker control (ticket #18) ----------

def test_worker_settings_modal_markup_present(markup):
    for token in ("workerOverlay", 'id="wName"', 'id="wConcurrency"',
                  "openWorkerSettings", "saveWorkerSettings"):
        assert token in markup, token


def test_worker_edit_button_is_owner_only(markup):
    """Spectators must not see it: the endpoint would 403 anyway."""
    assert 'owner-only" onclick="openWorkerSettings(' in markup


def test_save_worker_settings_patches_the_worker_endpoint(markup):
    body = markup[markup.index("async function saveWorkerSettings"):]
    body = body[:body.index("\n}\n")]
    assert '"PATCH", `./api/workers/${editingWorker.id}`' in body
    assert "desired_concurrency" in body
    assert "clear_desired_concurrency" in body


# ---------- ticket dependencies ----------

def test_deps_picker_present_in_ticket_modal(markup):
    for token in ('id="tDeps"', 'id="tBlockedNote"', "multiple"):
        assert token in markup, token


def test_save_ticket_sends_selected_deps(markup):
    body = markup[markup.index("async function saveTicket"):]
    assert 'getElementById("tDeps")' in body[:body.index("closeTicketModal")]
    assert "payload.depends_on" in body[:body.index("closeTicketModal")]


def test_card_shows_blocked_badge(markup):
    assert "badge blocked" in markup
    assert "t.blocked" in markup


# ---------- kill a running ticket ----------

def test_kill_button_exists_and_is_owner_only(markup):
    """Spectators must not see it: the endpoint would 403 anyway. The modal
    button sits inside the same '.row owner-only' wrapper as Save/Run/Delete;
    the per-card button on the board carries the class directly."""
    assert 'id="killBtn"' in markup
    row = markup[markup.index('<div class="row owner-only">'):]
    assert 'id="killBtn"' in row[:row.index("</div>")]
    assert 'owner-only" style="color:var(--bad)' in markup  # the card button


def test_kill_route_targets_the_kill_endpoint(markup):
    assert "/api/tickets/${" in markup and "/kill`" in markup


def test_kill_button_only_shown_for_in_progress_tickets(markup):
    assert 'editingTicket.status === "doing"' in markup
    assert 'killTicket(' in markup


def test_killed_is_still_a_ticket_status_option(markup):
    """It lost its own column in the five-column rework (it shows in Blocked)
    but a human must still be able to set it by hand."""
    assert '<option value="killed">killed</option>' in markup


# ---------- five columns (ticket #20) ----------

def test_board_renders_the_five_columns_in_order(markup):
    cols = markup[markup.index("const COLS = ["):]
    cols = cols[:cols.index("];")]
    for key, label in (("todo", "TODO"), ("ready", "Ready"), ("doing", "In progress"),
                       ("blocked", "Blocked"), ("done", "Done")):
        assert f'key: "{key}", label: "{label}"' in cols, key
    assert cols.index('"todo"') < cols.index('"done"')


def test_blocked_column_carries_failed_and_killed(markup):
    """The browser's copy of models.BOARD_COLUMNS — the two have to agree or
    a card lands in a column the server will not reorder."""
    cols = markup[markup.index("const COLS = ["):]
    cols = cols[:cols.index("];")]
    assert 'statuses: ["blocked", "failed", "killed"]' in cols


def test_review_is_gone_from_the_ui(markup):
    assert '<option value="review">' not in markup
    assert "review/done" not in markup  # the old dependency-picker hint


def test_a_card_whose_status_is_not_its_column_says_which(markup):
    """failed/killed share the Blocked column, so the card has to name the
    status or the two become indistinguishable."""
    assert "t.status !== col.key" in markup


def test_drops_and_reorders_address_a_column_not_a_status(markup):
    assert "dropTicket(event,'${col.key}')" in markup
    body = markup[markup.index("async function dropOnCard"):]
    body = body[:body.index("\n}\n")]
    assert "colOf(target.status)" in body
    assert "status: col.key" in body


# ---------- column width (ticket #21) ----------

def test_columns_share_the_board_evenly(markup):
    """Each column is ~1/5th of the window minus the sidebar, so .col has to
    flex rather than carry the old fixed width."""
    rule = markup[markup.index("\n  .col {"):]
    rule = rule[:rule.index("}")]
    assert "flex: 1 1 0" in rule
    # The fixed width it replaced. Matched with its leading "; " so the
    # min-width guard below — a substring of it — does not count as a hit.
    assert "; width:" not in rule


def test_a_narrow_board_still_scrolls_rather_than_squeezing_the_sidebar(markup):
    """min-width on the column plus overflow-x on the board; .side is the
    fixed-width one, so it must not shrink."""
    col = markup[markup.index("\n  .col {"):]
    assert "min-width: 210px" in col[:col.index("}")]
    board = markup[markup.index("\n  .board {"):]
    assert "overflow-x: auto" in board[:board.index("}")]
    side = markup[markup.index("\n  .side {"):]
    assert "flex-shrink: 0" in side[:side.index("}")]


# ---------- live agent output + chat ----------

def test_live_log_and_chat_panels_present(markup):
    for token in ("logWrap", "chatWrap", 'id="agentLog"', 'id="agentChat"',
                  'id="newChat"', "startLivePoll", "stopLivePoll", "sendChat"):
        assert token in markup, token


def test_chat_send_is_owner_only(markup):
    """Spectators must not see it: the endpoint would 403 anyway."""
    assert '<div class="row owner-only">\n        <input id="newChat"' in markup


def test_send_chat_hits_the_chat_endpoint(markup):
    body = markup[markup.index("async function sendChat"):]
    body = body[:body.index("\n}\n")]
    assert "/api/tickets/${editingTicket.id}/chat" in body


def test_live_poll_starts_and_stops_with_the_modal(markup):
    open_fn = markup[markup.index("function openTicketModal"):]
    open_fn = open_fn[:open_fn.index("\n}\n")]
    assert "startLivePoll()" in open_fn and "stopLivePoll()" in open_fn
    close_fn = markup[markup.index("function closeTicketModal"):]
    close_fn = close_fn[:close_fn.index("\n}")]
    assert "stopLivePoll()" in close_fn


# ---------- agent profiles ----------

def test_profiles_modal_markup_present(markup):
    for token in ("profilesOverlay", "profilesList", "pName", "pAllowedTools",
                  "pModel", "pSystemPrompt", "openProfilesModal", "saveProfile"):
        assert token in markup, token


def test_board_settings_has_a_default_profile_picker(markup):
    assert 'id="bDefaultProfile"' in markup
    assert "default_profile_id" in markup


def test_ticket_modal_has_a_profile_picker(markup):
    assert 'id="tProfile"' in markup
    assert "payload.profile_id" in markup


# ---------- commit gate + auto-push (ticket #15) ----------

def test_auto_push_checkbox_present_and_wired(markup):
    assert 'id="bAutoPush"' in markup
    assert 'document.getElementById("bAutoPush").checked = !!b.auto_push' in markup
    assert 'auto_push: document.getElementById("bAutoPush").checked' in markup


def test_ticket_modal_shows_the_commit_gate(markup):
    assert 'id="tCommitGate"' in markup
    assert "editingTicket.commit_gate" in markup
    assert "requirements_met" in markup
