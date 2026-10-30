"""Kho chunk WAV theo chương — tách đời sống của audio TTS khỏi patch.

    data/books/{book_id}/chapters/{chapter_index+1:04d}/
        chunk_000.wav, chunk_001.wav, ...   (index cục bộ trong chương)
        chunks.json                          (manifest)

Patch chỉ còn là phép ghép các chunk của các chương nó phủ: build lại danh sách
patch, reset hay xoá patch không đụng tới thư mục này, nên lần TTS sau chỉ phải
tổng hợp những chunk còn thiếu/lệch rồi ghép lại.

Mỗi chunk tự kiểm tra bằng ``key = md5(engine | config | text)`` lưu trong manifest:
chunk hợp lệ khi file tồn tại và key khớp. Pause không nằm trong file (được chèn lúc
ghép) nên không thuộc key; max_chars chỉ đổi cách tách, thứ đã phản ánh qua text.

Patch Text Studio (``clean_text``) không mang ranh giới chương: plan của nó dùng
cùng cơ chế manifest nhưng nằm trong thư mục chunk của patch.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path

from app.config import settings

MANIFEST = "chunks.json"


def get_book_chapters_dir(book_id: int) -> Path:
    return Path(settings.data_root) / "books" / str(book_id) / "chapters"


def get_chapter_chunk_dir(book_id: int, chapter_index: int) -> Path:
    # Đánh số từ 1 như cột "Ch." trên UI và số tập của patch.
    return get_book_chapters_dir(book_id) / f"{chapter_index + 1:04d}"


def chunk_key(engine_id: str, model_cfg: str, text: str) -> str:
    """Hash of everything that changes one chunk's audio: engine id, engine/model
    config (voice included) and the exact text."""
    return hashlib.md5(f"{engine_id}|{model_cfg}|{text}".encode("utf-8")).hexdigest()


class ChunkStore:
    def __init__(self, directory: Path):
        self.dir = Path(directory)

    def path(self, index: int) -> Path:
        return self.dir / f"chunk_{index:03d}.wav"

    def manifest(self) -> dict:
        try:
            data = json.loads((self.dir / MANIFEST).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"chunks": {}}
        if not isinstance(data, dict) or not isinstance(data.get("chunks"), dict):
            return {"chunks": {}}
        return data

    def entry(self, index: int) -> dict | None:
        entry = self.manifest()["chunks"].get(str(index))
        return entry if isinstance(entry, dict) else None

    def is_valid(self, index: int, key: str) -> bool:
        entry = self.entry(index)
        return bool(entry and entry.get("key") == key and self.path(index).is_file())

    def has_text(self, index: int, text: str) -> bool:
        """Chunk exists for this exact text, whatever engine produced it — used by
        previews, which have no engine at hand."""
        entry = self.entry(index)
        return bool(entry and entry.get("text") == text and self.path(index).is_file())

    def _write_manifest(self, data: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=self.dir, prefix=f".{MANIFEST}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False)
            os.replace(temp, self.dir / MANIFEST)
        except Exception:
            Path(temp).unlink(missing_ok=True)
            raise

    def record(self, index: int, *, key: str, text: str, frames: int, sample_rate: int,
               engine: str | None = None, voice: str | None = None, max_chars: int | None = None) -> None:
        data = self.manifest()
        data["chunks"][str(index)] = {
            "key": key, "text": text, "frames": int(frames), "sample_rate": int(sample_rate),
        }
        if engine is not None:
            data["engine"] = engine
        if voice is not None:
            data["voice"] = voice
        if max_chars:
            data["max_chars"] = int(max_chars)
        data["sample_rate"] = int(sample_rate)
        # Một chunk vừa được (tổng hợp) lại: chương chỉ "đủ" trở lại khi cả lượt chạy
        # đi hết chương và đánh dấu lại bằng mark_complete.
        data.pop("complete", None)
        self._write_manifest(data)

    def mark_complete(self, source: str, count: int) -> None:
        """Ghi nhận chương đã có đủ ``count`` chunk cho đúng nguồn ``source`` (xem
        repository.chapter_tts_source_key) — để bảng patch đếm chương sẵn sàng mà không
        phải chuẩn hoá lại văn bản."""
        data = self.manifest()
        data["complete"] = {"source": source, "count": int(count)}
        self._write_manifest(data)

    def is_complete(self, source: str, data: dict | None = None) -> bool:
        data = data if data is not None else self.manifest()
        complete = data.get("complete")
        if not isinstance(complete, dict) or complete.get("source") != source:
            return False
        count = int(complete.get("count") or 0)
        return count > 0 and all(
            str(i) in data["chunks"] and self.path(i).is_file() for i in range(count))

    def delete(self, index: int) -> bool:
        existed = self.path(index).is_file()
        self.path(index).unlink(missing_ok=True)
        data = self.manifest()
        removed = data["chunks"].pop(str(index), None) is not None
        if removed or "complete" in data:
            existed = existed or removed
            data.pop("complete", None)
            self._write_manifest(data)
        return existed

    def clear(self) -> int:
        count = len(list(self.dir.glob("chunk_*.wav"))) if self.dir.is_dir() else 0
        shutil.rmtree(self.dir, ignore_errors=True)
        return count

    def prune(self, count: int) -> None:
        """Drop chunks at index >= count: the chapter was re-split into fewer chunks."""
        if not self.dir.is_dir():
            return
        for path in self.dir.glob("chunk_*.wav"):
            try:
                index = int(path.stem.split("_", 1)[1])
            except (IndexError, ValueError):
                continue
            if index >= count:
                path.unlink(missing_ok=True)
        data = self.manifest()
        stale = [k for k in data["chunks"] if not k.isdigit() or int(k) >= count]
        if stale:
            for k in stale:
                data["chunks"].pop(k, None)
            self._write_manifest(data)

    def summary(self) -> dict:
        """Cheap on-disk state (no chunk plan): what the chapter column shows."""
        data = self.manifest()
        files = list(self.dir.glob("chunk_*.wav")) if self.dir.is_dir() else []
        ready = sum(1 for k in data["chunks"] if k.isdigit() and self.path(int(k)).is_file())
        frames = sum(int(e.get("frames") or 0) for k, e in data["chunks"].items()
                     if isinstance(e, dict) and k.isdigit() and self.path(int(k)).is_file())
        sample_rate = int(data.get("sample_rate") or 0)
        return {
            "max_chars": data.get("max_chars"),
            "chunks": ready,
            "files": len(files),
            "bytes": sum(p.stat().st_size for p in files),
            "duration": round(frames / sample_rate, 2) if sample_rate else 0,
            "engine": data.get("engine"),
            "voice": data.get("voice"),
        }


def remap_chapter_stores(book_id: int, mapping: dict[int, int]) -> int:
    """Sau khi mục lục bị đánh số lại: giữ kho chunk của chương cũ ``old`` dưới số mới
    ``mapping[old]``, xoá kho của mọi chương không có trong mapping (đã đổi/bị bỏ).
    Trả về số kho được giữ."""
    root = get_book_chapters_dir(book_id)
    if not root.is_dir():
        return 0
    staging = root.parent / f".chapters_remap_{uuid.uuid4().hex[:8]}"
    staging.mkdir()
    kept = 0
    for old, new in mapping.items():
        source = get_chapter_chunk_dir(book_id, old)
        if source.is_dir():
            os.replace(source, staging / f"{new + 1:04d}")
            kept += 1
    shutil.rmtree(root, ignore_errors=True)
    os.replace(staging, root)
    return kept


def chunk_audio_url(book_id: int, patch_id: int, chapter_index: int | None, index: int) -> str:
    """URL serving one stored chunk: the chapter endpoint for chapter chunks, the patch
    endpoint (patch chunk dir, numbered by plan position) for Text Studio chunks."""
    if chapter_index is None:
        return f"/books/{book_id}/patches/{patch_id}/chunk-audio/{index}"
    return f"/books/{book_id}/chapter-audio/{chapter_index}/chunks/{index}"


def resolve_plan_chunks(book_id: int, patch_dir: Path, plan: list[dict]) -> list[tuple[ChunkStore, int]]:
    """Where each item of a patch chunk plan lives: (store, index inside that store).

    Items carrying a ``chapter_index`` go to that chapter's store, numbered from 0
    within the chapter; items without one (Text Studio plan) go to ``patch_dir``
    numbered by their plan position, i.e. the historical chunk_NNN.wav naming."""
    stores: dict[int | None, ChunkStore] = {}
    counters: dict[int | None, int] = {}
    result = []
    for item in plan:
        chapter_index = item.get("chapter_index")
        if chapter_index not in stores:
            directory = patch_dir if chapter_index is None else get_chapter_chunk_dir(book_id, chapter_index)
            stores[chapter_index] = ChunkStore(directory)
            counters[chapter_index] = 0
        result.append((stores[chapter_index], counters[chapter_index]))
        counters[chapter_index] += 1
    return result
