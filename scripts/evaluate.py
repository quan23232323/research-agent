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

# 要点行：兼容项目符号（- * +）与有序列表（1. / 1、/ 1) / 1．）
# 真实模型两种写法都会输出；只认一种会让「要点支撑率」静默变成 None（实测 5 篇里 4 篇用有序列表）。
# 有序列表限制 1~2 位数字、且要求分隔符后确有内容，避免把「3.5 倍」这类正文误判成列表项。
POINT_RE = re.compile(r"^\s*(?:[-*+]\s+|\d{1,2}\s*[.、)．]\s*)(\S.{7,})$")
CITE_RE = re.compile(r"\[(\d+)\]")


def _body_only(report_md: str) -> str:
    """去掉「参考来源」小节后的正文。

    该小节每行都以 `[n]` 开头，若计入角标统计会凭空放大分母——
    12 条来源就等于白送 12 个「有效角标」，让有效率系统性偏松、失去判别力。
    """
    body = report_md
    for marker in ("## 参考来源", "## 参考文献", "## References"):
        idx = body.find(marker)
        if idx != -1:
            body = body[:idx]
    return body


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
    all_cites = [int(x) for x in CITE_RE.findall(_body_only(report_md))]
    valid = [c for c in all_cites if 1 <= c <= max(n_sources, 1)]
    return {
        "points": len(points),
        "point_support_rate": round(supported / len(points), 4),
        "cite_valid_rate": round(len(valid) / len(all_cites), 4) if all_cites else None,
    }


async def run_one(subquestions: list[str], topic: str, depth: str, length: str, settings) -> dict:
    task = Task()
    started = time.time()
    await run_task(task, settings, {"topic": topic, "angle": "", "depth": depth,
                                    "length": length, "subquestions": subquestions})
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


def _avg_cov(vals: list, total: int) -> str:
    """均值 + 可测样本覆盖度。

    只对真正测到的样本求均值，并显式标出覆盖度——否则用 1/5 个样本算出的均值
    会被读成全量指标（上一版报告就是把 1 个样本的 1.0 写成了「平均要点支撑率」）。
    """
    if not vals:
        return f"—（0/{total} 个主题可测）"
    val = f"{round(sum(vals) / len(vals), 4)}"
    return val if len(vals) == total else f"{val}（仅 {len(vals)}/{total} 个主题可测）"


def render_report(rows: list[dict], mock: bool) -> str:
    ok = [r for r in rows if r["status"] in ("done", "partial")]
    n = len(rows)
    support = [r["point_support_rate"] for r in ok if r["point_support_rate"] is not None]
    valid = [r["cite_valid_rate"] for r in ok if r["cite_valid_rate"] is not None]
    points_total = sum(r.get("points") or 0 for r in rows)
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
        f"| 核心要点总数 | {points_total} |",
        f"| 平均要点支撑率 | {_avg_cov(support, len(ok))} |",
        f"| 平均角标有效率 | {_avg_cov(valid, len(ok))} |",
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
    def _fmt(v):
        return "—" if v is None else v

    for r in rows:
        lines.append(
            f"| {r['topic'][:28]} | {r['status']} | {r['elapsed']} | {r['tokens']} "
            f"| {r['sources']} | {_fmt(r['point_support_rate'])} | {_fmt(r['cite_valid_rate'])} |")
    lines += [
        "",
        "## 指标口径与已知局限",
        "",
        "- **要点支撑率**：`核心要点` 小节列表项中带来源角标的占比（角标形如 `[1]`）；"
        "统计兼容项目符号与有序列表两种写法。",
        "- **角标有效率**：正文角标落在参考来源编号范围内的占比（越界即为无效角标）；"
        "统计时剔除参考来源小节自身的 `[n]` 列表标签，否则来源越多指标越松。",
        "- 汇总指标只对**可测样本**求均值并标出覆盖度；明细里 `—` 表示该主题无可测要点。",
        "- 逐主题原始数据落在 `docs/evaluation-rows.json`，可离线重算指标："
        "`python scripts/evaluate.py --reuse docs/evaluation-rows.json`（不调用模型、零成本）。",
        "- 支撑率接近 1 属预期：写作环节被要求逐条挂角标，它更像**约束是否被遵守**的守门指标，"
        "而非质量上限指标；判断质量还需人工抽检。",
    ]
    return "\n".join(lines) + "\n"


async def main() -> None:
    ap = argparse.ArgumentParser(description="研究助手评估集跑分")
    ap.add_argument("--count", type=int, default=5, help="跑前 N 个主题（默认 5）")
    ap.add_argument("--depth", default="quick", choices=["quick", "standard", "deep"])
    ap.add_argument("--length", default="short", choices=["short", "medium", "long"])
    ap.add_argument("--real", action="store_true", help="真实调用模式（默认 Mock）")
    ap.add_argument("--output", default="docs/evaluation-report.md")
    ap.add_argument("--rows", default="docs/evaluation-rows.json", help="逐主题原始数据落盘路径")
    ap.add_argument("--reuse", default="", help="从已有 rows JSON 重新渲染报告（不调用模型，零成本）")
    args = ap.parse_args()

    if args.reuse:
        src = Path(args.reuse)
        if not src.is_absolute():
            src = ROOT / src
        data = json.loads(src.read_text(encoding="utf-8"))
        report = render_report(data["rows"], mock=bool(data.get("mock", False)))
        out = ROOT / args.output
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report, encoding="utf-8")
        print(f"已从 {src.name} 重新渲染：{out}（未调用模型）")
        print("\n" + report.split("## 逐主题明细")[0])
        return

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
    rows_out = ROOT / args.rows
    rows_out.parent.mkdir(parents=True, exist_ok=True)
    rows_out.write_text(json.dumps(
        {"generated_at": datetime.now().isoformat(timespec="seconds"),
         "mock": settings.mock_llm, "depth": args.depth, "length": args.length, "rows": rows},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n评估报告已写入：{out}")
    print(f"逐主题原始数据：{rows_out}")
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
