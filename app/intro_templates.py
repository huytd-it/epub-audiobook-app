"""Rendering and validation helpers for dynamic intro / CTA templates."""
from __future__ import annotations

import re

ALLOWED_VARIABLES = {"channel_name", "book_title", "chapter_title", "author"}
_VARIABLE_RE = re.compile(r"\{(\w+)\}")
MAX_TEMPLATE_LENGTH = 300
MAX_CHANNEL_NAME_LENGTH = 100

# Estimated speech duration heuristic: ~15 chars per second
_CHARS_PER_SECOND = 15
_WARN_DURATION_SECONDS = 8


def render_template(template: str, variables: dict | None = None) -> str:
    """Substitute supported {variables}; missing/empty values become "".

    Unsupported placeholders are left untouched so that validate step can
    report them separately.
    """
    values = variables or {}
    def _repl(match: re.Match) -> str:
        name = match.group(1)
        if name in ALLOWED_VARIABLES:
            return str(values.get(name) or "")
        return match.group(0)
    return _VARIABLE_RE.sub(_repl, str(template or ""))


def validate_template(template: str, *, max_length: int = MAX_TEMPLATE_LENGTH) -> list[str]:
    """Return list of error strings for a template; empty means valid."""
    errors: list[str] = []
    tpl = str(template or "")
    if len(tpl) > max_length:
        errors.append(f"Template vượt quá {max_length} ký tự")
    unsupported = sorted({name for name in _VARIABLE_RE.findall(tpl) if name not in ALLOWED_VARIABLES})
    if unsupported:
        errors.append("Biến không hỗ trợ: " + ", ".join("{" + name + "}" for name in unsupported))
    return errors


def collect_warnings(template: str, variables: dict | None = None) -> list[str]:
    """Warnings for empty variables and excessive estimated duration."""
    values = variables or {}
    tpl = str(template or "")
    missing = sorted({
        name for name in _VARIABLE_RE.findall(tpl)
        if name in ALLOWED_VARIABLES and not str(values.get(name) or "").strip()
    })
    warnings: list[str] = []
    if missing:
        warnings.append("Thiếu dữ liệu cho biến: " + ", ".join("{" + name + "}" for name in missing))
    rendered_len = len(render_template(tpl, values))
    if rendered_len / _CHARS_PER_SECOND > _WARN_DURATION_SECONDS:
        warnings.append(f"Thời lượng ước tính vượt quá {_WARN_DURATION_SECONDS} giây (~{rendered_len / _CHARS_PER_SECOND:.1f}s)")
    return warnings


def estimated_seconds(rendered: str) -> float:
    """Estimated duration in seconds for a rendered string."""
    return len(rendered) / _CHARS_PER_SECOND
