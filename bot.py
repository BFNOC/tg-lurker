from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.tl.types import Channel, Chat

from config import Config
from database import Database
from dedup import DedupEngine

logger = logging.getLogger(__name__)


class Bot:
    def __init__(self, config: Config, db: Database, dedup: DedupEngine) -> None:
        self._config = config
        self._db = db
        self._dedup = dedup
        self._client: TelegramClient | None = None
        self._tz = ZoneInfo(config.tz)
        self._running = False
        self._ready = asyncio.Event()
        self._alert_keywords: list[str] = []
        self._alert_callback = None
        self._admin_cache: dict[tuple[int, int], tuple[bool, float]] = {}

    @property
    def client(self) -> TelegramClient:
        assert self._client is not None
        return self._client

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected()

    def _build_proxy(self) -> tuple | None:
        if not self._config.proxy_type or not self._config.proxy_host:
            return None
        import socks
        proxy_map = {
            "socks5": socks.SOCKS5,
            "socks4": socks.SOCKS4,
            "http": socks.HTTP,
        }
        proxy_type = proxy_map.get(self._config.proxy_type.lower())
        if proxy_type is None:
            return None
        return (proxy_type, self._config.proxy_host, self._config.proxy_port)

    async def start(self) -> None:
        proxy = self._build_proxy()
        self._client = TelegramClient(
            self._config.session_path,
            self._config.api_id,
            self._config.api_hash,
            proxy=proxy,
            auto_reconnect=True,
            connection_retries=10,
            retry_delay=5,
            flood_sleep_threshold=60,
            catch_up=True,
        )

        self._register_handler()

        await self._client.connect()
        logger.info("Connected to Telegram servers")

        if not await self._client.is_user_authorized():
            import sys
            logger.error(
                "Session not authorized. Run interactively first:\n"
                "  docker compose run --rm tg-lurker python main.py\n"
                "Then enter your phone number and verification code."
            )
            await self._client.disconnect()
            sys.exit(1)

        await self._client.start()
        self._running = True
        logger.info("Telegram client connected")

        await self._sync_groups()
        await self._rebuild_dedup()
        await self._reload_alert_keywords()

        self._ready.set()
        logger.info("Initialization complete, processing messages")

        await self._client.catch_up()
        logger.info("Catch-up dispatched")

    async def _sync_groups(self) -> None:
        async for dialog in self._client.iter_dialogs():
            entity = dialog.entity
            if isinstance(entity, (Channel, Chat)):
                await self._db.upsert_group(dialog.id, dialog.name or str(dialog.id))
        logger.info("Groups synced to database")

    async def _rebuild_dedup(self) -> None:
        biz_date = self._current_biz_date()
        texts = await self._db.get_message_texts_by_date(biz_date)
        self._dedup.rebuild_from_texts(texts)

        blocklist_raw = await self._db.get_setting("ad_keywords", "")
        if blocklist_raw:
            keywords = [k.strip() for k in blocklist_raw.split("\n") if k.strip()]
            self._dedup.set_blocklist(keywords)

        logger.info(f"Dedup rebuilt with {len(texts)} messages, {len(self._dedup._keyword_blocklist)} keywords")

    async def _reload_alert_keywords(self) -> None:
        raw = await self._db.get_setting("alert_keywords", "")
        self._alert_keywords = [k.strip().lower() for k in raw.split("\n") if k.strip()]
        logger.info(f"Alert keywords loaded: {len(self._alert_keywords)}")

    def set_alert_callback(self, callback) -> None:
        self._alert_callback = callback

    async def _check_alert(self, message, group_name: str, text: str) -> None:
        if not self._alert_keywords or not self._alert_callback:
            return

        if not message.sender_id:
            return

        text_lower = text.lower()
        matched = [kw for kw in self._alert_keywords if kw in text_lower]
        if not matched:
            return

        import time
        cache_key = (message.chat_id, message.sender_id)
        cached = self._admin_cache.get(cache_key)
        now = time.time()

        if cached and (now - cached[1]) < 300:
            is_admin = cached[0]
        else:
            is_admin = False
            try:
                perms = await self._client.get_permissions(message.chat_id, message.sender_id)
                is_admin = perms.is_admin or perms.is_creator
            except FloodWaitError as e:
                logger.warning(f"FloodWait {e.seconds}s on permission check, skipping alert")
                return
            except Exception as e:
                logger.debug(f"Permission check failed for {message.sender_id}: {e}")
                return
            self._admin_cache[cache_key] = (is_admin, now)

        if not is_admin:
            return

        sender_name = getattr(message, "post_author", "") or ""
        if not sender_name and message.sender:
            sender_name = getattr(message.sender, "first_name", "") or str(message.sender_id)

        alert_text = (
            f"🔔 实时告警\n\n"
            f"群组: {group_name}\n"
            f"发送者: {sender_name} (管理员)\n"
            f"关键词: {', '.join(matched)}\n\n"
            f"消息内容:\n{text[:500]}"
        )

        try:
            await self._alert_callback(alert_text)
            await self._db.insert_alert(
                group_id=message.chat_id,
                group_name=group_name,
                sender_id=message.sender_id,
                sender_name=sender_name,
                keywords=", ".join(matched),
                message_text=text[:500],
            )
        except Exception as e:
            logger.error(f"Alert send failed: {e}")

    def _register_handler(self) -> None:
        @self._client.on(events.NewMessage)
        async def handler(event):
            await self._ready.wait()
            chat = await event.get_chat()
            if not isinstance(chat, (Channel, Chat)):
                return
            chat_id = event.chat_id
            active_groups = await self._db.get_active_groups()
            active_ids = {g["group_id"] for g in active_groups}
            if chat_id not in active_ids:
                return
            group_name = getattr(chat, "title", None) or str(chat_id)
            await self._process_message(event.message, group_name)

    async def _process_message(self, message, group_name: str) -> None:
        if not message.text:
            return

        text = message.text.strip()
        if not text:
            return

        if message.sender_id and await self._db.is_sender_blocked(message.sender_id):
            return

        if not self._dedup.check_and_add(text):
            return

        await self._check_alert(message, group_name, text)

        biz_date = message.date.astimezone(self._tz).strftime("%Y-%m-%d")
        sender_id = message.sender_id
        sender = None
        if message.sender:
            sender = getattr(message.sender, "first_name", None) or str(message.sender_id)

        inserted = await self._db.insert_message(
            group_id=message.chat_id,
            group_name=group_name,
            message_id=message.id,
            sender_id=sender_id,
            sender_name=sender,
            text=text,
            timestamp=int(message.date.timestamp()),
            biz_date=biz_date,
        )
        if not inserted:
            self._dedup._hashes.discard(__import__("dedup").text_hash(text))

    def _current_biz_date(self) -> str:
        return datetime.now(self._tz).strftime("%Y-%m-%d")

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        logger.info("Shutting down bot...")
        if self._client:
            await self._client.disconnect()
        logger.info("Bot stopped")

    async def fetch_messages_around(self, group_id: int, message_id: int, radius: int) -> list[dict]:
        entity = await self._client.get_entity(group_id)
        ids = list(range(max(1, message_id - radius), message_id + radius + 1))
        messages = await self._client.get_messages(entity, ids=ids)
        result = []
        for msg in messages:
            if msg and msg.text:
                sender = ""
                if msg.sender:
                    sender = getattr(msg.sender, "first_name", "") or str(msg.sender_id)
                result.append({
                    "message_id": msg.id,
                    "sender_name": sender,
                    "text": msg.text,
                    "timestamp": int(msg.date.timestamp()),
                })
        return sorted(result, key=lambda m: m["message_id"])
