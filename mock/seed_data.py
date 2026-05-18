"""Seed database with fake data for UI preview."""

from __future__ import annotations

import asyncio
import random
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

from database import Database

TZ = ZoneInfo("Asia/Shanghai")

FAKE_GROUPS = [
    (-1001234567001, "Python 技术交流群"),
    (-1001234567002, "Go 语言爱好者"),
    (-1001234567003, "区块链讨论"),
    (-1001234567004, "AI/ML 前沿"),
    (-1001234567005, "独立开发者"),
    (-1001234567006, "量化交易研究"),
    (-1001234567007, "DevOps 实践"),
    (-1001234567008, "Rust 中文社区"),
    (-1001234567009, "创业者联盟"),
    (-1001234567010, "读书分享会"),
]

FAKE_SENDERS = ["张三", "李四", "王五", "赵六", "Alice", "Bob", "Charlie", "小明", "大卫", "Emma"]

FAKE_MESSAGES = {
    "Python 技术交流群": [
        "有人用过 FastAPI 的 WebSocket 吗？性能怎么样",
        "推荐一下 Python 3.12 的新特性，类型系统改进很大",
        "asyncio 和 trio 选哪个好？",
        "Django 5.0 发布了，谁升级了？",
        "poetry vs uv，现在 uv 速度快太多了",
        "有没有好用的 Python profiler 推荐",
        "Pydantic v2 迁移踩坑记录分享",
        "请问 GIL 在 3.13 中真的去掉了吗",
        "有人试过 mojo 吗？号称比 Python 快 68000 倍",
        "Flask 还是 FastAPI？新项目选哪个",
        "Python 的类型系统越来越像 TypeScript 了",
        "有没有人用 Polars 替代 Pandas 的？",
        "SQLAlchemy 2.0 的新 API 真的好用吗",
        "pytest 的 fixture 机制太强了",
        "有人在用 Ruff 做 linter 吗？速度确实快",
        "Python 3.14 要加 JIT 了？",
        "dataclass 和 pydantic 什么时候用哪个？",
        "有没有好的 Python 微服务框架推荐",
        "celery 还是 dramatiq？异步任务队列选型",
        "Python 的 match-case 语法大家用得多吗",
    ],
    "Go 语言爱好者": [
        "Go 1.23 的 range over func 大家用了吗",
        "gin vs fiber vs echo 选哪个框架",
        "goroutine 泄漏怎么排查比较好",
        "有人在生产环境用 Go generics 吗",
        "推荐一个 Go 的 ORM，GORM 太重了",
        "context 传值还是传指针？",
        "Go 的错误处理真的太啰嗦了",
        "有人用 Go 写过 CLI 工具吗？cobra 好用吗",
        "Go 的 channel 和 mutex 什么时候用哪个",
        "微服务用 Go 还是 Rust？",
        "Go 的 interface 设计哲学是什么",
        "有人用 sqlc 生成数据库代码吗",
        "Go 1.22 的 for loop 变量改动影响大吗",
        "protobuf 还是 JSON？gRPC 性能确实好",
        "Go 的 testing 包够用吗？需要 testify 吗",
    ],
    "AI/ML 前沿": [
        "Claude 4 的 coding 能力确实强",
        "RAG 系统用什么向量数据库比较好",
        "Fine-tuning vs RAG，什么场景用什么",
        "Llama 3 开源了，本地部署体验如何",
        "Agent 框架太多了，LangChain 还是 CrewAI",
        "多模态模型在工业场景的落地案例有吗",
        "Prompt Engineering 有系统的学习路径吗",
        "GPT-5 什么时候出？OpenAI 最近动作频繁",
        "本地跑 70B 模型需要什么配置？",
        "Embedding 模型选 OpenAI 还是开源的？",
        "AI 编程助手对比：Cursor vs Copilot vs Claude Code",
        "Stable Diffusion 3 的效果怎么样",
        "知识图谱 + LLM 的结合有人做过吗",
        "MCP 协议是什么？看起来很有前景",
        "AI Agent 的记忆系统怎么设计比较好",
        "DeepSeek V3 的性价比真的高",
        "Anthropic 的 Claude 和 OpenAI 的 GPT 哪个更适合编程",
        "向量数据库 Milvus vs Qdrant vs Weaviate 怎么选",
    ],
    "独立开发者": [
        "我的 SaaS 上线一个月，MRR $200",
        "独立开发最难的是营销还是技术？",
        "有人用 Stripe 收款遇到过风控吗",
        "分享一下我的技术栈：Next.js + Supabase",
        "SEO 对独立开发者重要吗",
        "Product Hunt 上线经验分享",
        "一个人做产品，怎么平衡开发和运营",
        "有没有好的用户反馈收集工具",
        "独立开发者的收入天花板在哪里",
        "做 B2B 还是 B2C？",
        "Landing page 用什么工具做比较快",
        "邮件营销对 SaaS 有用吗",
        "如何验证一个想法是否值得做",
    ],
    "区块链讨论": [
        "ETH 2.0 质押收益现在多少",
        "Solana 又宕机了...",
        "DeFi 协议安全审计找谁做",
        "NFT 市场还有机会吗",
        "Layer 2 方案对比：Arbitrum vs Optimism vs zkSync",
        "MEV 是什么？怎么防止被夹",
        "Web3 社交产品有前景吗",
        "比特币 ETF 通过后市场反应如何",
        "智能合约开发用 Solidity 还是 Rust",
        "DAO 治理的实际效果怎么样",
    ],
}

FAKE_SUMMARIES = {
    "Python 技术交流群": """今日讨论热度较高，主要围绕以下话题：

1. Python 版本与新特性：多人讨论了 Python 3.12/3.13 的类型系统改进 [m:1001]，GIL 移除进展引发热议。有人提到 3.14 可能加入 JIT 编译。

2. Web 框架选型：FastAPI WebSocket 性能讨论 [m:1000]，Flask vs FastAPI 的选择问题。共识是新项目优先 FastAPI。

3. 包管理工具：uv 的速度优势被多人认可 [m:1004]，poetry 用户开始迁移。

4. 数据处理：Polars 替代 Pandas 的讨论，SQLAlchemy 2.0 新 API 评价。

5. 代码质量：Pydantic v2 迁移经验分享 [m:1006]，Ruff linter 的使用体验，pytest fixture 机制讨论。""",

    "Go 语言爱好者": """今日活跃度中等，主要话题：

1. Go 1.23 新特性：range over func 的实际使用体验 [m:1020]，for loop 变量改动的影响讨论。

2. 框架选型：gin vs fiber vs echo 的性能和易用性对比 [m:1021]，微服务场景下的选择。

3. 并发问题：goroutine 泄漏排查方法 [m:1022]，channel vs mutex 的使用场景区分。

4. 工具链：sqlc 代码生成体验，cobra CLI 框架推荐，protobuf vs JSON 的取舍。

5. 语言设计：Go 的错误处理冗余问题持续被吐槽，interface 设计哲学讨论。""",

    "AI/ML 前沿": """今日讨论非常活跃，AI 领域动态频繁：

1. 模型对比：Claude 4 编码能力获得高度评价 [m:1035]，DeepSeek V3 性价比讨论，GPT-5 发布时间猜测。

2. RAG 系统：向量数据库选型（Milvus vs Qdrant vs Weaviate）[m:1036]，RAG vs Fine-tuning 的场景区分。

3. AI 编程工具：Cursor vs Copilot vs Claude Code 对比 [m:1045]，MCP 协议前景讨论。

4. Agent 框架：LangChain vs CrewAI 对比 [m:1039]，Agent 记忆系统设计方案，多模态模型工业落地案例。

5. 本地部署：70B 模型硬件需求讨论，Embedding 模型选型建议。""",

    "独立开发者": """今日分享氛围浓厚：

1. 收入分享：有人上线一个月 MRR $200 [m:1055]，讨论独立开发者收入天花板。

2. 技术栈：Next.js + Supabase 组合推荐 [m:1058]，Landing page 快速搭建工具讨论。

3. 营销策略：Product Hunt 上线经验 [m:1060]，SEO 重要性讨论，邮件营销效果评估。

4. 产品方向：B2B vs B2C 选择，想法验证方法论，用户反馈收集工具推荐。""",

    "区块链讨论": """今日讨论偏技术向：

1. Layer 2：Arbitrum vs Optimism vs zkSync 方案对比 [m:1066]，性能和成本分析。

2. ETH 生态：质押收益讨论 [m:1063]，Solana 稳定性问题再次被提及 [m:1064]。

3. 安全：DeFi 协议审计需求 [m:1065]，MEV 防护方案讨论。

4. 开发：智能合约语言选择（Solidity vs Rust），DAO 治理实际效果评估。""",
}


async def seed():
    db = Database("./data/messages.db")
    await db.connect()

    # Seed groups
    for gid, name in FAKE_GROUPS:
        await db.upsert_group(gid, name)
    # Activate first 5
    for gid, _ in FAKE_GROUPS[:5]:
        await db.toggle_group(gid, True)
    for gid, _ in FAKE_GROUPS[5:]:
        await db.toggle_group(gid, False)

    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")

    # Seed today's messages
    msg_id = 1000
    sender_id_base = 100000
    for gid, gname in FAKE_GROUPS[:5]:
        messages = FAKE_MESSAGES.get(gname, ["一般讨论内容"] * 5)
        for text in messages:
            sender = random.choice(FAKE_SENDERS)
            sender_id = sender_id_base + FAKE_SENDERS.index(sender)
            ts = int((now - timedelta(hours=random.randint(1, 12))).timestamp())
            await db.insert_message(gid, gname, msg_id, sender_id, sender, text, ts, today)
            msg_id += 1

    # Seed past summaries (last 5 days) with context windows
    import re
    ctx_conversations = [
        ["这个问题我之前也遇到过", "我的解决方案是用 XXX 库", "确实好用，感谢分享！", "不过要注意版本兼容性", "对，我踩过这个坑"],
        ["有人试过吗？", "我在生产环境跑了半年了", "稳定性怎么样", "偶尔会有内存泄漏", "可以用 profiler 排查一下"],
        ["刚看到这个新闻", "太强了吧", "这对我们的项目有什么影响", "可能需要重新评估技术选型", "先观望一下再说"],
        ["推荐一下这个工具", "链接发一下？", "https://example.com/tool", "看起来不错，star 数很高", "我去试试"],
        ["大家怎么看这个方案", "我觉得可行", "但是成本可能比较高", "有没有更便宜的替代方案", "可以考虑开源方案"],
    ]

    for days_ago in range(1, 6):
        past_date = (now - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        for gid, gname in FAKE_GROUPS[:5]:
            summary = FAKE_SUMMARIES.get(gname, f"{gname} 的日常讨论摘要。")
            count = random.randint(50, 200)
            cursor = await db.conn.execute(
                """INSERT OR IGNORE INTO summaries
                   (biz_date, biz_period, group_id, group_name, message_count, summary_text)
                   VALUES (?, 'daily', ?, ?, ?, ?)""",
                (past_date, gid, gname, count, summary),
            )
            await db.conn.commit()
            if cursor.rowcount == 0:
                continue
            summary_id = cursor.lastrowid

            refs = re.findall(r'\[m:(\d+)\]', summary)
            for i, ref_id in enumerate(refs):
                window_id = await db.insert_context_window(summary_id, gid, int(ref_id))
                conv = ctx_conversations[i % len(ctx_conversations)]
                ctx_messages = []
                base_ts = int((now - timedelta(days=days_ago, hours=random.randint(1, 10))).timestamp())
                for offset, text in enumerate(conv):
                    ctx_messages.append({
                        "group_id": gid,
                        "message_id": int(ref_id) - 2 + offset,
                        "sender_name": random.choice(FAKE_SENDERS),
                        "text": text,
                        "timestamp": base_ts + offset * 45,
                    })
                await db.insert_context_messages(window_id, ctx_messages)

    # Seed settings
    await db.set_setting("tg_push_enabled", "true")
    await db.set_setting("summary_retention_days", "7")

    await db.close()
    print(f"[SEED] Done: {len(FAKE_GROUPS)} groups, messages for today, summaries for 5 days")


if __name__ == "__main__":
    asyncio.run(seed())
