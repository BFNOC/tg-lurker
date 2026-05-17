from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from web import templates
from web.auth import is_authenticated, get_csrf_token, verify_csrf

router = APIRouter()


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


@router.get("/groups")
async def groups_page(request: Request):
    redirect = _require_auth(request)
    if redirect:
        return redirect

    db = request.app.state.db
    groups = await db.list_all_groups()
    return templates.TemplateResponse(request, "groups.html", {
        "groups": groups,
    })


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

    groups = await db.list_all_groups()
    return templates.TemplateResponse(request, "groups.html", {
        "groups": groups,
    }, headers={"HX-Trigger": "groupsUpdated"})


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
                "system_prompt", "user_prompt", "ad_keywords", "alert_keywords"):
        value = form.get(key, "")
        if key == "llm_api_key" and value.startswith("••••"):
            continue
        if value:
            await db.set_setting(key, str(value))
        elif key in ("system_prompt", "user_prompt", "ad_keywords", "alert_keywords"):
            await db.set_setting(key, "")

    bot = request.app.state.bot
    if bot and hasattr(bot, "_reload_alert_keywords"):
        await bot._reload_alert_keywords()

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
    }

    return templates.TemplateResponse(request, "settings.html", {
        "settings": settings,
        "saved": True,
    })


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
