"""评估集跑分脚本 —— 落地《产品设计方案》第 8 节的评估体系（v1.1 路线项）。

用法：
  python scripts/evaluate.py --count 5            # Mock 快速跑 5 个主题
  python scripts/evaluate.py --count 20 --real    # 真实模式全量跑分（需 API Key）
  python scripts/evaluate.py --output docs/evaluation-report.md

指标（设计方案 8 节）：
  质量   要点支撑率（核心要点中带来源角标的占比）
  质量   角标有效率（角标不越界）
  体验   端到端时长 / 完成率
  工程   子问题失败数 / 重写次数
  成本   Token 与估算成本
"""
import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("MOCK_LLM", "1")
os.environ.setdefault("MOCK_SEARCH", "1")

from backend.agent.graph import run_task  # noqa: E402
from backend.config import load_settings, validate_settings  # noqa: E402
from backend.core.events import Task  # noqa: E402

POINT_RE = re.compile(r"^\s*[-*]\s+(.+)$")
CITE_RE = re.compile(r"\[(\d+)\]")


def citation_metrics(report_md: str, n_sources: int) -> dict:
    """要点支撑率 + 角标有效率（自动统计，设计方案 8 节）。"""
    section = ""
    for chunk in report_md.split("## "):
        if chunk.strip().startswith("核心要点"):
            section = chunk
            break
    points = [m.group(1) for m in
              (POINT_RE.match(line) for line in section.splitlines()) if m]
    if not points:
        return {"points": 0, "point_support_rate": None, "cite_valid_rate": None}
    supported = sum(1 for p in points if CITE_RE.search(p))
    all_cites = [int(x) for x in CITE_RE.findall(report_md)]
    valid = [c for c in all_cites if 1 <= c <= max(n_sources, 1)]
    return {
        "points": len(points),
        "point_support_rate": round(supported / len(points), 4),
        "cite_valid_rate": round(len(valid) / len(all_cites), 4) if all_cites else None,
    }


async def run_one(topic_item: str, topic: str, depth: str, length: str, settings) -> dict:
    task = Task()
    started = time.time()
    await run_task(task, settings, {"topic": topic, "angle": "", "depth": depth,
                                    "length": length, "subquestions": topic_item})
    r = task.result or {}
    stats = r.get("stats") or {}
    cite = citation_metrics(r.get("report_md", ""), len(r.get("sources", [])))
    return {
        "id": task.id,
        "topic": topic,
        "status": task.status,
        "elapsed": round(time.time() - started, 1),
        "tokens": (stats.get("total_tokens") or {}).get("in", 0)
                  + (stats.get("total_tokens") or {}).get("out", 0),
        "cost": stats.get("cost_yuan"),
        "sources": stats.get("sources", 0),
        "subq_failed": stats.get("subq_failed", 0),
        "rewrites": stats.get("rewrite_count", 0),
        **cite,
    }


def render_report(rows: list[dict], mock: bool) -> str:
    ok = [r for r in rows if r["status"] in ("done", "partial")]
    n = len(rows)
    support = [r["point_support_rate"] for r in ok if r["point_support_rate"] is not None]
    valid = [r["cite_valid_rate"] for r in ok if r["cite_valid_rate"] is not None]
    lines = [
        "# 研究助手评估报告",
        "",
        f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')} ｜ 模式：{'Mock' if mock else '真实调用'}"
        f" ｜ 样本：{n} 个主题 ｜ 完成率：{len(ok)}/{n}",
        "",
        "## 汇总指标（对应设计方案第 8 节）",
        "",
        "| 指标 | 数值 |",
        "| --- | --- |",
        f"| 完成率 | {len(ok)}/{n} |",
        f"| 平均要点支撑率 | {round(sum(support)/len(support), 4) if support else '—'} |",
        f"| 平均角标有效率 | {round(sum(valid)/len(valid), 4) if valid else '—'} |",
        f"| 平均端到端时长 | {round(sum(r['elapsed'] for r in rows)/n, 1)}s |",
        f"| 平均 Token | {round(sum(r['tokens'] for r in rows)/n)} |",
        f"| 平均来源数 | {round(sum(r['sources'] for r in rows)/n, 1)} |",
        f"| 子问题失败总数 | {sum(r['subq_failed'] for r in rows)} |",
        f"| 触发重写总数 | {sum(r['rewrites'] for r in rows)} |",
        "",
        "## 逐主题明细",
        "",
        "| 主题 | 状态 | 时长(s) | Token | 来源 | 要点支撑率 | 角标有效率 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        lines.append(
            f"| {r['topic'][:28]} | {r['status']} | {r['elapsed']} | {r['tokens']} "
            f"| {r['sources']} | {r['point_support_rate']} | {r['cite_valid_rate']} |")
    return "\n".join(lines) + "\n"


async def main() -> None:
    ap = argparse.ArgumentParser(description="研究助手评估集跑分")
    ap.add_argument("--count", type=int, default=5, help="跑前 N 个主题（默认 5）")
    ap.add_argument("--depth", default="quick", choices=["quick", "standard", "deep"])
    ap.add_argument("--length", default="short", choices=["short", "medium", "long"])
    ap.add_argument("--real", action="store_true", help="真实调用模式（默认 Mock）")
    ap.add_argument("--output", default="docs/evaluation-report.md")
    args = ap.parse_args()

    settings = load_settings()
    errors = validate_settings(settings)
    if errors:
        print("配置校验失败：", errors)
        sys.exit(1)
    if args.real:
        os.environ["MOCK_LLM"] = "0"
        os.environ["MOCK_SEARCH"] = "0"
        settings = load_settings()          # 重新加载以读取 Mock 开关
        if settings.mock_llm:
            print("真实模式需要配置 LLM_API_KEY")
            sys.exit(1)
    topics = json.loads((ROOT / "scripts" / "eval_topics.json")
                        .read_text(encoding="utf-8"))["topics"][: args.count]
    rows = []
    for i, t in enumerate(topics, 1):
        print(f"[{i}/{len(topics)}] {t['topic']}")
        plan = await _plan(t["topic"], args.depth, settings)
        rows.append(await run_one(plan, t["topic"], args.depth, args.length, settings))

    report = render_report(rows, mock=settings.mock_llm)
    out = ROOT / args.output
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    print(f"\n评估报告已写入：{out}")
    print("\n" + report.split("## 逐主题明细")[0])


async def _plan(topic: str, depth: str, settings) -> list[str]:
    from backend.agent.llm import LLMClient
    from backend.agent.planner import plan_subquestions
    from backend.core.events import Task
    t = Task()
    llm = LLMClient(settings, t)
    from backend.agent.planner import DEPTH_N, _fallback_plan
    try:
        return await plan_subquestions(topic, depth, "", llm)
    except Exception:
        return _fallback_plan(topic, DEPTH_N.get(depth, 3))


if __name__ == "__main__":
    asyncio.run(main())
