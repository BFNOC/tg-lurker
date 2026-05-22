# web/ — Web UI 模块

> 面包屑: [tg-lurker](../CLAUDE.md) > web/

FastAPI Web 界面，提供 Dashboard、群组管理、摘要查看、消息浏览、设置、告警等页面。

## 结构

```
web/
├── __init__.py      # FastAPI app 工厂，中间件，模板配置
├── auth.py          # Session 认证 + CSRF (itsdangerous)
├── routes.py        # 所有页面路由和 API 端点
├── templates/       # Jinja2 模板 (HTMX 交互)
│   ├── base.html
│   ├── login.html
│   ├── dashboard.html
│   ├── groups.html
│   ├── summaries.html
│   ├── messages.html
│   ├── settings.html
│   ├── alerts.html
│   └── help.html
└── static/          # 静态资源
```

## 认证机制

- Cookie-based session (`tg_lurker_session`)，itsdangerous 签名，默认 30 天过期，可通过 `/settings` 或 `WEB_SESSION_DAYS` 调整
- CSRF: double-submit cookie pattern (`tg_lurker_csrf`)
- 所有 POST 路由需验证 CSRF token

## 路由

| 方法 | 路径 | 功能 |
|------|------|------|
| GET | `/dashboard` | 首页概览 |
| GET | `/groups` | 群组列表 |
| POST | `/groups/sync` | 同步 Telegram 群组 |
| POST | `/groups/{id}/toggle` | 启用/禁用群组 |
| GET | `/summaries` | 摘要列表 |
| GET | `/messages` | 消息浏览 |
| POST | `/messages/block-sender` | 拉黑发送者 |
| GET | `/settings` | 设置页 |
| POST | `/settings` | 保存设置 |
| POST | `/summary/trigger` | 手动触发摘要 |
| POST | `/summary/debug-curl` | 生成调试 curl |
| GET | `/api/context/{window_id}` | 获取上下文消息 |
| POST | `/api/context/fetch-telegram` | 从 TG 拉取上下文 |
| GET | `/alerts` | 告警列表 |

## 依赖

- `request.app.state.db` — Database 实例
- `request.app.state.bot` — Bot 实例 (可选)
- `request.app.state.scheduler` — SummaryScheduler 实例
- `request.app.state.config` — Config
