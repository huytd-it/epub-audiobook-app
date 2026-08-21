"""YouTube metadata configuration, validation, and resolution."""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
import string

import soundfile as sf

DEFAULT_TITLE_TEMPLATE = "{book_title} - Tập {episode_number} - Chương {chapter_start}-{chapter_end}: {patch_name} | {genre_tags}"
# Older builds shipped (and saved into book configs) an unaccented default template.
_LEGACY_TITLE_TEMPLATES = {
    "{book_title} - Tap {episode_number} - Chuong {chapter_start}-{chapter_end}: {patch_name} | {genre_tags}",
}
ALLOWED_FIELDS = {"book_title", "episode_number", "chapter_start", "chapter_end", "patch_name", "genre_tags"}
YOUTUBE_TITLE_LIMIT = 100
YOUTUBE_DESCRIPTION_LIMIT = 5000
CHAPTER_SECTION_HEADING = "📖 Nội dung:"
# Tên chương cho đoạn intro chèn trước nội dung patch trong video.
INTRO_TIMELINE_TITLE = "Giới thiệu"
OVERRIDE_FIELDS = {"title", "description", "genre_tags", "tags", "privacy_status", "playlist"}

# --- Playlist link -------------------------------------------------------------
# Every upload that lands in a playlist advertises that playlist in its own
# description: the link is the only way a listener can follow the whole book
# from the video page, so it ships even when the author wrote the description
# themselves.
PLAYLIST_URL_PREFIX = "https://www.youtube.com/playlist?list="
PLAYLIST_LINK_LABEL = "▶ Nghe trọn bộ (playlist):"
PLAYLIST_FOLLOW_LINE = "🔔 Mở playlist và bấm Lưu/Theo dõi để nhận tập mới."

# --- Extended description ("nội dung mở rộng") ---------------------------------
# A boilerplate block appended after the description (and the timeline, when it is
# shown). It exists so every upload carries the same copyright / AI / fiction
# notices. The wording lives in ``template``; the values that change per ebook are
# the placeholders below, filled from the sibling keys. A line whose placeholder
# resolves to an empty string is dropped, so a half-filled notice never ships.
EXTRA_PLACEHOLDERS = ("contact_email", "story_title", "story_source_name", "story_source_url", "fair_use_url")

DEFAULT_DESCRIPTION_EXTRA_TEMPLATE = """✧ For any copyright-related issues regarding images or videos, please contact me via email: {contact_email}
⚠ This story is created by the author with the support of AI. It uses an AI-generated voice and edited illustrative images solely for content delivery purposes.
⚠ Nội dung câu chuyện trong video hoàn toàn hư cấu, chỉ mang tính giải trí, không dựa trên bất kỳ sự kiện hay nhân vật có thật; bất kỳ sự trùng hợp nào với thực tế chỉ là ngẫu nhiên.
⚠ Nội dung không phù hợp với trẻ em dưới 13 tuổi nếu không có sự hướng dẫn của người lớn.
⚠ Đây là video game do tôi tự chơi và quay, thêm vào đó là các hình ảnh được tôi tạo ra và được lồng ghép với âm thanh kể truyện, nhằm tạo ra một trải nghiệm giải trí vui vẻ, giúp người nghe thư giãn.
Gameplay nền có tốc độ chậm, nhẹ nhàng, giúp não bớt phân tán để mọi người có thể nghe ở trạng thái nửa tập trung - nửa thư giãn.
Story source: {story_source_name} ({story_source_url})
Tên Truyện: {story_title}
I do not own all the materials used in this video and comply with copyright law and the Fair Use doctrine. ({fair_use_url})"""

DEFAULT_DESCRIPTION_EXTRA = {
    "enabled": False,
    "contact_email": "",
    "story_title": "",
    "story_source_name": "",
    "story_source_url": "",
    "fair_use_url": "https://www.youtube.com/howyoutubeworks/policies/copyright/",
    "template": DEFAULT_DESCRIPTION_EXTRA_TEMPLATE,
}

DEFAULT_BOOK_YOUTUBE_CONFIG = {"auto_upload": False, "title_template": DEFAULT_TITLE_TEMPLATE, "description": "", "genre_tags": "", "privacy_status": "private", "timeline_enabled": True, "description_extra": DEFAULT_DESCRIPTION_EXTRA, "playlist": {"mode": "none", "playlist_id": "", "title_template": "{book_title}", "description_template": ""}}


def load_timeline(audio_path) -> dict | None:
    """Load a version-1 timeline whose exact frame metadata matches its WAV."""
    try:
        info = sf.info(str(audio_path))
        timeline = json.loads(Path(audio_path).with_suffix(".timeline.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError, sf.SoundFileError):
        return None
    return validate_timeline(timeline, info.samplerate, info.frames)


def validate_timeline(timeline, samplerate: int, frames: int) -> dict | None:
    """The timeline itself, or None unless it describes exactly this audio.

    Split out of load_timeline so a timeline that arrives on its own - uploaded
    straight from a batch's result/ folder - is held to the same rule as one read
    from disk beside its WAV."""
    try:
        if not isinstance(timeline, dict) or timeline.get("version") != 1:
            return None
        rate, total = timeline["sample_rate"], timeline["total_frames"]
        if isinstance(rate, bool) or not isinstance(rate, int) or rate <= 0 or rate != samplerate:
            return None
        if isinstance(total, bool) or not isinstance(total, int) or total < 0 or total != frames:
            return None
        chapters = timeline["chapters"]
        if not isinstance(chapters, list) or not chapters:
            return None
        starts = []
        for index, chapter in enumerate(chapters):
            if set(chapter) != {"chapter_index", "title", "start_frame", "start_seconds"}:
                return None
            chapter_index = chapter["chapter_index"]
            start, seconds, title = chapter["start_frame"], chapter["start_seconds"], chapter["title"]
            if (isinstance(chapter_index, bool) or not isinstance(chapter_index, int) or
                    (index and chapter_index <= chapters[index - 1]["chapter_index"])):
                return None
            if (isinstance(start, bool) or not isinstance(start, int) or not 0 <= start <= total or
                    isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or
                    not math.isclose(seconds, start / rate, rel_tol=0, abs_tol=1e-9) or
                    not isinstance(title, str) or not title.strip()):
                return None
            starts.append(start)
        if starts[0] != 0 or any(b <= a for a, b in zip(starts, starts[1:])):
            return None
        return timeline
    except (TypeError, ValueError, KeyError):
        return None


_CHAPTER_HEADING_RE = re.compile(
    r"^\s*(?:chương|chuong|chapter|hồi|hoi|quyển|quyen|phần|phan)\s*0*(\d+)\s*[:.\-–—]*\s*",
    re.IGNORECASE,
)


def detect_chapter_number(title) -> int | None:
    """Real chapter number parsed from a heading like "Chương 12: Tên"."""
    match = _CHAPTER_HEADING_RE.match(title or "")
    return int(match.group(1)) if match else None


def strip_chapter_heading(title) -> str:
    """Chapter name with the "Chương N" prefix removed ("Chương 1: Mưa" -> "Mưa")."""
    title = (title or "").strip()
    match = _CHAPTER_HEADING_RE.match(title)
    return title[match.end():].strip() if match else title


def resolve_patch_chapter_range(patch) -> tuple[int, int, str]:
    """Detect the real chapter numbers a patch covers, plus its clean name.

    ``patch.chapter_start``/``chapter_end`` are 0-based DB indexes that count
    front matter (cover, "Mục lục", ...), so showing them verbatim mislabels
    the video. The audio's timeline sidecar lists the chapter titles actually
    spoken, so numbers parsed from those titles win; without a timeline the
    number in the patch's first-chapter title anchors the range. Raw indexes
    remain the last resort for books whose headings carry no numbers.
    """
    name = strip_chapter_heading(getattr(patch, "name", "") or "")
    audio_path = getattr(patch, "audio_path", None)
    timeline = load_timeline(audio_path) if audio_path else None
    if timeline:
        numbers = [n for n in (detect_chapter_number(ch["title"]) for ch in timeline["chapters"]) if n is not None]
        if numbers:
            return numbers[0], numbers[-1], name
    semantic_start = getattr(patch, "chapter_no_start", None)
    semantic_end = getattr(patch, "chapter_no_end", None)
    if semantic_start is not None:
        return int(semantic_start), int(semantic_end if semantic_end is not None else semantic_start), name
    start = detect_chapter_number(getattr(patch, "name", ""))
    if start is not None:
        return start, start + max(patch.chapter_end - patch.chapter_start, 0), name
    return patch.chapter_start, patch.chapter_end, name


def format_chapter_range(start: int, end: int) -> str:
    return f"Chương {start}" if start == end else f"Chương {start}-{end}"


def split_tags(value: str) -> list[str]:
    return list(dict.fromkeys(part.strip() for part in value.split(",") if part.strip()))


def _template_without_empty_optional_parts(template: str, patch_name: str, genre_text: str) -> str:
    if not patch_name:
        template = template.replace(": {patch_name}", "").replace("{patch_name}: ", "")
    if not genre_text:
        template = template.replace(" | {genre_tags}", "").replace("{genre_tags} | ", "")
    return template


def _validate_title_template(template: str) -> None:
    base = template
    if base.endswith(" | {genre_tags}"):
        base = base[:-len(" | {genre_tags}")]
    if base.endswith(": {patch_name}"):
        base = base[:-len(": {patch_name}")]
    if "{patch_name}" in base or "{genre_tags}" in base or base.rstrip()[-1:] in "-:|/":
        raise ValueError("optional title fields must be trailing suffixes")


def _validate_template(template: str, label: str) -> None:
    try:
        for _, name, _, _ in string.Formatter().parse(template):
            if name and name not in ALLOWED_FIELDS:
                raise ValueError(f"unknown {label} field: {name}")
    except (ValueError, TypeError) as exc:
        raise ValueError(f"invalid {label} template") from exc


def _json_object(value, default):
    try:
        parsed = json.loads(value or "{}") if isinstance(value, str) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        return default.copy()
    return parsed.copy() if isinstance(parsed, dict) else default.copy()


def validate_description_extra(extra) -> dict:
    """Normalize the extended description block (see DEFAULT_DESCRIPTION_EXTRA)."""
    if extra is None:
        extra = {}
    if not isinstance(extra, dict):
        raise ValueError("description_extra must be an object")
    result = {**DEFAULT_DESCRIPTION_EXTRA, **extra}
    if not isinstance(result["enabled"], bool):
        raise ValueError("description_extra.enabled must be a boolean")
    for key in (*EXTRA_PLACEHOLDERS, "template"):
        if not isinstance(result[key], str):
            raise ValueError(f"description_extra.{key} must be a string")
        result[key] = result[key].strip() if key != "template" else result[key]
    if len(result["template"]) > YOUTUBE_DESCRIPTION_LIMIT:
        raise ValueError("description_extra template exceeds 5000 characters")
    return {key: result[key] for key in DEFAULT_DESCRIPTION_EXTRA}


def render_description_extra(extra) -> str:
    """The extended block with its placeholders filled in.

    Placeholders are substituted literally (never through ``str.format``) so the
    author's own braces cannot raise. A line that needs a value the author left
    blank is dropped rather than shipped with an empty tail.
    """
    extra = validate_description_extra(extra)
    if not extra["enabled"]:
        return ""
    lines = []
    for line in extra["template"].splitlines():
        used = [key for key in EXTRA_PLACEHOLDERS if "{" + key + "}" in line]
        if used and not all(extra[key] for key in used):
            continue
        for key in used:
            line = line.replace("{" + key + "}", extra[key])
        lines.append(line)
    return "\n".join(lines).strip()


def validate_book_youtube_config(config: dict) -> dict:
    if not isinstance(config, dict):
        raise ValueError("config must be an object")
    result = {**DEFAULT_BOOK_YOUTUBE_CONFIG, **config}
    if result.get("title_template") in _LEGACY_TITLE_TEMPLATES:
        result["title_template"] = DEFAULT_TITLE_TEMPLATE
    if not isinstance(result["auto_upload"], bool):
        raise ValueError("auto_upload must be a boolean")
    if not isinstance(result["title_template"], str):
        raise ValueError("title template must be a string")
    _validate_template(result["title_template"], "title")
    _validate_template(result["description"], "description")
    template = result["title_template"]
    _validate_title_template(template)
    if not isinstance(result["description"], str) or not isinstance(result["genre_tags"], str):
        raise ValueError("description and genre_tags must be strings")
    if result["privacy_status"] not in {"private", "unlisted", "public"}:
        raise ValueError("invalid privacy status")
    if not isinstance(result["timeline_enabled"], bool):
        raise ValueError("timeline_enabled must be a boolean")
    result["description_extra"] = validate_description_extra(result["description_extra"])
    if not isinstance(result["playlist"], dict):
        raise ValueError("playlist must be an object")
    playlist = {**DEFAULT_BOOK_YOUTUBE_CONFIG["playlist"], **result["playlist"]}
    if playlist["mode"] == "create":
        playlist = {**DEFAULT_BOOK_YOUTUBE_CONFIG["playlist"], "mode": "none"}
    if playlist["mode"] not in {"none", "existing"} or not isinstance(playlist["playlist_id"], str):
        raise ValueError("invalid playlist")
    if playlist["mode"] == "existing" and not playlist["playlist_id"]:
        raise ValueError("existing playlist id is required")
    if len(result["description"]) > 5000:
        raise ValueError("description exceeds 5000 characters")
    result["playlist"] = playlist
    return result


def _clean_title(title: str) -> str:
    title = re.sub(r"\s*(?:\||:)\s*(?=-|$)", "", title)
    title = re.sub(r"\s*-\s*(?=-|$)", "", title)
    return re.sub(r"\s+", " ", title).strip(" -:|")


def _fit_title(title: str, genre_text: str, limit: int = YOUTUBE_TITLE_LIMIT) -> str:
    """Shrink an auto-generated title to YouTube's cap, dropping the least
    useful part first.

    A Vietnamese book title plus a chapter name routinely runs past 100
    characters, so composing without a length budget made the whole patch
    unpublishable. The genre suffix goes first - it is duplicated in `tags`
    anyway - and only then is the remaining text cut short.
    """
    if len(title) <= limit:
        return title
    suffix = f" | {genre_text}"
    if genre_text and title.endswith(suffix):
        title = title[: -len(suffix)]
        if len(title) <= limit:
            return title
    return title[: limit - 1].rstrip(" -:|") + "…"


def _validate_override(override: dict) -> dict:
    if not isinstance(override, dict) or any(k not in OVERRIDE_FIELDS for k in override):
        raise ValueError("invalid override field")
    for key in ("title", "description", "genre_tags"):
        if key in override and not isinstance(override[key], str):
            raise ValueError(f"{key} must be a string")
    if "tags" in override and (not isinstance(override["tags"], (str, list)) or (isinstance(override["tags"], list) and not all(isinstance(v, str) for v in override["tags"]))):
        raise ValueError("tags must be a string or list of strings")
    if "privacy_status" in override and override["privacy_status"] not in {"private", "unlisted", "public"}:
        raise ValueError("invalid privacy status")
    if "playlist" in override:
        playlist = override["playlist"]
        if not isinstance(playlist, dict) or playlist.get("mode", "none") not in {"none", "existing"} or not isinstance(playlist.get("playlist_id", ""), str):
            raise ValueError("invalid playlist")
    return override


_HASHTAG_SPLIT_RE = re.compile(r"\W+")


def _hashtag(text: str) -> str:
    words = [word for word in _HASHTAG_SPLIT_RE.split(text or "") if word]
    return "#" + "".join(word[0].upper() + word[1:] for word in words) if words else ""


def _hashtags(book_title: str, genre_text: str) -> str:
    """Hashtags from the book's series name and its genres.

    Everything after the first " - " in a book title is edition noise
    ("... - Tập 1 - Long Phi"), so only the series name becomes a tag.
    """
    sources = [(book_title or "").split(" - ")[0], *split_tags(genre_text), "audiobook"]
    tags: list[str] = []
    for source in sources:
        tag = _hashtag(source)
        if tag and tag not in tags:
            tags.append(tag)
    return " ".join(tags[:8])


def _chapter_lines(chapter_titles, timeline: str) -> list[str]:
    """Chapter list for the description: timestamped when the audio has a timeline."""
    if timeline:
        return timeline.split("\n")
    if not isinstance(chapter_titles, (list, tuple)):
        return []
    return [title.strip() for title in chapter_titles if isinstance(title, str) and title.strip()]


def playlist_url(playlist_id) -> str:
    """Watch-page URL of a playlist, or "" when there is no playlist to link."""
    playlist_id = (playlist_id or "").strip() if isinstance(playlist_id, str) else ""
    return f"{PLAYLIST_URL_PREFIX}{playlist_id}" if playlist_id else ""


def _playlist_lines(playlist) -> list[str]:
    """The "follow the whole book" block, empty unless a playlist is configured."""
    if not isinstance(playlist, dict) or playlist.get("mode") != "existing":
        return []
    url = playlist_url(playlist.get("playlist_id"))
    return [f"{PLAYLIST_LINK_LABEL} {url}", PLAYLIST_FOLLOW_LINE] if url else []


def _music_lines(music) -> list[str]:
    if not isinstance(music, dict) or not (music.get("name") or "").strip():
        return []
    lines = [f"🎵 Nhạc nền: {music['name'].strip()}"]
    lines += [text.strip() for text in (music.get("description"), music.get("license")) if (text or "").strip()]
    return lines


def _fit_description(blocks: list[list[str]], limit: int = YOUTUBE_DESCRIPTION_LIMIT) -> str:
    """Join the description blocks, shortening the chapter list until it fits.

    The chapter list is the only unbounded section - a book split into few, long
    patches can carry hundreds of chapters - so it is what gets cut, never the
    music credits, which exist to satisfy the track's licence.
    """
    blocks = [list(block) for block in blocks if block]

    def render() -> str:
        return "\n\n".join("\n".join(block) for block in blocks)

    if len(render()) <= limit:
        return render()
    chapters = next((block for block in blocks if block[0] == CHAPTER_SECTION_HEADING), None)
    if chapters is not None:
        chapters.append("…")
        while len(render()) > limit and len(chapters) > 2:
            del chapters[-2]
    text = render()
    return text if len(text) <= limit else text[:limit].rstrip()


def _default_description(values: dict, chapter_titles, music, timeline: str, extra: str = "", playlist_lines=()) -> str:
    """Fallback description so videos never upload with an empty one."""
    line = f"Tập {values['episode_number']} - {format_chapter_range(values['chapter_start'], values['chapter_end'])}"
    if values["patch_name"]:
        line += f": {values['patch_name']}"
    header = [values["book_title"], line]
    if values["genre_tags"]:
        header.append(f"Thể loại: {values['genre_tags']}")
    chapters = _chapter_lines(chapter_titles, timeline)
    return _fit_description([
        header,
        list(playlist_lines),
        [CHAPTER_SECTION_HEADING, *chapters] if chapters else [],
        _music_lines(music),
        extra.split("\n") if extra else [],
        [_hashtags(values["book_title"], values["genre_tags"])],
    ])


def audio_duration_seconds(path) -> float:
    """Độ dài file audio theo giây, 0.0 nếu không đọc được."""
    try:
        info = sf.info(str(path))
        return info.frames / info.samplerate if info.samplerate else 0.0
    except (OSError, TypeError, ValueError, RuntimeError, sf.SoundFileError):
        return 0.0


def _timeline_description(patch, intro_seconds: float = 0.0) -> str:
    """Các dòng "mm:ss Tên chương" cho description, tính theo timeline của *video*.

    Sidecar timeline mô tả file WAV, nhưng video phát intro trước nội dung đó, nên
    mọi mốc phải dời đi đúng độ dài intro. YouTube chỉ dựng chương khi mốc đầu tiên
    là 0:00 và mỗi chương dài tối thiểu 10 giây: intro đủ 10 giây thì thành chương
    "Giới thiệu" mở màn, intro ngắn hơn không đủ làm một chương nên chương đầu giữ
    0:00 và ôm luôn phần intro.
    """
    audio_path = getattr(patch, "audio_path", None)
    if not audio_path:
        return ""
    try:
        timeline = load_timeline(audio_path)
        if timeline is None:
            return ""
        sample_rate = timeline["sample_rate"]
        total_frames = timeline["total_frames"]
        chapters = timeline["chapters"]
        starts = []
        titles = []
        for chapter in chapters:
            start = chapter["start_frame"]
            start_seconds = chapter["start_seconds"]
            title = chapter["title"]
            starts.append(start)
            titles.append(title.strip())
        if len(starts) < 3 or any(b - a < sample_rate * 10 for a, b in zip(starts, starts[1:])) or total_frames - starts[-1] < sample_rate * 10:
            return ""
        offset = max(round((intro_seconds or 0) * sample_rate), 0)
        entries = [(start + offset, title) for start, title in zip(starts, titles)]
        if offset >= sample_rate * 10:
            entries.insert(0, (0, INTRO_TIMELINE_TITLE))
        else:
            entries[0] = (0, entries[0][1])

        def format_time(frame):
            seconds = frame // sample_rate
            minutes, seconds = divmod(seconds, 60)
            hours, minutes = divmod(minutes, 60)
            return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"

        return "\n".join(f"{format_time(start)} {title}" for start, title in entries)
    except (OSError, TypeError, ValueError, UnicodeDecodeError, KeyError, json.JSONDecodeError, sf.SoundFileError):
        return ""


def resolve_patch_youtube_metadata(book, patch, override: dict | None, context: dict | None = None, config: dict | None = None) -> dict:
    raw = _json_object(book.automation_config, {})
    config = validate_book_youtube_config(config if config is not None else raw.get("youtube", {}))
    override = _validate_override({k: v for k, v in _json_object(override, {}).items() if k in OVERRIDE_FIELDS})
    genre_value = override.get("genre_tags") or override.get("tags") or config["genre_tags"]
    if isinstance(genre_value, list):
        genre_value = ",".join(genre_value)
    if not isinstance(genre_value, str):
        raise ValueError("genre_tags override must be a string")
    genre_text = ", ".join(split_tags(genre_value))
    chapter_start, chapter_end, patch_name = resolve_patch_chapter_range(patch)
    values = {"book_title": book.title, "episode_number": patch.patch_index + 1, "chapter_start": chapter_start, "chapter_end": chapter_end, "patch_name": patch_name, "genre_tags": genre_text}
    try:
        if config["title_template"] == DEFAULT_TITLE_TEMPLATE:
            title = f"{book.title} - Tập {values['episode_number']} - {format_chapter_range(chapter_start, chapter_end)}"
            if values["patch_name"]:
                title += f": {values['patch_name']}"
            if genre_text:
                title += f" | {genre_text}"
        else:
            template = config["title_template"]
            if not values["patch_name"]:
                template = template.replace(": {patch_name}", "")
            if not genre_text:
                template = template.replace(" | {genre_tags}", "")
            title = _clean_title(template.format(**values))
    except (KeyError, ValueError, IndexError) as exc:
        raise ValueError("invalid title template") from exc
    explicit_title = bool(override.get("title"))
    title = override.get("title") or title
    title = _clean_title(title) if not explicit_title else title.strip()
    if genre_text and not explicit_title:
        suffix = f" | {genre_text}"
        if not title.endswith(suffix):
            title = title.rstrip(" |:") + suffix
    description_template = override.get("description") or config["description"]
    try:
        description = description_template.format(**values)
    except (KeyError, ValueError, IndexError) as exc:
        raise ValueError("invalid description template") from exc
    context = context if isinstance(context, dict) else {}
    timeline = _timeline_description(patch, context.get("intro_seconds") or 0) if config["timeline_enabled"] else ""
    extra = render_description_extra(config["description_extra"])
    playlist = {**config["playlist"], **(override.get("playlist") or {})}
    playlist = validate_book_youtube_config({**config, "playlist": playlist})["playlist"]
    playlist_block = _playlist_lines(playlist)
    if not description.strip():
        # The generated description already places the timeline inside its chapter
        # list, so only an author-written description gets the block appended.
        description = _default_description(
            values, context.get("chapter_titles"), context.get("music"), timeline, extra, playlist_block)
    else:
        # An author-written description keeps its own opening; the playlist link is
        # appended right below it, ahead of the timeline, unless it is already there.
        if playlist_block and playlist_url(playlist["playlist_id"]) not in description:
            candidate = f"{description.rstrip()}\n\n" + "\n".join(playlist_block)
            if len(candidate) <= YOUTUBE_DESCRIPTION_LIMIT:
                description = candidate
        if timeline:
            if description.rstrip() == timeline or description.endswith(f"\n\n{timeline}"):
                candidate = description
            else:
                candidate = f"{description}\n\n{timeline}" if description else timeline
            if len(candidate) <= YOUTUBE_DESCRIPTION_LIMIT:
                description = candidate
        if extra and not description.rstrip().endswith(extra):
            candidate = f"{description.rstrip()}\n\n{extra}" if description.strip() else extra
            if len(candidate) <= YOUTUBE_DESCRIPTION_LIMIT:
                description = candidate
    if not explicit_title:
        title = _fit_title(title, genre_text)
    privacy = override.get("privacy_status") or config["privacy_status"]
    if not isinstance(title, str) or not title or len(title) > YOUTUBE_TITLE_LIMIT:
        raise ValueError("title must be 1-100 characters")
    if not isinstance(description, str) or len(description) > 5000:
        raise ValueError("description exceeds 5000 characters")
    if privacy not in {"private", "unlisted", "public"}:
        raise ValueError("invalid privacy status")
    return {"title": title, "description": description, "tags": split_tags(genre_value), "privacy_status": privacy, "youtube": playlist}


def get_book_youtube_config(conn, book_id: int) -> dict:
    row = conn.execute("SELECT automation_config FROM book WHERE id = ?", (book_id,)).fetchone()
    return validate_book_youtube_config(_json_object((row[0] if row else None), {}).get("youtube", {}))


def save_book_youtube_config(conn, book_id: int, config: dict) -> None:
    validated = validate_book_youtube_config(config)
    row = conn.execute("SELECT automation_config FROM book WHERE id = ?", (book_id,)).fetchone()
    raw = _json_object(row[0] if row else None, {})
    raw["youtube"] = validated
    conn.execute("UPDATE book SET automation_config = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (json.dumps(raw), book_id))
    conn.commit()
    from app.production_defaults import set_book_group_mode_db
    set_book_group_mode_db(conn, book_id, "youtube", "custom")


def get_patch_youtube_override(conn, patch_id: int) -> dict:
    row = conn.execute("SELECT youtube_override FROM patch WHERE id = ?", (patch_id,)).fetchone()
    return _json_object(row[0] if row else None, {})


def save_patch_youtube_override(conn, patch_id: int, override: dict) -> None:
    _validate_override(override)
    normalized = {key: value for key, value in override.items() if value != ""}
    conn.execute("UPDATE patch SET youtube_override = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (json.dumps(normalized), patch_id))
    conn.commit()
