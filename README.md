# tg-lurker

Telegram 群聊潜水信息收集器 — 静默监控群消息，AI 每日摘要，Web 管理面板。

## 功能

- **被动监听** — 以 userbot 方式加入群聊，静默收集所有文本消息
- **广告过滤** — hash 精确去重 + trigram 85% 相似度 + 关键词黑名单 + 发送者拉黑
- **Bio 采集** — 低速抓取疑似广告发送者 Bio，按账号合并多群出现记录
- **AI 摘要** — 每日定时调用 LLM 生成按群分组的中文摘要
- **实时告警** — 管理员/群主发送的消息命中关键词时立即推送
- **Web 管理** — 暗色主题，中文界面，群组管理/消息浏览/设置/帮助
- **Docker 部署** — 单容器，volume 持久化

## 快速开始

### 1. 获取 Telegram API 凭证

1. 访问 https://my.telegram.org
2. 登录 → API development tools
3. 获取 `API_ID` 和 `API_HASH`

### 2. 获取 Owner ID

向 Telegram 的 `@userinfobot` 发送任意消息，获取你的数字 User ID。

### 3. 配置

```bash
cp .env.example .env
```

编辑 `.env`，填入必需项：

```env
API_ID=12345678
API_HASH=abcdef1234567890
OWNER_ID=123456789
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_API_KEY=sk-your-key
WEB_PASSWORD=your-password
```

### 4. 首次登录

Telethon 首次运行需要交互式验证：

```bash
# Docker 方式
docker compose run --rm tg-lurker python main.py

# 或本地方式
python main.py
```

按提示输入：
1. 手机号（带国际区号，如 `+8613800138000`）
2. Telegram 发送的验证码
3. 如有两步验证，输入密码

登录成功后 session 文件保存在 `./data/lurker.session`。

### 5. 启动

```bash
# Docker（推荐）
docker compose up -d

# 本地
python main.py
```

### 6. 访问 Web 管理

打开 `http://your-server:8080`，用 `WEB_PASSWORD` 登录。

## Web 管理面板

| 页面 | 功能 |
|------|------|
| 仪表盘 | 机器人状态、今日消息数、手动触发摘要（支持选群/选日期） |
| 群组 | 查看所有已加入群，toggle 开关监控状态 |
| 消息 | 浏览原始消息（分页）、拉黑发送者、手动加入 Bio 队列、管理黑名单 |
| 广告Bio | 查看疑似广告发送者 Bio 原文、来源群组和可点击入口 |
| 摘要 | 按日期查看历史摘要 |
| 设置 | LLM 配置、Prompt 自定义、广告关键词、实时告警关键词、推送开关 |
| 帮助 | 完整使用教程 |

## 配置项

| 变量 | 必需 | 默认值 | 说明 |
|------|------|--------|------|
| `API_ID` | 是 | - | Telegram API ID |
| `API_HASH` | 是 | - | Telegram API Hash |
| `OWNER_ID` | 是 | - | 接收推送的用户 ID |
| `LLM_BASE_URL` | 是 | - | LLM API 地址（任何 OpenAI 兼容） |
| `LLM_API_KEY` | 是 | - | LLM API Key |
| `WEB_PASSWORD` | 是 | - | Web 登录密码 |
| `LLM_MODEL` | 否 | `deepseek-chat` | 模型名 |
| `LLM_API_FORMAT` | 否 | `chat` | `chat` 或 `responses` |
| `LLM_PROXY_URL` | 否 | - | LLM 调用代理 |
| `SUMMARY_CRON` | 否 | `0 22 * * *` | 摘要时间（Asia/Shanghai） |
| `SUMMARY_RETENTION_DAYS` | 否 | `7` | 摘要保留天数 |
| `DB_PATH` | 否 | `./data/messages.db` | SQLite 路径 |
| `SESSION_PATH` | 否 | `./data/lurker.session` | Session 路径 |
| `PROXY_TYPE` | 否 | - | Telegram 代理类型（socks5/http） |
| `PROXY_HOST` | 否 | - | Telegram 代理地址 |
| `PROXY_PORT` | 否 | - | Telegram 代理端口 |
| `WEB_PORT` | 否 | `8080` | Web UI 端口 |
| `WEB_SESSION_DAYS` | 否 | `30` | Web 登录有效期（天，可在设置页修改） |
| `TG_PUSH_ENABLED` | 否 | `true` | Telegram 推送开关 |
| `TZ` | 否 | `Asia/Shanghai` | 时区 |

## 代理配置

Telegram 代理和 LLM 代理是**独立**的：

```env
# Telegram 连接代理（MTProto）
PROXY_TYPE=socks5
PROXY_HOST=127.0.0.1
PROXY_PORT=1080

# LLM API 调用代理（httpx）
LLM_PROXY_URL=http://127.0.0.1:7897
```

Docker 中访问宿主机代理用 `host.docker.internal`。

## 广告过滤

三层过滤机制：

1. **关键词黑名单** — 命中 ≥2 个关键词的消息直接丢弃（设置页配置）
2. **精确 hash** — 完全相同的消息只保留第一条
3. **相似度** — 与最近 200 条消息 trigram 相似度 ≥85% 的丢弃

另外支持**发送者拉黑**：在消息浏览页点击「拉黑」，该用户所有未来消息自动跳过。

## 实时告警

当**群主或管理员**发送的消息命中告警关键词时，立即通过 Telegram 私聊推送通知。

配置：设置页 → 实时告警 → 填入关键词（如：抽奖、福利、奖品、口令）

普通群员的消息不会触发告警。

## LLM 摘要失败处理

- 失败的群消息**不会被清空**，保留在数据库中
- 定时任务自动重试（5 分钟间隔，最多 3 次）
- 仪表盘显示「有未摘要的历史消息」警告
- 可手动选择日期和群组重新生成

## 推荐 LLM

| 平台 | Base URL | 模型 | 价格 |
|------|----------|------|------|
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` | ¥1/M tokens |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` | ¥0.8/M |
| 智谱 AI | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-flash` | 免费额度 |

20 个群每日摘要成本约 ¥0.05。

## 本地开发（Mock 模式）

不需要 Telegram 账号即可预览 Web UI：

```bash
# 创建虚拟环境
uv venv .venv && source .venv/bin/activate
uv pip install -r requirements.txt

# 填充测试数据
python mock/seed_data.py

# 启动 Mock Web（密码: demo）
python mock/run_web.py
```

访问 `http://localhost:8090`。

## 项目结构

```
tg-lurker/
├── main.py              # 入口
├── config.py            # 配置管理
├── database.py          # SQLite 封装
├── bot.py               # Telethon 监听 + 告警
├── dedup.py             # 广告去重引擎
├── summarizer.py        # LLM 摘要生成
├── scheduler.py         # 定时任务
├── web/                 # FastAPI Web UI
│   ├── __init__.py
│   ├── auth.py
│   ├── routes.py
│   └── templates/
├── mock/                # Mock 测试环境
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .env.example
```

## 注意事项

- 使用**备用号**，新号先正常使用几天再运行
- Session 文件等同于登录凭证，妥善保管
- 本工具只做被动监听，不主动发消息（除了推送给 owner）
- 建议部署在稳定网络环境，避免频繁断线重连
