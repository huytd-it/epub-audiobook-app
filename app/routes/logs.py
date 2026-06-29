from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates

from app.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/logs", response_class=HTMLResponse)
def view_logs(request: Request, lines: int = 500):
    log_path = settings.log_path
    content = ""
    try:
        if os.path.exists(log_path):
            with open(log_path, "r", encoding="utf-8") as f:
                all_lines = f.readlines()
                tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
                content = "".join(tail)
    except Exception as e:
        logger.error("Failed to read log file: %s", e)
        content = f"Error reading log file: {e}"
    return templates.TemplateResponse(
        request, "logs.html", {"log_content": content, "log_path": log_path, "max_lines": lines}
    )


@router.delete("/logs", response_class=PlainTextResponse)
def delete_logs():
    log_path = settings.log_path
    try:
        if os.path.exists(log_path):
            with open(log_path, "w", encoding="utf-8") as f:
                f.write("")
            logger.info("Log file cleared: %s", log_path)
            return "Log file cleared"
        return "Log file does not exist"
    except Exception as e:
        logger.error("Failed to clear log file: %s", e)
        return f"Error clearing log file: {e}"


@router.get("/logs/raw", response_class=PlainTextResponse)
def raw_logs(request: Request, lines: int = 500):
    log_path = settings.log_path
    try:
        if os.path.exists(log_path):
            with open(log_path, "r", encoding="utf-8") as f:
                all_lines = f.readlines()
                tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
                return "".join(tail)
        return ""
    except Exception as e:
        logger.error("Failed to read log file: %s", e)
        return f"Error reading log file: {e}"
