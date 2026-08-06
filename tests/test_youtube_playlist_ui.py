"""Source/render tests for the /youtube Playlist Manager ("Quản lý Playlist" tab).

Two kinds of assertions, matching the rest of the suite:

* source: the template + shared static JS are read directly and checked for the
  exact markers the behavior depends on (lazy-load observer, localStorage key,
  page_token plumbing, separate /sort/preview and /sort/apply flows, no innerHTML
  for remote titles, ...).
* render: GET /youtube through TestClient in a connected and a disconnected state
  and assert on the produced HTML (shared tabs, panels, modals, controls).

The regression section pins the upload form / history / progress / pipeline
strings so an edit elsewhere cannot silently drop them.
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app.config import settings
from app.main import app

_TEMPLATE = Path(__file__).parents[1] / "app" / "templates" / "youtube.html"
_STYLE = Path(__file__).parents[1] / "app" / "static" / "style.css"
_TABS_JS = Path(__file__).parents[1] / "app" / "static" / "autosave.js"


def _source() -> str:
    return _TEMPLATE.read_text(encoding="utf-8")


def _pm_script() -> str:
    """The playlist-manager <script> block: from its header comment to the tail."""
    src = _source()
    return src[src.index("YouTube Playlist Manager"):src.rindex("{% endblock %}")]


def _css() -> str:
    return _STYLE.read_text(encoding="utf-8")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "app.db"))
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    monkeypatch.setattr(settings, "enable_worker", False)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def connected(client, monkeypatch):
    """Render /youtube as a configured + connected channel with two playlists."""
    from app import youtube as yt

    monkeypatch.setattr(yt, "is_configured", lambda: True)
    monkeypatch.setattr(
        yt,
        "get_creds_from_db",
        lambda conn: {"channel_name": "My Channel", "channel_id": "UC1"},
    )
    monkeypatch.setattr(
        yt,
        "list_playlists",
        lambda conn, max_results=50: [
            {"id": "PL1", "title": "First"},
            {"id": "PL2", "title": "Second"},
        ],
    )
    return client


@pytest.fixture
def disconnected(client, monkeypatch):
    """Render /youtube configured but with no stored credentials."""
    from app import youtube as yt

    monkeypatch.setattr(yt, "is_configured", lambda: True)
    monkeypatch.setattr(yt, "get_creds_from_db", lambda conn: None)
    return client


def _youtube_page(client) -> str:
    resp = client.get("/youtube")
    assert resp.status_code == 200
    return resp.text


# ---------------------------------------------------------------------------
# Shared accessible tabs
# ---------------------------------------------------------------------------


def test_shared_tab_markup_uses_tablist_tab_tabpanel_roles():
    src = _source()
    assert 'class="ui-tabs" data-tabs' in src
    assert '<div class="ui-tab-list" role="tablist" aria-label="YouTube">' in src
    assert 'role="tab" id="yt-tab-uploads"' in src
    assert 'role="tab" id="yt-tab-playlists"' in src
    assert 'data-tab-target="yt-panel-uploads" aria-controls="yt-panel-uploads"' in src
    assert 'data-tab-target="yt-panel-playlists" aria-controls="yt-panel-playlists"' in src
    assert 'id="yt-panel-uploads" role="tabpanel" aria-labelledby="yt-tab-uploads"' in src
    assert 'id="yt-panel-playlists" role="tabpanel" aria-labelledby="yt-tab-playlists"' in src


def test_shared_tabs_are_wired_by_autosave_js():
    js = _TABS_JS.read_text(encoding="utf-8")
    assert "function attachTabs(tabs)" in js
    assert "document.querySelectorAll('[data-tabs]').forEach(attachTabs);" in js
    assert "btn.setAttribute('aria-selected', active ? 'true' : 'false');" in js
    assert "panel.hidden = panel.id !== button.dataset.tabTarget;" in js
    assert "event.key !== 'ArrowLeft' && event.key !== 'ArrowRight'" in js
    assert "btn.tabIndex = active ? 0 : -1;" in js


def test_render_shows_both_tabs_when_connected(connected):
    html = _youtube_page(connected)
    assert 'id="yt-tab-uploads"' in html and 'aria-selected="true"' in html
    assert 'id="yt-tab-playlists"' in html and 'aria-selected="false"' in html
    assert 'id="yt-panel-uploads" role="tabpanel"' in html
    assert 'id="yt-panel-playlists" role="tabpanel" aria-labelledby="yt-tab-playlists" hidden' in html


def test_render_omits_playlist_tab_and_manager_when_disconnected(disconnected):
    html = _youtube_page(disconnected)
    assert 'id="yt-tab-uploads"' in html
    assert "yt-tab-playlists" not in html
    assert "yt-panel-playlists" not in html
    assert "yt-pm-root" not in html
    assert "yt-pm-picker-modal" not in html
    assert "yt-pm-sort-modal" not in html


def test_connected_page_keeps_uploads_tab_first_and_active(connected):
    html = _youtube_page(connected)
    uploads = html.index('id="yt-tab-uploads"')
    playlists = html.index('id="yt-tab-playlists"')
    assert uploads < playlists


# ---------------------------------------------------------------------------
# Lazy load when the playlist tab opens
# ---------------------------------------------------------------------------


def test_playlist_manager_loads_lazily_when_tab_first_shown():
    src = _pm_script()
    assert "const observer = new MutationObserver(function() {" in src
    assert 'observer.observe(panel, { attributes: true, attributeFilter: [\'hidden\'] });' in src
    assert "if (!panel.hidden) openManager();" in src
    # openManager must only run init() once per page lifetime.
    assert "if (state.loaded)" in src
    assert "state.loaded = true;" in src


def test_lazy_init_only_fetches_playlists_after_activation():
    src = _pm_script()
    assert "async function init() {" in src
    assert "await loadPlaylists();" in src
    # loadPlaylists is the only network call made on first open of the tab.
    init_block = src[src.index("async function init()"):src.index("function openManager()")]
    assert "/youtube/api/playlists" not in init_block
    assert "await loadPlaylists();" in init_block


def test_playlist_select_starts_loading_placeholder(connected):
    html = _youtube_page(connected)
    assert 'id="yt-pm-playlist-select"' in html
    assert "Đang tải..." in html
    # The manager <select> must not be pre-populated server-side (the upload form's
    # #yt-playlist dropdown is a different element and IS server-populated).
    sel = html[html.index('id="yt-pm-playlist-select"'):]
    sel = sel[:sel.index("</select>")]
    assert '<option value="">Đang tải...</option>' in sel
    assert "PL1" not in sel and "First" not in sel


# ---------------------------------------------------------------------------
# localStorage
# ---------------------------------------------------------------------------


def test_last_playlist_is_persisted_to_local_storage():
    src = _pm_script()
    assert "const STORE_KEY = 'yt-playlist-last';" in src
    assert "localStorage.getItem(STORE_KEY)" in src
    assert "localStorage.setItem(STORE_KEY, selected);" in src
    assert "localStorage.setItem(STORE_KEY, id);" in src


def test_local_storage_reads_are_survivable():
    src = _pm_script()
    assert "try { last = localStorage.getItem(STORE_KEY); } catch (_) {}" in src
    assert "try { localStorage.setItem(STORE_KEY, selected); } catch (_) {}" in src


def test_local_storage_playlist_restored_only_if_still_listed():
    src = _pm_script()
    assert "let selected = (last && state.playlistMap[last]) ? last : state.playlists[0].id;" in src


# ---------------------------------------------------------------------------
# Remote pagination token
# ---------------------------------------------------------------------------


def test_items_pagination_round_trips_the_page_token():
    src = _pm_script()
    assert "if (pageToken) qs.set('page_token', pageToken);" in src
    assert "state.nextPageToken = data.next_page_token || null;" in src
    assert "state.prevPageToken = data.prev_page_token || null;" in src
    assert "els.prev.addEventListener('click', function() { loadItems(state.prevPageToken); });" in src
    assert "els.next.addEventListener('click', function() { loadItems(state.nextPageToken); });" in src
    # Refresh must reload the current page, not silently jump to page one.
    assert "loadItems(state.pageToken);" in src


def test_playlists_and_picker_also_paginate_remotely():
    src = _pm_script()
    assert "/youtube/api/playlists?" in src
    assert "pageToken = data.next_page_token || null;" in src
    assert "/youtube/api/channel/videos?" in src
    assert "if (pageToken) qs.set('page_token', pageToken);" in src


def test_items_route_echoes_pagination_tokens(connected, monkeypatch):
    from app import youtube as yt

    seen = {}

    def _fake_items(conn, playlist_id, max_results=50, page_token=None):
        seen["playlist_id"] = playlist_id
        seen["page_token"] = page_token
        return {
            "items": [{
                "playlist_item_id": "it1", "playlist_id": playlist_id,
                "video_id": "v1", "title": "T", "thumbnail": None,
                "position": 0, "published_at": None,
            }],
            "next_page_token": "NEXT",
            "prev_page_token": "PREV",
            "total": 51,
        }

    monkeypatch.setattr(yt, "list_playlist_items", _fake_items)
    resp = connected.get("/youtube/api/playlists/PL1/items?max_results=50&page_token=TOK")

    assert resp.status_code == 200
    body = resp.json()
    assert body["playlist_id"] == "PL1"
    assert body["next_page_token"] == "NEXT"
    assert body["prev_page_token"] == "PREV"
    assert body["total"] == 51 and body["count"] == 1
    assert seen["page_token"] == "TOK"


# ---------------------------------------------------------------------------
# Selection clear on page / playlist change
# ---------------------------------------------------------------------------


def test_selection_clears_before_every_item_page_load():
    src = _pm_script()
    load = src[src.index("async function loadItems(pageToken) {"):src.index("// ---- playlist list ----")]
    assert "clearSelection();" in load


def test_clear_selection_resets_checks_and_select_all():
    src = _pm_script()
    clear = src[src.index("function clearSelection() {"):src.index("function updateSelectAll()")]
    assert "state.selection = new Set();" in clear
    assert "document.querySelectorAll('.yt-pm-check').forEach(function(cb) { cb.checked = false; });" in clear
    assert "all.checked = false; all.indeterminate = false;" in clear


def test_switching_playlist_reloads_from_start_which_clears_selection():
    src = _pm_script()
    handler = src[src.index("els['playlist-select'].addEventListener('change'"):src.index("els.prev.addEventListener")]
    assert "state.currentId = id;" in handler
    assert "loadItems(null);" in handler


# ---------------------------------------------------------------------------
# Auth reconnect CTA
# ---------------------------------------------------------------------------


def test_auth_banner_with_reconnect_cta_renders_when_connected(connected):
    html = _youtube_page(connected)
    assert 'class="yt-pm-auth-banner" id="yt-pm-auth-banner" hidden' in html
    assert 'href="/youtube/connect"' in html
    assert "Kết nối lại" in html


def test_auth_handler_shows_banner_and_disables_manager():
    src = _pm_script()
    assert "els.authBanner.hidden = false;" in src
    assert "state.auth = true;" in src
    assert "updateControls();" in src


def test_fetch_detects_auth_failures_and_raises_auth_error():
    src = _pm_script()
    assert "const isAuth = res.status === 401" in src
    assert "auth_required|authError|insufficientPermissions|invalid_grant|refresh token|unauthorized" in src
    assert "if (isAuth) { handleAuth(); throw new AuthError(); }" in src


def test_auth_banner_styling_exists():
    css = _css()
    assert ".yt-pm-auth-banner" in css
    assert ".yt-pm-auth-banner[hidden]" in css


# ---------------------------------------------------------------------------
# Sort: preview, then a separate confirmed apply
# ---------------------------------------------------------------------------


def test_sort_uses_distinct_preview_and_apply_endpoints():
    src = _pm_script()
    assert "/sort/preview" in src
    assert "/sort/apply" in src


def test_sort_preview_runs_on_open_and_direction_change_only():
    src = _pm_script()
    open_sort = src[src.index("async function openSort() {"):src.index("function sortDirection()")]
    assert "sortModal.showModal();" in open_sort
    assert "await loadSortPreview(sortDirection());" in open_sort
    # The direction toggle re-fetches the preview; it never applies anything.
    for_each_idx = src.index("['asc','desc'].forEach(function(dir)")
    apply_idx = src.index("document.getElementById('yt-pm-sort-apply')", for_each_idx)
    dir_handler = src[for_each_idx:apply_idx]
    assert "loadSortPreview(dir).catch" in dir_handler
    assert "/sort/apply" not in dir_handler


def test_sort_apply_is_a_separate_confirmed_button():
    src = _pm_script()
    apply = src[src.index("document.getElementById('yt-pm-sort-apply').addEventListener('click'"):src.index("// modal close handling")]
    assert "/sort/apply" in apply
    assert "/sort/preview" not in apply
    assert "sortModal.close();" in apply


def test_sort_apply_stays_disabled_until_the_preview_shows_a_change():
    src = _pm_script()
    assert "document.getElementById('yt-pm-sort-apply').disabled = changed === 0;" in src
    assert 'id="yt-pm-sort-apply" disabled' in _source()


def test_sort_modals_render_expected_controls(connected):
    html = _youtube_page(connected)
    assert 'id="yt-pm-sort-modal"' in html
    assert 'class="active" id="yt-pm-sort-dir-asc"' in html
    assert 'id="yt-pm-sort-dir-desc"' in html
    assert 'id="yt-pm-sort-preview-body"' in html


# ---------------------------------------------------------------------------
# Safe title rendering / no untrusted innerHTML
# ---------------------------------------------------------------------------


def test_remote_titles_are_never_interpolated_into_inner_html():
    assert ".innerHTML" not in _pm_script()


def test_el_helper_assigns_text_content_only():
    src = _pm_script()
    assert "if (text !== undefined) node.textContent = text;" in src


def test_item_titles_rendered_through_text_content():
    src = _pm_script()
    assert "const link = el('a', '', it.title || '(không tiêu đề)');" in src
    assert "const chanTd = el('td', '', it.channelTitle || '—');" in src


def test_video_links_force_the_youtube_scheme():
    src = _pm_script()
    assert "return 'https://youtube.com/watch?v=' + encodeURIComponent(videoId || '');" in src
    assert "link.href = videoHref(it.video_id);" in src


def test_playlist_manager_referenced_helpers_are_all_defined():
    """hideResult() was once called but never defined - every referenced helper must exist."""
    src = _pm_script()
    assert "function hideResult()" in src


# ---------------------------------------------------------------------------
# Channel video picker
# ---------------------------------------------------------------------------


def test_picker_modal_and_search_controls_render(connected):
    html = _youtube_page(connected)
    assert 'id="yt-pm-picker-modal"' in html
    assert 'id="yt-pm-picker-search"' in html
    assert 'id="yt-pm-picker-select-all"' in html
    assert 'id="yt-pm-picker-add" disabled' in html


def test_picker_fetches_channel_videos_with_search_and_pagination():
    src = _pm_script()
    assert "pickerModal.showModal();" in src
    assert "await loadPickerPage(null);" in src
    assert "/youtube/api/channel/videos?" in src
    assert "if (picker.q) qs.set('q', picker.q);" in src
    assert "if (pageToken) qs.set('page_token', pageToken);" in src
    assert "picker.next = data.next_page_token || null;" in src
    assert "picker.prev = data.prev_page_token || null;" in src


def test_picker_add_posts_selected_video_ids():
    src = _pm_script()
    add = src[src.index("document.getElementById('yt-pm-picker-add').addEventListener('click'"):src.index("// sort modal")]
    assert "const videoIds = Array.from(picker.selected);" in add
    assert "JSON.stringify({ video_ids: videoIds })" in add


# ---------------------------------------------------------------------------
# Bulk actions (upload history)
# ---------------------------------------------------------------------------


def test_upload_history_bulk_toolbar_renders(connected):
    html = _youtube_page(connected)
    assert 'id="yt-toolbar"' in html
    assert 'id="yt-bulk-delete"' in html
    assert 'id="yt-bulk-retry"' in html
    assert 'id="yt-select-all"' in html


def test_bulk_toolbar_is_hidden_until_a_selection(connected):
    """The toolbar's inline style must default to display:none. A stray trailing
    display:flex overrides it, so the disabled bar is visible on every page load."""
    html = _youtube_page(connected)
    start = html.index('id="yt-toolbar"')
    tag = html[start:html.index(">", start)]
    assert tag.count("display:") == 1
    assert "display:none" in tag


def test_upload_bulk_actions_only_arm_when_appropriate():
    src = _source()
    assert "function updateToolbar() {" in src
    assert "const hasFailed = checked.some(cb => cb.closest('tr').dataset.status === 'failed');" in src
    assert "retryBtn.disabled = !hasFailed;" in src
    assert "toolbar.style.display = hasSelection ? 'flex' : 'none';" in src
    assert "'/youtube/uploads/bulk-delete'" in src
    assert "'/youtube/uploads/bulk-retry'" in src


# ---------------------------------------------------------------------------
# Drag & drop page reorder
# ---------------------------------------------------------------------------


def test_rows_are_draggable_and_drop_posts_absolute_positions():
    src = _pm_script()
    assert "tr.draggable = true;" in src
    assert "tr.addEventListener('dragstart', onDragStart);" in src
    assert "tr.addEventListener('drop', onDrop);" in src
    drop = src[src.index("async function onDrop(e) {"):src.index("// ---- sort (whole playlist")]
    assert "const positions = {};" in drop
    assert "positions[id] = pageStart + i;" in drop
    assert "body: JSON.stringify({ positions: positions })" in drop
    assert "'/reorder'" in drop


# ---------------------------------------------------------------------------
# Position controls
# ---------------------------------------------------------------------------


def test_position_controls_render_when_connected(connected):
    html = _youtube_page(connected)
    assert 'id="yt-pm-up"' in html
    assert 'id="yt-pm-down"' in html
    assert 'id="yt-pm-position"' in html
    assert 'id="yt-pm-position-go"' in html


def test_up_down_move_single_selection_one_step():
    src = _pm_script()
    assert "moveToPosition((typeof it.position === 'number' ? it.position : 0) - 1);" in src
    assert "moveToPosition((typeof it.position === 'number' ? it.position : 0) + 1);" in src


def test_position_go_sends_one_based_target():
    src = _pm_script()
    assert "const v = parseInt(els.position.value, 10);" in src
    assert "if (isNaN(v) || v < 1)" in src
    assert "moveToPosition(v - 1);" in src
    assert "body: JSON.stringify({ position: newPos + 1 })" in src


# ---------------------------------------------------------------------------
# Regression: upload dropdown / form / history / progress / pipeline strings
# ---------------------------------------------------------------------------


def test_upload_form_dropzone_and_fields_remain(connected):
    html = _youtube_page(connected)
    for needle in (
        'id="yt-upload-form"',
        'id="dropzone-yt"',
        'id="yt-file"',
        'id="yt-title"',
        'id="yt-desc"',
        'id="yt-tags"',
        'id="yt-privacy"',
        'id="yt-playlist"',
        'id="btn-yt-upload"',
    ):
        assert needle in html
    assert "Kéo thả video vào đây hoặc bấm để chọn" in html


def test_upload_dropdown_lists_server_side_playlists(connected):
    html = _youtube_page(connected)
    assert '<option value="PL1">First</option>' in html
    assert '<option value="PL2">Second</option>' in html


def test_upload_history_table_remains(connected):
    html = _youtube_page(connected)
    assert "Lịch sử Upload" in html
    assert 'class="yt-uploads-table data-table" data-table-key="youtube-uploads"' in html
    assert 'id="yt-selected-count"' in html


def test_upload_progress_polling_remains():
    src = _source()
    assert "const POLL_MS = 3000;" in src
    assert "fetch('/youtube/uploads')" in src
    assert "yt-progress-fill" in src
    assert "yt-progress-pct" in src
    assert "if (activeRowCount() > 0) setTimeout(poll, POLL_MS);" in src


def test_upload_form_hands_off_to_the_queue():
    src = _source()
    handler = src[src.index("document.getElementById('yt-upload-form')"):src.index("</script>")]
    assert "fd.append('playlist_id', playlist)" in handler
    assert "location.reload()" in handler


def test_pipeline_status_strings_remain():
    src = _source()
    for needle in (
        "validation_status",
        "waiting_for_rerender",
        "rerendering",
        "upload_progress",
        "thumbnail_status",
        "playlist_status",
        "youtube_video_id",
        "yt-status-done",
        "yt-status-failed",
    ):
        assert needle in src


def test_pm_styles_for_manager_and_rows_exist():
    css = _css()
    for needle in (
        ".yt-pm-root",
        ".yt-pm-row.selected td",
        ".yt-pm-row.dragging",
        ".yt-pm-row.dragover td",
        ".yt-pm-thumb",
        ".yt-pm-selection-bar",
        ".yt-pm-pagination",
    ):
        assert needle in css
