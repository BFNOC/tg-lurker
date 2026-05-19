from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from database import Database
from config import Config

_HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))


class CSRFContextMiddleware(BaseHTTPMiddleware):
    """Injects a CSRF token into every request and sets the CSRF cookie when absent."""

    async def dispatch(self, request: Request, call_next):
        """Reads or generates a CSRF token, attaches it to request.state, and ensures the cookie is set."""
        from web.auth import get_csrf_token, set_csrf_cookie, CSRF_COOKIE
        token = get_csrf_token(request)
        request.state.csrf_token = token
        response = await call_next(request)
        if not request.cookies.get(CSRF_COOKIE):
            set_csrf_cookie(response, token)
        return response


def create_app(config: Config, db: Database, bot=None, scheduler=None) -> FastAPI:
    """Builds and returns the FastAPI application with all middleware, routers, and template globals."""
    app = FastAPI(title="tg-lurker", docs_url=None, redoc_url=None)
    app.state.config = config
    app.state.db = db
    app.state.bot = bot
    app.state.scheduler = scheduler

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

    templates.env.filters["timestamp_to_time"] = timestamp_to_time

    return app
