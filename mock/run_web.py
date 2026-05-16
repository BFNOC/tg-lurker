"""Run web UI in mock mode (no Telegram connection needed)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import uvicorn

from database import Database
from config import Config
from mock.mock_bot import MockBot
from summarizer import Summarizer
from scheduler import SummaryScheduler
from web import create_app


MOCK_CONFIG = Config(
    api_id=0,
    api_hash="mock",
    owner_id=0,
    llm_base_url="http://localhost:11434/v1",
    llm_api_key="mock-key",
    llm_model="mock-model",
    llm_api_format="chat",
    db_path="./data/messages.db",
    session_path="./data/mock.session",
    web_port=8090,
    web_password="demo",
    tg_push_enabled=True,
    tz="Asia/Shanghai",
)


async def main():
    db = Database(MOCK_CONFIG.db_path)
    await db.connect()

    bot = MockBot()
    summarizer = Summarizer(MOCK_CONFIG, db)

    async def mock_send(text: str):
        print(f"[MOCK] TG send: {text[:60]}...")

    scheduler = SummaryScheduler(MOCK_CONFIG, summarizer, send_callback=mock_send)

    app = create_app(MOCK_CONFIG, db, bot=bot, scheduler=scheduler)

    print("=" * 50)
    print("  tg-lurker Web UI (MOCK MODE)")
    print(f"  URL: http://localhost:{MOCK_CONFIG.web_port}")
    print(f"  Password: demo")
    print("=" * 50)

    config = uvicorn.Config(app, host="0.0.0.0", port=MOCK_CONFIG.web_port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
