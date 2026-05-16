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
    ],
    "Go 语言爱好者": [
        "Go 1.23 的 range over func 大家用了吗",
        "gin vs fiber vs echo 选哪个框架",
        "goroutine 泄漏怎么排查比较好",
        "有人在生产环境用 Go generics 吗",
        "推荐一个 Go 的 ORM，GORM 太重了",
        "context 传值还是传指针？",
    ],
    "AI/ML 前沿": [
        "Claude 4 的 coding 能力确实强",
        "RAG 系统用什么向量数据库比较好",
        "Fine-tuning vs RAG，什么场景用什么",
        "Llama 3 开源了，本地部署体验如何",
        "Agent 框架太多了，LangChain 还是 CrewAI",
        "多模态模型在工业场景的落地案例有吗",
        "Prompt Engineering 有系统的学习路径吗",
    ],
    "独立开发者": [
        "我的 SaaS 上线一个月，MRR $200",
        "独立开发最难的是营销还是技术？",
        "有人用 Stripe 收款遇到过风控吗",
        "分享一下我的技术栈：Next.js + Supabase",
        "SEO 对独立开发者重要吗",
    ],
    "区块链讨论": [
        "ETH 2.0 质押收益现在多少",
        "Solana 又宕机了...",
        "DeFi 协议安全审计找谁做",
        "NFT 市场还有机会吗",
    ],
}

FAKE_SUMMARIES = {
    "Python 技术交流群": "主要讨论了 Python 3.12/3.13 新特性、FastAPI WebSocket 性能、包管理工具 uv 的优势，以及 Pydantic v2 迁移经验。",
    "Go 语言爱好者": "讨论了 Go 1.23 新特性 range over func、Web 框架选型（gin/fiber/echo）、goroutine 泄漏排查方法。",
    "AI/ML 前沿": "热议 Claude 4 编码能力、RAG vs Fine-tuning 选型、Agent 框架对比（LangChain vs CrewAI）、多模态模型工业落地。",
    "独立开发者": "分享了 SaaS 上线经验（MRR $200）、技术栈选择（Next.js + Supabase）、营销 vs 技术的平衡。",
    "区块链讨论": "讨论了 ETH 质押收益、Solana 稳定性问题、DeFi 安全审计需求。",
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

    # Seed past summaries (last 5 days)
    for days_ago in range(1, 6):
        past_date = (now - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        for gid, gname in FAKE_GROUPS[:5]:
            summary = FAKE_SUMMARIES.get(gname, f"{gname} 的日常讨论摘要。")
            count = random.randint(20, 150)
            await db.insert_summary(past_date, gid, gname, count, summary)

    # Seed settings
    await db.set_setting("tg_push_enabled", "true")
    await db.set_setting("summary_retention_days", "7")

    await db.close()
    print(f"[SEED] Done: {len(FAKE_GROUPS)} groups, messages for today, summaries for 5 days")


if __name__ == "__main__":
    asyncio.run(seed())
