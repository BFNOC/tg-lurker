from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from openai import AsyncOpenAI

from config import Config
from database import Database

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = """你是一个群聊摘要助手。请根据以下群聊消息，生成简洁的中文摘要。

要求：
- 提取主要话题和关键讨论
- 列出重要链接（如有）
- 忽略无意义的闲聊和重复内容
- 保持简洁，每个话题 1-2 句话"""

DEFAULT_USER_PROMPT = """群聊消息：
{messages}"""


class Summarizer:
    def __init__(self, config: Config, db: Database) -> None:
        self._config = config
        self._db = db
        self._tz = ZoneInfo(config.tz)
        self._lock = asyncio.Lock()
        self._client: AsyncOpenAI | None = None

    def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            http_client = None
            proxy_url = self._config.llm_proxy_url
            if proxy_url:
                http_client = httpx.AsyncClient(proxy=proxy_url)
            self._client = AsyncOpenAI(
                base_url=self._config.llm_base_url,
                api_key=self._config.llm_api_key,
                http_client=http_client,
                timeout=60.0,
            )
        return self._client

    async def _reload_llm_config(self) -> tuple[str, str, str, str]:
        base_url = await self._db.get_setting("llm_base_url", self._config.llm_base_url)
        api_key = await self._db.get_setting("llm_api_key", self._config.llm_api_key)
        model = await self._db.get_setting("llm_model", self._config.llm_model)
        api_format = await self._db.get_setting("llm_api_format", self._config.llm_api_format)
        return base_url, api_key, model, api_format

    async def _call_chat(self, client: AsyncOpenAI, model: str, system_prompt: str, user_prompt: str) -> str:
        messages = []
        if system_prompt:
            messages.append({"role": "developer", "content": system_prompt})
        messages.append({"role": "user", "content": user_prompt})
        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.3,
            max_tokens=2000,
        )
        return response.choices[0].message.content or ""

    async def _call_responses(self, client: AsyncOpenAI, model: str, system_prompt: str, user_prompt: str) -> str:
        response = await client.responses.create(
            model=model,
            instructions=system_prompt or None,
            input=user_prompt,
        )
        return response.output_text or ""

    async def _call_llm(self, system_prompt: str, user_prompt: str) -> str:
        base_url, api_key, model, api_format = await self._reload_llm_config()

        proxy_url = self._config.llm_proxy_url
        http_client = httpx.AsyncClient(proxy=proxy_url) if proxy_url else None

        client = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key,
            http_client=http_client,
            timeout=60.0,
        )

        try:
            if api_format == "responses":
                return await self._call_responses(client, model, system_prompt, user_prompt)
            return await self._call_chat(client, model, system_prompt, user_prompt)
        finally:
            await client.close()

    def _truncate_messages(self, messages: list[dict], max_chars: int = 3000) -> str:
        lines = []
        for msg in reversed(messages):
            sender = msg["sender_name"] or "Unknown"
            line = f"[{sender}]: {msg['text']}"
            lines.insert(0, line)

        text = "\n".join(lines)
        if len(text) <= max_chars:
            return text

        text = ""
        for line in reversed(lines):
            candidate = line + "\n" + text if text else line
            if len(candidate) > max_chars:
                break
            text = candidate
        return text

    async def summarize_group(self, biz_date: str, group_id: int, group_name: str) -> str | None:
        messages = await self._db.get_messages_by_date(biz_date, group_id)
        if not messages:
            return None

        msg_text = self._truncate_messages(messages)

        system_prompt = await self._db.get_setting("system_prompt", DEFAULT_SYSTEM_PROMPT)
        user_prompt_tpl = await self._db.get_setting("user_prompt", DEFAULT_USER_PROMPT)
        user_prompt = user_prompt_tpl.format(messages=msg_text)

        try:
            summary = await self._call_llm(system_prompt, user_prompt)
            return summary.strip() if summary else None
        except Exception as e:
            logger.error(f"LLM failed for group {group_name} ({group_id}): {e}")
            return None

    async def run_daily_summary(self, group_ids: list[int] | None = None, biz_date: str | None = None) -> dict:
        async with self._lock:
            return await self._execute_summary(group_ids, biz_date)

    async def _execute_summary(self, group_ids: list[int] | None = None, biz_date: str | None = None) -> dict:
        if biz_date is None:
            biz_date = datetime.now(self._tz).strftime("%Y-%m-%d")

        snapshot_ts = int(datetime.now(self._tz).timestamp())
        active_groups = await self._db.get_active_groups()

        if group_ids is not None:
            active_groups = [g for g in active_groups if g["group_id"] in group_ids]

        results: dict = {"date": biz_date, "groups": [], "failed": [], "snapshot_ts": snapshot_ts}

        for group in active_groups:
            group_id = group["group_id"]
            group_name = group["group_name"]

            summary = await self.summarize_group(biz_date, group_id, group_name)

            if summary is None:
                messages = await self._db.get_messages_by_date(biz_date, group_id)
                if messages:
                    results["failed"].append(group_name)
                continue

            messages = await self._db.get_messages_by_date(biz_date, group_id)
            msg_count = len(messages)

            await self._db.insert_summary(
                biz_date=biz_date,
                group_id=group_id,
                group_name=group_name,
                message_count=msg_count,
                summary_text=summary,
            )
            results["groups"].append({
                "name": group_name,
                "count": msg_count,
                "summary": summary,
            })

            await asyncio.sleep(1)

        if not results["failed"]:
            results["ready_to_clear"] = True
        else:
            results["ready_to_clear"] = False

        await self._cleanup_expired()

        return results

    async def _cleanup_expired(self) -> None:
        retention_str = await self._db.get_setting(
            "summary_retention_days", str(self._config.summary_retention_days)
        )
        retention = int(retention_str)
        cutoff = datetime.now(self._tz) - timedelta(days=retention)
        cutoff_date = cutoff.strftime("%Y-%m-%d")
        deleted = await self._db.delete_expired_summaries(cutoff_date)
        if deleted:
            logger.info(f"Cleaned {deleted} expired summaries before {cutoff_date}")

    def format_report(self, results: dict) -> str:
        lines = [f"📋 每日群聊摘要 ({results['date']})", ""]

        for g in results["groups"]:
            lines.append(f"【{g['name']}】({g['count']}条消息)")
            lines.append(g["summary"])
            lines.append("")

        if results["failed"]:
            lines.append("⚠️ 摘要失败的群:")
            for name in results["failed"]:
                lines.append(f"  - {name}")
            lines.append("")

        total = len(results["groups"]) + len(results["failed"])
        active = len(results["groups"])
        lines.append("──")
        lines.append(f"共监控 {total} 个活跃群，成功摘要 {active} 个")

        return "\n".join(lines)
