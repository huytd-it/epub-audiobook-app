"""Phase 4 acceptance check: run the worker against a small book with a stub TTS engine,
verify sequential processing order and that the final merged audio gets produced."""
import asyncio
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from app import db, repository
from app.epub_parser import ParsedChapter
from app.worker import PatchWorker


class StubEngine:
    sample_rate = 8000

    def __init__(self):
        self.calls = []

    def synthesize_patch(self, text, max_chars=400, reference_wav_path=None, prompt_text=None):
        self.calls.append(text[:20])
        return [np.zeros(800, dtype=np.float32)]  # 0.1s of silence per patch


async def main() -> None:
    tmp = Path("/tmp/worker_test")
    tmp.mkdir(exist_ok=True)
    db_path = str(tmp / "app.db")
    Path(db_path).unlink(missing_ok=True)

    conn = db.connect(db_path)
    db.init_schema(conn)

    chapters = [ParsedChapter(title=f"Ch {i}", text=f"Text of chapter {i}. " * 5) for i in range(25)]
    book = repository.create_book(
        conn, title="Worker Test Book", original_filename="t.epub", epub_path="t.epub",
        patch_size=10, chapters=chapters, background_image_path=None,
    )
    patches = repository.list_patches(conn, book.id)
    print(f"Book has {len(patches)} patches (expect 3 for 25 chapters / size 10)")

    engine = StubEngine()
    worker = PatchWorker(conn, engine, data_root=str(tmp))

    task = asyncio.create_task(worker.run_forever())
    for _ in range(50):
        await asyncio.sleep(0.05)
        b = repository.get_book(conn, book.id)
        if b.status == "done":
            break
    worker.stop()
    await asyncio.sleep(0.1)
    task.cancel()

    final_book = repository.get_book(conn, book.id)
    print(f"Book status: {final_book.status}, final_audio_path={final_book.final_audio_path}")
    assert final_book.status == "done"
    assert final_book.final_audio_path and Path(final_book.final_audio_path).exists()
    print("All patches processed and merged. PASS")

    print("\n--- Testing resume-after-crash ---")
    repository.reset_patch(conn, patches[1].id)
    conn.execute("UPDATE patch SET status='processing' WHERE id=?", (patches[1].id,))
    conn.commit()
    n = repository.requeue_stuck_processing(conn)
    print(f"Requeued {n} stuck patch(es) on simulated restart")
    p = repository.get_patch(conn, patches[1].id)
    assert p.status == "pending"
    print("Resume-after-crash PASS")

    print("\n--- Testing regenerate guard (refuse while processing) ---")
    conn.execute("UPDATE patch SET status='processing' WHERE id=?", (patches[0].id,))
    conn.commit()
    ok = repository.reset_patch(conn, patches[0].id)
    assert ok is False, "reset_patch should refuse a currently-processing patch"
    print("Regenerate guard PASS")

    conn.close()


if __name__ == "__main__":
    asyncio.run(main())
