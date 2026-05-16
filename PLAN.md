# tg-lurker — Telegram 群聊潜水信息收集器

## 目标

用一个 Telegram 账号以 userbot 方式加入目标群聊，静默收集消息，每天通过国产大模型整理摘要后发送给指定用户。摘要完成后清空当日数据。

## 架构

```
20+ Telegram 群聊
    │
    ▼
Telethon (userbot) ── 被动监听所有群消息
    │
    ▼
SQLite ── 当日消息缓存（每天清空）
    │
    ▼
APScheduler ── 每天定时触发
    │
    ▼
按群分组 ── 每群独立调 API 摘要（并行）
    │
    ▼
拼装汇总 ── 合并为一份完整摘要
    │
    ▼
Telegram 私聊 ── 发送给 owner
    │
    ▼
清空 SQLite 当日数据
```

## 项目结构

```
tg-lurker/
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
├── config.py          # 配置管理
├── bot.py             # Telethon 监听 + 消息存储
├── dedup.py           # 广告去重（hash + n-gram 相似度）
├── summarizer.py      # LLM 摘要生成（OpenAI 兼容）
├── scheduler.py       # 定时任务调度
├── main.py            # 入口，组装所有组件
└── PLAN.md
```

## 模块设计

### 1. config.py — 配置管理

从环境变量 / .env 读取：

| 变量 | 说明 | 示例 |
|------|------|------|
| `API_ID` | Telegram API ID | `12345678` |
| `API_HASH` | Telegram API Hash | `abcdef1234567890` |
| `OWNER_ID` | 接收摘要的用户 ID | `123456789` |
| `LLM_BASE_URL` | API 地址 | `https://api.deepseek.com/v1` |
| `LLM_API_KEY` | API Key | `sk-...` |
| `LLM_MODEL` | 模型名 | `deepseek-chat` |
| `SUMMARY_CRON` | 摘要推送时间 | `0 22 * * *` (每天22:00) |
| `DB_PATH` | SQLite 路径 | `/data/messages.db` |
| `SESSION_PATH` | Telethon session 路径 | `/data/lurker.session` |
| `GROUP_IDS` | 监控的群 ID 列表（逗号分隔） | `-1001234567890,-1009876543210` |
| `PROXY_TYPE` | 代理类型 | `socks5` / `http` / 留空直连 |
| `PROXY_HOST` | 代理地址 | `127.0.0.1` |
| `PROXY_PORT` | 代理端口 | `1080` |

### 2. bot.py — 消息监听

- Telethon 连接 Telegram（支持 SOCKS5 / HTTP 代理）
- 监听 `GROUP_IDS` 中所有群的 `NewMessage` 事件
- 每条消息存入 SQLite：`(id, group_id, group_name, sender_name, text, date)`
- 去重（消息 ID 唯一）
- SQLite 开启 WAL 模式 + 每次写入后 commit（断电最多丢 1 条）
- 过滤：只存文本消息，跳过纯图片/贴纸/加入通知/服务消息
- **广告去重**：同一条广告只记录一次或直接丢弃
  - 精确去重：消息文本 normalize（去标点空格转小写）后取 hash，重复直接跳过
  - 相似去重：用 n-gram 相似度检测"基本相同"的广告（阈值 85%）
  - 维护当日已存消息的 hash 集合（内存中），新消息先查再存

### 3. summarizer.py — 摘要生成

- 按群分组，每群独立调用 API 摘要（避免单次 prompt 过长）
- 调用 OpenAI 兼容 API（适配 DeepSeek / 通义千问 / 智谱等）
- 每群消息超过 3000 字时截断，取最新部分
- 输出格式：
  ```
  📋 每日群聊摘要 (2026-05-15)

  【群A】(128条消息)
  - 主要话题：XXX
  - 关键讨论：XXX
  - 重要链接：XXX

  【群B】(45条消息)
  - ...

  ──
  共监控 20 个群，今日活跃 15 个
  ```
- **严格顺序保证**：查消息 → API 摘要 → 发送成功 → 才清空数据
- 任何一步失败都不清空，等下次定时任务重试
- 重试机制：失败后 5 分钟重试，最多重试 3 次
- 静默群（当日无消息）不出现在摘要中

### 4. scheduler.py — 定时调度

- APScheduler CronTrigger，每天定时触发
- 调用 summarizer 生成摘要
- 通过 Telethon 发送给 OWNER_ID

### 5. main.py — 入口

- 初始化配置
- 启动 Telethon client
- 注册消息处理器
- 启动 scheduler
- 阻塞运行

## Docker 部署

```yaml
# docker-compose.yml
services:
  tg-lurker:
    build: .
    restart: always
    env_file: .env
    volumes:
      - ./data:/data
    # 如果用宿主机代理：
    extra_hosts:
      - "host.docker.internal:host-gateway"
```

代理配置方式：
1. **容器内直连代理**：设置 `PROXY_HOST=宿主机IP` 或 `host.docker.internal`
2. **Docker 网络代理**：设置 `HTTP_PROXY` / `HTTPS_PROXY` 环境变量
3. **Telethon 原生代理**：通过 `PROXY_TYPE` + `PROXY_HOST` + `PROXY_PORT` 配置，支持 SOCKS5 / HTTP

## 首次登录流程

Telethon 首次登录需要交互式验证（输入手机号、验证码）：

```bash
# 本地先登录，生成 session 文件
docker compose run --rm tg-lurker python main.py
# 输入手机号 → 输入验证码 → 登录成功
# session 文件保存在 ./data/lurker.session
# 之后正常启动：docker compose up -d
```

## 推荐 LLM 提供商

| 提供商 | BASE_URL | 模型 | 价格 |
|--------|----------|------|------|
| **DeepSeek** | `https://api.deepseek.com/v1` | `deepseek-chat` | ¥1/¥2 per 1M |
| **通义千问** | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` | ¥0.8/¥2 per 1M |
| **智谱** | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-flash` | 有免费额度 |

20 个群每日摘要成本约 ¥0.05，几乎免费。每群单独摘要，互不干扰。

## 依赖

```
telethon        # Telegram MTProto 客户端
aiosqlite       # 异步 SQLite
apscheduler     # 定时任务
openai          # OpenAI 兼容 API 客户端
pysocks         # SOCKS5 代理支持
python-dotenv   # .env 配置
httpx[socks]    # HTTP 代理支持（openai 底层用 httpx）
```

## 实现顺序

1. `config.py` — 配置管理
2. `dedup.py` — 广告去重模块
3. `bot.py` — 消息监听 + SQLite 存储（集成去重）
4. `summarizer.py` — LLM 摘要
4. `scheduler.py` — 定时任务
5. `main.py` — 组装入口
6. `Dockerfile` + `docker-compose.yml` — 容器化
7. `.env.example` — 配置模板
8. 测试：本地运行 → Docker 运行

## 风险与对策

| 风险 | 对策 |
|------|------|
| 新号被风控 | 先正常使用几天再跑脚本 |
| 消息超长 | 每群独立摘要，超 3000 字截断取最新 |
| API 并发限流 | 20 个群串行调用，间隔 1 秒，总耗时 < 1 分钟 |
| 账号被封 | session 文件备份，准备备用号 |
| API 不稳定 | 重试机制，失败后 5 分钟重试一次 |
| 极端丢消息 | WAL 模式 + 逐条 commit，最多丢 1 条 |
