"""Phase 1 acceptance check: parse an epub and print chapter stats."""
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.epub_parser import parse_epub


def main(epub_path: str) -> None:
    chapters = parse_epub(epub_path)
    print(f"Total chapters: {len(chapters)}")
    total_chars = 0
    for i, ch in enumerate(chapters):
        total_chars += ch.char_count
        preview = ch.text[:100].replace("\n", " ")
        print(f"[{i}] {ch.title!r} ({ch.char_count} chars): {preview}...")
    print(f"\nTotal chars across book: {total_chars}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python scripts/test_epub_parse.py <path-to-epub>")
        sys.exit(1)
    main(sys.argv[1])
