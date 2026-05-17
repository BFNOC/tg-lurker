from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

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

    for s in summaries:
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
