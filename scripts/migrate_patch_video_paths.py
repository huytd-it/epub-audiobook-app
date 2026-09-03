"""Migrate patch videos from legacy patch_videos/{patch_id}.mp4 to videos/{book_id}_{episode}.mp4.

    Dry-run mặc định, chỉ ghi khi --apply. Backup DB trước khi ghi.

    python scripts/migrate_patch_video_paths.py          # dry-run
    python scripts/migrate_patch_video_paths.py --apply  # thực hiện move + update DB
"""
from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.config import settings  # noqa: E402
from app.repository import get_patch_video_path, _legacy_patch_video_path, _patch_from_row  # noqa: E402


def backup_db(db_path: str) -> Path | None:
    src = Path(db_path)
    if not src.is_file():
        print(f"[backup] DB not found: {src} — skip backup.")
        return None
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = src.with_name(f"{src.stem}.bak_{ts}{src.suffix}")
    try:
        shutil.copy2(src, dst)
        print(f"[backup] Backup DB -> {dst}")
        return dst
    except OSError as exc:
        print(f"[backup] Backup failed: {exc}")
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate patch videos to videos/{book}_{episode}.mp4")
    parser.add_argument("--apply", action="store_true", help="Thực hiện move file + update DB (mặc định dry-run)")
    parser.add_argument("--db", default=None, help="Đường dẫn DB (mặc định settings.db_path)")
    args = parser.parse_args()

    db_path = args.db or settings.db_path
    conn = db.connect(db_path)

    rows = conn.execute("SELECT * FROM patch").fetchall()
    if not rows:
        print("No patches.")
        return 0

    plan_file: list[tuple[Path, Path]] = []  # (src, dst)
    plan_db_videos: list[tuple[str, str]] = []  # (old_path, new_path) for videos.file_path
    plan_db_pipeline: list[tuple[str, str]] = []
    warnings: list[str] = []

    for row in rows:
        patch = _patch_from_row(row)
        new_path = get_patch_video_path(patch.book_id, patch.patch_index)
        legacy_path = _legacy_patch_video_path(patch.book_id, patch.id)

        # file move plan: legacy exists -> new
        if legacy_path.is_file():
            if new_path.exists() and new_path != legacy_path:
                warnings.append(f"[WARN] patch {patch.id} book {patch.book_id} idx {patch.patch_index}: both legacy and new exist — skip move: {legacy_path} / {new_path}")
            else:
                plan_file.append((legacy_path, new_path))

        # DB videos rows pointing at legacy
        for vrow in conn.execute("SELECT id, file_path FROM videos WHERE patch_id=?", (patch.id,)).fetchall():
            fp = vrow["file_path"]
            if fp == str(legacy_path):
                plan_db_videos.append((fp, str(new_path)))
            elif fp == str(new_path):
                pass
            elif fp and "patch_videos" in fp:
                # generic legacy path not matching helper (e.g. custom data_root)
                plan_db_videos.append((fp, str(new_path)))

        # patch_pipeline video_path
        prow = conn.execute("SELECT video_path FROM patch_pipeline WHERE patch_id=?", (patch.id,)).fetchone()
        if prow and prow["video_path"] == str(legacy_path):
            plan_db_pipeline.append((prow["video_path"], str(new_path)))

    for w in warnings:
        print(w)

    if not plan_file and not plan_db_videos and not plan_db_pipeline:
        print("Nothing to migrate.")
        return 0

    print(f"\nFound {len(plan_file)} file(s) to move:")
    for src, dst in plan_file:
        print(f"  {src} -> {dst}")
    print(f"Found {len(plan_db_videos)} videos.file_path, {len(plan_db_pipeline)} patch_pipeline.video_path to update")

    if not args.apply:
        print("\n[dry-run] Not written. Run with --apply to apply.")
        return 0

    backup_db(db_path)

    # move files
    moved = 0
    for src, dst in plan_file:
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            moved += 1
        except OSError as exc:
            print(f"[ERROR] move {src} -> {dst}: {exc}")

    # update DB
    with conn:
        for old, new in plan_db_videos:
            conn.execute("UPDATE videos SET file_path=? WHERE file_path=?", (new, old))
        for old, new in plan_db_pipeline:
            conn.execute("UPDATE patch_pipeline SET video_path=? WHERE video_path=?", (new, old))

    print(f"\n[apply] Moved {moved}/{len(plan_file)} files, updated {len(plan_db_videos)} videos, {len(plan_db_pipeline)} pipeline.")
    # cleanup empty legacy dirs — skipped while a render job is in flight. An
    # empty patch_videos/ is not proof that nothing needs it: a render creates
    # the directory up front and only writes into it at the final mux, hours
    # later for a gameplay episode. Removing it underneath such a job makes
    # ffmpeg exit ENOENT after the whole render has already been paid for.
    running = conn.execute(
        "SELECT COUNT(*) FROM job WHERE job_type='patch_video' AND status IN ('running','pending')"
    ).fetchone()[0]
    if running:
        print(f"[cleanup] Skipped removing empty legacy dirs — {running} patch_video job(s) in flight.")
        return 0
    for book_dir in (Path(settings.data_root) / "books").glob("*"):
        legacy_dir = book_dir / "patch_videos"
        if legacy_dir.is_dir() and not any(legacy_dir.iterdir()):
            try:
                legacy_dir.rmdir()
                print(f"[cleanup] Removed empty {legacy_dir}")
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
