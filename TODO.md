# tg-lurker 功能路线图

## P0 — 高价值低成本

### 全文搜索
- [ ] messages 表添加 FTS5 虚拟表（SQLite 原生支持）
- [ ] database.py 添加 `search_messages(query, limit)` 方法
- [ ] Web UI 添加搜索页面（支持关键词 + 日期范围 + 群组筛选）
- [ ] 搜索结果高亮匹配词

### 消息统计面板
- [ ] Dashboard 添加统计卡片：每群日消息量趋势图
- [ ] 高频发言人 Top 10（按群/全局）
- [ ] 活跃时段热力图（按小时分布）
- [ ] 纯 SQL 聚合，前端用轻量图表库（Chart.js 或内联 SVG）

### 导出
- [ ] 摘要导出为 Markdown（含 context_messages 上下文）
- [ ] 按日期范围批量导出
- [ ] 导出格式：`.md` 文件，摘要正文 + 引用消息折叠块
- [ ] Web UI 添加导出按钮（返回文件下载）

## P1 — 中等投入

### RAG 问答
- [ ] 新增 embeddings 表（message_id, vector BLOB, model）
- [ ] 复用 OpenAI-compatible API 的 embedding 接口（或 Ollama nomic-embed-text）
- [ ] 设置页添加 embedding 模型配置（base_url, model_name）
- [ ] 新消息入库时异步生成 embedding
- [ ] Web UI 添加问答页面：输入自然语言 → 向量检索 → LLM 回答
- [ ] 支持引用来源消息

### 多频率摘要
- [ ] monitored_groups 表添加 `summary_cron` 字段（NULL = 使用全局）
- [ ] scheduler.py 支持 per-group cron 调度
- [ ] 群组设置页添加独立 cron 配置
- [ ] 高活跃群支持 4h/8h 频率

### 发送者画像
- [ ] 新增 sender_profiles 表（sender_id, tags, msg_count, last_seen）
- [ ] 定期（或摘要时）用 LLM 对高频发送者生成标签（技术/营销/闲聊/高质量）
- [ ] 消息页面显示发送者标签
- [ ] 辅助拉黑决策：标记为"营销"的发送者高亮提示

## P2 — 长期方向

### 跨群关联分析
- [ ] 摘要生成后，对同日多群摘要做二次 LLM 分析
- [ ] 识别跨群重复话题、信息差
- [ ] Dashboard 添加"今日跨群热点"卡片

### 周汇总（依赖 summaries 保留期延长）
- [ ] 将 summaries 默认保留期从 7 天改为 30 天
- [ ] 每周日对本周 daily summaries 做 LLM 提炼
- [ ] 周报推送到 owner

---

## 已排除

| 功能 | 原因 |
|------|------|
| 转发规则 | 用户偏好在 Web 查看，不需要自动转发 |
| Webhook 出口 | 当前只有一个用户，TG 推送够用 |
| 多用户支持 | 个人工具，不需要 |

---

## 技术备注

- **FTS5**：SQLite 内置，无需额外依赖。建表语句：`CREATE VIRTUAL TABLE messages_fts USING fts5(text, content=messages, content_rowid=id)`
- **Embedding 存储**：SQLite 存 BLOB，检索时加载到内存做余弦相似度。消息量不大（日均几千条）时性能足够，不需要专用向量数据库
- **图表**：推荐 Chart.js CDN 引入，不增加构建复杂度
