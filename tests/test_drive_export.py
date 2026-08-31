from __future__ import annotations

import json
import sqlite3

import pytest

from app import db, drive_export, repository


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    db.init_schema(connection)
    now = "2026-01-01T00:00:00+00:00"
    book_id = connection.execute(
        "INSERT INTO book (title, original_filename, epub_path, patch_size, status, created_at, updated_at) "
        "VALUES ('Book', 'book.epub', 'book.epub', 10, 'ready', ?, ?)",
        (now, now),
    ).lastrowid
    connection.execute(
        "INSERT INTO chapter (book_id, chapter_index, title, text, char_count) VALUES (?, 0, 'One', ?, ?)",
        (book_id, "Alpha one. Alpha two. Alpha three.", 34),
    )
    connection.execute(
        "INSERT INTO chapter (book_id, chapter_index, title, text, char_count) VALUES (?, 1, 'Two', ?, ?)",
        (book_id, "Beta one. Beta two.", 19),
    )
    connection.execute(
        "INSERT INTO chapter (book_id, chapter_index, title, text, char_count, is_excluded) VALUES (?, 2, 'Excluded', 'Never export.', 13, 1)",
        (book_id,),
    )
    connection.execute(
        "INSERT INTO chapter (book_id, chapter_index, title, text, char_count) VALUES (?, 3, 'Whitespace', '   ', 3)",
        (book_id,),
    )
    connection.execute(
        "INSERT INTO chapter (book_id, chapter_index, title, text, char_count) VALUES (?, 4, 'Punctuation', '...!!!', 6)",
        (book_id,),
    )
    patch_id = connection.execute(
        "INSERT INTO patch (book_id, patch_index, chapter_start, chapter_end, status, max_chars, created_at, updated_at) "
        "VALUES (?, 0, 0, 4, 'pending', 18, ?, ?)",
        (book_id, now, now),
    ).lastrowid
    connection.commit()
    yield connection, repository.get_book(connection, book_id), repository.get_patch(connection, patch_id)
    connection.close()


def test_write_patch_files_exports_chunk_metadata_at_chapter_boundaries(conn, tmp_path):
    connection, book, patch = conn
    manifest = drive_export._write_patch_files(connection, book, patch, tmp_path, None)

    metadata = manifest["chunk_metadata"]
    assert all(
        list(item.keys()) == ["chapter_index", "is_chapter_start", "chapter_title", "text"]
        for item in metadata
    )
    assert "chunks" not in manifest
    assert "expected_outputs" not in manifest
    assert manifest["chapter_titles"] == {"0": "One", "1": "Two"}
    assert [item["chapter_index"] for item in metadata] == [0, 0, 0, 1, 1]
    assert [item["is_chapter_start"] for item in metadata] == [True, False, False, True, False]
    # Chunk mở đầu mỗi chương phải đọc tiêu đề chương trước phần thân — thân chương
    # trong EPUB không chứa sẵn tiêu đề, nên nếu không ghép ở đây thì manifest (và
    # audio dựng từ nó) mất hẳn tên chương.
    assert [item["text"] for item in metadata] == [
        "One. Alpha one.",
        "Alpha two.",
        "Alpha three.",
        "Two. Beta one.",
        "Beta two.",
    ]
    assert all("Never export" not in item["text"] for item in metadata)
    assert [manifest[key] for key in ("patch_id", "book_id", "book_title", "patch_name", "chapter_start", "chapter_end", "max_chars", "chunk_count", "reference_wav", "reference_transcript", "voxcpm_model_id")] == [
        patch.id, patch.book_id, book.title, str(patch.patch_index), 0, 4, 18, 5, None, None, "openbmb/VoxCPM2"
    ]
    saved_manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert saved_manifest == manifest


def test_write_patch_files_rejects_empty_plan(conn, tmp_path):
    connection, book, patch = conn
    connection.execute("UPDATE patch SET chapter_start = 2, chapter_end = 4 WHERE id = ?", (patch.id,))
    connection.commit()
    patch = repository.get_patch(connection, patch.id)
    with pytest.raises(ValueError, match=f"patch {patch.id} has no text to export"):
        drive_export._write_patch_files(connection, book, patch, tmp_path, None)


def test_write_patch_files_writes_the_manifest_and_nothing_else(conn, tmp_path):
    """A patch folder is one file. Chunk text travels inside the manifest, and the
    background image stays in the app - both keep the Drive sync small."""
    connection, book, patch = conn
    dest = tmp_path / "patch"
    manifest = drive_export._write_patch_files(connection, book, patch, dest, "reference.wav")

    assert [p.name for p in dest.iterdir()] == ["manifest.json"]
    assert "background_image" not in manifest

    plan = repository.build_patch_chunk_plan(connection, patch)
    metadata = manifest["chunk_metadata"]
    assert len(metadata) == len(plan)
    assert "chunks" not in manifest
    assert "expected_outputs" not in manifest
    assert manifest["chunk_count"] == len(plan)

    for entry, item in zip(metadata, plan):
        assert set(entry) == {
            "chapter_index", "is_chapter_start", "chapter_title", "text",
        }
        assert entry["text"] == item["text"]
        assert entry["text"].strip()


def test_exported_manifest_satisfies_the_notebook_and_the_importer(conn, tmp_path):
    """The compact manifest has two independent readers - Cell 8 in the notebook and the
    app's Drive import - and each rebuilds the fields it leaves out. Nothing else pins
    those two rebuilds to what the exporter actually writes, so run one real export
    through both."""
    from test_notebook_templates import _cell8_helpers

    from app.routes.patches import _timeline_metadata

    connection, book, patch = conn
    dest = tmp_path / "patch"
    drive_export._write_patch_files(connection, book, patch, dest, None)
    exported = (dest / "manifest.json").read_text(encoding="utf-8")

    # Notebook side: rebuild the omitted fields, then run its own validator over them.
    helpers = _cell8_helpers()
    manifest = json.loads(exported)
    assert helpers["normalize_chunk_manifest"](manifest) is True
    assert helpers["validate_chunk_metadata"](manifest["chunk_metadata"], manifest["chunks"]) is not None
    assert [helpers["chunk_text_for"](manifest, i, str(dest)) for i in range(manifest["chunk_count"])] == [
        item["text"] for item in json.loads(exported)["chunk_metadata"]
    ]

    # Import side: titles come back out of the chapter_titles map, in chunk order.
    imported = _timeline_metadata(json.loads(exported))
    assert all(set(item) == {"chapter_index", "chapter_title", "is_chapter_start"} for item in imported)
    assert [item["chapter_title"] for item in imported] == ["One", "One", "One", "Two", "Two"]


