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
    """Redirects to /login if the request is not authenticated."""
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    return None


def _csrf_context(request: Request) -> dict:
    """Returns a template context dict containing the CSRF token."""
    return {"csrf_token": get_csrf_token(request)}


async def _require_csrf(request: Request):
    """Returns a 403 response if the CSRF token is invalid."""
    if not await verify_csrf(request):
        return HTMLResponse("<p style='color:var(--danger)'>CSRF token invalid</p>", status_code=403)
    return None


def _current_biz_date(request: Request) -> str:
    """Returns today's business date in the configured timezone."""
    config = request.app.state.config
    return datetime.now(ZoneInfo(config.tz)).strftime("%Y-%m-%d")


def _parse_optional_group_id(request: Request) -> int | None:
    """Parses an optional group_id query parameter."""
    group_id = request.query_params.get("group_id", "")
    return int(group_id) if group_id else None


def _safe_filename_part(name: str) -> str:
    """Sanitizes a string for use as a filename component."""
    safe_name = re.sub(r"[/\\<>:\"|?*\x00-\x1f]", "_", name).strip("_")
    return safe_name or "summary"


def _summary_filename(summary: dict) -> str:
    """Generates a safe .md filename for a summary export."""
    safe_name = _safe_filename_part(summary["group_name"])
    biz_period = summary.get("biz_period", "daily")
    period_part = "" if biz_period == "daily" else f"-{_safe_filename_part(biz_period)}"
    return f"{summary['biz_date']}{period_part}-{summary['group_id']}-{safe_name}.md"


def _download_headers(filename: str) -> dict:
    """Returns HTTP headers for a file download with RFC 5987 encoded filename."""
    from urllib.parse import quote
    encoded = quote(filename, safe="")
    return {"Content-Disposition": f"attachment; filename*=UTF-8''{encoded}"}


def _escape_markdown_table_cell(value) -> str:
    """Escapes a value for safe inclusion in a Markdown table cell."""
    text = "" if value is None else str(value)
    return text.replace("|", r"\|").replace("\r\n", "<br>").replace("\n", "<br>").replace("\r", "<br>")


def _format_biz_period(biz_period: str | None) -> str:
    """Returns a human-readable label for a business period string."""
    if not biz_period or biz_period == "daily":
        return "每日摘要"
    if biz_period.startswith("manual_"):
        return f"手动 ({biz_period[7:]})"
    return f"{biz_period} 摘要"


def _activity_class(avg_daily_messages: float) -> str:
    """Maps an average daily message count to a CSS activity class."""
    if avg_daily_messages < 100:
        return "activity-low"
    if avg_daily_messages < 500:
        return "activity-normal"
    if avg_daily_messages < 1500:
        return "activity-high"
    return "activity-extreme"


def _frequency_mode(summary_cron: str | None) -> str:
    """Determines the frequency mode key for a given cron expression."""
    if not summary_cron:
        return "default"
    for mode, cron in FREQUENCY_PRESETS.items():
        if mode != "default" and cron == summary_cron:
            return mode
    return "custom"


async def _groups_template_context(request: Request) -> dict:
    """Builds the template context for the groups page, including activity and frequency data."""
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
    """Renders a summary with its context windows and optional favorite note into a Markdown string."""
    created_at = datetime.fromtimestamp(summary["created_at"], tz).strftime("%Y-%m-%d %H:%M:%S")
    period_label = _format_biz_period(summary.get("biz_period", "daily"))
    lines = [
        f"# {summary['group_name']} — {summary['biz_date']} {period_label}",
        "",
        f"> 消息数: {summary['message_count']} | 时段: {period_label} | 生成时间: {created_at}",
        "",
    ]
    if summary.get("is_favorite") and summary.get("favorite_custom_text"):
        lines.extend([
            "## 收藏备注",
            "",
            summary["favorite_custom_text"],
            "",
        ])
    lines.extend([
        "## 摘要",
        "",
        summary["summary_text"],
        "",
        "## 上下文引用",
        "",
    ])

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
    """Returns a StreamingResponse that downloads a summary as a Markdown file."""
    markdown = _render_summary_markdown(summary, tz).encode("utf-8")
    return StreamingResponse(
        io.BytesIO(markdown),
        media_type="text/markdown; charset=utf-8",
        headers=_download_headers(_summary_filename(summary)),
    )


@router.get("/")
async def index(request: Request):
    """Redirects authenticated users to /dashboard."""
    redirect = _require_auth(request)
    if redirect:
        return redirect
    return RedirectResponse("/dashboard", status_code=303)


@router.get("/help")
async def help_page(request: Request):
    """Renders the help page."""
    redirect = _require_auth(request)
    if redirect:
        return redirect
    return templates.TemplateResponse(request, "help.html", {})


@router.get("/alerts")
async def alerts_page(request: Request):
    """Renders the paginated alerts page."""
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
    """Blocks a sender and returns an inline status snippet for HTMX."""
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
    """Unblocks a sender and returns an inline status snippet for HTMX."""
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
    """Renders the paginated message browser with date/group filters."""
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
    """Renders the main dashboard with today's stats and group overview."""
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
    """Renders the statistics page shell (data loaded via API calls)."""
    redirect = _require_auth(request)
    if redirect:
        return redirect
    return templates.TemplateResponse(request, "stats.html", {})


@router.get("/api/stats/today-groups")
async def api_today_groups(request: Request):
    """Returns today's per-group message stats as JSON."""
    redirect = _require_auth(request)
    if redirect:
        return redirect

    db = request.app.state.db
    groups = await db.get_today_stats_by_group(_current_biz_date(request))
    return JSONResponse(groups)


@router.get("/api/stats/hourly")
async def api_hourly_stats(request: Request):
    """Returns today's hourly message distribution as JSON."""
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
    """Returns today's top senders as JSON."""
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
    """Returns historical daily message counts as JSON."""
    redirect = _require_auth(request)
    if redirect:
        return redirect

    db = request.app.state.db
    data = await db.get_historical_daily_counts()
    return JSONResponse(data)


@router.get("/groups")
async def groups_page(request: Request):
    """Renders the groups management page with activity and frequency info."""
    redirect = _require_auth(request)
    if redirect:
        return redirect

    context = await _groups_template_context(request)
    return templates.TemplateResponse(request, "groups.html", context)


@router.post("/groups/sync")
async def sync_groups(request: Request):
    """Syncs groups from Telegram and reloads scheduler jobs."""
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
    """Toggles a group's active status and reloads scheduler jobs."""
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
    """Updates a group's summary frequency and reloads scheduler jobs."""
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
    """Exports summaries as a single .md file or a .zip archive."""
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
    """Exports a single summary by ID as a Markdown file."""
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
    """Renders the summaries page with context windows for a selected date."""
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


@router.post("/summaries/{summary_id}/favorite")
async def toggle_favorite(request: Request, summary_id: int):
    """Toggles favorite status for a summary. Returns updated card or button HTML."""
    redirect = _require_auth(request)
    if redirect:
        return redirect
    csrf_err = await _require_csrf(request)
    if csrf_err:
        return csrf_err

    db = request.app.state.db
    identity = await db.get_summary_identity(summary_id)
    if not identity:
        return HTMLResponse("<p>Summary not found</p>", status_code=404)

    biz_date, group_id, biz_period = identity
    existing = await db.get_favorite_by_natural_key(biz_date, group_id, biz_period)
    is_from_favorites = "favorites" in request.headers.get("x-hx-target", "")

    if existing:
        await db.delete_summary_favorite(biz_date, group_id, biz_period)
        if is_from_favorites:
            return HTMLResponse("")
        return _favorite_card_html(db, summary_id, False, biz_date, group_id, biz_period)
    else:
        form = await request.form()
        custom_text = (form.get("custom_text") or "").strip() or None
        if custom_text and len(custom_text) > 4000:
            custom_text = custom_text[:4000]
        await db.upsert_summary_favorite(biz_date, group_id, biz_period, custom_text)
        return _favorite_card_html(db, summary_id, True, biz_date, group_id, biz_period)


async def _favorite_card_html(
    db, summary_id: int, is_favorite: bool,
    biz_date: str, group_id: int, biz_period: str
) -> HTMLResponse:
    """Returns the full summary-item card HTML with correct favorite state."""
    from web.routes import _format_biz_period
    summary = await db.get_summary_with_context(summary_id)
    if not summary:
        return _favorite_button_html(summary_id, is_favorite)
    fav = await db.get_favorite_by_natural_key(biz_date, group_id, biz_period) if is_favorite else None
    custom_text = fav["custom_text"] if fav else None
    period_label = _format_biz_period(summary.get("biz_period", "daily"))
    group_name = summary["group_name"]
    message_count = summary["message_count"]
    summary_text = summary["summary_text"]

    from html import escape as html_escape
    import json as _json
    safe_text = html_escape(summary_text)
    safe_custom = html_escape(custom_text) if custom_text else ""
    fav_class = " favorited" if is_favorite else ""
    badge = '<span class="fav-badge"><svg style="width:10px;height:10px;" aria-hidden="true"><use href="#icon-star-filled"></use></svg> 已收藏</span>' if is_favorite else ""
    star_icon = "#icon-star-filled" if is_favorite else "#icon-star"
    star_color = 'color:var(--favorite-gold);' if is_favorite else ''
    btn_label = "已收藏" if is_favorite else "收藏"
    btn_class = " active" if is_favorite else ""
    aria_label = "取消收藏" if is_favorite else "添加收藏"
    windows_json = html_escape(_json.dumps(summary.get("windows", [])))

    note_html = ""
    if is_favorite:
        if custom_text:
            note_html = f"""<div class="fav-note" id="fav-note-{summary_id}">
                <div class="fav-note-display"><div class="fav-note-content">{safe_custom}</div>
                <div style="margin-top:8px;"><button class="btn btn-ghost btn-sm fav-note-toggle" onclick="toggleNoteEdit({summary_id})">编辑备注</button></div></div>
                <div class="fav-note-edit" style="display:none;"><textarea maxlength="4000" placeholder="添加收藏备注...">{safe_custom}</textarea>
                <div class="fav-note-actions"><button class="btn btn-ghost btn-sm" onclick="toggleNoteEdit({summary_id})">取消</button>
                <button class="btn btn-sm" onclick="saveNote({summary_id})">保存</button><span class="fav-note-char-count"></span></div></div></div>"""
        else:
            note_html = f"""<div class="fav-note" id="fav-note-{summary_id}">
                <div class="fav-note-display"><button class="btn btn-ghost btn-sm fav-note-toggle" onclick="toggleNoteEdit({summary_id})">+ 添加备注</button></div>
                <div class="fav-note-edit" style="display:none;"><textarea maxlength="4000" placeholder="添加收藏备注..."></textarea>
                <div class="fav-note-actions"><button class="btn btn-ghost btn-sm" onclick="toggleNoteEdit({summary_id})">取消</button>
                <button class="btn btn-sm" onclick="saveNote({summary_id})">保存</button><span class="fav-note-char-count"></span></div></div></div>"""

    return HTMLResponse(
        f"""<div class="summary-item animate-fade-in{fav_class}" data-summary-item-id="{summary_id}">
            <div class="summary-header">
                <div class="summary-group-name">
                    <svg style="width:18px;height:18px;vertical-align:middle;margin-right:4px;color:var(--accent);" aria-hidden="true" role="img"><use href="#icon-groups"></use></svg>
                    {html_escape(group_name)} {badge}
                </div>
                <div class="summary-actions">
                    <span class="summary-msg-count">{html_escape(period_label)}</span>
                    <span class="summary-msg-count">{message_count} 条消息</span>
                    <button class="btn btn-ghost btn-sm fav-btn{btn_class}"
                            hx-post="/summaries/{summary_id}/favorite"
                            hx-swap="outerHTML"
                            hx-target="closest .summary-item"
                            aria-label="{aria_label}">
                        <svg style="width:14px;height:14px;{star_color}" aria-hidden="true"><use href="{star_icon}"></use></svg>
                        <span>{btn_label}</span>
                    </button>
                    <a class="btn btn-ghost btn-sm" href="/summaries/{summary_id}/export">导出</a>
                    <button class="btn btn-ghost btn-sm delete-btn"
                            hx-post="/summaries/{summary_id}/delete"
                            hx-confirm="确定删除这条摘要吗？{('此摘要已收藏，删除后收藏记录也会被删除。' if is_favorite else '')}"
                            hx-target="closest .summary-item"
                            hx-swap="outerHTML swap:300ms">删除</button>
                </div>
            </div>
            <div class="summary-text" data-summary-id="{summary_id}" data-group-id="{group_id}"
                 data-windows='{windows_json}'>{safe_text}</div>
            {note_html}
        </div>"""
    )


@router.patch("/summaries/{summary_id}/favorite")
async def update_favorite_note(request: Request, summary_id: int):
    """Updates the custom text for a favorite."""
    redirect = _require_auth(request)
    if redirect:
        return redirect
    csrf_err = await _require_csrf(request)
    if csrf_err:
        return csrf_err

    db = request.app.state.db
    identity = await db.get_summary_identity(summary_id)
    if not identity:
        return HTMLResponse("<p>Summary not found</p>", status_code=404)

    biz_date, group_id, biz_period = identity
    form = await request.form()
    custom_text = (form.get("custom_text") or "").strip() or None
    custom_text_max = 4000
    if custom_text and len(custom_text) > custom_text_max:
        return HTMLResponse(f"<p>备注最多 {custom_text_max} 字符</p>", status_code=400)

    await db.upsert_summary_favorite(biz_date, group_id, biz_period, custom_text)
    return _favorite_note_html(summary_id, custom_text)


@router.delete("/summaries/{summary_id}/favorite")
async def remove_favorite(request: Request, summary_id: int):
    """Removes a favorite record."""
    redirect = _require_auth(request)
    if redirect:
        return redirect
    csrf_err = await _require_csrf(request)
    if csrf_err:
        return csrf_err

    db = request.app.state.db
    identity = await db.get_summary_identity(summary_id)
    if not identity:
        return HTMLResponse("<p>Summary not found</p>", status_code=404)

    biz_date, group_id, biz_period = identity
    await db.delete_summary_favorite(biz_date, group_id, biz_period)
    return _favorite_button_html(summary_id, False)


@router.post("/summaries/{summary_id}/delete")
async def delete_summary(request: Request, summary_id: int):
    """Deletes a summary and its associated favorite."""
    redirect = _require_auth(request)
    if redirect:
        return redirect
    csrf_err = await _require_csrf(request)
    if csrf_err:
        return csrf_err

    db = request.app.state.db
    deleted = await db.delete_summary(summary_id)
    if not deleted:
        return HTMLResponse("<p>Summary not found</p>", status_code=404)

    hx_target = request.headers.get("x-hx-target", "")
    if "favorites" in hx_target:
        return HTMLResponse("", headers={"HX-Trigger": "favoriteRemoved"})
    return HTMLResponse("", headers={"HX-Trigger": "summaryDeleted"})


def _favorite_button_html(summary_id: int, is_favorite: bool) -> HTMLResponse:
    """Returns an HTML fragment for the favorite toggle button."""
    if is_favorite:
        return HTMLResponse(
            f"""<button class="btn btn-ghost btn-sm fav-btn active"
                        hx-post="/summaries/{summary_id}/favorite"
                        hx-swap="outerHTML"
                        aria-label="取消收藏">
                    <svg style="width:14px;height:14px;color:var(--favorite-gold);" aria-hidden="true"><use href="#icon-star-filled"></use></svg>
                    <span>已收藏</span>
                </button>"""
        )
    return HTMLResponse(
        f"""<button class="btn btn-ghost btn-sm fav-btn"
                    hx-post="/summaries/{summary_id}/favorite"
                    hx-swap="outerHTML"
                    aria-label="添加收藏">
                <svg style="width:14px;height:14px;" aria-hidden="true"><use href="#icon-star"></use></svg>
                <span>收藏</span>
            </button>"""
    )


def _favorite_note_html(summary_id: int, custom_text: str | None) -> HTMLResponse:
    """Returns an HTML fragment for the favorite note display."""
    if custom_text:
        from html import escape
        safe_text = escape(custom_text).replace("\n", "<br>")
        return HTMLResponse(
            f"""<div class="fav-note-display" data-summary-id="{summary_id}">
                    <div class="fav-note-content">{safe_text}</div>
                    <button class="btn btn-ghost btn-sm fav-note-edit-btn"
                            onclick="toggleNoteEdit({summary_id})">编辑</button>
                </div>"""
        )
    return HTMLResponse(
        f"""<div class="fav-note-empty" data-summary-id="{summary_id}">
                <button class="btn btn-ghost btn-sm fav-note-add-btn"
                        onclick="toggleNoteEdit({summary_id})">+ 添加备注</button>
            </div>"""
    )


async def _delete_favorite_by_id(db, favorite_id: int) -> bool:
    """Deletes a favorite record by its own id. Returns True if deleted."""
    cursor = await db.conn.execute(
        "DELETE FROM summary_favorites WHERE id = ?", (favorite_id,)
    )
    await db.conn.commit()
    return cursor.rowcount > 0


@router.delete("/favorites/{favorite_id}")
async def remove_favorite_by_id(request: Request, favorite_id: int):
    """Removes a favorite record by its own id (for orphaned favorites)."""
    redirect = _require_auth(request)
    if redirect:
        return redirect
    csrf_err = await _require_csrf(request)
    if csrf_err:
        return csrf_err

    db = request.app.state.db
    deleted = await _delete_favorite_by_id(db, favorite_id)
    if not deleted:
        return HTMLResponse("<p>Favorite not found</p>", status_code=404)
    return HTMLResponse("")


@router.get("/favorites")
async def favorites_page(request: Request):
    """Renders the favorites page showing all favorited summaries."""
    redirect = _require_auth(request)
    if redirect:
        return redirect

    db = request.app.state.db
    config = request.app.state.config
    tz = ZoneInfo(config.tz)

    favorites = await db.get_all_favorites()
    for fav in favorites:
        fav["biz_period_label"] = _format_biz_period(fav.get("biz_period", "daily"))
        if fav["summary_id"]:
            fav["context_windows"] = await db.get_context_windows_by_summary(fav["summary_id"])
        else:
            fav["context_windows"] = []

    group_ids = sorted({fav["group_id"] for fav in favorites})
    all_groups = await db.get_all_groups()
    group_name_map = {g["group_id"]: g["group_name"] for g in all_groups}

    return templates.TemplateResponse(request, "favorites.html", {
        "favorites": favorites,
        "group_ids": group_ids,
        "group_name_map": group_name_map,
        "tz": tz,
    })


@router.get("/favorites/export")
async def export_favorites(request: Request):
    """Exports all favorites as a .zip archive."""
    redirect = _require_auth(request)
    if redirect:
        return redirect

    db = request.app.state.db
    config = request.app.state.config
    tz = ZoneInfo(config.tz)

    favorites = await db.get_all_favorites()
    if not favorites:
        return HTMLResponse("<p>No favorites found</p>", status_code=404)

    exportable = [f for f in favorites if f["summary_id"]]
    if not exportable:
        return HTMLResponse("<p>No exportable favorites</p>", status_code=404)

    if len(exportable) == 1:
        full = await db.get_summary_with_context(exportable[0]["summary_id"])
        if full:
            return _markdown_download_response(full, tz)

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for fav in exportable:
            full = await db.get_summary_with_context(fav["summary_id"])
            if full:
                zf.writestr(_summary_filename(full), _render_summary_markdown(full, tz))
    zip_buffer.seek(0)

    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers=_download_headers("favorites.zip"),
    )


@router.get("/settings")
async def settings_page(request: Request):
    """Renders the settings form with current configuration values."""
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
        "filter_bot_messages": await db.get_setting("filter_bot_messages", "true"),
        "context_radius": await db.get_setting("context_radius", "30"),
        "context_max_rows": await db.get_setting("context_max_rows", "50000"),
    }

    return templates.TemplateResponse(request, "settings.html", {
        "settings": settings,
        "saved": False,
    })


@router.post("/settings")
async def save_settings(request: Request):
    """Saves settings from the form, reloads bot keywords and scheduler jobs."""
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
                "filter_bot_messages", "context_radius", "context_max_rows"):
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
    if bot and hasattr(bot, "_reload_filter_bots"):
        await bot._reload_filter_bots()

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
        "filter_bot_messages": await db.get_setting("filter_bot_messages", "true"),
        "context_radius": await db.get_setting("context_radius", "30"),
        "context_max_rows": await db.get_setting("context_max_rows", "50000"),
    }

    return templates.TemplateResponse(request, "settings.html", {
        "settings": settings,
        "saved": True,
    })


@router.post("/settings/test-push")
async def test_push(request: Request):
    """Sends a test message to the owner via Telegram."""
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
    """Manually triggers summary generation and returns the result report."""
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
        tz = ZoneInfo(request.app.state.config.tz)
        now = datetime.now(tz)
        biz_period = f"manual_{now.strftime('%H:%M:%S')}"

        results = await scheduler.trigger_now(group_ids=group_ids, biz_date=target_date, biz_period=biz_period)
        report = request.app.state.scheduler._summarizer.format_report(results)
        escaped = html_mod.escape(report)
        return HTMLResponse(f"<pre class='whitespace-pre-wrap' style='font-size:13px;line-height:1.7;'>{escaped}</pre>")
    except Exception as e:
        import html as html_mod
        return HTMLResponse(f"<p style='color:var(--danger)'>Error: {html_mod.escape(str(e))}</p>")


@router.post("/summary/debug-curl")
async def debug_curl(request: Request):
    """Generates a curl command for debugging the LLM API call."""
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
    """Returns stored context messages for a given window ID as JSON."""
    redirect = _require_auth(request)
    if redirect:
        return redirect

    db = request.app.state.db
    messages = await db.get_context_messages(window_id)
    return JSONResponse({"messages": messages})


@router.post("/api/context/fetch-telegram")
async def fetch_telegram_context(request: Request):
    """Fetches context messages directly from Telegram around a specific message."""
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
