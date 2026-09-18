"""自检节点：核查引用真实性、要点与来源一致性；不合格打回重写。

对应《产品设计方案》7.2 幻觉兜底：
- 规则核验：角标 [n] 不越界、至少存在一条引用
- 链接抽样存活检查（非 Mock 模式）
- LLM 核验：要点是否被来源支撑；输出 {"verdict":"pass|rewrite","reasons":[...]}
- Reviewer 自身失败（L2 耗尽）不阻断交付，标记「自检未完成」
"""
import asyncio
import json
import re

import requests

from backend.agent.llm import LLMError
from backend.agent.tools import UA, Ctx

REVIEWER_SYS = (
    "你是严格的审稿人。核查报告：1) 事实性要点是否都有来源角标 [n]；"
    "2) 要点内容是否被对应来源素材支撑（而不是编造）。\n"
    '只输出 JSON：{"verdict":"pass" 或 "rewrite","reasons":["原因1","原因2"]}'
)


def _rule_check(report: str, n_sources: int) -> list[str]:
    problems = []
    # 只核查正文：参考来源小节自身含 [n] 角标，不能作为"有引用"的证据（v1.3 修复）
    body = report.split("## 参考来源")[0]
    cites = [int(x) for x in re.findall(r"\[(\d+)\]", body)]
    bad = sorted({c for c in cites if c < 1 or c > n_sources})
    if bad:
        problems.append(f"以下角标越界（来源仅 {n_sources} 条）：{bad}")
    if not cites:
        problems.append("全文没有任何来源角标，违反「无来源不陈述」规则")
    return problems


def _link_alive(url: str, timeout: int = 6) -> bool:
    try:
        with requests.get(url, headers=UA, timeout=timeout, stream=True) as r:
            return r.status_code < 400
    except Exception:
        return False


async def reviewer_node(state: dict, ctx: Ctx) -> dict:
    if state.get("deliver_mode") != "full":
        return {"review": {"verdict": "pass", "reasons": ["降级交付，跳过自检"]}}

    ctx.task.emit({"type": "stage", "stage": "review", "detail": "正在自检报告"})
    report = state["report_md"]
    materials = state["materials"]
    problems = _rule_check(report, len(materials))

    if not ctx.settings.mock_search:                            # 链接抽样存活
        for m in materials[:2]:
            alive = await asyncio.to_thread(_link_alive, m["url"])
            if not alive:
                problems.append(f"来源不可访问：{m['url']}")

    try:
        src_list = "\n".join(f"[{i}] {m['title']} {m['url']} 摘要：{m['snippet'][:150]}"
                             for i, m in enumerate(materials, 1))
        resp = await ctx.llm.chat("reviewer", [
            {"role": "system", "content": REVIEWER_SYS},
            {"role": "user", "content": f"报告：\n{report[:6000]}\n\n来源清单：\n{src_list}"},
        ])
        verdict = json.loads(re.sub(r"```(json)?", "", resp["content"]).strip())
        reasons = list(verdict.get("reasons", []))
        if verdict.get("verdict") != "pass":
            problems.extend(reasons or ["审稿人未给出具体原因"])
    except LLMError as e:                                       # L2 耗尽：不阻断交付
        return {"review": {"verdict": "pass", "reasons": [f"自检未完成（{str(e)[:60]}）"],
                           "degraded": True}}
    except Exception:
        pass                                                    # JSON 解析失败：以规则核验结果为准

    if problems:
        limit = ctx.settings.reviewer_rewrite_limit
        if state.get("rewrite_count", 0) < limit:               # 打回重写
            ctx.task.emit({"type": "stage", "stage": "review",
                           "detail": f"自检未通过，打回重写（第 {state.get('rewrite_count', 0) + 1}/{limit} 次）"})
            return {"review": {"verdict": "rewrite", "reasons": problems}}
        problems.append(f"已达到重写上限 {limit} 次，按当前版本交付")
    return {"review": {"verdict": "pass", "reasons": problems or ["引用角标均在来源范围内"]}}
