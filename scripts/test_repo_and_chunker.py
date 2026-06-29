"""Phase 2 acceptance check: parse epub, insert into a temp SQLite db, print patch/chunk stats."""
import io
import sys
import tempfile
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, repository
from app.chunker import split_into_tts_chunks
from app.epub_parser import parse_epub


def main(epub_path: str) -> None:
    chapters = parse_epub(epub_path)
    print(f"Parsed {len(chapters)} chapters")

    db_path = tempfile.mktemp(suffix=".db")
    conn = db.connect(db_path)
    db.init_schema(conn)

    book = repository.create_book(
        conn,
        title="Test Book",
        original_filename=Path(epub_path).name,
        epub_path=epub_path,
        patch_size=10,
        chapters=chapters,
        background_image_path=None,
    )
    print(f"Book id={book.id} status={book.status}")

    repository.auto_build_patches(conn, book.id, start_chapter=0)
    patches = repository.list_patches(conn, book.id)
    print(f"Generated {len(patches)} patches")
    for p in patches[:3]:
        print(f"  patch {p.patch_index}: chapters [{p.chapter_start}, {p.chapter_end}] status={p.status}")

    first_patch = patches[0]
    chs = repository.get_chapters_in_range(conn, book.id, first_patch.chapter_start, first_patch.chapter_end)
    patch_text = "\n\n".join(c.text for c in chs)
    chunks = split_into_tts_chunks(patch_text, max_chars=400)
    print(f"\nPatch 0 text length: {len(patch_text)} chars -> {len(chunks)} TTS chunks")
    lengths = [len(c) for c in chunks]
    print(f"  chunk length min/max/avg: {min(lengths)}/{max(lengths)}/{sum(lengths)//len(lengths)}")
    assert all(0 < l for l in lengths), "found an empty chunk"
    over = [l for l in lengths if l > 400]
    print(f"  chunks over max_chars (sentence too long, expected rare): {len(over)}")

    print("\nClaiming next pending patch...")
    claimed = repository.claim_next_pending_patch(conn)
    print(f"  claimed patch_index={claimed.patch_index} status={claimed.status}")

    print("Simulating crash: leaving it in 'processing', then requeueing on restart...")
    n = repository.requeue_stuck_processing(conn)
    print(f"  requeued {n} patch(es)")
    again = repository.get_patch(conn, claimed.id)
    print(f"  patch status now: {again.status}")

    conn.close()
    Path(db_path).unlink(missing_ok=True)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python scripts/test_repo_and_chunker.py <path-to-epub>")
        sys.exit(1)
    main(sys.argv[1])
