from __future__ import annotations

import asyncio
import logging

from config import Config, parse_llm_providers
from database import Database
from summarizer import Summarizer, load_llm_providers


def _config(db_path: str) -> Config:
    return Config(
        api_id=1,
        api_hash="hash",
        owner_id=1,
        llm_base_url="https://legacy.example/v1",
        llm_api_key="legacy-secret-key",
        db_path=db_path,
        session_path="./data/test.session",
        web_password="secret",
    )


async def _with_summarizer(tmp_path, fn):
    db = Database(str(tmp_path / "messages.db"))
    await db.connect()
    try:
        await fn(db, Summarizer(_config(str(tmp_path / "messages.db")), db))
    finally:
        await db.close()


def test_llm_failover_uses_providers_in_order_after_failure(tmp_path):
    async def run(db, summarizer):
        await db.set_setting(
            "llm_providers",
            '[{"id":"first","base_url":"https://one","api_key":"first-key","model":"first-model","api_format":"chat"},'
            '{"id":"second","base_url":"https://two","api_key":"second-key","model":"second-model","api_format":"responses"}]',
        )
        attempted: list[str] = []

        async def call(provider, model, _system, _user):
            attempted.append(f"{provider.id}/{model}")
            if provider.id == "first":
                raise RuntimeError("unavailable")
            return "done"

        summarizer._call_provider = call
        assert await summarizer._call_llm("system", "user") == "done"
        assert attempted == ["first/first-model", "second/second-model"]

    asyncio.run(_with_summarizer(tmp_path, run))


def test_llm_failover_continues_after_empty_response(tmp_path):
    async def run(db, summarizer):
        await db.set_setting(
            "llm_providers",
            '[{"id":"first","base_url":"https://one","api_key":"first-key","model":"first-model"},'
            '{"id":"second","base_url":"https://two","api_key":"second-key","model":"second-model"}]',
        )
        attempted: list[str] = []

        async def call(provider, model, _system, _user):
            attempted.append(f"{provider.id}/{model}")
            return "" if provider.id == "first" else "fallback result"

        summarizer._call_provider = call
        assert await summarizer._call_llm("system", "user") == "fallback result"
        assert attempted == ["first/first-model", "second/second-model"]

    asyncio.run(_with_summarizer(tmp_path, run))


def test_llm_provider_loader_falls_back_to_legacy_settings(tmp_path):
    async def run(db, summarizer):
        await db.set_setting("llm_base_url", "https://saved.example/v1")
        await db.set_setting("llm_api_key", "saved-key")
        await db.set_setting("llm_model", "saved-model")
        providers = await load_llm_providers(summarizer._config, db)
        assert [(p.id, p.base_url, p.api_key, p.model, p.api_format) for p in providers] == [
            ("legacy", "https://saved.example/v1", "saved-key", "saved-model", "chat")
        ]

    asyncio.run(_with_summarizer(tmp_path, run))


def test_llm_failover_logs_no_api_key(tmp_path, caplog):
    async def run(db, summarizer):
        await db.set_setting(
            "llm_providers",
            '[{"id":"only","base_url":"https://one","api_key":"never-log-this-key","model":"one"}]',
        )

        async def call(_provider, _model, _system, _user):
            raise RuntimeError("request failed")

        summarizer._call_provider = call
        with caplog.at_level(logging.WARNING):
            try:
                await summarizer._call_llm("system", "user")
            except RuntimeError:
                pass
        assert "never-log-this-key" not in caplog.text

    asyncio.run(_with_summarizer(tmp_path, run))


def test_llm_provider_parser_normalizes_legacy_and_ordered_models():
    providers = parse_llm_providers(
        '[{"id":"legacy","base_url":"https://one","api_key":"key-one","model":"legacy-model"},'
        '{"id":"multi","base_url":"https://two","api_key":"key-two","models":["fast", "", "reliable"]}]'
    )

    assert [(provider.id, provider.model, provider.models) for provider in providers] == [
        ("legacy", "legacy-model", ("legacy-model",)),
        ("multi", "fast", ("fast", "reliable")),
    ]


def test_llm_failover_exhausts_provider_models_before_next_provider(tmp_path):
    async def run(db, summarizer):
        await db.set_setting(
            "llm_providers",
            '[{"id":"first","base_url":"https://one","api_key":"first-key","models":["first-a", "first-b"]},'
            '{"id":"second","base_url":"https://two","api_key":"second-key","models":["second-a"]}]',
        )
        attempted: list[tuple[str, str]] = []

        async def call(provider, model, _system, _user):
            attempted.append((provider.id, model))
            if model == "first-a":
                raise RuntimeError("unavailable")
            return "" if model == "first-b" else "fallback result"

        summarizer._call_provider = call
        assert await summarizer._call_llm("system", "user") == "fallback result"
        assert attempted == [("first", "first-a"), ("first", "first-b"), ("second", "second-a")]

    asyncio.run(_with_summarizer(tmp_path, run))
