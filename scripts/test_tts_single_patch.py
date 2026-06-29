"""Phase 3 acceptance check.

By default runs against a STUB engine (no GPU/model download) to validate the chunk -> synth ->
concat wiring. Pass --real to use the actual VoxCPM2 model (requires `pip install voxcpm`,
torch+CUDA installed first, and ideally >=8GB VRAM - this dev machine only has 4GB, so --real
may OOM or fall back to slow CPU inference).
"""
import io
import sys
import time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from app import db, repository
from app.epub_parser import parse_epub


class StubEngine:
    """Generates silence instead of real speech - validates the pipeline wiring only."""

    sample_rate = 48000

    def synthesize_chunk(self, text: str) -> np.ndarray:
        duration_s = max(0.2, len(text) / 15)  # rough chars-per-second guess, just for timing realism
        return np.zeros(int(duration_s * self.sample_rate), dtype=np.float32)

    def synthesize_patch(self, text: str, max_chars: int = 400) -> list[np.ndarray]:
        from app.chunker import split_into_tts_chunks

        return [self.synthesize_chunk(c) for c in split_into_tts_chunks(text, max_chars)]


def main(epub_path: str, use_real: bool) -> None:
    chapters = parse_epub(epub_path)

    db_path = "/tmp/test_tts_phase3.db"
    Path(db_path).unlink(missing_ok=True)
    conn = db.connect(db_path)
    db.init_schema(conn)
    book = repository.create_book(
        conn, title="t", original_filename="t.epub", epub_path=epub_path,
        patch_size=10, chapters=chapters, background_image_path=None,
    )
    repository.auto_build_patches(conn, book.id, start_chapter=0)
    patches = repository.list_patches(conn, book.id)
    first = patches[0]
    chs = repository.get_chapters_in_range(conn, book.id, first.chapter_start, first.chapter_end)
    patch_text = "\n\n".join(c.text for c in chs)

    if use_real:
        from app.tts_engine import VoxCPMEngine

        engine = VoxCPMEngine()
        print("Loading real VoxCPM2 model (this downloads weights on first run)...")
    else:
        engine = StubEngine()
        print("Using StubEngine (silence) - pipeline wiring check only, no real audio")

    start = time.time()
    wavs = engine.synthesize_patch(patch_text, max_chars=400)
    elapsed = time.time() - start

    total_samples = sum(w.size for w in wavs)
    audio_duration = total_samples / engine.sample_rate
    rtf = elapsed / audio_duration if audio_duration else float("nan")
    print(f"Synthesized {len(wavs)} chunks, total audio duration: {audio_duration:.1f}s")
    print(f"Wall time: {elapsed:.1f}s, RTF (wall/audio): {rtf:.3f}")

    import soundfile as sf

    out_path = "/tmp/test_patch0.wav"
    sf.write(out_path, np.concatenate(wavs), engine.sample_rate)
    print(f"Wrote {out_path}")

    conn.close()


if __name__ == "__main__":
    args = sys.argv[1:]
    use_real = "--real" in args
    args = [a for a in args if a != "--real"]
    if len(args) != 1:
        print("Usage: python scripts/test_tts_single_patch.py <path-to-epub> [--real]")
        sys.exit(1)
    main(args[0], use_real)
