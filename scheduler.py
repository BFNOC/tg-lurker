from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from config import Config
from summarizer import Summarizer

logger = logging.getLogger(__name__)


class SummaryScheduler:
    MAX_RETRIES = 3
    RETRY_DELAY_MINUTES = 5

    def __init__(
        self,
        config: Config,
        summarizer: Summarizer,
        send_callback=None,
    ) -> None:
        self._config = config
        self._summarizer = summarizer
        self._send_callback = send_callback
        self._scheduler = AsyncIOScheduler(timezone=ZoneInfo(config.tz))
        self._retry_count = 0

    def start(self) -> None:
        parts = self._config.summary_cron.split()
        if len(parts) == 5:
            minute, hour, day, month, dow = parts
        else:
            minute, hour, day, month, dow = "0", "22", "*", "*", "*"

        trigger = CronTrigger(
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=dow,
            timezone=ZoneInfo(self._config.tz),
        )

        self._scheduler.add_job(
            self._run_summary_job,
            trigger=trigger,
            id="daily_summary",
            replace_existing=True,
        )
        self._scheduler.start()
        logger.info(f"Scheduler started: cron={self._config.summary_cron}")

    async def _run_summary_job(self) -> None:
        self._retry_count = 0
        await self._attempt_summary()

    async def _attempt_summary(self) -> None:
        try:
            results = await self._summarizer.run_daily_summary()
            report = self._summarizer.format_report(results)

            if self._send_callback and results["groups"]:
                tg_push = await self._summarizer._db.get_setting(
                    "tg_push_enabled",
                    str(self._config.tg_push_enabled).lower(),
                )
                if tg_push.lower() not in ("false", "0", "no"):
                    await self._send_callback(report)

            if results.get("ready_to_clear"):
                biz_date = results["date"]
                snapshot_ts = results.get("snapshot_ts")
                for g in results["groups"]:
                    if g.get("no_ref"):
                        logger.warning(f"Skipping message cleanup for {g['name']}: no context references")
                        continue
                    keep_ids = g.get("keep_ids", set())
                    await self._summarizer._db.delete_messages_except_context(
                        biz_date, snapshot_ts, keep_ids, g["group_id"]
                    )

            logger.info(f"Summary complete: {len(results['groups'])} groups")
            self._retry_count = 0

        except Exception as e:
            self._retry_count += 1
            logger.error(f"Summary failed (attempt {self._retry_count}): {e}")

            if self._retry_count < self.MAX_RETRIES:
                self._scheduler.add_job(
                    self._attempt_summary,
                    "date",
                    run_date=datetime.now(ZoneInfo(self._config.tz))
                    + timedelta(minutes=self.RETRY_DELAY_MINUTES),
                    id=f"summary_retry_{self._retry_count}",
                    replace_existing=True,
                )
                logger.info(f"Retry scheduled in {self.RETRY_DELAY_MINUTES} minutes")

    async def trigger_now(self, group_ids: list[int] | None = None, biz_date: str | None = None) -> dict:
        return await self._summarizer.run_daily_summary(group_ids, biz_date)

    def stop(self) -> None:
        self._scheduler.shutdown(wait=False)
