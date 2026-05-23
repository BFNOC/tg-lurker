from __future__ import annotations

import html
import re
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from starlette.middleware.base import BaseHTTPMiddleware

from database import Database
from config import Config

_HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))


class CSRFContextMiddleware(BaseHTTPMiddleware):
    """Injects a CSRF token into every request and sets the CSRF cookie when absent."""

    async def dispatch(self, request: Request, call_next):
        """Reads or generates a CSRF token, attaches it to request.state, and ensures the cookie is set."""
        from web.auth import (
            CSRF_COOKIE,
            ensure_session_days_loaded,
            get_csrf_token,
            get_session_max_age,
            set_csrf_cookie,
        )
        await ensure_session_days_loaded(request)
        token = get_csrf_token(request)
        request.state.csrf_token = token
        response = await call_next(request)
        if not request.cookies.get(CSRF_COOKIE):
            set_csrf_cookie(response, token, get_session_max_age(request))
        return response


def create_app(config: Config, db: Database, bot=None, scheduler=None) -> FastAPI:
    """Builds and returns the FastAPI application with all middleware, routers, and template globals."""
    app = FastAPI(title="tg-lurker", docs_url=None, redoc_url=None)
    app.state.config = config
    app.state.db = db
    app.state.bot = bot
    app.state.scheduler = scheduler
    app.state.web_session_days = config.web_session_days
    app.state.web_session_days_loaded = False

    app.add_middleware(CSRFContextMiddleware)
    app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")

    from web.auth import router as auth_router
    from web.routes import router as routes_router

    app.include_router(auth_router)
    app.include_router(routes_router)

    templates.env.globals["csrf_token_value"] = lambda request: getattr(request.state, "csrf_token", "")

    import os
    templates.env.globals["version"] = os.environ.get("APP_VERSION", "dev")
    templates.env.globals["commit_short"] = os.environ.get("APP_COMMIT", "")

    from datetime import datetime
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(config.tz)

    def timestamp_to_time(ts):
        """Converts a Unix timestamp to HH:MM string in the configured timezone."""
        return datetime.fromtimestamp(ts, tz).strftime("%H:%M")

    def timestamp_to_datetime(ts):
        """Converts a Unix timestamp to a compact datetime string in the configured timezone."""
        if not ts:
            return "未抓取"
        return datetime.fromtimestamp(ts, tz).strftime("%m-%d %H:%M")

    def linkify_bio(text):
        """Escapes Bio text and turns URLs / Telegram usernames into clickable links."""
        if not text:
            return ""

        pattern = re.compile(r"(https?://[^\s<]+|t\.me/[A-Za-z0-9_/?=&.%#-]+|@[A-Za-z0-9_]{5,32})")
        parts: list[str] = []
        last = 0
        value = str(text)

        def escape_segment(segment: str) -> str:
            return html.escape(segment).replace("\n", "<br>")

        for match in pattern.finditer(value):
            parts.append(escape_segment(value[last:match.start()]))
            raw = match.group(0)
            if raw.startswith("@"):
                href = f"https://t.me/{raw[1:]}"
            elif raw.startswith("t.me/"):
                href = f"https://{raw}"
            else:
                href = raw
            parts.append(
                f'<a href="{html.escape(href, quote=True)}" target="_blank" '
                f'rel="noopener noreferrer">{html.escape(raw)}</a>'
            )
            last = match.end()
        parts.append(escape_segment(value[last:]))
        return Markup("".join(parts))

    templates.env.filters["timestamp_to_time"] = timestamp_to_time
    templates.env.filters["timestamp_to_datetime"] = timestamp_to_datetime
    templates.env.filters["linkify_bio"] = linkify_bio

    return app
