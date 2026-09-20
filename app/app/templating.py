"""Shared Jinja environment plus the filters the templates rely on."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi.templating import Jinja2Templates

from .config import APP_NAME, APP_TAGLINE

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def ago(value: str | None) -> str:
    if not value:
        return "never"
    try:
        then = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    delta = datetime.now(timezone.utc) - then
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def yesno(value) -> str:
    return "yes" if value else "no"


def asset(path: str) -> str:
    """Static URL with the file's timestamp attached.

    Browsers cache the stylesheet and script hard, so a changed file needs a changed
    URL; otherwise an updated console renders with the previous layout.
    """
    file = Path(__file__).parent / "static" / path
    try:
        stamp = int(file.stat().st_mtime)
    except OSError:
        stamp = 0
    return f"/static/{path}?v={stamp}"


templates.env.filters["ago"] = ago
templates.env.filters["yesno"] = yesno
templates.env.globals["asset"] = asset
templates.env.globals["app_name"] = APP_NAME
templates.env.globals["tagline"] = APP_TAGLINE
