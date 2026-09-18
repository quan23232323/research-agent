"""检索节点：对每个子问题执行 ReAct 循环（思考->行动->观察），收集素材。

对应《产品设计方案》：
- 3.2③ 子问题并行 + 实时进度（stage 事件）
- 4 节 Researcher 职责：调用 web_search / read_page，素材去重
- 5.3 并行检索（Semaphore 控并发）
- 6 节 ReAct 循环上限防死循环
- 7.1 L1：工具失败 -> 重试/换源已由 tools 层处理，仍失败则跳过该子问题并标记
"""
import asyncio
import json

from backend.agent import tools
from backend.agent.llm import BudgetExceeded, LLMError
from backend.agent.tools import Ctx, ToolError

RESEARCHER_SYS = (
    "你是一名严谨的研究员，负责回答给定的研究子问题。你可以调用工具：\n"
    "- web_search(query)：联网搜索，先调用它获取候选资料；\n"
    "- read_page(url)：深入阅读某条搜索结果的正文（最多阅读 2 个最相关网页）。\n"
    "工作方式（ReAct）：先搜索，再挑选最相关的 1~2 个链接阅读，资料足够后"
    "直接输出一段 100 字以内的素材小结作为最终回答，不再调用工具。"
    "严禁编造未在工具结果中出现的链接或数据。"
)

TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "web_search",
        "description": "联网搜索，返回标题、链接、摘要列表",
        "parameters": {"type": "object",
                       "properties": {"query": {"type": "string", "description": "搜索关键词"}},
                       "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "read_page",
        "description": "读取网页正文（自动截断），用于深入阅读某个搜索结果",
        "parameters": {"type": "object",
                       "properties": {"url": {"type": "string", "description": "网页链接"}},
                       "required": ["url"]}}},
]


def _append_assistant(msgs: list, resp: dict) -> None:
    """把模型回复按 OpenAI 消息格式写回历史（含 tool_calls）。"""
    entry = {"role": "assistant", "content": resp["content"] or ""}
    if resp["tool_calls"]:
        entry["tool_calls"] = [
            {"id": tc["id"], "type": "function",
             "function": {"name": tc["name"],
                          "arguments": json.dumps(tc["args"], ensure_ascii=False)}}
            for tc in resp["tool_calls"]
        ]
    msgs.append(entry)


async def _react_loop(subq: str, ctx: Ctx) -> list[dict]:
    """单子问题 ReAct 循环，返回该子问题收集到的素材列表。"""
    msgs = [
        {"role": "system", "content": RESEARCHER_SYS},
        {"role": "user", "content": f"研究子问题：{subq}"},
    ]
    materials: list[dict] = []
    had_tool_error = False

    for _round in range(ctx.settings.max_react_rounds):    # 成本防线：循环上限
        resp = await ctx.llm.chat("researcher", msgs, tools=TOOL_SCHEMAS)
        if not resp["tool_calls"]:
            break                                            # 模型给出最终小结，结束
        _append_assistant(msgs, resp)
        for tc in resp["tool_calls"]:
            try:
                if tc["name"] == "web_search":
                    results = await tools.web_search(tc["args"].get("query", subq), ctx)
                    materials.extend(
                        {"subq": subq, "title": r["title"], "url": r["url"],
                         "snippet": r["snippet"], "content": ""}
                        for r in results
                    )
                    payload = json.dumps(results, ensure_ascii=False)[:4000]
                elif tc["name"] == "read_page":
                    url = tc["args"].get("url", "")
                    page = await tools.read_page(url, ctx)
                    for mtr in materials:
                        if mtr["url"] == url and not mtr["content"]:
                            mtr["content"] = page["content"]
                            mtr["title"] = page["title"] or mtr["title"]
                    payload = json.dumps({"url": url, "chars": len(page["content"])},
                                         ensure_ascii=False)
                else:
                    payload = json.dumps({"error": f"未知工具 {tc['name']}"})
            except ToolError as e:                           # L1：单次调用失败不阻断循环
                had_tool_error = True
                payload = json.dumps({"error": str(e)}, ensure_ascii=False)
                ctx.task.emit({"type": "tool", "name": tc["name"], "status": "failed",
                               "detail": str(e)[:120]})
            msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": payload})

    if not materials and had_tool_error:                     # L1：彻底失败 -> 跳过并标记
        raise ToolError("该子问题资料获取失败（重试与换源后仍失败）")
    return materials


def _dedupe(materials: list[dict]) -> list[dict]:
    seen, out = set(), []
    for m in materials:
        if m["url"] and m["url"] not in seen:
            seen.add(m["url"])
            out.append(m)
    return out


async def researcher_node(state: dict, ctx: Ctx) -> dict:
    subqs = state["subquestions"]
    total = len(subqs)
    per_subq: list[list[dict]] = [[] for _ in subqs]
    failed: list[dict] = []
    budget_hit = False
    sem = asyncio.Semaphore(ctx.settings.search_concurrency)   # 并行检索

    async def run_one(i: int, sq: str) -> None:
        nonlocal budget_hit
        async with sem:
            ctx.task.emit({"type": "stage", "stage": "research",
                           "detail": f"正在检索子问题 {i + 1}/{total}",
                           "subq_index": i + 1, "subq_total": total, "subq": sq})
            try:
                per_subq[i] = await _react_loop(sq, ctx)
            except BudgetExceeded:                              # 预算触顶：中止本子问题
                budget_hit = True
            except ToolError as e:                              # L1：跳过并标记
                failed.append({"subq": sq, "reason": str(e)})
                ctx.task.emit({"type": "stage", "stage": "research",
                               "detail": f"子问题 {i + 1}/{total} 资料获取失败（已跳过）",
                               "skipped": True})
            except LLMError as e:                               # L2 耗尽
                failed.append({"subq": sq, "reason": str(e)})
            except Exception as e:                              # 其他异常
                failed.append({"subq": sq, "reason": str(e)})

    await asyncio.gather(*(run_one(i, sq) for i, sq in enumerate(subqs)))

    materials = _dedupe([m for group in per_subq for m in group])
    materials.sort(key=lambda m: 0 if m["content"] else 1)   # 已阅读正文的来源排前
    materials = materials[: ctx.settings.max_sources]        # 成本防线：限制写入报告的素材量
    update: dict[str, object] = {"materials": materials, "failed_subqs": failed}
    if budget_hit:
        update["budget_exceeded"] = True
    return update
