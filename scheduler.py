from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from config import Config
from database import Database
from summarizer import Summarizer

logger = logging.getLogger(__name__)


class SummaryScheduler:
    MAX_RETRIES = 3
    RETRY_DELAY_MINUTES = 5

    def __init__(
        self,
        config: Config,
        summarizer: Summarizer,
        db: Database | None = None,
        send_callback=None,
    ) -> None:
        self._config = config
        self._summarizer = summarizer
        self._db = db or summarizer._db
        self._send_callback = send_callback
        self._scheduler = AsyncIOScheduler(timezone=ZoneInfo(config.tz))
        self._tz = ZoneInfo(config.tz)
        self._reload_lock = asyncio.Lock()

    def start(self) -> None:
        if not self._scheduler.running:
            self._scheduler.start()
        self._scheduler.add_job(
            self.reload_jobs,
            "date",
            run_date=datetime.now(self._tz),
            id="summary:reload",
            replace_existing=True,
        )
        logger.info("Scheduler started")

    @staticmethod
    def validate_cron(expr: str, timezone: ZoneInfo) -> CronTrigger:
        return CronTrigger.from_crontab(expr, timezone=timezone)

    def _job_options(self) -> dict:
        return {
            "replace_existing": True,
            "max_instances": 1,
            "coalesce": True,
            "misfire_grace_time": 300,
        }

    async def reload_jobs(self) -> None:
        async with self._reload_lock:
            for job in self._scheduler.get_jobs():
                if job.id == "summary:global" or job.id.startswith("summary:group:"):
                    self._scheduler.remove_job(job.id)

            await self._reload_jobs_inner()

    async def _reload_jobs_inner(self) -> None:
        global_cron = await self._db.get_setting("summary_cron", self._config.summary_cron)
        try:
            global_trigger = self.validate_cron(global_cron, self._tz)
        except ValueError as e:
            logger.error(f"Invalid global summary cron {global_cron!r}: {e}")
            global_cron = self._config.summary_cron or "0 22 * * *"
            global_trigger = self.validate_cron(global_cron, self._tz)

        self._scheduler.add_job(
            self._run_global_summary,
            trigger=global_trigger,
            id="summary:global",
            **self._job_options(),
        )

        custom_groups = await self._db.get_custom_cron_groups()
        for group in custom_groups:
            group_id = group["group_id"]
            cron_expr = group["summary_cron"]
            try:
                trigger = self.validate_cron(cron_expr, self._tz)
            except ValueError as e:
                logger.error(f"Invalid group summary cron for {group['group_name']} ({group_id}): {e}")
                continue
            self._scheduler.add_job(
                self._run_group_summary,
                trigger=trigger,
                args=[group_id],
                id=f"summary:group:{group_id}",
                **self._job_options(),
            )

        logger.info(f"Summary jobs loaded: global={global_cron}, custom_groups={len(custom_groups)}")

    async def _run_global_summary(self) -> None:
        await self._attempt_global_summary(0)

    async def _attempt_global_summary(self, retry_count: int) -> None:
        try:
            groups = await self._db.get_default_cron_groups()
            if not groups:
                logger.info("Global summary skipped: no default-cron active groups")
                return

            group_ids = [g["group_id"] for g in groups]
            results = await self._summarizer.run_daily_summary(group_ids=group_ids, skip_existing=True)
            report = self._summarizer.format_report(results)

            await self._send_report(report, bool(results["groups"]))

            if results.get("ready_to_clear"):
                await self._clear_messages(results)
                await self._clear_custom_group_messages(results["date"], results.get("snapshot_ts"))

            logger.info(f"Summary complete: {len(results['groups'])} groups")

        except Exception as e:
            await self._schedule_retry("global", retry_count, e)

    async def _run_group_summary(self, group_id: int) -> None:
        await self._attempt_group_summary(group_id, 0)

    async def _attempt_group_summary(self, group_id: int, retry_count: int) -> None:
        try:
            result = await self._summarizer.run_incremental_summary(group_id)
            if not result:
                logger.info(f"Group summary skipped: group_id={group_id}")
                return

            report = self._summarizer.format_incremental_report(result)
            await self._send_report(report, True)
            logger.info(f"Group summary complete: group_id={group_id}")
        except Exception as e:
            await self._schedule_retry(f"group:{group_id}", retry_count, e)

    async def _send_report(self, report: str, has_groups: bool) -> None:
        if not self._send_callback or not has_groups:
            return
        tg_push = await self._db.get_setting(
            "tg_push_enabled",
            str(self._config.tg_push_enabled).lower(),
        )
        if tg_push.lower() not in ("false", "0", "no"):
            await self._send_callback(report)

    async def _clear_messages(self, results: dict) -> None:
        biz_date = results["date"]
        snapshot_ts = results.get("snapshot_ts")
        for g in results["groups"]:
            if g.get("skip_clear"):
                logger.warning(f"Skipping message cleanup for {g['name']}: no valid context references")
                continue
            keep_ids = g.get("keep_ids", set())
            await self._db.delete_messages_except_context(
                biz_date, snapshot_ts, keep_ids, g["group_id"]
            )

    async def _clear_custom_group_messages(self, biz_date: str, snapshot_ts: int | None) -> None:
        for group in await self._db.get_custom_cron_groups():
            keep_ids = await self._db.get_context_message_ids_for_group_date(biz_date, group["group_id"])
            if not keep_ids:
                logger.warning(f"Skipping custom message cleanup for {group['group_name']}: no valid context references")
                continue
            await self._db.delete_messages_except_context(
                biz_date, snapshot_ts, keep_ids, group["group_id"]
            )

    async def _schedule_retry(self, scope: str, retry_count: int, error: Exception) -> None:
        next_retry = retry_count + 1
        logger.error(f"Summary failed scope={scope} attempt={next_retry}: {error}")

        if next_retry >= self.MAX_RETRIES:
            return

        if scope == "global":
            func = self._attempt_global_summary
            args = [next_retry]
        else:
            group_id = int(scope.split(":", 1)[1])
            func = self._attempt_group_summary
            args = [group_id, next_retry]

        self._scheduler.add_job(
            func,
            "date",
            run_date=datetime.now(self._tz) + timedelta(minutes=self.RETRY_DELAY_MINUTES),
            args=args,
            id=f"summary:retry:{scope}:{next_retry}",
            replace_existing=True,
        )
        logger.info(f"Retry scheduled scope={scope} in {self.RETRY_DELAY_MINUTES} minutes")

    async def trigger_now(self, group_ids: list[int] | None = None, biz_date: str | None = None) -> dict:
        return await self._summarizer.run_daily_summary(group_ids, biz_date)

    def stop(self) -> None:
        self._scheduler.shutdown(wait=False)
