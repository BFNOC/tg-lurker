# mock/ — 开发模拟模块

> 面包屑: [tg-lurker](../CLAUDE.md) > mock/

无需 Telegram 登录即可运行 Web UI 的开发环境，用于前端调试和功能预览。

## 结构

```
mock/
├── __init__.py     # 包标记
├── mock_bot.py     # MockBot — 模拟 Telegram 连接状态
├── run_web.py      # 入口：启动 mock 模式 Web 服务
└── seed_data.py    # 生成假数据（群组、消息、摘要、上下文窗口）
```

## 使用方式

```bash
# 1. 生成 seed 数据（写入 ./data/messages.db）
python -m mock.seed_data

# 2. 启动 mock Web UI
python -m mock.run_web
# → http://localhost:8090  密码: demo
```

## MockBot

模拟 `Bot` 的最小接口：
- `is_connected` → 始终 True
- `client.send_message()` → 打印到 stdout
- 不实现 `_sync_groups`、`_reload_alert_keywords` 等方法

## seed_data

生成内容：
- 10 个假群组（前 5 个激活）
- 当日消息（每群 10-20 条，含真实中文技术讨论）
- 过去 5 天的摘要（含 `[m:ID]` 引用）
- 每条摘要关联 context_windows + context_messages（5 条对话上下文）
- 默认设置项

## 默认配置

| 项 | 值 |
|----|-----|
| Web 端口 | 8090 |
| 密码 | demo |
| 数据库 | ./data/messages.db |
| LLM | http://localhost:11434/v1 (不实际调用) |
