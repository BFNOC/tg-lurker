from __future__ import annotations

import asyncio
import logging
import signal
import sys

import uvicorn

from config import load_config
from database import Database
from dedup import DedupEngine
from bot import Bot
from summarizer import Summarizer
from scheduler import SummaryScheduler
from web import create_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    config = load_config()

    db = Database(config.db_path)
    await db.connect()
    logger.info(f"Database connected: {config.db_path}")

    dedup = DedupEngine()
    bot = Bot(config, db, dedup)
    summarizer = Summarizer(config, db)

    async def send_to_owner(text: str) -> None:
        await bot.client.send_message(config.owner_id, text)

    bot.set_alert_callback(send_to_owner)
    scheduler = SummaryScheduler(config, summarizer, db=db, send_callback=send_to_owner)

    app = create_app(config, db, bot=bot, scheduler=scheduler)

    await bot.start()
    scheduler.start()

    web_config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=config.web_port,
        log_level="warning",
    )
    server = uvicorn.Server(web_config)

    async def shutdown() -> None:
        logger.info("Shutting down...")
        scheduler.stop()
        server.should_exit = True
        await bot.stop()
        await db.close()
        logger.info("Shutdown complete")

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown()))

    logger.info(f"Web UI: http://0.0.0.0:{config.web_port}")
    logger.info("tg-lurker running. Press Ctrl+C to stop.")

    await server.serve()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
