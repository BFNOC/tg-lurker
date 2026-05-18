from __future__ import annotations

import io
import re
import zipfile
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from web import templates
from web.auth import is_authenticated, get_csrf_token, verify_csrf

router = APIRouter()

FREQUENCY_PRESETS = {
    "default": None,
    "4h": "0 0,4,8,12,16,20 * * *",
    "8h": "0 0,8,16 * * *",
    "12h": "0 0,12 * * *",
    "daily": "0 22 * * *",
}

FREQUENCY_LABELS = {
    "default": "全局默认",
    "4h": "每4小时",
    "8h": "每8小时",
    "12h": "每12小时",
    "daily": "每天一次",
    "custom": "自定义",
}


def _require_auth(request: Request):
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    return None


def _csrf_context(request: Request) -> dict:
    return {"csrf_token": get_csrf_token(request)}


async def _require_csrf(request: Request):
    if not await verify_csrf(request):
        return HTMLResponse("<p style='color:var(--danger)'>CSRF token invalid</p>", status_code=403)
    return None


def _current_biz_date(request: Request) -> str:
    config = request.app.state.config
    return datetime.now(ZoneInfo(config.tz)).strftime("%Y-%m-%d")


def _parse_optional_group_id(request: Request) -> int | None:
    group_id = request.query_params.get("group_id", "")
    return int(group_id) if group_id else None


def _safe_filename_part(name: str) -> str:
    safe_name = re.sub(r"[/\\<>:\"|?*\x00-\x1f]", "_", name).strip("_")
    return safe_name or "summary"


def _summary_filename(summary: dict) -> str:
    safe_name = _safe_filename_part(summary["group_name"])
    biz_period = summary.get("biz_period", "daily")
    period_part = "" if biz_period == "daily" else f"-{_safe_filename_part(biz_period)}"
    return f"{summary['biz_date']}{period_part}-{summary['group_id']}-{safe_name}.md"


def _download_headers(filename: str) -> dict:
    from urllib.parse import quote
    encoded = quote(filename, safe="")
    return {"Content-Disposition": f"attachment; filename*=UTF-8''{encoded}"}


def _escape_markdown_table_cell(value) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", r"\|").replace("\r\n", "<br>").replace("\n", "<br>").replace("\r", "<br>")


def _format_biz_period(biz_period: str | None) -> str:
    if not biz_period or biz_period == "daily":
        return "每日摘要"
    return f"{biz_period} 摘要"


def _activity_class(avg_daily_messages: float) -> str:
    if avg_daily_messages < 100:
        return "activity-low"
    if avg_daily_messages < 500:
        return "activity-normal"
    if avg_daily_messages < 1500:
        return "activity-high"
    return "activity-extreme"


def _frequency_mode(summary_cron: str | None) -> str:
    if not summary_cron:
        return "default"
    for mode, cron in FREQUENCY_PRESETS.items():
        if mode != "default" and cron == summary_cron:
            return mode
    return "custom"


async def _groups_template_context(request: Request) -> dict:
    db = request.app.state.db
    config = request.app.state.config
    global_cron = await db.get_setting("summary_cron", config.summary_cron)
    groups = await db.list_groups_with_activity()
    for group in groups:
        avg = group["avg_daily_messages"]
        mode = _frequency_mode(group.get("summary_cron"))
        group["avg_daily_messages_display"] = int(round(avg))
        group["activity_class"] = _activity_class(avg)
        group["frequency_mode"] = mode
        group["frequency_label"] = FREQUENCY_LABELS.get(mode, "自定义")
        group["effective_summary_cron"] = group.get("summary_cron") or global_cron
    return {
        "groups": groups,
        "global_summary_cron": global_cron,
        "frequency_presets": FREQUENCY_PRESETS,
        "frequency_labels": FREQUENCY_LABELS,
    }


def _render_summary_markdown(summary: dict, tz: ZoneInfo) -> str:
    created_at = datetime.fromtimestamp(summary["created_at"], tz).strftime("%Y-%m-%d %H:%M:%S")
    period_label = _format_biz_period(summary.get("biz_period", "daily"))
    lines = [
        f"# {summary['group_name']} — {summary['biz_date']} {period_label}",
        "",
        f"> 消息数: {summary['message_count']} | 时段: {period_label} | 生成时间: {created_at}",
        "",
        "## 摘要",
        "",
        summary["summary_text"],
        "",
        "## 上下文引用",
        "",
    ]

    if not summary["windows"]:
        lines.append("暂无上下文引用")
        lines.append("")
        return "\n".join(lines)

    for window in summary["windows"]:
        lines.extend([
            f"### [m:{window['ref_message_id']}]",
            "",
            "| 时间 | 发送者 | 内容 |",
            "|------|--------|------|",
        ])
        for message in window["messages"]:
            msg_time = datetime.fromtimestamp(message["timestamp"], tz).strftime("%H:%M")
            sender_name = _escape_markdown_table_cell(message.get("sender_name") or "Unknown")
            text = _escape_markdown_table_cell(message.get("text") or "")
            lines.append(f"| {msg_time} | {sender_name} | {text} |")
        lines.append("")

    return "\n".join(lines)


def _markdown_download_response(summary: dict, tz: ZoneInfo) -> StreamingResponse:
    markdown = _render_summary_markdown(summary, tz).encode("utf-8")
    return StreamingResponse(
        io.BytesIO(markdown),
        media_type="text/markdown; charset=utf-8",
        headers=_download_headers(_summary_filename(summary)),
    )


@router.get("/")
async def index(request: Request):
    redirect = _require_auth(request)
    if redirect:
        return redirect
    return RedirectResponse("/dashboard", status_code=303)


@router.get("/help")
async def help_page(request: Request):
    redirect = _require_auth(request)
    if redirect:
        return redirect
    return templates.TemplateResponse(request, "help.html", {})


@router.get("/alerts")
async def alerts_page(request: Request):
    redirect = _require_auth(request)
    if redirect:
        return redirect

    db = request.app.state.db
    page = int(request.query_params.get("page", "1"))
    per_page = 30

    total = await db.get_alert_count()
    alerts = await db.get_alerts(limit=per_page, offset=(page - 1) * per_page)
    total_pages = max(1, (total + per_page - 1) // per_page)

    return templates.TemplateResponse(request, "alerts.html", {
        "alerts": alerts,
        "total": total,
        "page": page,
        "total_pages": total_pages,
    })


@router.post("/messages/block-sender")
async def block_sender(request: Request):
    redirect = _require_auth(request)
    if redirect:
        return redirect
    csrf_err = await _require_csrf(request)
    if csrf_err:
        return csrf_err

    db = request.app.state.db
    form = await request.form()
    sender_id = int(form.get("sender_id", "0"))
    sender_name = form.get("sender_name", "")

    if sender_id:
        await db.block_sender(sender_id, sender_name, reason="ad")

    return HTMLResponse(f"<span style='color:var(--danger);font-size:12px;font-weight:700;'>已拉黑</span>")


@router.post("/messages/unblock-sender")
async def unblock_sender(request: Request):
    redirect = _require_auth(request)
    if redirect:
        return redirect
    csrf_err = await _require_csrf(request)
    if csrf_err:
        return csrf_err

    db = request.app.state.db
    form = await request.form()
    sender_id = int(form.get("sender_id", "0"))

    if sender_id:
        await db.unblock_sender(sender_id)

    return HTMLResponse("<span style='color:var(--success);font-size:12px;font-weight:700;'>已解除</span>")


@router.get("/messages")
async def messages_page(request: Request):
    redirect = _require_auth(request)
    if redirect:
        return redirect

    db = request.app.state.db
    config = request.app.state.config
    tz = ZoneInfo(config.tz)

    date = request.query_params.get("date", datetime.now(tz).strftime("%Y-%m-%d"))
    group_id = request.query_params.get("group_id", "")
    page = int(request.query_params.get("page", "1"))
    tab = request.query_params.get("tab", "messages")
    per_page = 100

    all_groups = await db.list_all_groups()
    gid = int(group_id) if group_id else None

    total = await db.get_message_count_by_date(date, gid)
    messages = await db.get_messages_by_date(date, gid, limit=per_page, offset=(page - 1) * per_page)
    total_pages = max(1, (total + per_page - 1) // per_page)

    blocked = await db.get_blocked_senders()

    unsummarized = await db.get_unsummarized_dates()
    available_dates = [u["biz_date"] for u in unsummarized]
    if date not in available_dates:
        available_dates.insert(0, date)

    return templates.TemplateResponse(request, "messages.html", {
        "messages": messages,
        "selected_date": date,
        "selected_group": group_id,
        "available_dates": sorted(set(available_dates), reverse=True),
        "groups": all_groups,
        "total": total,
        "page": page,
        "total_pages": total_pages,
        "per_page": per_page,
        "blocked": blocked,
        "tab": tab,
    })


@router.get("/dashboard")
async def dashboard(request: Request):
    redirect = _require_auth(request)
    if redirect:
        return redirect

    db = request.app.state.db
    bot = request.app.state.bot
    config = request.app.state.config
    tz = ZoneInfo(config.tz)
    biz_date = datetime.now(tz).strftime("%Y-%m-%d")

    msg_count = await db.get_today_message_count(biz_date)
    groups = await db.get_active_groups()
    summaries = await db.get_summaries_by_date(biz_date)
    unsummarized = await db.get_unsummarized_dates()
    last_summary_time = ""
    if summaries:
        last_ts = max(s["created_at"] for s in summaries)
        last_summary_time = datetime.fromtimestamp(last_ts, tz).strftime("%Y-%m-%d %H:%M")

    return templates.TemplateResponse(request, "dashboard.html", {
        "connected": bot.is_connected if bot else False,
        "msg_count": msg_count,
        "group_count": len(groups),
        "groups": groups,
        "last_summary": last_summary_time,
        "biz_date": biz_date,
        "unsummarized": unsummarized,
    })


@router.get("/stats")
async def stats_page(request: Request):
    redirect = _require_auth(request)
    if redirect:
        return redirect
    return templates.TemplateResponse(request, "stats.html", {})


@router.get("/api/stats/today-groups")
async def api_today_groups(request: Request):
    redirect = _require_auth(request)
    if redirect:
        return redirect

    db = request.app.state.db
    groups = await db.get_today_stats_by_group(_current_biz_date(request))
    return JSONResponse(groups)


@router.get("/api/stats/hourly")
async def api_hourly_stats(request: Request):
    redirect = _require_auth(request)
    if redirect:
        return redirect

    try:
        group_id = _parse_optional_group_id(request)
    except (ValueError, TypeError):
        return JSONResponse({"error": "Invalid group_id"}, status_code=400)

    db = request.app.state.db
    config = request.app.state.config
    data = await db.get_today_hourly_distribution(_current_biz_date(request), config.tz, group_id)
    return JSONResponse(data)


@router.get("/api/stats/top-senders")
async def api_top_senders(request: Request):
    redirect = _require_auth(request)
    if redirect:
        return redirect

    try:
        group_id = _parse_optional_group_id(request)
    except (ValueError, TypeError):
        return JSONResponse({"error": "Invalid group_id"}, status_code=400)

    db = request.app.state.db
    data = await db.get_today_top_senders(_current_biz_date(request), group_id)
    return JSONResponse(data)


@router.get("/api/stats/daily-trend")
async def api_daily_trend(request: Request):
    redirect = _require_auth(request)
    if redirect:
        return redirect

    db = request.app.state.db
    data = await db.get_historical_daily_counts()
    return JSONResponse(data)


@router.get("/groups")
async def groups_page(request: Request):
    redirect = _require_auth(request)
    if redirect:
        return redirect

    context = await _groups_template_context(request)
    return templates.TemplateResponse(request, "groups.html", context)


@router.post("/groups/sync")
async def sync_groups(request: Request):
    redirect = _require_auth(request)
    if redirect:
        return redirect
    csrf_err = await _require_csrf(request)
    if csrf_err:
        return csrf_err

    bot = request.app.state.bot
    if not bot or not bot.is_connected:
        return HTMLResponse("<span style='color:var(--danger);font-weight:600;'>Bot 未连接</span>")

    await bot._sync_groups()
    scheduler = request.app.state.scheduler
    if scheduler is not None:
        await scheduler.reload_jobs()
    context = await _groups_template_context(request)
    return templates.TemplateResponse(request, "groups.html", context)


@router.post("/groups/{group_id}/toggle")
async def toggle_group(request: Request, group_id: int):
    redirect = _require_auth(request)
    if redirect:
        return redirect
    csrf_err = await _require_csrf(request)
    if csrf_err:
        return csrf_err

    db = request.app.state.db
    form = await request.form()
    is_active = form.get("is_active", "0") == "1"
    await db.toggle_group(group_id, is_active)

    scheduler = request.app.state.scheduler
    if scheduler is not None:
        await scheduler.reload_jobs()

    context = await _groups_template_context(request)
    return templates.TemplateResponse(request, "groups.html", context, headers={"HX-Trigger": "groupsUpdated"})


@router.post("/groups/{group_id}/frequency")
async def update_group_frequency(request: Request, group_id: int):
    redirect = _require_auth(request)
    if redirect:
        return redirect
    csrf_err = await _require_csrf(request)
    if csrf_err:
        return csrf_err

    db = request.app.state.db
    config = request.app.state.config
    form = await request.form()
    mode = str(form.get("frequency_mode", "default"))
    custom_cron = str(form.get("custom_cron", "")).strip()

    if mode == "custom":
        summary_cron = custom_cron
    elif mode in FREQUENCY_PRESETS:
        summary_cron = FREQUENCY_PRESETS[mode]
    else:
        summary_cron = None

    if mode == "custom" and not summary_cron:
        context = await _groups_template_context(request)
        context.update({"frequency_error_group_id": group_id, "frequency_error": "请输入自定义 Cron 表达式"})
        return templates.TemplateResponse(request, "groups.html", context)

    if summary_cron is not None:
        try:
            CronTrigger.from_crontab(summary_cron, timezone=ZoneInfo(config.tz))
        except ValueError as e:
            context = await _groups_template_context(request)
            context.update({"frequency_error_group_id": group_id, "frequency_error": f"Cron 无效: {e}"})
            return templates.TemplateResponse(request, "groups.html", context)

    await db.update_group_summary_cron(group_id, summary_cron)

    scheduler = request.app.state.scheduler
    if scheduler is not None:
        await scheduler.reload_jobs()

    context = await _groups_template_context(request)
    return templates.TemplateResponse(request, "groups.html", context, headers={"HX-Trigger": "groupsUpdated"})


@router.get("/summaries/export")
async def export_summaries(request: Request):
    redirect = _require_auth(request)
    if redirect:
        return redirect

    db = request.app.state.db
    config = request.app.state.config
    tz = ZoneInfo(config.tz)

    date = request.query_params.get("date", datetime.now(tz).strftime("%Y-%m-%d"))
    try:
        group_id = _parse_optional_group_id(request)
    except (ValueError, TypeError):
        return HTMLResponse("<p>Invalid group_id</p>", status_code=400)

    summaries = await db.get_summaries_by_date_for_export(date, group_id)
    if not summaries:
        return HTMLResponse("<p>No summaries found</p>", status_code=404)
    if len(summaries) == 1:
        return _markdown_download_response(summaries[0], tz)

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for summary in summaries:
            zf.writestr(_summary_filename(summary), _render_summary_markdown(summary, tz))
    zip_buffer.seek(0)

    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers=_download_headers(f"{date}-summaries.zip"),
    )


@router.get("/summaries/{summary_id}/export")
async def export_summary(request: Request, summary_id: int):
    redirect = _require_auth(request)
    if redirect:
        return redirect

    db = request.app.state.db
    config = request.app.state.config
    summary = await db.get_summary_with_context(summary_id)
    if not summary:
        return HTMLResponse("<p>Summary not found</p>", status_code=404)

    return _markdown_download_response(summary, ZoneInfo(config.tz))


@router.get("/summaries")
async def summaries_page(request: Request):
    redirect = _require_auth(request)
    if redirect:
        return redirect

    db = request.app.state.db
    config = request.app.state.config
    tz = ZoneInfo(config.tz)

    date = request.query_params.get("date", datetime.now(tz).strftime("%Y-%m-%d"))
    available_dates = await db.get_available_dates()
    summaries = await db.get_summaries_by_date(date)

    for s in summaries:
        s["biz_period_label"] = _format_biz_period(s.get("biz_period", "daily"))
        s["context_windows"] = await db.get_context_windows_by_summary(s["id"])
        await db.touch_summary(s["id"])

    return templates.TemplateResponse(request, "summaries.html", {
        "summaries": summaries,
        "selected_date": date,
        "available_dates": available_dates,
    })


@router.get("/settings")
async def settings_page(request: Request):
    redirect = _require_auth(request)
    if redirect:
        return redirect

    db = request.app.state.db
    config = request.app.state.config

    raw_key = await db.get_setting("llm_api_key", config.llm_api_key)
    masked_key = "••••••••" + raw_key[-4:] if len(raw_key) > 4 else "••••••••"

    settings = {
        "summary_cron": await db.get_setting("summary_cron", config.summary_cron),
        "summary_retention_days": await db.get_setting("summary_retention_days", str(config.summary_retention_days)),
        "tg_push_enabled": await db.get_setting("tg_push_enabled", str(config.tg_push_enabled).lower()),
        "llm_base_url": await db.get_setting("llm_base_url", config.llm_base_url),
        "llm_api_key": masked_key,
        "llm_model": await db.get_setting("llm_model", config.llm_model),
        "llm_api_format": await db.get_setting("llm_api_format", config.llm_api_format),
        "system_prompt": await db.get_setting("system_prompt", ""),
        "user_prompt": await db.get_setting("user_prompt", ""),
        "ad_keywords": await db.get_setting("ad_keywords", ""),
        "alert_keywords": await db.get_setting("alert_keywords", ""),
        "context_radius": await db.get_setting("context_radius", "30"),
        "context_max_rows": await db.get_setting("context_max_rows", "50000"),
    }

    return templates.TemplateResponse(request, "settings.html", {
        "settings": settings,
        "saved": False,
    })


@router.post("/settings")
async def save_settings(request: Request):
    redirect = _require_auth(request)
    if redirect:
        return redirect
    csrf_err = await _require_csrf(request)
    if csrf_err:
        return csrf_err

    db = request.app.state.db
    form = await request.form()

    for key in ("summary_cron", "summary_retention_days", "tg_push_enabled",
                "llm_base_url", "llm_api_key", "llm_model", "llm_api_format",
                "system_prompt", "user_prompt", "ad_keywords", "alert_keywords",
                "context_radius", "context_max_rows"):
        value = form.get(key, "")
        if key == "llm_api_key" and value.startswith("••••"):
            continue
        if key == "context_radius":
            try:
                value = str(max(5, min(100, int(value or "30"))))
            except (ValueError, TypeError):
                value = "30"
        if key == "context_max_rows":
            try:
                value = str(max(1000, min(500000, int(value or "50000"))))
            except (ValueError, TypeError):
                value = "50000"
        if value:
            await db.set_setting(key, str(value))
        elif key in ("system_prompt", "user_prompt", "ad_keywords", "alert_keywords"):
            await db.set_setting(key, "")

    bot = request.app.state.bot
    if bot and hasattr(bot, "_reload_alert_keywords"):
        await bot._reload_alert_keywords()

    scheduler = request.app.state.scheduler
    if scheduler is not None:
        await scheduler.reload_jobs()

    config = request.app.state.config
    raw_key = await db.get_setting("llm_api_key", config.llm_api_key)
    masked_key = "••••••••" + raw_key[-4:] if len(raw_key) > 4 else "••••••••"

    settings = {
        "summary_cron": await db.get_setting("summary_cron", config.summary_cron),
        "summary_retention_days": await db.get_setting("summary_retention_days", str(config.summary_retention_days)),
        "tg_push_enabled": await db.get_setting("tg_push_enabled", str(config.tg_push_enabled).lower()),
        "llm_base_url": await db.get_setting("llm_base_url", config.llm_base_url),
        "llm_api_key": masked_key,
        "llm_model": await db.get_setting("llm_model", config.llm_model),
        "llm_api_format": await db.get_setting("llm_api_format", config.llm_api_format),
        "system_prompt": await db.get_setting("system_prompt", ""),
        "user_prompt": await db.get_setting("user_prompt", ""),
        "ad_keywords": await db.get_setting("ad_keywords", ""),
        "alert_keywords": await db.get_setting("alert_keywords", ""),
        "context_radius": await db.get_setting("context_radius", "30"),
        "context_max_rows": await db.get_setting("context_max_rows", "50000"),
    }

    return templates.TemplateResponse(request, "settings.html", {
        "settings": settings,
        "saved": True,
    })


@router.post("/settings/test-push")
async def test_push(request: Request):
    redirect = _require_auth(request)
    if redirect:
        return redirect
    csrf_err = await _require_csrf(request)
    if csrf_err:
        return csrf_err

    bot = request.app.state.bot
    if not bot or not bot.is_connected:
        return HTMLResponse("<span style='color:var(--danger);font-weight:600;'>Bot 未连接</span>")

    config = request.app.state.config
    try:
        await bot.client.send_message(config.owner_id, "🔔 tg-lurker 推送测试\n\n如果你看到这条消息，说明推送功能正常工作。")
        return HTMLResponse("<span style='color:var(--success);font-weight:600;'>已发送，请检查 Telegram</span>")
    except Exception as e:
        return HTMLResponse(f"<span style='color:var(--danger);font-weight:600;'>发送失败: {e}</span>")


@router.post("/summary/trigger")
async def trigger_summary(request: Request):
    redirect = _require_auth(request)
    if redirect:
        return redirect
    csrf_err = await _require_csrf(request)
    if csrf_err:
        return csrf_err

    scheduler = request.app.state.scheduler
    if scheduler is None:
        return HTMLResponse("<p style='color:var(--danger)'>Scheduler not available</p>")

    try:
        import html as html_mod
        form = await request.form()
        selected = form.getlist("group_ids")
        group_ids = [int(gid) for gid in selected] if selected else None
        target_date = form.get("biz_date", None) or None

        results = await scheduler.trigger_now(group_ids=group_ids, biz_date=target_date)
        report = request.app.state.scheduler._summarizer.format_report(results)
        escaped = html_mod.escape(report)
        return HTMLResponse(f"<pre class='whitespace-pre-wrap' style='font-size:13px;line-height:1.7;'>{escaped}</pre>")
    except Exception as e:
        import html as html_mod
        return HTMLResponse(f"<p style='color:var(--danger)'>Error: {html_mod.escape(str(e))}</p>")


@router.post("/summary/debug-curl")
async def debug_curl(request: Request):
    redirect = _require_auth(request)
    if redirect:
        return redirect
    csrf_err = await _require_csrf(request)
    if csrf_err:
        return csrf_err

    import html as html_mod
    import json

    db = request.app.state.db
    config = request.app.state.config
    tz = ZoneInfo(config.tz)

    form = await request.form()
    selected = form.getlist("group_ids")
    target_date = form.get("biz_date", None) or datetime.now(tz).strftime("%Y-%m-%d")

    base_url = await db.get_setting("llm_base_url", config.llm_base_url)
    api_key = await db.get_setting("llm_api_key", config.llm_api_key)
    model = await db.get_setting("llm_model", config.llm_model)
    api_format = await db.get_setting("llm_api_format", config.llm_api_format)
    system_prompt = await db.get_setting("system_prompt", "")
    user_prompt_tpl = await db.get_setting("user_prompt", "")

    if not system_prompt:
        from summarizer import DEFAULT_SYSTEM_PROMPT
        system_prompt = DEFAULT_SYSTEM_PROMPT
    if not user_prompt_tpl:
        from summarizer import DEFAULT_USER_PROMPT
        user_prompt_tpl = DEFAULT_USER_PROMPT

    active_groups = await db.get_active_groups()
    if selected:
        gids = [int(g) for g in selected]
        active_groups = [g for g in active_groups if g["group_id"] in gids]

    if not active_groups:
        return HTMLResponse("<p>No active groups</p>")

    group = active_groups[0]
    messages = await db.get_messages_by_date(target_date, group["group_id"])
    if not messages:
        return HTMLResponse(f"<p>No messages for {group['group_name']} on {target_date}</p>")

    from summarizer import Summarizer
    s = Summarizer(config, db)
    msg_text = s._truncate_messages(messages)
    user_prompt = user_prompt_tpl.format(messages=msg_text)

    masked_key = api_key[:8] + "..." + api_key[-4:] if len(api_key) > 12 else "sk-***"

    if api_format == "responses":
        body = {
            "model": model,
            "input": [
                {"role": "developer", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        url = f"{base_url.rstrip('/')}/responses"
    else:
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.3,
            "max_tokens": 2000,
        }
        url = f"{base_url.rstrip('/')}/chat/completions"

    body_json = json.dumps(body, ensure_ascii=False, indent=2)
    curl_cmd = (
        f"curl -X POST '{url}' \\\n"
        f"  -H 'Content-Type: application/json' \\\n"
        f"  -H 'Authorization: Bearer {masked_key}' \\\n"
        f"  -d '{body_json}'"
    )

    info = (
        f"API Format: {api_format}\n"
        f"Model: {model}\n"
        f"URL: {url}\n"
        f"Messages count: {len(messages)}\n"
        f"Truncated text length: {len(msg_text)} chars\n"
        f"Group: {group['group_name']}\n\n"
        f"--- curl command (replace key with real one) ---\n\n"
        f"{curl_cmd}"
    )

    return HTMLResponse(f"<pre style='font-size:12px;line-height:1.6;white-space:pre-wrap;word-break:break-all;'>{html_mod.escape(info)}</pre>")


@router.get("/api/context/{window_id}")
async def get_context(request: Request, window_id: int):
    redirect = _require_auth(request)
    if redirect:
        return redirect

    db = request.app.state.db
    messages = await db.get_context_messages(window_id)
    return JSONResponse({"messages": messages})


@router.post("/api/context/fetch-telegram")
async def fetch_telegram_context(request: Request):
    redirect = _require_auth(request)
    if redirect:
        return redirect
    csrf_err = await _require_csrf(request)
    if csrf_err:
        return csrf_err

    bot = request.app.state.bot
    if not bot or not bot.is_connected:
        return JSONResponse({"error": "Bot not connected"}, status_code=503)

    form = await request.form()
    try:
        group_id = int(form.get("group_id", "0"))
        message_id = int(form.get("message_id", "0"))
    except (ValueError, TypeError):
        return JSONResponse({"error": "Invalid parameters"}, status_code=400)
    try:
        radius = max(1, min(100, int(form.get("radius", "30"))))
    except (ValueError, TypeError):
        return JSONResponse({"error": "Invalid radius"}, status_code=400)

    if not group_id or not message_id:
        return JSONResponse({"error": "Missing group_id or message_id"}, status_code=400)

    db = request.app.state.db
    active_groups = await db.get_active_groups()
    active_ids = {g["group_id"] for g in active_groups}
    if group_id not in active_ids:
        return JSONResponse({"error": "Group not monitored"}, status_code=403)

    try:
        messages = await bot.fetch_messages_around(group_id, message_id, radius)
        return JSONResponse({"messages": messages})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
