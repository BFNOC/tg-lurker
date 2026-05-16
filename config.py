from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    owner_id: int
    llm_base_url: str
    llm_api_key: str
    llm_model: str = "deepseek-chat"
    llm_api_format: str = "chat"
    llm_proxy_url: str = ""
    summary_cron: str = "0 22 * * *"
    summary_retention_days: int = 7
    db_path: str = "./data/messages.db"
    session_path: str = "./data/lurker.session"
    proxy_type: str = ""
    proxy_host: str = ""
    proxy_port: int = 0
    web_port: int = 8080
    web_password: str = ""
    tg_push_enabled: bool = True
    tz: str = "Asia/Shanghai"


def _require(name: str) -> str:
    val = os.getenv(name, "").strip()
    if not val:
        print(f"ERROR: {name} is required but not set", file=sys.stderr)
        sys.exit(1)
    return val


def load_config(env_path: str | None = None) -> Config:
    load_dotenv(env_path or ".env")

    proxy_port_raw = os.getenv("PROXY_PORT", "0").strip()
    proxy_port = int(proxy_port_raw) if proxy_port_raw else 0

    web_port_raw = os.getenv("WEB_PORT", "8080").strip()
    web_port = int(web_port_raw) if web_port_raw else 8080

    retention_raw = os.getenv("SUMMARY_RETENTION_DAYS", "7").strip()
    retention = int(retention_raw) if retention_raw else 7

    tg_push_raw = os.getenv("TG_PUSH_ENABLED", "true").strip().lower()
    tg_push = tg_push_raw not in ("false", "0", "no")

    return Config(
        api_id=int(_require("API_ID")),
        api_hash=_require("API_HASH"),
        owner_id=int(_require("OWNER_ID")),
        llm_base_url=_require("LLM_BASE_URL"),
        llm_api_key=_require("LLM_API_KEY"),
        llm_model=os.getenv("LLM_MODEL", "deepseek-chat").strip(),
        llm_api_format=os.getenv("LLM_API_FORMAT", "chat").strip(),
        llm_proxy_url=os.getenv("LLM_PROXY_URL", "").strip(),
        summary_cron=os.getenv("SUMMARY_CRON", "0 22 * * *").strip(),
        summary_retention_days=retention,
        db_path=os.getenv("DB_PATH", "./data/messages.db").strip(),
        session_path=os.getenv("SESSION_PATH", "./data/lurker.session").strip(),
        proxy_type=os.getenv("PROXY_TYPE", "").strip(),
        proxy_host=os.getenv("PROXY_HOST", "").strip(),
        proxy_port=proxy_port,
        web_port=web_port,
        web_password=_require("WEB_PASSWORD"),
        tg_push_enabled=tg_push,
        tz=os.getenv("TZ", "Asia/Shanghai").strip(),
    )
