from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from dotenv import load_dotenv


@dataclass(frozen=True)
class LLMProvider:
    """A complete OpenAI-compatible LLM upstream configuration."""
    id: str
    base_url: str
    api_key: str
    model: str
    api_format: str = "chat"
    models: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Normalizes models and preserves ``model`` as the primary-model view."""
        models = tuple(str(item).strip() for item in self.models if str(item).strip())
        if not models:
            model = self.model.strip()
            models = (model,) if model else ()
        object.__setattr__(self, "models", models)
        object.__setattr__(self, "model", models[0] if models else "")


def parse_llm_providers(raw: str) -> tuple[LLMProvider, ...]:
    """Parses a JSON provider list, discarding incomplete or unsupported entries."""
    try:
        values = json.loads(raw)
    except (TypeError, ValueError):
        return ()
    if not isinstance(values, list):
        return ()

    providers: list[LLMProvider] = []
    used_ids: set[str] = set()
    for index, value in enumerate(values, start=1):
        if not isinstance(value, dict):
            continue
        base_url = str(value.get("base_url", "")).strip()
        api_key = str(value.get("api_key", "")).strip()
        raw_models = value.get("models")
        if isinstance(raw_models, list):
            models = tuple(str(item).strip() for item in raw_models if str(item).strip())
        else:
            legacy_model = str(value.get("model", "")).strip()
            models = (legacy_model,) if legacy_model else ()
        api_format = str(value.get("api_format", "chat")).strip().lower()
        provider_id = str(value.get("id", f"provider-{index}")).strip()
        if (
            not base_url
            or not api_key
            or not models
            or api_format not in ("chat", "responses")
            or not provider_id
            or provider_id in used_ids
        ):
            continue
        providers.append(LLMProvider(provider_id, base_url, api_key, models[0], api_format, models))
        used_ids.add(provider_id)
    return tuple(providers)


@dataclass(frozen=True)
class Config:
    """Holds all application configuration loaded from environment variables."""
    api_id: int
    api_hash: str
    owner_id: int
    llm_base_url: str
    llm_api_key: str
    llm_model: str = "deepseek-chat"
    llm_api_format: str = "chat"
    llm_providers: tuple[LLMProvider, ...] = ()
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
    web_session_days: int = 30
    tg_push_enabled: bool = True
    tz: str = "Asia/Shanghai"


def _require(name: str) -> str:
    """Reads a required environment variable, exiting if unset or empty."""
    val = os.getenv(name, "").strip()
    if not val:
        print(f"ERROR: {name} is required but not set", file=sys.stderr)
        sys.exit(1)
    return val


def load_config(env_path: str | None = None) -> Config:
    """Loads environment variables from a .env file and returns a Config instance."""
    load_dotenv(env_path or ".env")

    proxy_port_raw = os.getenv("PROXY_PORT", "0").strip()
    proxy_port = int(proxy_port_raw) if proxy_port_raw else 0

    web_port_raw = os.getenv("WEB_PORT", "8080").strip()
    web_port = int(web_port_raw) if web_port_raw else 8080

    web_session_days_raw = os.getenv("WEB_SESSION_DAYS", "30").strip()
    web_session_days = int(web_session_days_raw) if web_session_days_raw else 30
    web_session_days = max(1, min(365, web_session_days))

    retention_raw = os.getenv("SUMMARY_RETENTION_DAYS", "7").strip()
    retention = int(retention_raw) if retention_raw else 7

    tg_push_raw = os.getenv("TG_PUSH_ENABLED", "true").strip().lower()
    tg_push = tg_push_raw not in ("false", "0", "no")

    env_providers = parse_llm_providers(os.getenv("LLM_PROVIDERS", ""))
    legacy_base_url = os.getenv("LLM_BASE_URL", "").strip()
    legacy_api_key = os.getenv("LLM_API_KEY", "").strip()
    if not env_providers:
        if not legacy_base_url:
            legacy_base_url = _require("LLM_BASE_URL")
        if not legacy_api_key:
            legacy_api_key = _require("LLM_API_KEY")
    elif not legacy_base_url or not legacy_api_key:
        legacy_base_url = legacy_base_url or env_providers[0].base_url
        legacy_api_key = legacy_api_key or env_providers[0].api_key

    return Config(
        api_id=int(_require("API_ID")),
        api_hash=_require("API_HASH"),
        owner_id=int(_require("OWNER_ID")),
        llm_base_url=legacy_base_url,
        llm_api_key=legacy_api_key,
        llm_model=os.getenv("LLM_MODEL", "deepseek-chat").strip(),
        llm_api_format=os.getenv("LLM_API_FORMAT", "chat").strip(),
        llm_providers=env_providers,
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
        web_session_days=web_session_days,
        tg_push_enabled=tg_push,
        tz=os.getenv("TZ", "Asia/Shanghai").strip(),
    )
