"""Keyword presets + TikTok hashtag mapping (per topics / book-specific)."""
from __future__ import annotations

# keyword -> {queries (Pexels/Pixabay), tiktok_hashtags, bilibili_keywords, notes}
KEYWORD_PRESETS: dict[str, dict] = {
    "nau-an": {
        "queries": ["cooking background", "asmr cooking loop", "food preparation"],
        "tiktok_hashtags": ["asmrcooking", "foodtok", "cookingtiktok"],
        "bilibili_keywords": ["美食制作", "cooking"],
        "notes": "Tránh clip có mặt người nói to / text cháy màn hình. Ưu tiên góc quay overhead, steam, chảo chiên.",
    },
    "minecraft": {
        "queries": ["minecraft gameplay background", "minecraft ambient loop"],
        "tiktok_hashtags": ["minecraft", "minecraftasmr", "minecraftbuild"],
        "bilibili_keywords": ["我的世界", "minecraft"],
        "notes": "Gameplay không có IP nhạc thương mại. Tránh clip có overlay HUD dày đặc.",
    },
    "thien-nhien": {
        "queries": ["forest rain loop", "ocean waves", "mountain timelapse"],
        "tiktok_hashtags": ["nature", "lofi", "rain", "asmr"],
        "bilibili_keywords": ["自然", "风景"],
        "notes": "Loại clip không có người nói, không nhạc EDM — giữ ambient tự nhiên.",
    },
    "vu-tru": {
        "queries": ["space nebula", "starry night sky", "nebula timelapse"],
        "tiktok_hashtags": ["space", "universe", "stars"],
        "bilibili_keywords": ["宇宙", "星空"],
        "notes": "Timelapse/nghiên cứu vũ trụ thường tối — tăng brightness khi render.",
    },
    "thanh-pho": {
        "queries": ["city night timelapse", "rainy city loop", "urban traffic"],
        "tiktok_hashtags": ["city", "nightcity", "rainycity"],
        "bilibili_keywords": ["城市夜景", "city"],
        "notes": "Rain/city night rất hợp sách tâm lý/hiện thực. Tránh clip có biển quảng cáo rõ chữ.",
    },
    "gameplay": {
        "queries": ["retro game loop", "arcade background", "pixel art"],
        "tiktok_hashtags": ["retrogaming", "pixelart", "gameplay"],
        "bilibili_keywords": ["复古游戏", "像素"],
        "notes": "Fallback chung cho gameplay ngoài Minecraft.",
    },
}


def get_preset(keyword: str) -> dict:
    return KEYWORD_PRESETS.get(keyword, {})


def book_keyword(book_title: str = "", author: str = "", subjects: str = "") -> str:
    """Map EPUB metadata -> keyword slug. Keep generic to avoid over-filtering."""
    text = f"{book_title} {author} {subjects}".lower()
    if any(k in text for k in ("cooking", "cuisine", "recipe", "ẩm thực", "nấu", "món ăn", "bếp")):
        return "nau-an"
    if any(k in text for k in ("minecraft", "game", "gaming", "trò chơi")):
        return "minecraft"
    if any(k in text for k in ("space", "universe", "star", "vũ trụ", "vũ trụ", "khám phá")):
        return "vu-tru"
    if any(k in text for k in ("nature", "forest", "ocean", "thiên nhiên", "rừng", "biển")):
        return "thien-nhien"
    if any(k in text for k in ("city", "urban", "thành phố", "đô thị")):
        return "thanh-pho"
    return "thien-nhien"


def custom_book_keyword(name: str) -> str:
    slug = "".join(c if c.isalnum() or c in "-_" else "-" for c in name.strip().lower())
    slug = "-".join(part for part in slug.split("-") if part)
    return f"theo-sach:{slug}" if slug else "theo-sach:custom"
