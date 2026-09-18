"""撰写节点：素材 -> 结构化报告（含来源角标）+ L3 降级。

对应《产品设计方案》：
- 3.2④ 报告模板（摘要/要点/分析/结论）与硬性规则「无来源不陈述」
- 5.3 撰写阶段流式输出（chunk 事件）
- 7.1 L2：流式不可用时降级为普通调用
- 7.1 L3：Writer 失败/预算触顶 -> 基于素材交付「素材汇编版」
- 素材为空 -> 交付 failed（由 runner 按 L4 处理）
"""
from backend.agent.llm import BudgetExceeded, LLMError
from backend.agent.tools import Ctx

LENGTH_CHARS = {"short": 1000, "medium": 2000, "long": 3000}

WRITER_SYS = (
    "你是一名严谨的研究报告撰写人。根据提供的编号素材撰写中文研究报告，遵守：\n"
    "1. 结构：## 摘要（150 字内）→ ## 核心要点（3~6 条，每条必须是事实且带来源角标 [n]）"
    "→ ## 分节分析 → ## 结论与展望。\n"
    "2. 硬性规则：任何事实性陈述必须标注素材编号角标 [n]；无来源的观点只能放在"
    "「分析（以下为分析推论）」小节并显式标注。\n"
    "3. 不要输出参考来源列表（系统会自动追加）。\n"
)


def digest_materials(materials: list[dict], per_source: int = 1200) -> str:
    lines = []
    for i, m in enumerate(materials, 1):
        body = m["content"] or m["snippet"] or ""
        lines.append(f"[{i}] {m['title']}\nURL: {m['url']}\n内容：{body[:per_source]}\n")
    return "\n".join(lines)


def _append_sources(report: str, materials: list[dict]) -> str:
    lines = ["", "## 参考来源", ""]
    for i, m in enumerate(materials, 1):
        lines.append(f"[{i}] {m['title']}")
        lines.append(f"    {m['url']}")
    return report.rstrip() + "\n" + "\n".join(lines) + "\n"


def compile_partial(topic: str, materials: list[dict], failed: list[dict], reason: str) -> str:
    """L3 素材汇编版：不经过 LLM，直接由素材拼装（要点 + 来源，不含成文分析）。"""
    parts = [f"# {topic} 研究素材汇编（降级交付 L3）", "",
             f"> 生成原因：{reason}", "", "## 已收集素材", ""]
    for i, m in enumerate(materials, 1):
        excerpt = (m["content"] or m["snippet"] or "")[:300]
        parts += [f"### [{i}] {m['title']}", f"来源：{m['url']}", "", excerpt, ""]
    if failed:
        parts += ["## 检索失败项（L1 兜底后仍失败）", ""]
        parts += [f"- {f['subq']}：{f['reason'][:100]}" for f in failed]
    return "\n".join(parts) + "\n"


def _degrade_partial(ctx: Ctx, topic: str, materials: list[dict],
                     state: dict, reason: str) -> dict:
    """L3：撰写彻底失败或预算触顶 -> 不经 LLM，直接用素材拼装交付。"""
    ctx.task.emit({"type": "stage", "stage": "write",
                   "detail": f"撰写失败，降级为素材汇编（L3）：{reason[:80]}"})
    return {"report_md": compile_partial(topic, materials,
                                         state.get("failed_subqs", []), reason),
            "deliver_mode": "partial"}


async def writer_node(state: dict, ctx: Ctx) -> dict:
    ctx.task.emit({"type": "stage", "stage": "write", "detail": "正在撰写报告"})
    materials = state["materials"]
    topic = state["topic"]

    if not materials:      # 规划后无任何素材 -> L4（runner 处理）
        return {"report_md": "", "deliver_mode": "failed"}

    sys_prompt = WRITER_SYS + f"可用来源共 {len(materials)} 条。\n"
    review = state.get("review") or {}
    rewrite_note = ""
    if review.get("verdict") == "rewrite":
        rewrite_note = ("\n上一版审稿意见（请修正后重写）：\n- "
                        + "\n- ".join(review.get("reasons", [])[:5]) + "\n")

    user = (f"主题：{topic}\n关注角度：{state.get('angle') or '无'}\n"
            f"目标篇幅：约 {LENGTH_CHARS.get(state.get('length', 'medium'), 2000)} 字\n"
            f"{rewrite_note}\n编号素材：\n{digest_materials(materials)}")
    msgs = [{"role": "system", "content": sys_prompt},
            {"role": "user", "content": user}]

    try:
        # 流式撰写：内部自带 L2 重试/降级与 Token 预算记账（不再绕过预算硬顶）
        report = await ctx.llm.stream_chat(
            "writer", msgs,
            on_chunk=lambda text: ctx.task.emit({"type": "chunk", "text": text}))
    except BudgetExceeded as e:
        return _degrade_partial(ctx, topic, materials, state, str(e))
    except LLMError as e:
        # L2 第三层：流式不可用（部分服务端不支持 stream）-> 降级为普通调用
        ctx.task.emit({"type": "stage", "stage": "write",
                       "detail": f"流式输出失败，降级为普通调用：{str(e)[:60]}"})
        try:
            resp = await ctx.llm.chat("writer", msgs)
            report = resp["content"]
        except (LLMError, BudgetExceeded) as e2:
            return _degrade_partial(ctx, topic, materials, state, str(e2))

    return {"report_md": _append_sources(report, materials),
            "deliver_mode": "full",
            "rewrite_count": state.get("rewrite_count", 0) + (1 if rewrite_note else 0)}
