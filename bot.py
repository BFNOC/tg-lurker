from __future__ import annotations

import asyncio
import logging
import random
import time
from contextlib import suppress
from datetime import datetime
from zoneinfo import ZoneInfo

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.tl import functions
from telethon.tl.types import Channel, Chat, InputPeerUser, InputUser, User

from config import Config
from database import Database
from dedup import DedupEngine

logger = logging.getLogger(__name__)

BIO_TRIGGER_KEYWORDS = (
    "看主页",
    "看简介",
    "点击头像",
    "个人资料",
    "bio",
    "profile",
    "卡网自取",
    "发卡",
    "自动发货",
    "联系客服",
)

BIO_WORKER_MIN_INTERVAL = 600
BIO_WORKER_MAX_INTERVAL = 1200
BIO_WORKER_EMPTY_SLEEP = 60
BIO_DAILY_FETCH_LIMIT = 40


class Bot:
    """Manages the Telegram userbot connection, message collection, and alert dispatch."""

    def __init__(self, config: Config, db: Database, dedup: DedupEngine) -> None:
        """Initializes the bot with config, database, and dedup engine."""
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
        self._admin_cache_max = 1000
        self._filter_bots = True
        self._ad_keywords: list[str] = []
        self._bio_worker_task: asyncio.Task | None = None
        self._bio_pause_until = 0.0
        self._bio_fetch_count_today = 0
        self._bio_fetch_day = ""

    @property
    def client(self) -> TelegramClient:
        """Returns the underlying Telethon client."""
        assert self._client is not None
        return self._client

    @property
    def is_connected(self) -> bool:
        """Reports whether the Telegram client is connected."""
        return self._client is not None and self._client.is_connected()

    def _build_proxy(self) -> tuple | None:
        """Builds a PySocks proxy tuple from config, or None if no proxy is set."""
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
        """Connects to Telegram, syncs groups, and begins processing messages."""
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
            if not sys.stdin.isatty():
                logger.error(
                    "Session not authorized. Run interactively first:\n"
                    "  docker compose run -it --rm tg-lurker python main.py\n"
                    "Then enter your phone number and verification code."
                )
                await self._client.disconnect()
                sys.exit(1)
            await self._client.start()

        await self._client.start()
        self._running = True
        logger.info("Telegram client connected")

        await self._sync_groups()
        await self._rebuild_dedup()
        await self._reload_alert_keywords()
        await self._reload_filter_bots()
        await self._db.reset_running_bio_tasks()

        self._ready.set()
        self._bio_worker_task = asyncio.create_task(self._bio_fetch_worker())
        logger.info("Initialization complete, processing messages")

        await self._client.catch_up()
        logger.info("Catch-up dispatched")

    async def _sync_groups(self) -> None:
        """Syncs all joined Telegram groups and channels to the database."""
        async for dialog in self._client.iter_dialogs():
            entity = dialog.entity
            if isinstance(entity, (Channel, Chat)):
                await self._db.upsert_group(dialog.id, dialog.name or str(dialog.id))
        logger.info("Groups synced to database")

    async def _rebuild_dedup(self) -> None:
        """Rebuilds the dedup engine with today's messages and the current ad-keyword blocklist."""
        biz_date = self._current_biz_date()
        texts = await self._db.get_message_texts_by_date(biz_date)
        self._dedup.rebuild_from_texts(texts)
        await self._reload_ad_keywords()
        logger.info(f"Dedup rebuilt with {len(texts)} messages, {len(self._ad_keywords)} keywords")

    async def _reload_ad_keywords(self) -> None:
        """Reloads ad keywords used by message filtering and Bio candidate detection."""
        blocklist_raw = await self._db.get_setting("ad_keywords", "")
        self._ad_keywords = [k.strip().lower() for k in blocklist_raw.split("\n") if k.strip()]
        self._dedup.set_blocklist(self._ad_keywords)

    async def _reload_alert_keywords(self) -> None:
        """Reloads alert keywords from the settings table."""
        raw = await self._db.get_setting("alert_keywords", "")
        self._alert_keywords = [k.strip().lower() for k in raw.split("\n") if k.strip()]
        logger.info(f"Alert keywords loaded: {len(self._alert_keywords)}")

    async def _reload_filter_bots(self) -> None:
        """Reloads the bot message filter toggle from the settings table."""
        raw = await self._db.get_setting("filter_bot_messages", "true")
        normalized = raw.strip().lower()
        if normalized not in ("true", "false"):
            logger.warning(f"Invalid filter_bot_messages value '{raw}', defaulting to true")
            normalized = "true"
        self._filter_bots = normalized == "true"

    def set_alert_callback(self, callback) -> None:
        """Registers an async callback invoked when an alert keyword is matched."""
        self._alert_callback = callback

    def _sender_display_name(self, sender, fallback_id: int | None = None) -> str | None:
        """从 Telegram User 对象提取稳定显示名。"""
        if not sender:
            return str(fallback_id) if fallback_id else None
        first = getattr(sender, "first_name", None) or ""
        last = getattr(sender, "last_name", None) or ""
        name = " ".join(part for part in (first, last) if part).strip()
        if name:
            return name
        username = getattr(sender, "username", None)
        if username:
            return f"@{username}"
        return str(fallback_id) if fallback_id else None

    def _bio_candidate_reason(self, text: str) -> tuple[bool, str, int]:
        """根据消息内容判断是否需要排队抓取发送者 Bio。"""
        text_lower = text.lower()
        matched: list[str] = []
        for kw in BIO_TRIGGER_KEYWORDS:
            if kw.lower() in text_lower:
                matched.append(kw)
        for kw in self._ad_keywords:
            if kw and kw in text_lower and kw not in matched:
                matched.append(kw)

        if not matched:
            return False, "", 0

        priority = 10 + min(len(matched), 5)
        return True, "消息命中 Bio 线索: " + ", ".join(matched[:6]), priority

    async def _record_sender_observation(
        self,
        message,
        group_name: str,
        text: str,
        candidate_info: tuple[bool, str, int] | None = None,
    ) -> None:
        """保存 sender 最近出现信息，并在疑似引流时加入 Bio 抓取队列。"""
        if not message.sender_id:
            return

        is_candidate, reason, priority = candidate_info or self._bio_candidate_reason(text)
        sender = message.sender
        if sender is None and is_candidate:
            try:
                sender = await message.get_sender()
            except Exception as e:
                logger.debug("Failed to get sender for Bio candidate %s: %s", message.sender_id, e)
        if sender is not None and not isinstance(sender, User):
            return
        access_hash = getattr(sender, "access_hash", None)
        username = getattr(sender, "username", None)
        display_name = self._sender_display_name(sender, message.sender_id)
        timestamp = int(message.date.timestamp())
        await self._db.upsert_sender_observation(
            sender_id=message.sender_id,
            access_hash=access_hash,
            username=username,
            display_name=display_name,
            group_id=message.chat_id,
            group_name=group_name,
            message_id=message.id,
            message_text=text,
            timestamp=timestamp,
        )

        if is_candidate:
            queued = await self._db.queue_bio_fetch(message.sender_id, reason, priority=priority)
            if queued:
                logger.info("Queued Bio fetch for sender %s: %s", message.sender_id, reason)

    def _reset_bio_daily_budget(self) -> None:
        """按业务时区重置 Bio 抓取每日预算。"""
        today = self._current_biz_date()
        if self._bio_fetch_day != today:
            self._bio_fetch_day = today
            self._bio_fetch_count_today = 0

    async def _bio_fetch_worker(self) -> None:
        """低速消费 Bio 抓取队列，避免频繁调用 Telegram 完整资料接口。"""
        await self._ready.wait()
        logger.info("Bio fetch worker started")
        while self._running:
            self._reset_bio_daily_budget()
            now = time.time()
            if now < self._bio_pause_until:
                await asyncio.sleep(min(BIO_WORKER_EMPTY_SLEEP, self._bio_pause_until - now))
                continue
            if self._bio_fetch_count_today >= BIO_DAILY_FETCH_LIMIT:
                await asyncio.sleep(BIO_WORKER_EMPTY_SLEEP * 5)
                continue

            task = await self._db.claim_next_bio_fetch_task()
            if not task:
                await asyncio.sleep(BIO_WORKER_EMPTY_SLEEP)
                continue

            sender_id = task["sender_id"]
            try:
                await self._fetch_and_store_bio(task)
                self._bio_fetch_count_today += 1
                logger.info("Fetched Bio for sender %s", sender_id)
            except FloodWaitError as e:
                retry_after = max(e.seconds, BIO_WORKER_EMPTY_SLEEP * 5)
                self._bio_pause_until = time.time() + retry_after
                await self._db.fail_bio_fetch(sender_id, f"FloodWait {e.seconds}s", retry_after=retry_after)
                logger.warning("Bio fetch FloodWait %ss, paused queue", e.seconds)
            except Exception as e:
                await self._db.fail_bio_fetch(sender_id, str(e))
                logger.warning("Bio fetch failed for sender %s: %s", sender_id, e)

            await asyncio.sleep(random.randint(BIO_WORKER_MIN_INTERVAL, BIO_WORKER_MAX_INTERVAL))

    async def _fetch_and_store_bio(self, task: dict) -> None:
        """调用 Telegram users.getFullUser 并保存完整 Bio 原文。"""
        assert self._client is not None
        sender_id = int(task["sender_id"])
        username = (task.get("username") or "").strip().lstrip("@")
        candidates: list[tuple[str, object, bool]] = []
        if username:
            candidates.append(("username cache", username, False))
            candidates.append(("username fresh", username, True))
        if task.get("access_hash"):
            candidates.append(("cached access_hash", InputUser(sender_id, int(task["access_hash"])), False))
        candidates.append(("sender_id", sender_id, False))

        full = None
        user = None
        resolved_access_hash = task.get("access_hash")
        last_error: Exception | None = None
        for source, candidate, fresh_resolve in candidates:
            try:
                target = await self._resolve_bio_user_target(candidate, sender_id, fresh_resolve=fresh_resolve)
                full = await self._client(functions.users.GetFullUserRequest(id=target))
                resolved_access_hash = getattr(target, "access_hash", resolved_access_hash)
                break
            except FloodWaitError:
                raise
            except Exception as e:
                last_error = e
                logger.debug("Bio fetch attempt failed for sender %s via %s: %s", sender_id, source, e)

        if full is None:
            if last_error:
                raise last_error
            raise ValueError("no Telegram user candidate available for Bio fetch")

        full_user = getattr(full, "full_user", None)
        bio_text = getattr(full_user, "about", None) if full_user else None

        for item in getattr(full, "users", []) or []:
            if isinstance(item, User) and item.id == sender_id:
                user = item
                break

        await self._db.complete_bio_fetch(
            sender_id=sender_id,
            bio_text=bio_text,
            username=getattr(user, "username", None) if user else task.get("username"),
            display_name=self._sender_display_name(user, sender_id) if user else task.get("display_name"),
            access_hash=getattr(user, "access_hash", None) if user else resolved_access_hash,
        )

    async def _resolve_bio_user_target(
        self,
        candidate: object,
        sender_id: int,
        fresh_resolve: bool = False,
    ) -> object:
        """把队列候选值解析成 users.getFullUser 可接受的用户对象。"""
        assert self._client is not None
        if isinstance(candidate, InputUser):
            target = candidate
        elif fresh_resolve:
            target = await self._client.get_entity(candidate)
        else:
            target = await self._client.get_input_entity(candidate)

        if isinstance(target, User):
            if target.id != sender_id or target.access_hash is None:
                raise ValueError("resolved Telegram user does not match sender_id")
            return InputUser(target.id, int(target.access_hash))
        if isinstance(target, InputUser):
            if target.user_id != sender_id:
                raise ValueError("resolved Telegram user does not match sender_id")
            return target
        if isinstance(target, InputPeerUser):
            if target.user_id != sender_id:
                raise ValueError("resolved Telegram user does not match sender_id")
            return InputUser(target.user_id, int(target.access_hash))
        raise ValueError("sender is not a Telegram user, skip Bio fetch")

    async def _check_alert(self, message, group_name: str, text: str) -> None:
        """Checks a message against alert keywords and notifies if the sender is an admin.

        Admin status is cached for 5 minutes per (chat_id, sender_id) pair to avoid
        repeated Telegram API calls. FloodWait errors cause the alert to be silently skipped.
        """
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
            if len(self._admin_cache) >= self._admin_cache_max:
                expired = [k for k, (_, ts) in self._admin_cache.items() if now - ts >= 300]
                for k in expired:
                    del self._admin_cache[k]
                if len(self._admin_cache) >= self._admin_cache_max:
                    oldest_key = min(self._admin_cache, key=lambda k: self._admin_cache[k][1])
                    del self._admin_cache[oldest_key]
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
        """Registers the Telethon NewMessage event handler."""
        @self._client.on(events.NewMessage)
        async def handler(event):
            """Filters incoming messages to active groups and delegates to _process_message."""
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
        """Processes a single Telegram message: deduplicates, checks alerts, and persists it.

        If the database insert fails, the dedup hash is rolled back so the message
        can be retried on the next encounter.
        """
        if not message.text:
            return

        text = message.text.strip()
        if not text:
            return

        if message.sender_id and await self._db.is_sender_blocked(message.sender_id):
            return

        if self._filter_bots:
            sender = message.sender
            if sender is None and message.sender_id:
                try:
                    sender = await message.get_sender()
                except Exception as e:
                    logger.debug(f"Failed to get sender for bot check: {e}")
            if getattr(sender, "bot", False):
                return

        candidate_info = self._bio_candidate_reason(text)
        is_bio_candidate = candidate_info[0]
        if is_bio_candidate:
            try:
                await self._record_sender_observation(message, group_name, text, candidate_info)
            except Exception as e:
                logger.warning("Failed to record sender observation for %s: %s", message.sender_id, e)

        if not self._dedup.check_and_add(text):
            return

        await self._check_alert(message, group_name, text)

        biz_date = message.date.astimezone(self._tz).strftime("%Y-%m-%d")
        sender_id = message.sender_id
        sender = None
        if message.sender:
            sender = self._sender_display_name(message.sender, message.sender_id)

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
            self._dedup.remove_hash(text)
            return

        if not is_bio_candidate:
            try:
                await self._record_sender_observation(message, group_name, text, candidate_info)
            except Exception as e:
                logger.warning("Failed to record sender observation for %s: %s", message.sender_id, e)

    def _current_biz_date(self) -> str:
        """Returns the current business date in YYYY-MM-DD format using the configured timezone."""
        return datetime.now(self._tz).strftime("%Y-%m-%d")

    async def stop(self) -> None:
        """Disconnects the Telegram client and stops the bot."""
        if not self._running:
            return
        self._running = False
        logger.info("Shutting down bot...")
        if self._bio_worker_task:
            self._bio_worker_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._bio_worker_task
            self._bio_worker_task = None
        if self._client:
            await self._client.disconnect()
        logger.info("Bot stopped")

    async def fetch_messages_around(self, group_id: int, message_id: int, radius: int) -> list[dict]:
        """Fetches messages within a radius of a given message ID from Telegram.

        Raises RuntimeError on FloodWait to let the caller decide on retry timing.
        """
        try:
            entity = await self._client.get_entity(group_id)
            ids = list(range(max(1, message_id - radius), message_id + radius + 1))
            messages = await self._client.get_messages(entity, ids=ids)
        except FloodWaitError as e:
            logger.warning(f"FloodWait {e.seconds}s on fetch_messages_around")
            raise RuntimeError(f"Rate limited, retry after {e.seconds}s")
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
