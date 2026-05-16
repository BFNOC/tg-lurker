from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from database import Database
from config import Config

_HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))


def create_app(config: Config, db: Database, bot=None, scheduler=None) -> FastAPI:
    app = FastAPI(title="tg-lurker", docs_url=None, redoc_url=None)
    app.state.config = config
    app.state.db = db
    app.state.bot = bot
    app.state.scheduler = scheduler

    app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")

    from web.auth import router as auth_router
    from web.routes import router as routes_router

    app.include_router(auth_router)
    app.include_router(routes_router)

    return app
