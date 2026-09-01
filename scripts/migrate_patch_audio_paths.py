"""Chuan hoa cot patch.audio_path ve tuyet doi (settings.data_root).

Layout cu:  data/books/{book}/patches/{patch_id}.wav  (tuyet doi, cu)
Layout moi: data/books/{book}/audio/{book}_{episode}.wav (tuong doi, moi)

DB dang ton tai song song hai dang; script nay tim file that tren dia
(qua resolve_patch_audio) roi chuan hoa ve duong dan tuyet doi.

Mac dinh dry-run: chi in ra tung thay doi du kien.
Chi ghi DB khi truyen --apply. Truoc khi ghi se backup file DB.

    python scripts/migrate_patch_audio_paths.py          # dry-run
    python scripts/migrate_patch_audio_paths.py --apply  # ghi DB
"""
from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

# Dam bao import duoc app.* khi chay tu scripts/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.config import settings  # noqa: E402
from app.repository import _patch_from_row, resolve_patch_audio  # noqa: E402


def backup_db(db_path: str) -> Path | None:
    src = Path(db_path)
    if not src.is_file():
        print(f"[backup] DB chua ton tai: {src} — bo qua backup.")
        return None
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = src.with_name(f"{src.stem}.bak_{ts}{src.suffix}")
    try:
        shutil.copy2(src, dst)
        print(f"[backup] Da backup DB -> {dst}")
        return dst
    except OSError as exc:
        print(f"[backup] Khong backup duoc DB: {exc}")
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Chuan hoa patch.audio_path ve tuyet doi")
    parser.add_argument("--apply", action="store_true", help="Ghi thay doi vao DB (mac dinh dry-run)")
    parser.add_argument("--db", default=None, help="Duong dan DB (mac dinh settings.db_path)")
    args = parser.parse_args()

    db_path = args.db or settings.db_path
    conn = db.connect(db_path)

    rows = conn.execute("SELECT * FROM patch WHERE audio_path IS NOT NULL AND audio_path != ''").fetchall()
    if not rows:
        print("Khong co patch nao co audio_path.")
        return 0

    plan: list[tuple[int, str, str]] = []  # (patch_id, old, new)
    warnings: list[str] = []

    for row in rows:
        patch = _patch_from_row(row)
        raw = patch.audio_path
        resolved = resolve_patch_audio(patch)
        if resolved is None:
            warnings.append(
                f"[WARN] patch {patch.id} (book {patch.book_id} idx {patch.patch_index}): "
                f"khong candidate nao ton tai tren dia — raw={raw!r} — bo qua"
            )
            continue
        new_abs = str(resolved)
        if raw != new_abs:
            plan.append((patch.id, raw, new_abs))

    for w in warnings:
        print(w)

    if not plan:
        print("Khong co dong nao can chuan hoa.")
        return 0

    print(f"\nTim thay {len(plan)} patch can chuan hoa:")
    for pid, old, new in plan:
        print(f"  patch {pid}: {old!r} -> {new!r}")

    if not args.apply:
        print("\n[dry-run] Chua ghi DB. Chay voi --apply de ghi.")
        return 0

    # --apply: backup roi ghi
    backup_db(db_path)

    with conn:
        conn.executemany("UPDATE patch SET audio_path = ? WHERE id = ?", [(new, pid) for pid, _old, new in plan])

    print(f"\n[apply] Da cap nhat {len(plan)} dong.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
