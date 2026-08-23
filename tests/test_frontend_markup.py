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


def section_body(markup, key):
    """The SECTION_BODY entry for one settings section.

    Settings moved out of modals and into a scoped page, so most of the old
    'is this field present' assertions now have to look inside the function
    that builds a section rather than at static markup.
    """
    body = markup[markup.index("const SECTION_BODY = {"):]
    start = body.index(f"\n  {key}: ")
    nxt = [body.index(f"\n  {k}: ") for k in
           ("project", "repo", "agents", "import", "concurrency", "profiles")
           if body.index(f"\n  {k}: ") > start]
    return body[start:min(nxt)] if nxt else body[start:body.index("\n};")]


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


# ---------- settings: a scoped page, not a pile of modals ----------

def test_settings_has_a_board_scope_and_a_cluster_scope(markup):
    """Every section has to hang off one of the two things the API persists:
    a board row, or the cluster's settings row."""
    scopes = markup[markup.index("const SCOPES = ["):]
    scopes = scopes[:scopes.index("];")]
    assert '{key: "board", label: "This board"}' in scopes
    assert '{key: "cluster", label: "Cluster"}' in scopes


def test_settings_sections_are_the_six_real_ones(markup):
    sections = markup[markup.index("const SETTINGS_SECTIONS = ["):]
    sections = sections[:sections.index("\n];")]
    for key, scope in (("project", "board"), ("repo", "board"), ("agents", "board"),
                       ("import", "board"), ("concurrency", "cluster"),
                       ("profiles", "cluster")):
        assert f'{{key: "{key}", scope: "{scope}"' in sections, key
    # Nothing on screen may claim to save something the API does not take.
    assert '"site"' not in sections


def test_every_saving_section_routes_to_a_real_endpoint(markup):
    """A section either declares save: board/cluster, or shows no Save button."""
    sections = markup[markup.index("const SETTINGS_SECTIONS = ["):]
    sections = sections[:sections.index("\n];")]
    for saver in ('save: "board"', 'save: "cluster"'):
        assert saver in sections
    router = markup[markup.index("async function saveSettingsSection"):]
    router = router[:router.index("\n}")]
    assert 'sec.save === "board"' in router and "saveBoardSettings()" in router
    assert 'sec.save === "cluster"' in router and "saveClusterSettings()" in router
    # No save target -> the whole action row is hidden rather than inert.
    assert 'actions.classList.toggle("hidden", !sec.save)' in markup


def test_settings_tab_is_owner_only(markup):
    """It is the only way into the mutating panels, so it is gated twice: the
    class hides it for spectators, and showSettings() refuses anyway."""
    assert '<button id="tabSettings" class="owner-only"' in markup
    opener = markup[markup.index("function showSettings(section)"):]
    assert 'if (sessionMode === "spectator") return;' in opener[:opener.index("\n}")]


def test_board_settings_fields_present(markup):
    for key, tokens in (
        ("project", ("bDescription", "bOutOfScope", "bCommitReq")),
        ("repo", ("bRepoUrl", "bUseWorktrees", "bAutoPush")),
        ("agents", ("bDefaultProfile",)),
    ):
        body = section_body(markup, key)
        for token in tokens:
            assert token in body, f"{key}/{token}"
    assert "async function saveBoardSettings" in markup


def test_board_settings_wires_repo_url_on_open_and_save(markup):
    """Fields read through boardValue() so an unsaved edit survives a re-render
    (the draft), and the same accessor is what gets PATCHed."""
    assert 'value="${esc(boardValue("repo_url") || "")}"' in section_body(markup, "repo")
    assert "setBoardDraft('repo_url', this.value)" in markup
    save = markup[markup.index("async function saveBoardSettings"):]
    save = save[:save.index("\n}")]
    assert 'repo_url: boardValue("repo_url") || ""' in save


def test_cluster_settings_fields_present(markup):
    body = section_body(markup, "concurrency")
    for token in ("csEnabled", "csCap", "csStopAll", "csInFlight"):
        assert token in body, token
    assert "async function saveClusterSettings" in markup


def test_in_flight_count_is_read_only(markup):
    """It is a server-side count, not a setting — it must not look editable."""
    body = section_body(markup, "concurrency")
    assert '<span class="valuePill" id="csInFlight">' in body


def test_cluster_draft_is_fetched_fresh_when_the_section_opens(markup):
    """Otherwise the in-flight number is a snapshot from page load."""
    body = markup[markup.index("async function renderSettings"):]
    assert 'sec.save === "cluster" && clusterDraft === null' in body[:body.index("\n}")]


def test_cluster_settings_saves_all_three_fields(markup):
    body = markup[markup.index("async function saveClusterSettings"):]
    body = body[:body.index("\n}")]
    assert "enabled:" in body
    assert "concurrency_cap:" in body
    assert "stop_all_requested:" in body


def test_toggles_report_their_state_to_assistive_tech(markup):
    """They are <button>s, not checkboxes, so the state has to be announced."""
    body = markup[markup.index("function tglCtl("):]
    body = body[:body.index("\n}")]
    assert 'aria-pressed="${!!on}"' in body


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
    """Spectators must not see them: the endpoints would 403 anyway. Edit and
    Revoke sit in one gated row under each worker in the rail."""
    row = markup[markup.index('<div class="wActions owner-only">'):]
    row = row[:row.index("</div>")]
    assert "openWorkerSettings(" in row
    assert "revokeWorker(" in row


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
    the rail's Kill run carries the class directly."""
    assert 'id="killBtn"' in markup
    row = markup[markup.index('<div class="row owner-only">'):]
    assert 'id="killBtn"' in row[:row.index("</div>")]
    assert 'class="btn danger owner-only" onclick="killTicket(' in markup


def test_kill_from_the_rail_does_not_close_a_modal_that_is_not_open(markup):
    """killTicket is reachable from both the rail and the modal now."""
    body = markup[markup.index("async function killTicket"):]
    body = body[:body.index("\n}")]
    assert 'ticketOverlay").classList.contains("show")' in body


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


def test_every_column_carries_its_own_hue(markup):
    """The header dot and count chip are tinted per column."""
    cols = markup[markup.index("const COLS = ["):]
    cols = cols[:cols.index("];")]
    assert cols.count("hue:") == 5


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


# ---------- the board fits the viewport (design: board viewport + layout) ----------

def test_the_app_shell_owns_the_viewport(markup):
    """The page itself must never scroll: the shell is exactly one screen tall
    and every scrollbar lives in a named child."""
    assert "#appView { height: 100vh; height: 100dvh;" in markup
    assert "overflow: hidden; }" in markup[markup.index("#appView {"):
                                           markup.index("#appView {") + 200]


def test_columns_share_the_width_but_stop_shrinking(markup):
    """flex:1 1 0 keeps five equal columns; the floor is what makes the strip
    scroll sideways instead of crushing the cards."""
    assert ".col { flex: 1 1 0; min-width: 224px;" in markup


def test_only_the_column_body_scrolls_vertically(markup):
    """The header and count chip stay put while the cards move."""
    assert ".colBody { flex: 1; min-height: 0; overflow-y: auto;" in markup
    assert ".boardScroll { position: relative; flex: 1; min-width: 0; overflow-x: auto; overflow-y: hidden;" in markup


def test_the_side_rail_has_a_fixed_width(markup):
    assert ".side { flex: 0 0 320px; width: 320px;" in markup


def test_the_rail_gets_out_of_the_way_on_a_narrow_screen(markup):
    narrow = markup[markup.index("@media (max-width: 820px) {"):]
    narrow = narrow[:narrow.index("\n  }")]
    assert ".side { display: none; }" in narrow


# ---------- selecting a ticket fills the rail ----------

def test_clicking_a_card_selects_it_and_double_click_opens_it(markup):
    """One click is a cheap look in the rail; the modal is the editor."""
    assert "selectTicket(${t.id})" in markup
    assert "ondblclick" in markup and "openTicketModal(${t.id})" in markup


def test_the_detail_rail_offers_the_full_editor(markup):
    body = markup[markup.index('<div class="detailActions">'):]
    body = body[:body.index("</div>")]
    assert "openTicketModal(" in body


def test_the_selected_card_is_marked(markup):
    assert "selectedTicket && selectedTicket.id === t.id" in markup


# ---------- live agent output + chat ----------

def test_live_log_and_chat_panels_present(markup):
    for token in ("logWrap", "chatWrap", 'id="agentLog"', 'id="agentChat"',
                  'id="newChat"', "startLivePoll", "stopLivePoll", "sendChat"):
        assert token in markup, token


def test_chat_send_is_owner_only(markup):
    """Spectators must not see it: the endpoint would 403 anyway."""
    row = markup[markup.index('<input id="newChat"') - 400:markup.index('<input id="newChat"')]
    assert '<div class="row owner-only">' in row


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


def test_live_poll_follows_the_selection_too(markup):
    """The rail tails output without the modal, so selecting starts the poll
    and clearing the selection stops it."""
    sel = markup[markup.index("function selectTicket(id)"):]
    assert "startLivePoll()" in sel[:sel.index("\n}")]
    clear = markup[markup.index("function clearSelection()"):]
    assert "stopLivePoll()" in clear[:clear.index("\n}")]


def test_one_log_buffer_feeds_both_boxes(markup):
    """The modal and the rail show the same lines; polling follows whichever
    ticket is open, preferring the modal."""
    assert "function livePollTicket() { return editingTicket || selectedTicket; }" in markup
    paint = markup[markup.index("function paintLog()"):]
    paint = paint[:paint.index("\n}")]
    assert '"detailLog"' in paint and '"agentLog"' in paint


# ---------- the live feed ----------

def test_feed_is_derived_from_the_queue_the_board_already_loads(markup):
    """No new endpoint: every row is a moment already recorded on a WorkItem."""
    body = markup[markup.index("function buildFeed()"):]
    body = body[:body.index("\n}")]
    for field in ("queued_at", "claimed_at", "finished_at", "kill_requested"):
        assert field in body, field


def test_feed_timestamps_are_read_as_utc(markup):
    """The server sends naive UTC (models.utcnow); Date.parse would otherwise
    read them as local time and every age would be hours out."""
    body = markup[markup.index("function tms(s)"):]
    body = body[:body.index("\n}")]
    assert 's + "Z"' in body


# ---------- agent profiles ----------

def test_profiles_section_markup_present(markup):
    body = section_body(markup, "profiles")
    for token in ("profilesList", "pName", "pAllowedTools", "pModel",
                  "pSystemPrompt", "saveProfile"):
        assert token in body, token


def test_board_settings_has_a_default_profile_picker(markup):
    assert 'id="bDefaultProfile"' in markup
    assert "default_profile_id" in markup


def test_ticket_modal_has_a_profile_picker(markup):
    assert 'id="tProfile"' in markup
    assert "payload.profile_id" in markup


# ---------- commit gate + auto-push (ticket #15) ----------

def test_auto_push_toggle_present_and_wired(markup):
    body = section_body(markup, "repo")
    assert 'tglCtl("bAutoPush", !!boardValue("auto_push"), "toggleBoard(event,\'auto_push\')")' in body
    save = markup[markup.index("async function saveBoardSettings"):]
    save = save[:save.index("\n}")]
    assert 'auto_push: !!boardValue("auto_push")' in save


def test_ticket_modal_shows_the_commit_gate(markup):
    assert 'id="tCommitGate"' in markup
    assert "editingTicket.commit_gate" in markup
    assert "requirements_met" in markup


# ---------- theming ----------

def test_light_and_dark_are_both_real_palettes(markup):
    """The design is the dark one; light is derived so the existing toggle
    doesn't become a switch to a broken theme."""
    light = markup[markup.index("  :root {"):markup.index("  body.dark {")]
    dark = markup[markup.index("  body.dark {"):]
    dark = dark[:dark.index("\n  }")]
    for token in ("--bg:", "--panel:", "--ink:", "--accent:"):
        assert token in light, f"light {token}"
        assert token in dark, f"dark {token}"


def test_dark_mode_toggle_and_its_stored_preference_survive(markup):
    assert "function toggleDarkMode" in markup
    assert "kc_theme" in markup
    assert 'id="darkModeItem"' in markup


def test_the_ui_asks_for_the_design_typefaces(markup):
    assert "IBM+Plex+Sans" in markup and "IBM+Plex+Mono" in markup
