"""规划节点：主题 -> 3~5 个研究子问题。

说明：规划在 POST /api/plan 阶段执行（而非研究任务内），以支持用户对子问题
增删改后再启动 —— 对应《产品设计方案》3.2②「任务规划（可干预性是信任来源）」。
"""
import json
import re

from backend.agent.llm import LLMClient

DEPTH_N = {"quick": 3, "standard": 4, "deep": 5}

PLANNER_SYS = (
    "你是研究规划专家。把给定研究主题拆解为若干个互相补充、可独立检索的研究子问题，"
    "覆盖：现状、技术/方案、应用/案例、挑战、趋势等角度。"
    "只输出一个 JSON 数组，数组元素为字符串（子问题），不要输出任何其他文字。"
)


def _fallback_plan(topic: str, n: int) -> list[str]:
    pool = [
        f"{topic}的整体发展现状与最新进展",
        f"{topic}的核心技术路线与代表方案",
        f"{topic}的典型应用场景与落地案例",
        f"{topic}当前面临的主要挑战",
        f"{topic}的未来发展趋势",
    ]
    return pool[:n]


def _parse_json_list(text: str) -> list[str]:
    text = re.sub(r"```(json)?", "", text).strip()
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        raise ValueError(" planner 输出中未找到 JSON 数组")
    data = json.loads(m.group(0))
    # 去除模型可能自带的序号前缀（"1. " / "2、" / "（3）" / "(4)" / "五、" 等）
    items = [re.sub(r"^\s*(?:\d+[.、)）]|[一二三四五六七八九十]+、|[（(]\d+[)）])\s*",
                    "", str(x)).strip()
             for x in data]
    items = [x for x in items if x]
    if not (1 <= len(items) <= 12):        # 超量不报错，由调用方截断
        raise ValueError(f"子问题数量异常：{len(items)}")
    return items


async def plan_subquestions(topic: str, depth: str, angle: str, llm: LLMClient) -> list[str]:
    n = DEPTH_N.get(depth, 4)
    user = f"主题：{topic}\n关注角度：{angle or '无'}\n请拆解为 {n} 个互补的研究子问题"
    try:
        resp = await llm.chat("planner", [
            {"role": "system", "content": PLANNER_SYS},
            {"role": "user", "content": user},
        ])
        return _parse_json_list(resp["content"])[:n]    # 超量截断到目标数量
    except Exception:
        # 规划输出不可解析时使用模板拆解，不阻断主流程
        return _fallback_plan(topic, n)
