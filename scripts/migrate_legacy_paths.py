"""Chuyển media của sách từ layout cũ sang layout hiện hành — cả file trên đĩa lẫn
mọi tham chiếu đường dẫn nằm trong DB.

    cũ                                          mới
    books/{b}/patches/{patch_id}.wav            books/{b}/audio/{b}_{ep}.wav
    books/{b}/patches/{patch_id}.ass            books/{b}/audio/{b}_{ep}.ass
    books/{b}/patches/{patch_id}.timeline.json  books/{b}/audio/{b}_{ep}.timeline.json
    books/{b}/patches/{patch_id}_chunks/        books/{b}/audio/{b}_{ep}_chunks/
    books/{b}/patch_videos/{patch_id}.mp4       books/{b}/videos/{b}_{ep}.mp4

``ep`` = patch_index + 1, đệm 0 cho đủ 3 chữ số — tức khoá theo THỨ TỰ patch chứ không
phải patch_id, nên chỉ suy ra được khi hàng patch còn sống. File thuộc patch đã bị xoá
(rebuild sinh id mới) không có đích hợp lệ: script gọi chúng là "orphan", mặc định để
nguyên; ``--orphans archive`` dồn chúng sang books/{b}/_legacy_archive/ để dọn sạch thư
mục cũ mà không mất dữ liệu (và cập nhật luôn videos.file_path trỏ theo).

Gộp và thay thế hai script cũ: migrate_patch_audio_paths.py (chỉ chuẩn hoá cột, không
di chuyển file) và migrate_patch_video_paths.py (chỉ video, và bỏ sót videos.file_path
khi patch_id NULL).

Mặc định dry-run. Chỉ ghi khi --apply, luôn backup DB trước khi ghi, và từ chối chạy khi
hàng đợi còn job sống (một render/TTS đang chạy vẫn đang cầm đường dẫn cũ).

    python scripts/migrate_legacy_paths.py                     # dry-run toàn bộ
    python scripts/migrate_legacy_paths.py --book 22           # chỉ một sách
    python scripts/migrate_legacy_paths.py --apply             # move file + update DB
    python scripts/migrate_legacy_paths.py --apply --orphans archive --relink-audio
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.config import settings  # noqa: E402
from app.repository import (  # noqa: E402
    _chunk_dir_for,
    _legacy_patch_video_path,
    get_patch_audio_path,
    get_patch_chunk_dir,
    get_patch_video_path,
)

LEGACY_AUDIO_DIR = "patches"
LEGACY_VIDEO_DIR = "patch_videos"
ARCHIVE_DIR = "_legacy_archive"

AUDIO_SUFFIXES = (".wav", ".ass", ".timeline.json")

_CHUNK_RE = re.compile(r"^(\d+)_chunks$")
_AUDIO_RE = re.compile(r"^(\d+)\.(wav|ass|timeline\.json)$")

# Cột chứa một đường dẫn trần. (bảng, cột) — bảng/cột thiếu trong DB cũ đều được bỏ qua.
PATH_COLUMNS = (
    ("patch", "audio_path"),
    ("book", "final_audio_path"),
    ("book", "final_video_path"),
    ("videos", "file_path"),
    ("videos", "source_audio"),
    ("patch_pipeline", "video_path"),
    ("patch_pipeline", "thumbnail_path"),
    ("youtube_uploads", "video_path"),
    ("book_job", "output_path"),
)

# Cột chứa JSON có đường dẫn nằm sâu bên trong (snapshot render, payload job...).
JSON_COLUMNS = (
    ("patch_pipeline", "media_snapshot"),
    ("patch_pipeline", "config_snapshot"),
    ("videos", "render_config_json"),
    ("job", "payload_json"),
    ("job", "result_json"),
)

ACTIVE_JOB_STATUSES = ("pending", "running", "cancelling")


@dataclass
class Move:
    """Một file/thư mục cũ và đích của nó theo layout mới."""

    src: Path
    dst: Path
    patch_id: int
    kind: str  # audio | chunks | video


@dataclass
class Orphan:
    """File layout cũ không suy ra được đích (patch đã bị xoá / tên lạ)."""

    path: Path
    book_id: int
    reason: str
    kind: str


@dataclass
class DbEdit:
    table: str
    column: str
    row_id: int
    old: str
    new: str


@dataclass
class Plan:
    moves: list[Move] = field(default_factory=list)
    orphans: list[Orphan] = field(default_factory=list)
    conflicts: list[tuple[Path, Path]] = field(default_factory=list)
    db_edits: list[DbEdit] = field(default_factory=list)
    patch_links: list[tuple[int, Path]] = field(default_factory=list)  # --relink-audio
    video_links: list[tuple[int, int]] = field(default_factory=list)  # (videos.id, patch_id)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def backup_db(db_path: str) -> Path | None:
    src = Path(db_path)
    if not src.is_file():
        print(f"[backup] Không thấy DB: {src} — bỏ qua backup.")
        return None
    dst = src.with_name(f"{src.stem}.bak_{datetime.now().strftime('%Y%m%d_%H%M%S')}{src.suffix}")
    try:
        shutil.copy2(src, dst)
        print(f"[backup] Đã backup DB -> {dst}")
        return dst
    except OSError as exc:
        print(f"[backup] Không backup được DB: {exc}")
        return None


def active_jobs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Job đang sống trong hàng đợi. Một TTS/render đang chạy vẫn cầm đường dẫn cũ đã
    đọc lúc claim, nên move file dưới chân nó sẽ làm job chết giữa chừng."""
    if not _has_table(conn, "job"):
        return []
    marks = ", ".join("?" for _ in ACTIVE_JOB_STATUSES)
    return conn.execute(
        f"SELECT id, job_type, status, book_id, patch_id FROM job WHERE status IN ({marks})",
        ACTIVE_JOB_STATUSES,
    ).fetchall()


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    if not _has_table(conn, table):
        return False
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})"))


def load_patches(conn: sqlite3.Connection, book_id: int | None) -> dict[int, sqlite3.Row]:
    sql = "SELECT id, book_id, patch_index, status, audio_path FROM patch"
    args: tuple = ()
    if book_id is not None:
        sql += " WHERE book_id=?"
        args = (book_id,)
    return {row["id"]: row for row in conn.execute(sql, args)}


def legacy_targets(book_id: int, patch_id: int, patch_index: int) -> dict[Path, Path]:
    """Bảng cũ -> mới cho MỌI artefact của một patch, kể cả file chưa từng tồn tại:
    DB có thể còn trỏ vào một đường dẫn cũ mà file đã bị xoá từ lâu."""
    new_audio = get_patch_audio_path(book_id, patch_index)
    legacy_audio = Path(settings.data_root) / "books" / str(book_id) / LEGACY_AUDIO_DIR / f"{patch_id}.wav"
    mapping = {
        legacy_audio.with_suffix(suffix): new_audio.with_suffix(suffix) for suffix in AUDIO_SUFFIXES
    }
    mapping[_chunk_dir_for(book_id, patch_id)] = get_patch_chunk_dir(book_id, patch_index)
    mapping[_legacy_patch_video_path(book_id, patch_id)] = get_patch_video_path(book_id, patch_index)
    return mapping


def build_remap(patches: dict[int, sqlite3.Row]) -> dict[str, str]:
    """normcase(đường dẫn cũ) -> đường dẫn mới, cho mọi patch còn sống."""
    remap: dict[str, str] = {}
    for patch in patches.values():
        for old, new in legacy_targets(patch["book_id"], patch["id"], patch["patch_index"]).items():
            remap[os.path.normcase(str(old))] = str(new)
    return remap


def remap_value(remap: dict[str, str], value) -> str | None:
    """Đổi một chuỗi trong DB sang layout mới, hoặc None nếu không liên quan.

    Nhận cả hai dạng phái sinh của audio_fingerprint (``path:size:sha`` và
    ``path:missing``) để so sánh fingerprint sau migration vẫn khớp: chỉ phần path đổi,
    size/hash giữ nguyên nên nội dung audio vẫn được coi là không đổi."""
    if not isinstance(value, str) or not value:
        return None
    direct = remap.get(os.path.normcase(value))
    if direct:
        return direct
    head, sep, tail = value.rpartition(":")
    if sep and tail == "missing":
        new = remap.get(os.path.normcase(head))
        if new:
            return f"{new}:missing"
    parts = value.rsplit(":", 2)
    if len(parts) == 3 and parts[1].isdigit():
        new = remap.get(os.path.normcase(parts[0]))
        if new:
            return f"{new}:{parts[1]}:{parts[2]}"
    return None


def remap_json(remap: dict[str, str], node):
    """Đệ quy thay mọi chuỗi-đường-dẫn trong một cây JSON. Trả về (cây mới, số lần đổi)."""
    if isinstance(node, dict):
        changed = 0
        out = {}
        for key, value in node.items():
            out[key], count = remap_json(remap, value)
            changed += count
        return out, changed
    if isinstance(node, list):
        changed = 0
        out = []
        for item in node:
            new_item, count = remap_json(remap, item)
            out.append(new_item)
            changed += count
        return out, changed
    new = remap_value(remap, node)
    return (new, 1) if new is not None else (node, 0)


def _book_dirs(book_id: int | None):
    root = Path(settings.data_root) / "books"
    if not root.is_dir():
        return []
    if book_id is not None:
        target = root / str(book_id)
        return [target] if target.is_dir() else []
    return sorted((d for d in root.iterdir() if d.is_dir() and d.name.isdigit()), key=lambda d: int(d.name))


def plan_file_moves(patches: dict[int, sqlite3.Row], book_id: int | None, plan: Plan) -> None:
    for book_dir in _book_dirs(book_id):
        bid = int(book_dir.name)
        _scan_legacy_audio(book_dir / LEGACY_AUDIO_DIR, bid, patches, plan)
        _scan_legacy_videos(book_dir / LEGACY_VIDEO_DIR, bid, patches, plan)


def _resolve(entry: Path, bid: int, patch_id: int, kind: str,
             patches: dict[int, sqlite3.Row], plan: Plan) -> sqlite3.Row | None:
    patch = patches.get(patch_id)
    if patch is None:
        plan.orphans.append(Orphan(entry, bid, "patch row đã bị xoá", kind))
        return None
    if patch["book_id"] != bid:
        plan.orphans.append(Orphan(entry, bid, f"patch thuộc book {patch['book_id']}", kind))
        return None
    return patch


def _queue(entry: Path, dst: Path, patch_id: int, kind: str, plan: Plan) -> None:
    if dst.exists():
        plan.conflicts.append((entry, dst))
        return
    plan.moves.append(Move(entry, dst, patch_id, kind))


def _scan_legacy_audio(legacy_dir: Path, bid: int, patches, plan: Plan) -> None:
    if not legacy_dir.is_dir():
        return
    for entry in sorted(legacy_dir.iterdir()):
        if entry.is_dir():
            match = _CHUNK_RE.match(entry.name)
            if not match:
                plan.orphans.append(Orphan(entry, bid, "tên thư mục không theo layout cũ", "chunks"))
                continue
            patch_id = int(match.group(1))
            patch = _resolve(entry, bid, patch_id, "chunks", patches, plan)
            if patch is not None:
                _queue(entry, get_patch_chunk_dir(bid, patch["patch_index"]), patch_id, "chunks", plan)
            continue
        match = _AUDIO_RE.match(entry.name)
        if not match:
            plan.orphans.append(Orphan(entry, bid, "tên file không theo layout cũ", "audio"))
            continue
        patch_id = int(match.group(1))
        patch = _resolve(entry, bid, patch_id, "audio", patches, plan)
        if patch is not None:
            suffix = f".{match.group(2)}"
            dst = get_patch_audio_path(bid, patch["patch_index"]).with_suffix(suffix)
            _queue(entry, dst, patch_id, "audio", plan)


def _scan_legacy_videos(legacy_dir: Path, bid: int, patches, plan: Plan) -> None:
    if not legacy_dir.is_dir():
        return
    for entry in sorted(legacy_dir.iterdir()):
        if entry.is_dir() or entry.suffix.lower() != ".mp4" or not entry.stem.isdigit():
            plan.orphans.append(Orphan(entry, bid, "tên file không theo layout cũ", "video"))
            continue
        patch_id = int(entry.stem)
        patch = _resolve(entry, bid, patch_id, "video", patches, plan)
        if patch is not None:
            _queue(entry, get_patch_video_path(bid, patch["patch_index"]), patch_id, "video", plan)


def plan_db_edits(conn: sqlite3.Connection, remap: dict[str, str], plan: Plan) -> None:
    for table, column in PATH_COLUMNS:
        if not _has_column(conn, table, column):
            continue
        rows = conn.execute(
            f"SELECT id, {column} AS value FROM {table} WHERE {column} IS NOT NULL AND {column} != ''"
        ).fetchall()
        for row in rows:
            new = remap_value(remap, row["value"])
            if new and new != row["value"]:
                plan.db_edits.append(DbEdit(table, column, row["id"], row["value"], new))

    for table, column in JSON_COLUMNS:
        if not _has_column(conn, table, column):
            continue
        rows = conn.execute(
            f"SELECT id, {column} AS value FROM {table} WHERE {column} IS NOT NULL AND {column} != ''"
        ).fetchall()
        for row in rows:
            try:
                tree = json.loads(row["value"])
            except (TypeError, ValueError):
                continue
            new_tree, changed = remap_json(remap, tree)
            if changed:
                plan.db_edits.append(DbEdit(table, column, row["id"], row["value"], json.dumps(new_tree)))


def plan_video_links(conn: sqlite3.Connection, plan: Plan) -> None:
    """videos.patch_id còn NULL nhưng file_path (sau remap) đã chỉ đúng một patch sống.
    Không có link này thì Video Library không tìm lại được video của patch, và index
    UNIQUE(patch_id) không dedupe được row do writer sau chèn thêm."""
    if not _has_column(conn, "videos", "patch_id"):
        return
    remapped = {edit.row_id: edit.new for edit in plan.db_edits
                if edit.table == "videos" and edit.column == "file_path"}
    rows = conn.execute(
        "SELECT id, book_id, file_path FROM videos WHERE patch_id IS NULL AND file_path IS NOT NULL"
    ).fetchall()
    for row in rows:
        path = Path(remapped.get(row["id"], row["file_path"]))
        book_dir = path.parent.parent
        if path.parent.name != "videos" or not book_dir.name.isdigit():
            continue
        book_part, _, episode = path.stem.partition("_")
        if not (book_part.isdigit() and episode.isdigit() and book_part == book_dir.name):
            continue
        patch = conn.execute(
            "SELECT id FROM patch WHERE book_id=? AND patch_index=?",
            (int(book_part), int(episode) - 1),
        ).fetchone()
        if patch is None:
            continue
        taken = conn.execute("SELECT 1 FROM videos WHERE patch_id=?", (patch["id"],)).fetchone()
        if taken is None:
            plan.video_links.append((row["id"], patch["id"]))


def plan_relink_audio(conn: sqlite3.Connection, patches: dict[int, sqlite3.Row], plan: Plan) -> None:
    """Patch chưa có audio_path nhưng sau khi move sẽ có wav đúng chỗ theo layout mới.

    Chỉ chạy khi --relink-audio: một wav mồ côi có thể là tàn dư của lần reset patch
    (reset xoá audio_path trước, file ở vị trí cũ không bị đụng tới), nên nối lại là
    quyết định của người dùng chứ không phải mặc định của migration."""
    landing = {move.dst for move in plan.moves if move.kind == "audio" and move.dst.suffix == ".wav"}
    for patch in patches.values():
        if patch["audio_path"]:
            continue
        if patch["status"] not in ("pending", "failed"):
            continue
        target = get_patch_audio_path(patch["book_id"], patch["patch_index"])
        if target in landing or target.is_file():
            plan.patch_links.append((patch["id"], target))


def archive_path(orphan: Orphan) -> Path:
    return (Path(settings.data_root) / "books" / str(orphan.book_id) / ARCHIVE_DIR
            / orphan.path.parent.name / orphan.path.name)


def apply_moves(moves: list[Move]) -> int:
    moved = 0
    for move in moves:
        try:
            move.dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(move.src), str(move.dst))
            moved += 1
        except OSError as exc:
            print(f"[LỖI] move {move.src} -> {move.dst}: {exc}")
    return moved


def apply_db_edits(conn: sqlite3.Connection, plan: Plan) -> None:
    with conn:
        for edit in plan.db_edits:
            conn.execute(
                f"UPDATE {edit.table} SET {edit.column}=? WHERE id=?", (edit.new, edit.row_id)
            )
        for video_id, patch_id in plan.video_links:
            conn.execute("UPDATE videos SET patch_id=? WHERE id=?", (patch_id, video_id))
        for video_id, new_path in {
            edit.row_id: edit.new for edit in plan.db_edits
            if edit.table == "videos" and edit.column == "file_path"
        }.items():
            conn.execute("UPDATE videos SET filename=? WHERE id=?", (Path(new_path).name, video_id))
        for patch_id, target in plan.patch_links:
            conn.execute(
                "UPDATE patch SET status='done', audio_path=?, updated_at=? WHERE id=?",
                (str(target), _now(), patch_id),
            )


def cleanup_empty_dirs(book_id: int | None) -> None:
    for book_dir in _book_dirs(book_id):
        for name in (LEGACY_AUDIO_DIR, LEGACY_VIDEO_DIR):
            legacy = book_dir / name
            if legacy.is_dir() and not any(legacy.iterdir()):
                try:
                    legacy.rmdir()
                    print(f"[dọn] Đã xoá thư mục rỗng {legacy}")
                except OSError:
                    pass


def build_plan(conn: sqlite3.Connection, *, book_id: int | None, relink_audio: bool) -> Plan:
    patches = load_patches(conn, book_id)
    plan = Plan()
    plan_file_moves(patches, book_id, plan)
    plan_db_edits(conn, build_remap(patches), plan)
    plan_video_links(conn, plan)
    if relink_audio:
        plan_relink_audio(conn, patches, plan)
    return plan


def print_plan(plan: Plan, orphan_mode: str) -> None:
    print(f"\n== {len(plan.moves)} file/thư mục sẽ chuyển ==")
    for move in plan.moves[:40]:
        print(f"  [{move.kind}] {move.src} -> {move.dst}")
    if len(plan.moves) > 40:
        print(f"  ... và {len(plan.moves) - 40} mục nữa")

    if plan.conflicts:
        print(f"\n== {len(plan.conflicts)} xung đột (đích đã tồn tại — BỎ QUA, không ghi đè) ==")
        for src, dst in plan.conflicts[:20]:
            print(f"  {src}  ><  {dst}")

    if plan.orphans:
        size = sum(_size_of(o.path) for o in plan.orphans)
        action = "sẽ dồn vào _legacy_archive/" if orphan_mode == "archive" else "GIỮ NGUYÊN (dùng --orphans archive để dồn)"
        print(f"\n== {len(plan.orphans)} orphan, {size / 1e9:.2f} GB — {action} ==")
        by_reason: dict[str, int] = {}
        for orphan in plan.orphans:
            by_reason[f"{orphan.kind}: {orphan.reason}"] = by_reason.get(f"{orphan.kind}: {orphan.reason}", 0) + 1
        for reason, count in sorted(by_reason.items()):
            print(f"  {count:4d} × {reason}")

    print(f"\n== {len(plan.db_edits)} giá trị trong DB sẽ đổi ==")
    by_column: dict[str, int] = {}
    for edit in plan.db_edits:
        key = f"{edit.table}.{edit.column}"
        by_column[key] = by_column.get(key, 0) + 1
    for key, count in sorted(by_column.items()):
        print(f"  {count:4d} × {key}")

    if plan.video_links:
        print(f"\n== {len(plan.video_links)} row videos sẽ được nối lại patch_id ==")
    if plan.patch_links:
        print(f"\n== {len(plan.patch_links)} patch sẽ được nối lại audio_path (status -> done) ==")
        for patch_id, target in plan.patch_links[:20]:
            print(f"  patch {patch_id} -> {target}")


def _size_of(path: Path) -> int:
    try:
        if path.is_dir():
            return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
        return path.stat().st_size
    except OSError:
        return 0


def apply_orphan_archive(conn: sqlite3.Connection, plan: Plan) -> int:
    """Dồn orphan sang _legacy_archive/ và kéo theo videos.file_path để DB không trỏ hụt."""
    archived = 0
    for orphan in plan.orphans:
        dst = archive_path(orphan)
        if dst.exists():
            print(f"[bỏ qua] đích archive đã tồn tại: {dst}")
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(orphan.path), str(dst))
            archived += 1
        except OSError as exc:
            print(f"[LỖI] archive {orphan.path}: {exc}")
            continue
        if _has_column(conn, "videos", "file_path"):
            conn.execute(
                "UPDATE videos SET file_path=?, filename=? WHERE file_path=?",
                (str(dst), dst.name, str(orphan.path)),
            )
    conn.commit()
    return archived


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Migrate media sách sang layout mới (file + DB)")
    parser.add_argument("--apply", action="store_true", help="Thực hiện move file + ghi DB (mặc định dry-run)")
    parser.add_argument("--db", default=None, help="Đường dẫn DB (mặc định settings.db_path)")
    parser.add_argument("--book", type=int, default=None, help="Chỉ xử lý một book_id")
    parser.add_argument("--orphans", choices=("keep", "archive"), default="keep",
                        help="File của patch đã bị xoá: giữ nguyên (mặc định) hoặc dồn vào _legacy_archive/")
    parser.add_argument("--relink-audio", action="store_true",
                        help="Nối lại patch.audio_path (status -> done) cho patch có wav đúng chỗ nhưng DB đang bỏ trống")
    parser.add_argument("--force", action="store_true", help="Bỏ qua chốt chặn hàng đợi còn job sống")
    args = parser.parse_args(argv)

    db_path = args.db or settings.db_path
    conn = db.connect(db_path)

    plan = build_plan(conn, book_id=args.book, relink_audio=args.relink_audio)
    print_plan(plan, args.orphans)

    nothing = not (plan.moves or plan.db_edits or plan.video_links or plan.patch_links
                   or (args.orphans == "archive" and plan.orphans))
    if nothing:
        print("\nKhông có gì để migrate.")
        return 0

    if not args.apply:
        print("\n[dry-run] Chưa ghi gì. Chạy lại với --apply để thực hiện.")
        return 0

    jobs = active_jobs(conn)
    if jobs and not args.force:
        print(f"\n[DỪNG] Hàng đợi còn {len(jobs)} job sống — move file dưới chân chúng sẽ làm job chết:")
        for job in jobs[:10]:
            print(f"  job {job['id']} {job['job_type']} {job['status']} (book {job['book_id']}, patch {job['patch_id']})")
        print("Dừng worker rồi chạy lại, hoặc thêm --force nếu chắc chắn.")
        return 1

    backup_db(db_path)
    moved = apply_moves(plan.moves)
    apply_db_edits(conn, plan)
    archived = apply_orphan_archive(conn, plan) if args.orphans == "archive" else 0

    print(f"\n[apply] Đã chuyển {moved}/{len(plan.moves)} mục, cập nhật {len(plan.db_edits)} giá trị DB, "
          f"nối {len(plan.video_links)} videos.patch_id, {len(plan.patch_links)} patch.audio_path"
          + (f", archive {archived} orphan" if args.orphans == "archive" else ""))
    cleanup_empty_dirs(args.book)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
