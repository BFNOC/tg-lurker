from __future__ import annotations

import asyncio
import logging
import re
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
- 保持简洁，每个话题 1-2 句话
- 在摘要中引用关键消息时，使用 [m:消息ID] 格式标注来源
- 每个话题至少引用 1 条代表性消息的 ID"""

DEFAULT_USER_PROMPT = """群聊消息：
{messages}"""

_REF_INSTRUCTION = "\n\n[重要] 在摘要中必须使用 [m:消息ID] 格式引用关键消息来源，每个话题至少引用1条。"

_REF_PATTERN = re.compile(r"\[m:(\d+)\]")


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
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_prompt})
        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.3,
            max_tokens=2000,
        )
        return response.choices[0].message.content or ""

    async def _call_responses(self, client: AsyncOpenAI, model: str, system_prompt: str, user_prompt: str) -> str:
        input_messages = []
        if system_prompt:
            input_messages.append({"role": "developer", "content": system_prompt})
        input_messages.append({"role": "user", "content": user_prompt})
        response = await client.responses.create(
            model=model,
            input=input_messages,
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
            ts = datetime.fromtimestamp(msg["timestamp"], self._tz).strftime("%H:%M")
            line = f"[m:{msg['message_id']}][{ts}][{sender}]: {msg['text']}"
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

    @staticmethod
    def parse_referenced_ids(text: str) -> list[int]:
        return [int(m) for m in _REF_PATTERN.findall(text)]

    @staticmethod
    def _group_nearby_refs(ref_ids: list[int], messages: list[dict], radius: int) -> list[list[int]]:
        if not ref_ids:
            return []
        msg_positions = {m["message_id"]: i for i, m in enumerate(messages)}
        sorted_refs = sorted(set(ref_ids), key=lambda r: msg_positions.get(r, 0))
        groups: list[list[int]] = [[sorted_refs[0]]]
        for ref in sorted_refs[1:]:
            first_ref = groups[-1][0]
            pos_first = msg_positions.get(first_ref, 0)
            pos_curr = msg_positions.get(ref, 0)
            if pos_curr - pos_first <= 2 * radius:
                groups[-1].append(ref)
            else:
                groups.append([ref])
        return groups

    async def _create_context_windows(
        self, summary_id: int, group_id: int, ref_ids: list[int], messages: list[dict], context_radius: int
    ) -> set[int]:
        valid_msg_ids = {m["message_id"] for m in messages}
        valid_refs = [r for r in ref_ids if r in valid_msg_ids]
        ref_groups = self._group_nearby_refs(valid_refs, messages, context_radius)
        keep_ids: set[int] = set()
        for group in ref_groups:
            primary_ref = group[0]
            window_msgs = await self._db.get_messages_around(group_id, primary_ref, context_radius)
            if not window_msgs:
                continue
            if len(group) > 1:
                all_ids = {m["message_id"] for m in window_msgs}
                for extra_ref in group[1:]:
                    extra_msgs = await self._db.get_messages_around(group_id, extra_ref, context_radius)
                    for m in extra_msgs:
                        if m["message_id"] not in all_ids:
                            window_msgs.append(m)
                            all_ids.add(m["message_id"])
                window_msgs.sort(key=lambda m: m["message_id"])
            window_id = await self._db.insert_context_window(
                summary_id, group_id, primary_ref, covered_refs=group
            )
            await self._db.insert_context_messages(window_id, window_msgs)
            keep_ids.update(m["message_id"] for m in window_msgs)
        return keep_ids

    async def _summarize_messages(self, messages: list[dict]) -> tuple[str, list[int]] | None:
        if not messages:
            return None

        msg_text = self._truncate_messages(messages)

        system_prompt = await self._db.get_setting("system_prompt", "") or DEFAULT_SYSTEM_PROMPT
        system_prompt += _REF_INSTRUCTION
        user_prompt_tpl = await self._db.get_setting("user_prompt", "") or DEFAULT_USER_PROMPT
        user_prompt = user_prompt_tpl.format(messages=msg_text)

        try:
            summary = await self._call_llm(system_prompt, user_prompt)
            if not summary:
                return None
            summary = summary.strip()
            ref_ids = self.parse_referenced_ids(summary)
            return (summary, ref_ids)
        except Exception as e:
            group_name = messages[0].get("group_name", "Unknown")
            group_id = messages[0].get("group_id", 0)
            logger.error(f"LLM failed for group {group_name} ({group_id}): {e}")
            return None

    async def summarize_group(self, biz_date: str, group_id: int, group_name: str) -> tuple[str, list[int]] | None:
        messages = await self._db.get_messages_by_date(biz_date, group_id)
        return await self._summarize_messages(messages)

    async def run_incremental_summary(self, group_id: int) -> dict | None:
        async with self._lock:
            now = datetime.now(self._tz)
            snapshot_ts = int(now.timestamp())
            biz_date = now.strftime("%Y-%m-%d")
            biz_period = f"{now.hour:02d}:00"

            if await self._db.summary_exists(biz_date, group_id, biz_period):
                return None

            active_groups = await self._db.get_active_groups()
            group = next((g for g in active_groups if g["group_id"] == group_id), None)
            if not group:
                return None

            last_ts = await self._db.get_last_summary_ts(group_id)
            if last_ts is None:
                messages = await self._db.get_messages_by_date(biz_date, group_id)
            else:
                messages = await self._db.get_messages_since(group_id, last_ts, snapshot_ts)
            if not messages or len(messages) < 5:
                return None

            result = await self._summarize_messages(messages)
            if result is None:
                return None

            summary, ref_ids = result
            summary_id = await self._db.insert_summary(
                biz_date=biz_date,
                group_id=group_id,
                group_name=group["group_name"],
                message_count=len(messages),
                summary_text=summary,
                biz_period=biz_period,
            )
            if summary_id is None:
                return None

            context_radius = await self._get_context_radius()
            await self._create_context_windows(summary_id, group_id, ref_ids, messages, context_radius)

            await self._cleanup_expired()
            return {
                "date": biz_date,
                "biz_period": biz_period,
                "group_id": group_id,
                "name": group["group_name"],
                "count": len(messages),
                "summary": summary,
            }

    async def run_daily_summary(
        self,
        group_ids: list[int] | None = None,
        biz_date: str | None = None,
        biz_period: str = "daily",
        skip_existing: bool = False,
    ) -> dict:
        async with self._lock:
            return await self._execute_summary(group_ids, biz_date, biz_period, skip_existing)

    async def _get_context_radius(self) -> int:
        try:
            return max(5, min(100, int(await self._db.get_setting("context_radius", "30"))))
        except (ValueError, TypeError):
            return 30

    async def _execute_summary(
        self,
        group_ids: list[int] | None = None,
        biz_date: str | None = None,
        biz_period: str = "daily",
        skip_existing: bool = False,
    ) -> dict:
        if biz_date is None:
            biz_date = datetime.now(self._tz).strftime("%Y-%m-%d")

        snapshot_ts = int(datetime.now(self._tz).timestamp())
        active_groups = await self._db.get_active_groups()

        if group_ids is not None:
            active_groups = [g for g in active_groups if g["group_id"] in group_ids]

        context_radius = await self._get_context_radius()
        results: dict = {
            "date": biz_date,
            "biz_period": biz_period,
            "groups": [],
            "failed": [],
            "snapshot_ts": snapshot_ts,
        }

        for group in active_groups:
            group_id = group["group_id"]
            group_name = group["group_name"]

            if skip_existing and await self._db.summary_exists(biz_date, group_id, biz_period):
                continue

            result = await self.summarize_group(biz_date, group_id, group_name)

            if result is None:
                messages = await self._db.get_messages_by_date(biz_date, group_id)
                if messages:
                    results["failed"].append(group_name)
                continue

            summary, ref_ids = result
            messages = await self._db.get_messages_by_date(biz_date, group_id)
            msg_count = len(messages)

            summary_id = await self._db.insert_summary(
                biz_date=biz_date,
                group_id=group_id,
                group_name=group_name,
                message_count=msg_count,
                summary_text=summary,
                biz_period=biz_period,
            )
            if summary_id is None:
                continue

            keep_ids = await self._create_context_windows(summary_id, group_id, ref_ids, messages, context_radius)

            results["groups"].append({
                "name": group_name,
                "count": msg_count,
                "summary": summary,
                "group_id": group_id,
                "keep_ids": keep_ids,
                "skip_clear": not keep_ids,
            })

            await asyncio.sleep(1)

        if not results["failed"]:
            results["ready_to_clear"] = True
        else:
            results["ready_to_clear"] = False

        await self._cleanup_expired()

        return results

    def format_incremental_report(self, result: dict) -> str:
        period_label = "每日摘要" if result["biz_period"] == "daily" else f"{result['biz_period']} 摘要"
        return "\n".join([
            f"📋 群聊摘要 ({result['date']} {period_label})",
            "",
            f"【{result['name']}】({result['count']}条消息)",
            result["summary"],
        ])

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

        max_rows = int(await self._db.get_setting("context_max_rows", "50000"))
        cleaned = await self._db.cleanup_lru_contexts(max_rows)
        if cleaned:
            logger.info(f"Cleaned {cleaned} LRU context messages")

    def format_report(self, results: dict) -> str:
        biz_period = results.get("biz_period", "daily")
        if biz_period.startswith("manual_"):
            lines = [f"📋 手动群聊摘要 ({results['date']} {biz_period[7:]})", ""]
        else:
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
