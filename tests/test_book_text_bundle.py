"""Text-only book bundle: export to another machine and merge it back."""
from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

from app import db, repository
from app.book_text_bundle import (
    BUNDLE_FORMAT,
    bundle_filename,
    export_book_text,
    import_book_text,
    machine_id,
    validate_bundle,
)
from app.config import settings
from app.epub_parser import ParsedChapter
from app.main import app


def _conn():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    return conn


def _seed(conn, title: str = "Truyện thử") -> int:
    chapters = [
        ParsedChapter(title=f"Chương {i + 1}: Tên {i + 1}", text=f"Nội dung chương {i + 1}. " * 4)
        for i in range(4)
    ]
    book = repository.create_book(
        conn,
        title=title,
        original_filename="truyen.epub",
        epub_path="",
        patch_size=2,
        chapters=chapters,
        background_image_path=None,
        author="Tác giả",
        description="Mô tả",
        language="vi",
        publisher="NXB",
        subjects="kiếm hiệp",
    )
    repository.rebuild_patches(conn, book.id, [(0, 1), (2, 3)])
    repository.create_replace_rule(conn, book.id, find="Nội dung", replace="Đoạn", is_regex=False, position=0)
    return book.id


def _patches(conn, book_id: int):
    return {p.patch_index: p for p in repository.list_patches(conn, book_id)}


def test_export_contains_only_text_layers():
    conn = _conn()
    book_id = _seed(conn)
    bundle = export_book_text(conn, book_id)

    assert bundle["format"] == BUNDLE_FORMAT
    assert bundle["version"] == 1
    assert bundle["bundle_id"]
    assert bundle["book"]["title"] == "Truyện thử"
    assert len(bundle["chapters"]) == 4
    assert [row["chapter_index"] for row in bundle["toc"]] == [0, 1, 2, 3]
    assert [row["title"] for row in bundle["toc"]] == [c["title"] for c in bundle["chapters"]]
    assert bundle["stats"]["chapter_count"] == 4
    assert bundle["stats"]["excluded_chapter_count"] == 0
    assert bundle["stats"]["empty_chapter_count"] == 0
    assert bundle["stats"]["first_chapter_no"] == 1
    assert bundle["stats"]["last_chapter_no"] == 4
    assert len(bundle["patches"]) == 2
    assert bundle["replace_rules"][0]["find"] == "Nội dung"
    # No media or filesystem paths anywhere in the payload.
    serialized = str(bundle)
    assert "audio_path" not in serialized
    assert "final_audio_path" not in serialized
    assert "epub_path" not in serialized


def test_toc_keeps_excluded_and_empty_chapters():
    conn = _conn()
    book_id = _seed(conn)
    conn.execute(
        "UPDATE chapter SET is_excluded = 1, text = '', char_count = 0 "
        "WHERE book_id = ? AND chapter_index = 1",
        (book_id,),
    )
    conn.commit()

    bundle = export_book_text(conn, book_id)
    assert [row["chapter_index"] for row in bundle["toc"]] == [0, 1, 2, 3]
    excluded = bundle["toc"][1]
    assert excluded["is_excluded"] is True
    assert excluded["has_text"] is False
    assert bundle["stats"]["excluded_chapter_count"] == 1
    assert bundle["stats"]["empty_chapter_count"] == 1
    # The full text list still carries the row so an import can rebuild it.
    assert bundle["chapters"][1]["text"] == ""


def test_bundle_survives_validation_round_trip():
    conn = _conn()
    book_id = _seed(conn)
    bundle = export_book_text(conn, book_id)
    assert validate_bundle(copy.deepcopy(bundle)) is not None


def test_validate_rejects_foreign_or_incomplete_files():
    with pytest.raises(ValueError, match="không phải file text"):
        validate_bundle({"format": "something-else", "version": 1})
    with pytest.raises(ValueError, match="thiếu phần thông tin sách"):
        validate_bundle({"format": BUNDLE_FORMAT, "version": 1, "chapters": []})
    with pytest.raises(ValueError, match="không có chương"):
        validate_bundle({"format": BUNDLE_FORMAT, "version": 1, "book": {}})
    with pytest.raises(ValueError, match="phiên bản"):
        validate_bundle({"format": BUNDLE_FORMAT, "version": 999, "book": {}, "chapters": []})


def test_validate_requires_full_toc_when_present():
    conn = _conn()
    bundle = export_book_text(conn, _seed(conn))

    incomplete = copy.deepcopy(bundle)
    incomplete["toc"] = incomplete["toc"][:-1]
    with pytest.raises(ValueError, match="Mục lục thiếu chương"):
        validate_bundle(incomplete)

    duplicated = copy.deepcopy(bundle)
    duplicated["toc"][1]["chapter_index"] = duplicated["toc"][0]["chapter_index"]
    with pytest.raises(ValueError, match="trùng trong mục lục"):
        validate_bundle(duplicated)

    unknown = copy.deepcopy(bundle)
    unknown["toc"][0]["chapter_index"] = 99
    with pytest.raises(ValueError, match="không có trong file"):
        validate_bundle(unknown)

    wrong_type = copy.deepcopy(bundle)
    wrong_type["toc"] = "Chương 1"
    with pytest.raises(ValueError, match="Danh mục chương không hợp lệ"):
        validate_bundle(wrong_type)


def test_import_creates_book_on_another_machine():
    source = _conn()
    book_id = _seed(source)
    bundle = export_book_text(source, book_id)

    target = _conn()
    summary = import_book_text(target, bundle)
    assert summary["created"] is True
    created_id = summary["book_id"]

    book = repository.get_book(target, created_id)
    assert book is not None
    assert book.title == "Truyện thử"
    assert len(repository.list_chapters(target, created_id)) == 4
    patches = _patches(target, created_id)
    assert sorted(patches) == [0, 1]
    assert all(p.status == "pending" for p in patches.values())
    assert repository.list_replace_rules(target, created_id)[0].find == "Nội dung"


def test_import_round_trip_merges_into_same_book():
    origin = _conn()
    book_id = _seed(origin)
    original = export_book_text(origin, book_id)

    other = _conn()
    imported = import_book_text(other, original)
    assert imported["created"] is True

    # Edit the text on the other machine, then copy the file back.
    chapter_id = other.execute(
        "SELECT id FROM chapter WHERE book_id=? AND chapter_index=2", (imported["book_id"],)
    ).fetchone()["id"]
    other.execute("UPDATE chapter SET text='Bản sửa trên máy khác.' WHERE id=?", (chapter_id,))
    other.commit()
    edited = export_book_text(other, imported["book_id"])
    assert edited["bundle_id"] == original["bundle_id"]

    back = import_book_text(origin, edited)
    assert back["created"] is False
    assert back["book_id"] == book_id
    assert back["chapters"]["updated"] == 1
    assert origin.execute("SELECT COUNT(*) AS n FROM book").fetchone()["n"] == 1
    merged_text = origin.execute(
        "SELECT text FROM chapter WHERE book_id=? AND chapter_index=2", (book_id,)
    ).fetchone()["text"]
    assert merged_text == "Bản sửa trên máy khác."


def test_export_carries_explicit_book_and_machine_ids():
    conn = _conn()
    book_id = _seed(conn)
    bundle = export_book_text(conn, book_id)
    local = machine_id(conn)

    assert bundle["book"]["id"] == book_id
    assert bundle["source"] == {"machine_id": local, "book_id": book_id, "title": "Truyện thử"}
    assert bundle["book_ids"] == {local: book_id}
    assert bundle_filename(repository.get_book(conn, book_id)) == f"book-{book_id}-Truy-n-th.book-text.json"
    # Stable across exports.
    assert export_book_text(conn, book_id)["source"]["machine_id"] == local


def test_round_trip_uses_book_ids_when_mapping_lost_and_title_changed():
    origin = _conn()
    _seed(origin, title="Sách khác")  # shift ids so local ids differ between machines
    book_id = _seed(origin)
    original = export_book_text(origin, book_id)

    other = _conn()
    imported = import_book_text(other, original)
    assert imported["identity"]["source_book_id"] == book_id
    other_id = imported["book_id"]
    assert other_id != book_id

    other.execute("UPDATE book SET title='Tên mới' WHERE id=?", (other_id,))
    other.commit()
    edited = export_book_text(other, other_id)
    assert edited["source"]["book_id"] == other_id
    assert edited["book_ids"] == {machine_id(origin): book_id, machine_id(other): other_id}

    # Origin lost every bundle mapping (e.g. restored an old DB backup).
    origin.execute("DELETE FROM app_state WHERE key LIKE 'book_text_bundle:book:%'")
    origin.execute("DELETE FROM app_state WHERE key LIKE 'book_text_bundle:id:%'")
    origin.commit()

    back = import_book_text(origin, edited)
    assert back["created"] is False
    assert back["book_id"] == book_id
    assert back["matched_by"] == "book_id"
    assert repository.get_book(origin, book_id).title == "Tên mới"
    assert origin.execute("SELECT COUNT(*) AS n FROM book").fetchone()["n"] == 2

    # Copying again from the origin still lands in the same book on the other machine.
    again = import_book_text(other, export_book_text(origin, book_id))
    assert again["book_id"] == other_id
    assert again["matched_by"] == "book_id"


def test_book_id_is_not_trusted_for_an_unrelated_book():
    origin = _conn()
    book_id = _seed(origin)
    bundle = export_book_text(origin, book_id)

    other = _conn()
    unrelated = _seed(other, title="Không liên quan")
    other.execute("UPDATE book SET created_at='2000-01-01T00:00:00+00:00' WHERE id=?", (unrelated,))
    other.commit()
    forged = copy.deepcopy(bundle)
    forged["book_ids"][machine_id(other)] = unrelated

    summary = import_book_text(other, forged)
    assert summary["created"] is True
    assert summary["book_id"] != unrelated


def test_pinned_target_warns_when_file_belongs_to_another_book():
    conn = _conn()
    first = _seed(conn, title="Sách A")
    second = _seed(conn, title="Sách B")
    bundle = export_book_text(conn, first)

    summary = import_book_text(conn, bundle, target_book_id=second, dry_run=True)
    assert summary["book_id"] == second
    assert summary["matched_by"] == "target"
    assert any(f"#{first}" in warning for warning in summary["warnings"])


def test_merge_keeps_audio_and_resets_only_edited_patch():
    origin = _conn()
    book_id = _seed(origin)
    bundle = export_book_text(origin, book_id)
    patches = _patches(origin, book_id)
    origin.execute(
        "UPDATE patch SET status='done', audio_path='/tmp/patch0.wav' WHERE id=?",
        (patches[0].id,),
    )
    origin.execute(
        "UPDATE patch SET status='done', audio_path='/tmp/patch1.wav' WHERE id=?",
        (patches[1].id,),
    )
    origin.commit()

    unchanged = import_book_text(origin, bundle)
    assert unchanged["text_changed"] == []
    kept = _patches(origin, book_id)[0]
    assert kept.status == "done"
    assert kept.audio_path == "/tmp/patch0.wav"

    edited = copy.deepcopy(bundle)
    edited["chapters"][0]["text"] = "Nội dung đã sửa hoàn toàn."
    summary = import_book_text(origin, edited)
    assert summary["text_changed"] == [0]
    refreshed = _patches(origin, book_id)[0]
    assert refreshed.status == "pending"
    assert refreshed.next_chunk_index == 0
    assert refreshed.audio_path == "/tmp/patch0.wav"  # file kept for regeneration
    # The untouched patch keeps its done status.
    assert _patches(origin, book_id)[1].status == "done"


def test_import_new_mode_does_not_steal_bundle_identity():
    origin = _conn()
    book_id = _seed(origin)
    bundle = export_book_text(origin, book_id)

    first = import_book_text(origin, bundle, mode="new")
    second = import_book_text(origin, bundle, mode="new")
    assert first["created"] is True and second["created"] is True
    assert first["book_id"] != second["book_id"] != book_id
    assert origin.execute("SELECT COUNT(*) AS n FROM book").fetchone()["n"] == 3

    # A later merge still resolves to the original book, not a fork.
    merged = import_book_text(origin, bundle)
    assert merged["book_id"] == book_id


def test_dry_run_does_not_write():
    origin = _conn()
    book_id = _seed(origin)
    bundle = export_book_text(origin, book_id)
    bundle["chapters"][0]["text"] = "Chỉ xem trước."

    summary = import_book_text(origin, bundle, dry_run=True)
    assert summary["dry_run"] is True
    assert repository.get_chapter_text(origin, book_id, 0) != "Chỉ xem trước."
    assert origin.execute("SELECT COUNT(*) AS n FROM book").fetchone()["n"] == 1


def test_failed_import_leaves_no_partial_writes():
    origin = _conn()
    book_id = _seed(origin)
    bundle = export_book_text(origin, book_id)
    bundle["book"]["title"] = "Ghi dở"
    bundle["chapters"][0]["text"] = "Ghi dở"
    bundle["replace_rules"][0]["position"] = "not-a-number"

    with pytest.raises(ValueError):
        import_book_text(origin, bundle)

    assert origin.in_transaction is False
    origin.commit()  # A later unrelated commit must not persist anything.
    assert repository.get_book(origin, book_id).title == "Truyện thử"
    assert repository.get_chapter_text(origin, book_id, 0) != "Ghi dở"


def test_import_accepts_numeric_string_chapter_indexes():
    origin = _conn()
    book_id = _seed(origin)
    bundle = export_book_text(origin, book_id)
    for row in bundle["chapters"] + bundle["toc"]:
        row["chapter_index"] = str(row["chapter_index"])

    summary = import_book_text(origin, bundle)
    assert summary["book_id"] == book_id
    assert summary["text_changed"] == []


def test_excluding_a_chapter_resets_only_the_patch_containing_it():
    origin = _conn()
    book_id = _seed(origin)
    bundle = export_book_text(origin, book_id)
    origin.execute("UPDATE patch SET status='done' WHERE book_id=?", (book_id,))
    origin.commit()

    bundle["chapters"][0]["is_excluded"] = True
    summary = import_book_text(origin, bundle)
    assert summary["text_changed"] == [0]
    patches = _patches(origin, book_id)
    assert patches[0].status == "pending"
    assert patches[1].status == "done"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    monkeypatch.setattr(settings, "enable_worker", False)
    with TestClient(app) as c:
        yield c


def _seed_via_client(client) -> int:
    return _seed(client.app.state.conn)


def test_download_text_bundle_route(client):
    book_id = _seed_via_client(client)
    resp = client.get(f"/books/{book_id}/text-bundle")
    assert resp.status_code == 200
    assert resp.headers["content-disposition"].startswith("attachment")
    data = resp.json()
    assert data["format"] == BUNDLE_FORMAT
    assert data["stats"]["chapter_count"] == 4


def test_download_rejects_book_without_chapters(client):
    conn = client.app.state.conn
    now = "2026-01-01T00:00:00+00:00"
    cur = conn.execute(
        """INSERT INTO book (title, original_filename, epub_path, patch_size, status, created_at, updated_at)
           VALUES ('Rỗng','', '', 10, 'ready', ?, ?)""",
        (now, now),
    )
    conn.commit()
    resp = client.get(f"/books/{cur.lastrowid}/text-bundle")
    assert resp.status_code == 400


def test_import_route_dry_run_then_apply(client):
    book_id = _seed_via_client(client)
    exported = client.get(f"/books/{book_id}/text-bundle").content

    preview = client.post(
        "/text-bundles/import",
        files={"file": ("book.book-text.json", exported, "application/json")},
        data={"mode": "merge", "target_book_id": str(book_id), "dry_run": "true"},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["dry_run"] is True
    assert preview.json()["created"] is False

    applied = client.post(
        "/text-bundles/import",
        files={"file": ("book.book-text.json", exported, "application/json")},
        data={"mode": "merge", "target_book_id": str(book_id)},
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["book_id"] == book_id


def test_import_route_rejects_bad_json(client):
    resp = client.post(
        "/text-bundles/import",
        files={"file": ("book.json", b"not json", "application/json")},
        data={"mode": "merge"},
    )
    assert resp.status_code == 400


def test_import_route_rejects_unknown_mode(client):
    book_id = _seed_via_client(client)
    exported = client.get(f"/books/{book_id}/text-bundle").content
    resp = client.post(
        "/text-bundles/import",
        files={"file": ("book.book-text.json", exported, "application/json")},
        data={"mode": "replace"},
    )
    assert resp.status_code == 400
