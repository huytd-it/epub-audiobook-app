"""Classification vocabulary for voice reference clips.

Voices are tagged with one gender and any number of story genres, so the
library can be filtered down to "the male voices that suit tiên hiệp" instead
of scrolling a flat list. The vocabulary lives here rather than in the route or
the frontend so the validation and the pickers can't drift apart - the API
exposes it at GET /voices/taxonomy and the page renders whatever it returns.

Values stored in voice_meta are the slugs below; genres are stored as a
comma-separated slug list (a voice usually fits several genres).
"""
from __future__ import annotations

GENDERS: list[dict[str, str]] = [
    {"value": "male", "label": "Nam"},
    {"value": "female", "label": "Nữ"},
    {"value": "child", "label": "Trẻ em"},
    {"value": "neutral", "label": "Trung tính"},
]

GENRES: list[dict[str, str]] = [
    {"value": "tien-hiep", "label": "Tiên hiệp"},
    {"value": "huyen-huyen", "label": "Huyền huyễn"},
    {"value": "kiem-hiep", "label": "Kiếm hiệp"},
    {"value": "do-thi", "label": "Đô thị"},
    {"value": "ngon-tinh", "label": "Ngôn tình"},
    {"value": "trinh-tham", "label": "Trinh thám"},
    {"value": "kinh-di", "label": "Kinh dị"},
    {"value": "lich-su", "label": "Lịch sử - Quân sự"},
    {"value": "khoa-huyen", "label": "Khoa huyễn"},
    {"value": "phieu-luu", "label": "Phiêu lưu"},
    {"value": "tam-ly", "label": "Tâm lý - Đời thường"},
    {"value": "hoi-ky", "label": "Hồi ký - Tự truyện"},
    {"value": "thieu-nhi", "label": "Thiếu nhi"},
    {"value": "hoc-duong", "label": "Teen - Học đường"},
]

GENDER_VALUES = {item["value"] for item in GENDERS}
GENRE_VALUES = {item["value"] for item in GENRES}

GENDER_LABELS = {item["value"]: item["label"] for item in GENDERS}
GENRE_LABELS = {item["value"]: item["label"] for item in GENRES}


class InvalidTag(ValueError):
    """A gender/genre slug that is not part of the vocabulary."""


def normalize_gender(raw) -> str:
    """Return a stored gender value: a known slug, or '' for unclassified."""
    if raw is None:
        return ""
    value = str(raw).strip().lower()
    if not value:
        return ""
    if value not in GENDER_VALUES:
        raise InvalidTag(f"Giới tính giọng không hợp lệ: '{value}'")
    return value


def normalize_genres(raw) -> str:
    """Return the stored genre column: known slugs joined by ','.

    Accepts either a list of slugs or an already-joined string, so the same
    helper can validate a JSON body and a legacy form field.
    """
    if raw is None:
        return ""
    parts = raw if isinstance(raw, (list, tuple, set)) else str(raw).split(",")
    seen: list[str] = []
    for part in parts:
        value = str(part).strip().lower()
        if not value:
            continue
        if value not in GENRE_VALUES:
            raise InvalidTag(f"Thể loại truyện không hợp lệ: '{value}'")
        if value not in seen:
            seen.append(value)
    return ",".join(seen)


def split_genres(stored: str | None) -> list[str]:
    """Explode the stored genre column back into a slug list for API output."""
    if not stored:
        return []
    return [part for part in (item.strip() for item in stored.split(",")) if part]
