"""Focused unit tests for the playlist-management service layer in app/youtube.py.

Every test runs against a reusable in-memory fake of the YouTube Data API's
playlistItems resource (list/insert/delete/update), so nothing touches the network
and failure injection can exercise the partial-failure paths.

Coverage maps to the semantics the playlist-manager UI and routes rely on:

* natural-title sort keys: numeric chunks, case-insensitive, stable ties, asc/desc
* reorder preview being fully non-mutating (reads only; no insert/update/delete)
* applying an explicit video-id order mapping, including unlisted items keeping their
  relative place at the end and duplicate ids in the order being deduplicated
* listing/normalizing playlistItems responses and full-playlist pagination
* single-item playlistItem.id delete/update plus the video-id resolution path
* the optional insert position on add (append when omitted)
* duplicate skipping for add/copy, both against the playlist and within one batch
* partial-failure continuation for add/remove/copy/move/reorder/sort
* move ordering (add-before-remove), duplicate retention, and source-removal failure
* page boundary helpers and reorder_playlist_page
"""
from __future__ import annotations

import re

import pytest

from app import youtube as yt


def _conn():
    """Dummy connection; get_youtube_service is monkeypatched to ignore it."""
    return None


class FakeHttpError(Exception):
    """Stand-in for googleapiclient.errors.HttpError raised by the fake API."""

    def __init__(self, message, status=500):
        super().__init__(message)
        self.status = status


def _raw_item(item_id, playlist_id, video_id, title, position, *, published_at=None, thumbnail=None):
    thumbnails = {"medium": {"url": thumbnail}} if thumbnail else {}
    return {
        "id": item_id,
        "snippet": {
            "playlistId": playlist_id,
            "position": position,
            "resourceId": {"kind": "youtube#video", "videoId": video_id},
            "title": title,
            "thumbnails": thumbnails,
        },
        "contentDetails": {"videoPublishedAt": published_at},
    }


def _raw_list(specs, playlist_id="PL1"):
    """Raw playlistItems resources in order; ids PI1, PI2, ..."""
    return [
        _raw_item(f"PI{i + 1}", playlist_id, video_id, title, i)
        for i, (video_id, title) in enumerate(specs)
    ]


def _items(specs):
    """Normalized playlist items in order (no API involved)."""
    return [
        {
            "playlist_item_id": f"PI{i + 1}",
            "playlist_id": "PL1",
            "video_id": video_id,
            "title": title,
            "description": "",
            "thumbnail": None,
            "position": i,
            "published_at": None,
        }
        for i, (video_id, title) in enumerate(specs)
    ]


class _ApiCall:
    def __init__(self, api, method, kwargs):
        self._api, self._method, self._kwargs = api, method, kwargs

    def execute(self):
        return getattr(self._api, f"_handle_{self._method}")(**self._kwargs)


class FakePlaylistItems:
    """A faithful-enough in-memory model of the playlistItems resource.

    Items live in per-playlist lists ordered by zero-based position. insert appends
    when no position is given, otherwise it inserts at that index shifting later
    items; update removes the item and re-inserts it at the new position; delete
    removes by id and the remaining positions close the gap. list paginates with
    page{index} tokens, capped at the fake's page_size. Failure sets let tests
    exercise the partial-failure paths deterministically.
    """

    def __init__(self, *, page_size=50):
        self.playlists = {}
        self._next_id = 1
        self.page_size = page_size
        self.list_calls = []
        self.insert_calls = []
        self.delete_calls = []
        self.update_calls = []
        self.log = []
        self.fail_insert = set()
        self.fail_delete = set()
        self.fail_update = set()

    # -- resource entry points (return objects with .execute()) -------------------

    def list(self, **kwargs):
        return _ApiCall(self, "list", kwargs)

    def insert(self, **kwargs):
        return _ApiCall(self, "insert", kwargs)

    def delete(self, **kwargs):
        return _ApiCall(self, "delete", kwargs)

    def update(self, **kwargs):
        return _ApiCall(self, "update", kwargs)

    # -- test helpers --------------------------------------------------------------

    def seed(self, playlist_id, items):
        store = []
        for raw in items:
            store.append(dict(raw))
            m = re.search(r"(\d+)$", raw["id"] or "")
            self._next_id = max(self._next_id, (int(m.group(1)) + 1) if m else self._next_id)
        self.playlists[playlist_id] = store
        self._reindex(playlist_id)
        return self

    def items_of(self, playlist_id):
        """Raw item list for a playlist, ordered by position."""
        return [dict(raw) for raw in self.playlists.get(playlist_id, [])]

    def video_ids_of(self, playlist_id):
        return [raw["snippet"]["resourceId"]["videoId"] for raw in self.items_of(playlist_id)]

    # -- handlers -----------------------------------------------------------------

    def _reindex(self, playlist_id):
        for i, raw in enumerate(self.playlists.get(playlist_id, [])):
            raw["snippet"]["position"] = i

    def _clamp(self, pos, length):
        return max(0, min(int(pos), length))

    def _handle_list(self, **params):
        playlist_id = params["playlistId"]
        max_results = params.get("maxResults") or len(self.playlists.get(playlist_id, []))
        per_page = max(1, min(max_results, self.page_size))
        all_items = self.playlists.get(playlist_id, [])
        page = 0
        if params.get("pageToken") and str(params["pageToken"]).startswith("page"):
            page = int(str(params["pageToken"])[len("page"):])
        start = page * per_page
        chunk = all_items[start:start + per_page]
        self.list_calls.append({"params": {**params}})
        return {
            "items": [dict(raw) for raw in chunk],
            "nextPageToken": f"page{page + 1}" if start + per_page < len(all_items) else None,
            "prevPageToken": f"page{page - 1}" if page > 0 else None,
            "pageInfo": {"totalResults": len(all_items), "resultsPerPage": len(chunk)},
        }

    def _handle_insert(self, part=None, body=None):
        self.insert_calls.append({"part": part, "body": body})
        snippet = body["snippet"]
        playlist_id = snippet["playlistId"]
        video_id = snippet["resourceId"]["videoId"]
        position = snippet.get("position")
        if video_id in self.fail_insert:
            raise FakeHttpError(f"insert failed for {video_id}", 500)
        self.log.append(("insert", video_id))
        store = self.playlists.setdefault(playlist_id, [])
        item_id = f"PI{self._next_id}"
        self._next_id += 1
        pos = len(store) if position is None else self._clamp(position, len(store))
        raw = _raw_item(item_id, playlist_id, video_id, f"Title {video_id}", pos)
        store.insert(pos, raw)
        self._reindex(playlist_id)
        return dict(raw)

    def _handle_delete(self, id=None):
        self.delete_calls.append({"id": id})
        if id in self.fail_delete:
            raise FakeHttpError(f"delete failed for {id}", 500)
        self.log.append(("delete", id))
        for playlist_id, store in self.playlists.items():
            for i, raw in enumerate(store):
                if raw["id"] == id:
                    del store[i]
                    self._reindex(playlist_id)
                    return {}
        raise FakeHttpError(f"playlist item {id} not found", 404)

    def _handle_update(self, part=None, body=None):
        self.update_calls.append({"part": part, "body": body})
        item_id = body["id"]
        if item_id in self.fail_update:
            raise FakeHttpError(f"update failed for {item_id}", 500)
        self.log.append(("update", item_id))
        snippet = body["snippet"]
        playlist_id = snippet["playlistId"]
        position = int(snippet["position"])
        video_id = snippet["resourceId"]["videoId"]
        store = self.playlists.get(playlist_id)
        if store is None:
            raise FakeHttpError("playlist not found", 404)
        idx = next((i for i, raw in enumerate(store) if raw["id"] == item_id), None)
        if idx is None:
            raise FakeHttpError(f"playlist item {item_id} not found", 404)
        raw = store.pop(idx)
        pos = self._clamp(position, len(store))
        store.insert(pos, raw)
        raw["snippet"]["position"] = pos
        self._reindex(playlist_id)
        return dict(raw)


class FakeService:
    def __init__(self, api):
        self._api = api

    def playlistItems(self):
        return self._api


@pytest.fixture
def fake(monkeypatch):
    """Build a fake playlistItems API and wire it into app.youtube's service factory."""
    api = FakePlaylistItems()
    monkeypatch.setattr(yt, "_require_google_imports", lambda: None)
    monkeypatch.setattr(yt, "get_youtube_service", lambda conn: FakeService(api))
    return api


# ---------------------------------------------------------------------------
# Natural sort: numeric chunks, case-insensitive, stable ties, asc/desc
# ---------------------------------------------------------------------------


def test_natural_sort_key_orders_numeric_chunks():
    titles = ["Part 10", "Part 2", "Part 1", "Part 2b", "Part 2"]
    assert sorted(titles, key=yt._natural_sort_key) == ["Part 1", "Part 2", "Part 2", "Part 2b", "Part 10"]


def test_natural_sort_key_handles_multiple_number_groups():
    titles = ["S2E10", "S10E2", "S2E2"]
    assert sorted(titles, key=yt._natural_sort_key) == ["S2E2", "S2E10", "S10E2"]


def test_natural_sort_key_is_case_insensitive():
    titles = ["alpha", "BETA", "Alpha"]
    assert sorted(titles, key=yt._natural_sort_key) == ["alpha", "Alpha", "BETA"]


def test_natural_sort_key_handles_empty_titles():
    assert yt._natural_sort_key("") == yt._natural_sort_key(None)


# ---------------------------------------------------------------------------
# Episode sort: series name in front of the marker, then episode number
# ---------------------------------------------------------------------------


def test_episode_sort_key_orders_by_episode_not_by_trailing_text():
    # The chapter range that follows "Tập N" would drive a plain title sort
    # ("Chương 10-14" < "Chương 5-9"); the episode key must ignore it.
    titles = [
        "Dị Độ Lữ Xá - Tập 3 - Chương 10-14",
        "Dị Độ Lữ Xá - Tập 1 - Chương 1-4",
        "Dị Độ Lữ Xá - Tập 10 - Chương 40-44",
        "Dị Độ Lữ Xá - Tập 2 - Chương 5-9",
    ]
    assert sorted(titles, key=yt._episode_sort_key) == [
        "Dị Độ Lữ Xá - Tập 1 - Chương 1-4",
        "Dị Độ Lữ Xá - Tập 2 - Chương 5-9",
        "Dị Độ Lữ Xá - Tập 3 - Chương 10-14",
        "Dị Độ Lữ Xá - Tập 10 - Chương 40-44",
    ]


def test_episode_sort_key_groups_by_series_name():
    titles = ["Book B - Tập 1", "Book A - Tập 2", "Book B - Tập 2", "Book A - Tập 1"]
    assert sorted(titles, key=yt._episode_sort_key) == [
        "Book A - Tập 1", "Book A - Tập 2", "Book B - Tập 1", "Book B - Tập 2",
    ]


@pytest.mark.parametrize("marker", ["Tập", "tập", "TẬP", "Tap", "Phần", "Part", "EP.", "#"])
def test_episode_sort_key_accepts_marker_spellings(marker):
    titles = [f"Sách - {marker} 10", f"Sách - {marker} 2"]
    assert sorted(titles, key=yt._episode_sort_key) == [f"Sách - {marker} 2", f"Sách - {marker} 10"]


def test_episode_sort_key_handles_marker_first_titles():
    titles = ["Tập 2 - Dị Độ Lữ Xá", "Tập 10 - Dị Độ Lữ Xá", "Tập 1 - Dị Độ Lữ Xá"]
    assert sorted(titles, key=yt._episode_sort_key) == [
        "Tập 1 - Dị Độ Lữ Xá", "Tập 2 - Dị Độ Lữ Xá", "Tập 10 - Dị Độ Lữ Xá",
    ]


def test_episode_sort_key_interleaves_both_title_layouts():
    # A playlist that mixes the upload template's "Tập N - Sách" with hand-named
    # "Sách - Tập N" must still come out in episode order, not in two blocks.
    titles = [
        "Dị Độ Lữ Xá - Tập 3 - Chương 9-12",
        "Tập 1 - Dị Độ Lữ Xá",
        "Dị Độ Lữ Xá - Tập 4",
        "Tập 2 - Dị Độ Lữ Xá - Chương 5-8",
    ]
    assert sorted(titles, key=yt._episode_sort_key) == [
        "Tập 1 - Dị Độ Lữ Xá",
        "Tập 2 - Dị Độ Lữ Xá - Chương 5-8",
        "Dị Độ Lữ Xá - Tập 3 - Chương 9-12",
        "Dị Độ Lữ Xá - Tập 4",
    ]


def test_episode_sort_key_falls_back_for_titles_without_a_marker():
    titles = ["Intro 10", "Intro 2", "Sách - Tập 1"]
    assert sorted(titles, key=yt._episode_sort_key) == ["Intro 2", "Intro 10", "Sách - Tập 1"]


def test_episode_sort_key_handles_empty_titles():
    assert yt._episode_sort_key("") == yt._episode_sort_key(None)


def test_new_order_episode_mode_beats_natural_mode():
    items = _items([
        ("v2", "Dị Độ Lữ Xá - Tập 2"),
        ("v1", "Tập 1 - Dị Độ Lữ Xá"),
    ])
    # natural compares whole strings, so the two title layouts split into blocks
    assert [i["video_id"] for i in yt._new_order(items, None, "asc", "natural")] == ["v2", "v1"]
    assert [i["video_id"] for i in yt._new_order(items, None, "asc", "episode")] == ["v1", "v2"]


def test_sort_key_for_unknown_mode_falls_back_to_natural():
    assert yt._sort_key_for("nonsense") is yt._natural_sort_key
    assert yt._sort_key_for("episode") is yt._episode_sort_key


def test_sort_playlist_episode_mode_applies_episode_order(fake):
    fake.seed("PL1", _raw_list([
        ("v2", "Sách - Tập 2 - Chương 1-4"),
        ("v10", "Sách - Tập 10 - Chương 5-9"),
        ("v1", "Sách - Tập 1 - Chương 90-94"),
    ]))
    yt.sort_playlist(_conn(), "PL1", direction="asc", mode="episode")
    assert fake.video_ids_of("PL1") == ["v1", "v2", "v10"]


def test_sort_preview_episode_mode_is_non_mutating(fake):
    fake.seed("PL1", _raw_list([("v2", "Sách - Tập 2"), ("v1", "Sách - Tập 1")]))
    result = yt.sort_playlist_preview(_conn(), "PL1", "asc", "episode")
    assert result["ordered"] == ["v1", "v2"]
    assert fake.update_calls == [] and fake.video_ids_of("PL1") == ["v2", "v1"]


def test_new_order_asc_and_desc_directions():
    items = _items([("v2", "Part 2"), ("v10", "Part 10"), ("v1", "Part 1")])
    assert [i["video_id"] for i in yt._new_order(items, None, "asc")] == ["v1", "v2", "v10"]
    assert [i["video_id"] for i in yt._new_order(items, None, "desc")] == ["v10", "v2", "v1"]


def test_new_order_is_stable_for_equal_titles():
    items = _items([("a", "Same"), ("b", "Same"), ("c", "Same")])
    assert [i["video_id"] for i in yt._new_order(items, None, "asc")] == ["a", "b", "c"]
    assert [i["video_id"] for i in yt._new_order(items, None, "desc")] == ["a", "b", "c"]


def test_new_order_sorts_case_insensitively():
    items = _items([("a", "Zebra"), ("b", "apple"), ("c", "Mango")])
    assert [i["video_id"] for i in yt._new_order(items, None, "asc")] == ["b", "c", "a"]


def test_new_order_direction_is_case_insensitive():
    items = _items([("a", "A"), ("b", "B")])
    assert [i["video_id"] for i in yt._new_order(items, None, "DESC")] == ["b", "a"]


# ---------------------------------------------------------------------------
# Apply mapping: explicit order, dedup, unlisted tail
# ---------------------------------------------------------------------------


def test_new_order_explicit_mapping_with_unlisted_tail():
    items = _items([("a", "A"), ("b", "B"), ("c", "C"), ("d", "D")])
    assert [i["video_id"] for i in yt._new_order(items, ["d", "a"], "asc")] == ["d", "a", "b", "c"]


def test_new_order_dedupes_duplicate_ids_in_order():
    items = _items([("a", "A"), ("b", "B")])
    assert [i["video_id"] for i in yt._new_order(items, ["a", "b", "a"], "asc")] == ["a", "b"]


def test_new_order_ignores_unknown_ids():
    items = _items([("a", "A"), ("b", "B")])
    assert [i["video_id"] for i in yt._new_order(items, ["zzz", "b"], "asc")] == ["b", "a"]


def test_new_order_empty_list_keeps_original_order():
    items = _items([("a", "A"), ("b", "B")])
    assert [i["video_id"] for i in yt._new_order(items, [], "asc")] == ["a", "b"]


def test_new_order_explicit_order_ignores_direction():
    items = _items([("a", "A"), ("b", "B"), ("c", "C")])
    assert [i["video_id"] for i in yt._new_order(items, ["c", "a"], "desc")] == ["c", "a", "b"]


def test_compute_positions_pairs_items_with_zero_based_positions():
    items = _items([("a", "A"), ("b", "B"), ("c", "C")])
    pairs = yt._compute_positions(items, ["c", "a", "b"], "asc")
    assert [(i["video_id"], p) for i, p in pairs] == [("c", 0), ("a", 1), ("b", 2)]


# ---------------------------------------------------------------------------
# Preview: non-mutating, reads only
# ---------------------------------------------------------------------------


def test_reorder_preview_by_natural_sort_is_non_mutating(fake):
    fake.seed("PL1", _raw_list([("v2", "Part 2"), ("v1", "Part 1"), ("v3", "Part 3")]))
    result = yt.reorder_playlist_preview(_conn(), "PL1", direction="asc")

    assert [i["video_id"] for i in result["items"]] == ["v1", "v2", "v3"]
    assert result["ordered"] == ["v1", "v2", "v3"]
    positions = {i["video_id"]: (i["current_position"], i["new_position"]) for i in result["items"]}
    assert positions == {"v1": (1, 0), "v2": (0, 1), "v3": (2, 2)}

    # no writes of any kind happened
    assert fake.insert_calls == []
    assert fake.delete_calls == []
    assert fake.update_calls == []
    # and the underlying playlist is untouched
    assert fake.video_ids_of("PL1") == ["v2", "v1", "v3"]


def test_sort_preview_is_non_mutating(fake):
    fake.seed("PL1", _raw_list([("v1", "Part 1"), ("v2", "Part 2"), ("v10", "Part 10")]))
    result = yt.sort_playlist_preview(_conn(), "PL1", direction="desc")
    assert result["ordered"] == ["v10", "v2", "v1"]
    assert fake.insert_calls == [] and fake.delete_calls == [] and fake.update_calls == []
    assert fake.video_ids_of("PL1") == ["v1", "v2", "v10"]


def test_sort_preview_matches_reorder_preview(fake):
    fake.seed("PL1", _raw_list([("v2", "Part 2"), ("v1", "Part 1")]))
    assert yt.sort_playlist_preview(_conn(), "PL1", "asc") == yt.reorder_playlist_preview(
        _conn(), "PL1", order=None, direction="asc"
    )
    assert fake.update_calls == [] and fake.delete_calls == [] and fake.insert_calls == []


def test_reorder_preview_with_explicit_order_is_non_mutating(fake):
    fake.seed("PL1", _raw_list([("v1", "A"), ("v2", "B"), ("v3", "C")]))
    result = yt.reorder_playlist_preview(_conn(), "PL1", order=["v3", "v1"])
    assert [i["video_id"] for i in result["items"]] == ["v3", "v1", "v2"]
    assert result["ordered"] == ["v3", "v1", "v2"]
    assert fake.insert_calls == [] and fake.delete_calls == [] and fake.update_calls == []


# ---------------------------------------------------------------------------
# Apply: reorder / sort writes through playlistItems.update
# ---------------------------------------------------------------------------


def test_reorder_playlist_applies_explicit_mapping(fake):
    fake.seed("PL1", _raw_list([("v1", "A"), ("v2", "B"), ("v3", "C")]))
    result = yt.reorder_playlist(_conn(), "PL1", order=["v3", "v1", "v2"])

    assert result["requested"] == 3
    assert result["succeeded"] == 3 and result["failed"] == 0 and result["skipped"] == 0
    assert fake.video_ids_of("PL1") == ["v3", "v1", "v2"]

    # each update targeted the right playlistItem id / playlist / video / position
    bodies = [c["body"] for c in fake.update_calls]
    assert [(b["id"], b["snippet"]["position"]) for b in bodies] == [("PI3", 0), ("PI1", 1), ("PI2", 2)]
    for b in bodies:
        assert b["snippet"]["playlistId"] == "PL1"
        assert b["snippet"]["resourceId"]["kind"] == "youtube#video"


def test_reorder_playlist_result_items_carry_status_and_message(fake):
    fake.seed("PL1", _raw_list([("v1", "A"), ("v2", "B")]))
    result = yt.reorder_playlist(_conn(), "PL1", order=["v2", "v1"])
    assert result["items"][0]["status"] == "succeeded"
    assert "moved to position 0" in result["items"][0]["message"]


def test_reorder_playlist_skips_items_already_in_place(fake):
    fake.seed("PL1", _raw_list([("v1", "Part 1"), ("v2", "Part 2")]))
    result = yt.reorder_playlist(_conn(), "PL1", direction="asc")
    assert result["skipped"] == 2 and result["succeeded"] == 0
    assert fake.update_calls == []


def test_sort_playlist_uses_natural_order(fake):
    fake.seed("PL1", _raw_list([("v2", "Part 2"), ("v10", "Part 10"), ("v1", "Part 1")]))
    result = yt.sort_playlist(_conn(), "PL1", direction="asc")
    assert result["succeeded"] == 3
    assert fake.video_ids_of("PL1") == ["v1", "v2", "v10"]


def test_sort_playlist_desc(fake):
    fake.seed("PL1", _raw_list([("v1", "Part 1"), ("v2", "Part 2"), ("v10", "Part 10")]))
    result = yt.sort_playlist(_conn(), "PL1", direction="desc")
    assert fake.video_ids_of("PL1") == ["v10", "v2", "v1"]
    assert result["succeeded"] + result["skipped"] == 3


def test_reorder_playlist_explicit_order_keeps_unlisted_at_end(fake):
    fake.seed("PL1", _raw_list([("v1", "A"), ("v2", "B"), ("v3", "C"), ("v4", "D")]))
    result = yt.reorder_playlist(_conn(), "PL1", order=["v4", "v2"])
    assert fake.video_ids_of("PL1") == ["v4", "v2", "v1", "v3"]
    assert result["succeeded"] == 3 and result["skipped"] == 1


def test_reorder_playlist_partial_failure_continues(fake):
    fake.seed("PL1", _raw_list([("v1", "A"), ("v2", "B"), ("v3", "C")]))
    fake.fail_update.add("PI3")
    result = yt.reorder_playlist(_conn(), "PL1", order=["v3", "v1", "v2"])
    assert result["failed"] == 1 and result["succeeded"] == 2
    # v3 never moved; v1 and v2 still applied their new positions
    assert fake.video_ids_of("PL1") == ["v1", "v3", "v2"]


# ---------------------------------------------------------------------------
# List mapping / normalization / pagination
# ---------------------------------------------------------------------------


def test_normalize_playlist_item_maps_all_fields():
    raw = {
        "id": "PI9",
        "snippet": {
            "playlistId": "PL1",
            "position": 3,
            "resourceId": {"kind": "youtube#video", "videoId": "vidX"},
            "title": "Hello",
            "description": "Mô tả video",
            "thumbnails": {
                "default": {"url": "d.jpg"},
                "high": {"url": "h.jpg"},
                "medium": {"url": "m.jpg"},
            },
        },
        "contentDetails": {"videoPublishedAt": "2026-01-01T00:00:00Z"},
    }
    item = yt._normalize_playlist_item(raw)
    assert item == {
        "playlist_item_id": "PI9",
        "playlist_id": "PL1",
        "video_id": "vidX",
        "title": "Hello",
        "description": "Mô tả video",
        "thumbnail": "m.jpg",
        "position": 3,
        "published_at": "2026-01-01T00:00:00Z",
    }


def test_normalize_playlist_item_falls_back_to_playlist_id_param():
    raw = {"id": "P1", "snippet": {}, "contentDetails": {}}
    item = yt._normalize_playlist_item(raw, "PL_FALLBACK")
    assert item == {
        "playlist_item_id": "P1",
        "playlist_id": "PL_FALLBACK",
        "video_id": "",
        "title": "",
        "description": "",
        "thumbnail": None,
        "position": 0,
        "published_at": None,
    }


def test_list_playlist_items_normalizes_page(fake):
    fake.seed("PL1", [_raw_item("PI9", "PL1", "vidX", "Hello", 0)])
    page = yt.list_playlist_items(_conn(), "PL1", max_results=50)
    assert page["total"] == 1
    assert page["items"] == [{
        "playlist_item_id": "PI9",
        "playlist_id": "PL1",
        "video_id": "vidX",
        "title": "Hello",
        "description": "",
        "thumbnail": None,
        "position": 0,
        "published_at": None,
    }]


def test_list_playlist_items_passes_max_results_to_api(fake):
    fake.seed("PL1", _raw_list([("v1", "A")]))
    yt.list_playlist_items(_conn(), "PL1", max_results=17)
    assert fake.list_calls[-1]["params"]["maxResults"] == 17


def test_list_playlist_items_round_trips_page_tokens(fake):
    fake.page_size = 2
    fake.seed("PL1", _raw_list([("v1", "T1"), ("v2", "T2"), ("v3", "T3")]))

    page1 = yt.list_playlist_items(_conn(), "PL1", max_results=2)
    assert [i["video_id"] for i in page1["items"]] == ["v1", "v2"]
    assert page1["next_page_token"] == "page1"
    assert page1["prev_page_token"] is None

    page2 = yt.list_playlist_items(_conn(), "PL1", max_results=2, page_token=page1["next_page_token"])
    assert [i["video_id"] for i in page2["items"]] == ["v3"]
    assert page2["next_page_token"] is None
    assert page2["prev_page_token"] == "page0"
    assert page2["total"] == 3


def test_get_all_playlist_items_paginates_until_exhausted(fake):
    fake.page_size = 3
    fake.seed("PL1", _raw_list([(f"v{i}", f"Title {i}") for i in range(7)]))
    items = yt.get_all_playlist_items(_conn(), "PL1")
    assert [i["video_id"] for i in items] == ["v0", "v1", "v2", "v3", "v4", "v5", "v6"]
    assert [c["params"].get("pageToken") for c in fake.list_calls] == [None, "page1", "page2"]


def test_find_playlist_item_locates_across_pages(fake):
    fake.page_size = 2
    fake.seed("PL1", _raw_list([("v1", "A"), ("v2", "B"), ("v3", "C")]))
    item = yt.find_playlist_item(_conn(), "PL1", "v3")
    assert item["playlist_item_id"] == "PI3"
    assert item["video_id"] == "v3"


def test_find_playlist_item_returns_none_when_absent(fake):
    fake.seed("PL1", _raw_list([("v1", "A")]))
    assert yt.find_playlist_item(_conn(), "PL1", "missing") is None


# ---------------------------------------------------------------------------
# playlistItem.id delete / update and video-id resolution
# ---------------------------------------------------------------------------


def test_remove_playlist_item_deletes_by_item_id(fake):
    fake.seed("PL1", _raw_list([("v1", "A"), ("v2", "B")]))
    yt.remove_playlist_item(_conn(), "PL1", "PI1")
    assert fake.delete_calls == [{"id": "PI1"}]
    assert fake.video_ids_of("PL1") == ["v2"]


def test_remove_video_from_playlist_resolves_video_to_item(fake):
    fake.seed("PL1", _raw_list([("v1", "A"), ("v2", "B")]))
    assert yt.remove_video_from_playlist(_conn(), "PL1", "v1") is True
    assert fake.delete_calls == [{"id": "PI1"}]
    assert fake.video_ids_of("PL1") == ["v2"]


def test_remove_video_from_playlist_false_when_absent(fake):
    fake.seed("PL1", _raw_list([("v1", "A")]))
    assert yt.remove_video_from_playlist(_conn(), "PL1", "nope") is False
    assert fake.delete_calls == []


def test_update_playlist_item_position_with_video_id(fake):
    fake.seed("PL1", _raw_list([("v1", "A"), ("v2", "B"), ("v3", "C")]))
    yt.update_playlist_item_position(_conn(), "PL1", "PI1", 2, video_id="v1")
    body = fake.update_calls[-1]["body"]
    assert body["id"] == "PI1"
    assert body["snippet"]["playlistId"] == "PL1"
    assert body["snippet"]["position"] == 2
    assert body["snippet"]["resourceId"]["videoId"] == "v1"
    assert fake.video_ids_of("PL1") == ["v2", "v3", "v1"]


def test_update_playlist_item_position_resolves_video_id_when_omitted(fake):
    fake.seed("PL1", _raw_list([("v1", "A"), ("v2", "B"), ("v3", "C")]))
    yt.update_playlist_item_position(_conn(), "PL1", "PI2", 0)
    assert fake.list_calls, "the video id must be resolved with a list call when omitted"
    body = fake.update_calls[-1]["body"]
    assert body["id"] == "PI2"
    assert body["snippet"]["resourceId"]["videoId"] == "v2"
    assert body["snippet"]["position"] == 0


def test_update_playlist_item_position_raises_when_item_missing(fake):
    fake.seed("PL1", _raw_list([("v1", "A")]))
    with pytest.raises(ValueError):
        yt.update_playlist_item_position(_conn(), "PL1", "PI99", 0)
    assert fake.update_calls == []


def test_move_playlist_item_is_alias_of_position_update(fake):
    fake.seed("PL1", _raw_list([("v1", "A"), ("v2", "B")]))
    yt.move_playlist_item(_conn(), "PL1", "PI2", 0, video_id="v2")
    assert len(fake.update_calls) == 1
    assert fake.update_calls[0]["body"]["id"] == "PI2"
    assert fake.update_calls[0]["body"]["snippet"]["position"] == 0


# ---------------------------------------------------------------------------
# Optional insert position
# ---------------------------------------------------------------------------


def test_add_video_to_playlist_without_position_appends(fake):
    fake.seed("PL1", _raw_list([("v1", "A")]))
    yt.add_video_to_playlist(_conn(), "PL1", "v2")
    body = fake.insert_calls[-1]["body"]
    assert "position" not in body["snippet"]
    assert fake.video_ids_of("PL1") == ["v1", "v2"]


def test_add_video_to_playlist_with_position_inserts(fake):
    fake.seed("PL1", _raw_list([("v1", "A"), ("v2", "B")]))
    yt.add_video_to_playlist(_conn(), "PL1", "v3", position=0)
    body = fake.insert_calls[-1]["body"]
    assert body["snippet"]["position"] == 0
    assert fake.video_ids_of("PL1") == ["v3", "v1", "v2"]


def test_bulk_add_carries_position_to_every_insert(fake):
    fake.seed("PL1", _raw_list([("v0", "Zero")]))
    yt.bulk_add_to_playlist(_conn(), "PL1", ["v1", "v2"], position=1)
    assert len(fake.insert_calls) == 2
    assert all(c["body"]["snippet"]["position"] == 1 for c in fake.insert_calls)


# ---------------------------------------------------------------------------
# Duplicate handling
# ---------------------------------------------------------------------------


def test_bulk_add_skips_existing_duplicates(fake):
    fake.seed("PL1", _raw_list([("v1", "A"), ("v2", "B")]))
    result = yt.bulk_add_to_playlist(_conn(), "PL1", ["v2", "v3"])
    assert result["succeeded"] == 1 and result["skipped"] == 1 and result["failed"] == 0
    assert result["items"] == [
        {"key": "v2", "status": "skipped", "message": "duplicate: already in playlist"},
        {"key": "v3", "status": "succeeded", "message": ""},
    ]
    assert fake.video_ids_of("PL1") == ["v1", "v2", "v3"]


def test_bulk_add_skips_duplicates_within_the_same_batch(fake):
    fake.seed("PL1", _raw_list([("v1", "A")]))
    result = yt.bulk_add_to_playlist(_conn(), "PL1", ["v2", "v2"])
    assert result["succeeded"] == 1 and result["skipped"] == 1
    assert fake.video_ids_of("PL1") == ["v1", "v2"]


def test_bulk_add_with_skip_duplicates_false_adds_duplicates(fake):
    fake.seed("PL1", _raw_list([("v1", "A")]))
    result = yt.bulk_add_to_playlist(_conn(), "PL1", ["v1"], skip_duplicates=False)
    assert result["succeeded"] == 1
    assert len(fake.video_ids_of("PL1")) == 2


def test_copy_playlist_items_skips_duplicates_in_target(fake):
    fake.seed("PL1", _raw_list([("v1", "A"), ("v2", "B"), ("v3", "C")]))
    fake.seed("PL2", _raw_list([("v2", "B")]))
    result = yt.copy_playlist_items(_conn(), "PL1", "PL2")
    assert result["succeeded"] == 2 and result["skipped"] == 1
    assert set(fake.video_ids_of("PL2")) == {"v1", "v2", "v3"}


def test_copy_playlist_items_video_ids_filter(fake):
    fake.seed("PL1", _raw_list([("v1", "A"), ("v2", "B")]))
    fake.seed("PL2", [])
    result = yt.copy_playlist_items(_conn(), "PL1", "PL2", video_ids=["v2"])
    assert result["succeeded"] == 1 and result["requested"] == 1
    assert fake.video_ids_of("PL2") == ["v2"]


# ---------------------------------------------------------------------------
# Partial-failure continuation
# ---------------------------------------------------------------------------


def test_bulk_add_partial_failure_continues(fake):
    fake.seed("PL1", _raw_list([("v1", "A")]))
    fake.fail_insert.add("v3")
    result = yt.bulk_add_to_playlist(_conn(), "PL1", ["v2", "v3", "v4"])
    assert result["succeeded"] == 2 and result["failed"] == 1
    assert fake.video_ids_of("PL1") == ["v1", "v2", "v4"]


def test_bulk_remove_partial_failure_continues(fake):
    fake.seed("PL1", _raw_list([("v1", "A"), ("v2", "B"), ("v3", "C")]))
    fake.fail_delete.add("PI2")
    result = yt.bulk_remove_from_playlist(_conn(), "PL1", video_ids=["v1", "v2", "v3"])
    assert result["succeeded"] == 2 and result["failed"] == 1
    assert fake.video_ids_of("PL1") == ["v2"]


def test_bulk_remove_by_playlist_item_ids(fake):
    fake.seed("PL1", _raw_list([("v1", "A"), ("v2", "B"), ("v3", "C")]))
    result = yt.bulk_remove_from_playlist(_conn(), "PL1", playlist_item_ids=["PI1", "PI3"])
    assert result["succeeded"] == 2 and result["failed"] == 0
    assert fake.video_ids_of("PL1") == ["v2"]


def test_bulk_remove_skips_videos_not_in_playlist(fake):
    fake.seed("PL1", _raw_list([("v1", "A")]))
    result = yt.bulk_remove_from_playlist(_conn(), "PL1", video_ids=["v1", "missing"])
    assert result["succeeded"] == 1 and result["skipped"] == 1
    by_key = {i["key"]: i["status"] for i in result["items"]}
    assert by_key == {"v1": "succeeded", "missing": "skipped"}


def test_copy_partial_failure_continues(fake):
    fake.seed("PL1", _raw_list([("v1", "A"), ("v2", "B"), ("v3", "C")]))
    fake.seed("PL2", [])
    fake.fail_insert.add("v2")
    result = yt.copy_playlist_items(_conn(), "PL1", "PL2")
    assert result["succeeded"] == 2 and result["failed"] == 1
    assert set(fake.video_ids_of("PL2")) == {"v1", "v3"}


def test_bulk_ops_with_empty_inputs_do_not_touch_api(fake):
    empty = {"requested": 0, "succeeded": 0, "skipped": 0, "failed": 0, "items": []}
    assert yt.bulk_add_to_playlist(_conn(), "PL1", []) == empty
    assert yt.bulk_remove_from_playlist(_conn(), "PL1") == empty
    assert fake.insert_calls == [] and fake.delete_calls == []


# ---------------------------------------------------------------------------
# Move: ordering, duplicate retention, source-removal failure
# ---------------------------------------------------------------------------


def test_move_playlist_items_adds_before_removes(fake):
    fake.seed("PL1", _raw_list([("v1", "A"), ("v2", "B")]))
    fake.seed("PL2", [])
    result = yt.move_playlist_items(_conn(), "PL1", "PL2")
    assert result["succeeded"] == 2 and result["failed"] == 0
    assert fake.video_ids_of("PL1") == []
    assert set(fake.video_ids_of("PL2")) == {"v1", "v2"}

    # each moved video is inserted before its own source item is deleted
    ops = [op for op in fake.log if op[0] in ("insert", "delete")]
    assert ops == [("insert", "v1"), ("delete", "PI1"), ("insert", "v2"), ("delete", "PI2")]


def test_move_duplicate_in_target_retains_source(fake):
    fake.seed("PL1", _raw_list([("v1", "A"), ("v2", "B")]))
    fake.seed("PL2", _raw_list([("v2", "B")]))
    result = yt.move_playlist_items(_conn(), "PL1", "PL2")
    assert result["skipped"] == 1 and result["succeeded"] == 1
    assert fake.video_ids_of("PL1") == ["v2"]
    assert set(fake.video_ids_of("PL2")) == {"v2", "v1"}
    assert fake.delete_calls == [{"id": "PI1"}]


def test_move_add_failure_retains_source(fake):
    fake.seed("PL1", _raw_list([("v1", "A"), ("v2", "B")]))
    fake.seed("PL2", [])
    fake.fail_insert.add("v2")
    result = yt.move_playlist_items(_conn(), "PL1", "PL2")
    assert result["succeeded"] == 1 and result["failed"] == 1
    # the failed add never triggers a source delete
    assert fake.delete_calls == [{"id": "PI1"}]
    assert fake.video_ids_of("PL1") == ["v2"]
    assert fake.video_ids_of("PL2") == ["v1"]
    assert "source retained" in result["items"][1]["message"]


def test_move_source_removal_failure_still_counts_as_succeeded(fake):
    fake.seed("PL1", _raw_list([("v1", "A")]))
    fake.seed("PL2", [])
    fake.fail_delete.add("PI1")
    result = yt.move_playlist_items(_conn(), "PL1", "PL2")
    assert result["succeeded"] == 1 and result["failed"] == 0
    assert "source removal failed" in result["items"][0]["message"]
    # the add happened but the source item is retained
    assert len(fake.insert_calls) == 1
    assert fake.video_ids_of("PL1") == ["v1"]
    assert fake.video_ids_of("PL2") == ["v1"]


# ---------------------------------------------------------------------------
# Page boundary helpers and reorder_playlist_page
# ---------------------------------------------------------------------------


def test_playlist_page_range_boundaries():
    assert yt.playlist_page_range(0) == (0, 50)
    assert yt.playlist_page_range(49) == (0, 50)
    assert yt.playlist_page_range(50) == (50, 100)
    assert yt.playlist_page_range(149) == (100, 150)
    assert yt.playlist_page_range(0, page_size=25) == (0, 25)
    assert yt.playlist_page_range(25, page_size=25) == (25, 50)


def test_playlist_page_for():
    assert yt.playlist_page_for(0) == 0
    assert yt.playlist_page_for(49) == 0
    assert yt.playlist_page_for(50) == 1
    assert yt.playlist_page_for(149) == 2
    assert yt.playlist_page_for(0, page_size=25) == 0
    assert yt.playlist_page_for(25, page_size=25) == 1


def test_reorder_playlist_page_reorders_within_span(fake):
    fake.seed("PL1", _raw_list([("v1", "A"), ("v2", "B"), ("v3", "C"), ("v4", "D")]))
    result = yt.reorder_playlist_page(_conn(), "PL1", page_index=0,
                                      order=["v4", "v3", "v2", "v1"], page_size=4)
    assert result["succeeded"] == 4 and result["failed"] == 0
    assert fake.video_ids_of("PL1") == ["v4", "v3", "v2", "v1"]
    # every target position stayed inside the page's span
    for c in fake.update_calls:
        assert 0 <= c["body"]["snippet"]["position"] < 4


def test_reorder_playlist_page_targets_absolute_positions(fake):
    fake.seed("PL1", _raw_list(
        [("v1", "A"), ("v2", "B"), ("v3", "C"), ("v4", "D"), ("v5", "E"), ("v6", "F")]
    ))
    result = yt.reorder_playlist_page(_conn(), "PL1", page_index=1,
                                      order=["v6", "v5", "v4"], page_size=3)
    assert result["succeeded"] >= 1 and result["failed"] == 0
    # page-1 target positions are absolute [3, 6), never relative to the page
    for c in fake.update_calls:
        assert 3 <= c["body"]["snippet"]["position"] < 6
    positions = {raw["snippet"]["resourceId"]["videoId"]: raw["snippet"]["position"]
                 for raw in fake.items_of("PL1")}
    assert (positions["v4"], positions["v5"], positions["v6"]) == (5, 4, 3)


def test_reorder_playlist_page_fails_cleanly_when_items_missing(fake):
    fake.seed("PL1", _raw_list([("v1", "A"), ("v2", "B")]))
    result = yt.reorder_playlist_page(_conn(), "PL1", page_index=0,
                                      order=["v1", "v2", "v3"], page_size=3)
    assert result["failed"] == 3
    assert fake.update_calls == []
    assert {i["status"] for i in result["items"]} == {"failed"}


def test_reorder_playlist_page_skips_already_correct(fake):
    fake.seed("PL1", _raw_list([("v1", "A"), ("v2", "B")]))
    result = yt.reorder_playlist_page(_conn(), "PL1", page_index=0,
                                      order=["v1", "v2"], page_size=2)
    assert result["skipped"] == 2 and result["succeeded"] == 0
    assert fake.update_calls == []


def test_reorder_playlist_page_partial_failure_continues(fake):
    fake.seed("PL1", _raw_list([("v1", "A"), ("v2", "B"), ("v3", "C")]))
    fake.fail_update.add("PI2")
    result = yt.reorder_playlist_page(_conn(), "PL1", page_index=0,
                                      order=["v3", "v1", "v2"], page_size=3)
    assert result["succeeded"] == 2 and result["failed"] == 1


# ---------------------------------------------------------------------------
# Batch result bookkeeping
# ---------------------------------------------------------------------------


def test_batch_result_shape_and_counters():
    result = yt._new_batch_result()
    assert result == {"requested": 0, "succeeded": 0, "skipped": 0, "failed": 0, "items": []}
    yt._batch_add(result, "a", "succeeded")
    yt._batch_add(result, "b", "skipped", "why")
    yt._batch_add(result, "c", "failed", "boom")
    assert result["requested"] == 3
    assert result["succeeded"] == 1 and result["skipped"] == 1 and result["failed"] == 1
    assert result["items"][1] == {"key": "b", "status": "skipped", "message": "why"}
    with pytest.raises(ValueError):
        yt._batch_add(result, "d", "bogus")



