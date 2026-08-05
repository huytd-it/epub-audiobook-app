"""Pillow-based text overlay renderer for patch background images.

Renders the overlay text (book title + patch name) onto a background image
and saves it as PNG. The resulting image is used as-is for video generation
(no drawtext in FFmpeg).

Config is a per-book dict (book.overlay_config, JSON) controlling position,
alignment, font, text color, background box, and shadow.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import settings
from app.models import Book, Patch

logger = logging.getLogger(__name__)


# Default config — also used when book.overlay_config is missing/empty.
DEFAULT_OVERLAY_CONFIG: dict[str, Any] = {
    "text": "",                # empty = book title + patch name
    "position": "top",        # top | center | bottom
    "alignment": "center",    # left | center | right
    "font_size": 52,
    "font_path": "",          # empty = use fallback chain
    "text_color": "#FFFFFF",
    "shadow": {
        "enabled": True,
        "color": "#000000",
        "offset": 3,
    },
    "box": {
        "enabled": False,
        "color": "#000000",
        "opacity": 60,        # 0–100
        "padding_x": 24,
        "padding_y": 12,
        "radius": 12,         # rounded corners in px
    },

    "margin": 40,             # distance from image edge
    "offset_x": 0,            # px nudge applied after anchoring (drag-to-position)
    "offset_y": 0,
    "overlays": [],
}


def get_default_overlay_config() -> dict[str, Any]:
    from copy import deepcopy
    return deepcopy(DEFAULT_OVERLAY_CONFIG)


def parse_overlay_config(raw: str | None) -> dict[str, Any]:
    """Parse a book's overlay_config JSON, falling back to defaults on error."""
    if not raw:
        return get_default_overlay_config()
    try:
        cfg = json.loads(raw)
    except (TypeError, ValueError) as exc:
        logger.warning("image_overlay: invalid overlay_config JSON: %s — using defaults", exc)
        return get_default_overlay_config()
    merged = get_default_overlay_config()
    for key, val in cfg.items():
        if isinstance(val, dict) and isinstance(merged.get(key), dict):
            merged[key].update(val)
        else:
            merged[key] = val
    if not merged.get("overlays"):
        merged["overlays"] = [{key: value for key, value in merged.items() if key != "overlays"}]
    return merged


def list_overlay_fonts() -> list[dict[str, str]]:
    """Return usable, server-local fonts exposed to the overlay editor."""
    seen: set[str] = set()
    fonts: list[dict[str, str]] = []
    candidates = ([settings.default_font_path] if settings.default_font_path else []) + _FONT_PATHS
    for raw in candidates:
        path = Path(raw)
        key = str(path).lower()
        if key in seen or not path.is_file():
            continue
        seen.add(key)
        fonts.append({"name": path.stem, "path": str(path)})
    return fonts


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    from PIL import ImageColor
    return ImageColor.getrgb(hex_color or "#FFFFFF")[:3]


def get_patch_overlay_path(book_id: int, patch_id: int) -> Path:
    return Path(settings.data_root) / "books" / str(book_id) / "patch_overlays" / f"{patch_id}.png"


def _resolve_background(book: Book, background_path: str | None = None) -> Path | None:
    if background_path and Path(background_path).exists():
        return Path(background_path)
    if book.background_image_path and Path(book.background_image_path).exists():
        return Path(book.background_image_path)
    default = Path(settings.default_background_image)
    if default.exists():
        return default
    return None


_VIETNAMESE_TEST = "âêôưỢĐáàảãạéèẻẽẹíìỉĩịóòỏõọúùủũụýỳỷỹỵ"

_FONT_PATHS: list[str] = [
    "C:/Windows/Fonts/SEGOEUI.TTF",
    "C:/Windows/Fonts/segoeui.ttf",
    "C:/Windows/Fonts/ARIAL.TTF",
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/TIMES.TTF",
    "C:/Windows/Fonts/times.ttf",
    "C:/Windows/Fonts/TAHOMA.TTF",
    "C:/Windows/Fonts/tahoma.ttf",
    str(Path(__file__).parent.parent / "assets" / "fonts" / "NotoSansVietnamese.ttf"),
    str(Path(__file__).parent.parent / "assets" / "fonts" / "NotoSansVietnamese-Regular.ttf"),
]


def _validate_vietnamese(font, test_chars: str = _VIETNAMESE_TEST) -> bool:
    from PIL import Image, ImageDraw
    try:
        img_ref = Image.new("RGB", (80, 80))
        draw_ref = ImageDraw.Draw(img_ref)
        draw_ref.text((0, 0), "\uFFFD", font=font, fill=(255, 255, 255))
        tofu_pixels = set(img_ref.getdata())
        for ch in test_chars:
            img = Image.new("RGB", (80, 80))
            draw = ImageDraw.Draw(img)
            draw.text((0, 0), ch, font=font, fill=(255, 255, 255))
            if set(img.getdata()) == tofu_pixels:
                return False
        return True
    except Exception:
        return False


def _try_load_font(path: str, size: int):
    from PIL import ImageFont
    try:
        font = ImageFont.truetype(path, size)
        if _validate_vietnamese(font):
            return font
        logger.warning("image_overlay: font at %s loaded but missing Vietnamese glyphs", path)
    except Exception as exc:
        logger.debug("image_overlay: cannot load font %s: %s", path, exc)
    return None


def _load_font(font_path: str | None, size: int):
    from PIL import ImageFont
    candidates: list[str] = []
    if font_path:
        candidates.append(font_path)
    candidates.extend(_FONT_PATHS)
    for fp in candidates:
        if not fp or not Path(fp).exists():
            continue
        font = _try_load_font(fp, size)
        if font:
            return font
    logger.error("image_overlay: no usable font found — Vietnamese characters may render as boxes")
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _wrap_lines(draw, text: str, font, max_width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        test = f"{current} {word}".strip()
        try:
            tw = draw.textlength(test, font=font)
        except AttributeError:
            tw = draw.textbbox((0, 0), test, font=font)[2]
        if tw <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [text]


def _measure_lines(draw, lines: list[str], font) -> tuple[int, int]:
    """Return (max_line_width, total_height) for the given lines."""
    max_w = 0
    for line in lines:
        try:
            w = draw.textlength(line, font=font)
        except AttributeError:
            w = draw.textbbox((0, 0), line, font=font)[2]
        if w > max_w:
            max_w = w
    try:
        line_height = draw.textbbox((0, 0), "Ag", font=font)[3] + 8
    except AttributeError:
        line_height = 60
    return int(max_w), len(lines) * line_height


def _draw_with_shadow(draw, xy: tuple[float, float], text: str, font, fill, shadow_cfg: dict):
    if shadow_cfg.get("enabled", True):
        color = _hex_to_rgb(shadow_cfg.get("color", "#000000"))
        offset = int(shadow_cfg.get("offset", 3))
        for dx in range(-offset, offset + 1):
            for dy in range(-offset, offset + 1):
                if dx != 0 or dy != 0:
                    draw.text((xy[0] + dx, xy[1] + dy), text, font=font, fill=color)
    draw.text(xy, text, font=font, fill=fill)


def _draw_text_block(img, draw, lines: list[str], cfg: dict, text_color) -> tuple[int, int, int, int]:
    """Draw the text block and return its rect (x, y, w, h) in image pixels."""
    width, height = img.size
    font_size = int(cfg.get("font_size", 52))
    font = _load_font(cfg.get("font_path") or settings.default_font_path or None, font_size)

    margin = int(cfg.get("margin", 40))
    max_w, block_h = _measure_lines(draw, lines, font)

    box_cfg = cfg.get("box", {})
    pad_x = int(box_cfg.get("padding_x", 24)) if box_cfg.get("enabled") else 0
    pad_y = int(box_cfg.get("padding_y", 12)) if box_cfg.get("enabled") else 0
    box_w = max_w + pad_x * 2
    box_h = block_h + pad_y * 2

    position = cfg.get("position", "top")
    alignment = cfg.get("alignment", "center")

    if position == "top":
        y0 = margin
    elif position == "bottom":
        y0 = height - margin - box_h
    else:  # center
        y0 = (height - box_h) // 2

    if alignment == "left":
        x0 = margin
    elif alignment == "right":
        x0 = width - margin - box_w
    else:  # center
        x0 = (width - box_w) // 2

    x0 += int(cfg.get("offset_x", 0) or 0)
    y0 += int(cfg.get("offset_y", 0) or 0)
    # Keep the block inside the image so a stale drag offset can't push it off-frame.
    x0 = max(0, min(width - box_w, x0))
    y0 = max(0, min(height - box_h, y0))

    if box_cfg.get("enabled"):
        from PIL import Image, ImageDraw
        box_color = _hex_to_rgb(box_cfg.get("color", "#000000"))
        opacity = max(0, min(100, int(box_cfg.get("opacity", 60)))) / 100.0
        radius = int(box_cfg.get("radius", 0))
        layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ldraw = ImageDraw.Draw(layer)
        if radius > 0:
            ldraw.rounded_rectangle(
                (x0, y0, x0 + box_w, y0 + box_h),
                radius=radius,
                fill=(*box_color, int(255 * opacity)),
            )
        else:
            ldraw.rectangle(
                (x0, y0, x0 + box_w, y0 + box_h),
                fill=(*box_color, int(255 * opacity)),
            )
        img.paste(layer, (0, 0), layer)
        draw = ImageDraw.Draw(img)

    try:
        line_height = draw.textbbox((0, 0), "Ag", font=font)[3] + 8
    except AttributeError:
        line_height = font_size + 8

    y = y0 + pad_y
    for line in lines:
        try:
            lw = draw.textlength(line, font=font)
        except AttributeError:
            lw = draw.textbbox((0, 0), line, font=font)[2]
        if alignment == "left":
            lx = x0 + pad_x
        elif alignment == "right":
            lx = x0 + box_w - pad_x - lw
        else:
            lx = x0 + (box_w - lw) / 2
        _draw_with_shadow(draw, (lx, y), line, font, text_color, cfg.get("shadow", {}))
        y += line_height

    return int(x0), int(y0), int(box_w), int(box_h)


def overlay_cfg_from_values(values) -> dict:
    """Build a clamped overlay config from HTML form fields or query params.

    Checkbox fields arrive as "on" when checked and are absent when unchecked,
    so a missing checkbox means disabled. Shared by every studio-style live
    preview + save endpoint (book detail, video creator) so they all interpret
    the same field names identically.
    """

    def _int(name: str, default: int, lo: int, hi: int) -> int:
        try:
            val = int(values.get(name, default))
        except (TypeError, ValueError):
            val = default
        return max(lo, min(hi, val))

    cfg = get_default_overlay_config()
    position = values.get("position") or "top"
    alignment = values.get("alignment") or "center"
    cfg["position"] = position if position in ("top", "center", "bottom") else "top"
    cfg["alignment"] = alignment if alignment in ("left", "center", "right") else "center"
    cfg["font_size"] = _int("font_size", 52, 12, 200)
    cfg["text"] = str(values.get("text") or "").strip()[:500]
    cfg["text_color"] = values.get("text_color") or "#FFFFFF"
    cfg["margin"] = _int("margin", 40, 0, 200)
    cfg["offset_x"] = _int("offset_x", 0, -4000, 4000)
    cfg["offset_y"] = _int("offset_y", 0, -4000, 4000)
    cfg["shadow"] = {
        "enabled": values.get("shadow_enabled") in ("on", "1", "true", True),
        "color": values.get("shadow_color") or "#000000",
        "offset": _int("shadow_offset", 3, 0, 20),
    }
    cfg["box"] = {
        "enabled": values.get("box_enabled") in ("on", "1", "true", True),
        "color": values.get("box_color") or "#000000",
        "opacity": _int("box_opacity", 60, 0, 100),
        "padding_x": _int("box_padding_x", 24, 0, 200),
        "padding_y": _int("box_padding_y", 12, 0, 200),
        "radius": _int("box_radius", 12, 0, 200),
    }
    overlays_json = values.get("overlays_json")
    if overlays_json:
        try:
            raw_overlays = json.loads(overlays_json)
        except (TypeError, ValueError):
            raw_overlays = []
        allowed_fonts = {item["path"] for item in list_overlay_fonts()}
        overlays = []
        for raw in raw_overlays[:12] if isinstance(raw_overlays, list) else []:
            if not isinstance(raw, dict):
                continue
            item = overlay_cfg_from_values(raw)
            item["text"] = str(raw.get("text") or "")[:500]
            font_path = str(raw.get("font_path") or "")
            item["font_path"] = font_path if font_path in allowed_fonts else ""
            overlays.append(item)
        cfg["overlays"] = overlays
    return cfg


def expand_overlay_text(template: str, book: Book, patch: Patch) -> str:
    patch_name = patch.name or str(patch.patch_index)
    values = {
        "book_title": book.title,
        "patch_name": patch_name,
        "patch_index": patch.patch_index,
        "episode": patch.patch_index + 1,
        "chapter": f"{patch.chapter_start}-{patch.chapter_end}",
        "chapter_start": patch.chapter_start,
        "chapter_end": patch.chapter_end,
    }
    text = template or "{book_title} - {patch_name}"
    for key, value in values.items():
        text = text.replace("{" + key + "}", str(value))
    return text


def build_overlay_lines(image: "Image.Image", text: str, cfg: dict) -> list[str]:
    """Wrap overlay text against the image width, same as render_patch_overlay."""
    from PIL import ImageDraw
    draw = ImageDraw.Draw(image)
    font_size = int(cfg.get("font_size", 52))
    font = _load_font(cfg.get("font_path") or settings.default_font_path or None, font_size)
    return _wrap_lines(draw, text, font, image.size[0] - 80)


def render_overlay_with_rect(
    image: "Image.Image",
    lines: list[str],
    cfg: dict,
) -> tuple["Image.Image", tuple[int, int, int, int]]:
    """Render an overlay onto an in-memory PIL image.

    Returns (mutated image, text-block rect) — the rect is what the studio
    preview uses as its drag handle.
    """
    from PIL import ImageDraw
    if image.mode != "RGB":
        image = image.convert("RGB")
    draw = ImageDraw.Draw(image)
    text_color = _hex_to_rgb(cfg.get("text_color", "#FFFFFF"))
    rect = _draw_text_block(image, draw, lines, cfg, text_color)
    return image, rect

def render_overlay(
    image: "Image.Image",
    lines: list[str],
    cfg: dict,
) -> "Image.Image":
    """Render an overlay onto an in-memory PIL image. Returns the mutated image.

    Public for the live preview endpoint.
    """
    image, _ = render_overlay_with_rect(image, lines, cfg)
    return image


def render_patch_overlay(
    book: Book, patch: Patch, cfg: dict | None = None, out_path: str | None = None,
    background_path: str | None = None,
) -> None:
    """Render background + overlay text onto a PNG file."""
    cfg = cfg or parse_overlay_config(book.overlay_config)
    bg = _resolve_background(book, background_path)
    if bg is None:
        raise ValueError(f"no background image available for book {book.id}")
    if out_path is None:
        out_path = str(get_patch_overlay_path(book.id, patch.id))

    from PIL import Image, ImageDraw
    img = Image.open(str(bg)).convert("RGB")
    overlays = cfg.get("overlays") or [cfg]
    for overlay in overlays:
        text = expand_overlay_text(overlay.get("text", ""), book, patch)
        lines = build_overlay_lines(img, text, overlay)
        img, _ = render_overlay_with_rect(img, lines, overlay)

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(out), "PNG")


def needs_rerender(book: Book, patch: Patch, out_path: Path) -> bool:
    if not out_path.exists():
        return True
    bg = _resolve_background(book)
    if bg is None:
        return False
    try:
        return bg.stat().st_mtime > out_path.stat().st_mtime
    except OSError:
        return True


def ensure_patch_overlay(
    book: Book, patch: Patch, font_path: str | None = None, *,
    background_path: str | None = None, out_path: str | None = None, force: bool = False,
) -> str | None:
    """Build overlay config from legacy font_path arg and render if stale."""
    cfg = parse_overlay_config(book.overlay_config)
    if font_path and not cfg.get("font_path"):
        cfg["font_path"] = font_path
    if _resolve_background(book, background_path) is None:
        return None
    output = Path(out_path) if out_path else get_patch_overlay_path(book.id, patch.id)
    if force or background_path or needs_rerender(book, patch, output):
        try:
            render_patch_overlay(book, patch, cfg, str(output), background_path)
        except Exception as exc:
            logger.error("image_overlay: failed to render overlay for patch %s: %s", patch.id, exc)
            return None
    return str(output)
