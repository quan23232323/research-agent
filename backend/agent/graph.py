"""LangGraph 工作流编排与任务运行器。

路由规则（对应《产品设计方案》第 4 节四节点 + 7.1 兜底）：
  researcher -> writer -> reviewer -> (rewrite? writer : END)
  writer 产出 partial（L3）时直接 END；产出 failed 时由 runner 按 L4 处理。
"""
import time
from functools import partial
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from backend.agent.llm import LLMClient
from backend.agent.researcher import researcher_node
from backend.agent.reviewer import reviewer_node
from backend.agent.tools import Ctx, write_report
from backend.agent.writer import writer_node
from backend.core.events import RUNNING, Task
from backend.core.log import get_logger

LOG = get_logger("workflow")


class ResearchState(TypedDict, total=False):
    topic: str
    angle: str
    depth: str
    length: str
    subquestions: list
    materials: list
    failed_subqs: list
    budget_exceeded: bool
    report_md: str
    deliver_mode: str          # full | partial | failed
    review: dict
    rewrite_count: int


def build_graph(ctx: Ctx):
    g = StateGraph(ResearchState)
    g.add_node("researcher", partial(researcher_node, ctx=ctx))
    g.add_node("writer", partial(writer_node, ctx=ctx))
    g.add_node("reviewer", partial(reviewer_node, ctx=ctx))

    g.add_edge(START, "researcher")
    g.add_edge("researcher", "writer")
    g.add_conditional_edges(
        "writer",
        lambda s: "reviewer" if s.get("deliver_mode") == "full" else END,
        {"reviewer": "reviewer", END: END},
    )
    g.add_conditional_edges(
        "reviewer",
        lambda s: ("rewrite"
                   if s.get("review", {}).get("verdict") == "rewrite"
                   and s.get("rewrite_count", 0) < ctx.settings.reviewer_rewrite_limit
                   else END),
        {"rewrite": "writer", END: END},
    )
    return g.compile()


def _stats(llm, started: float, final: dict, path: Path, settings) -> dict:
    tokens = {node: dict(v) for node, v in llm.stats.items()}
    total_in = sum(v["in"] for v in tokens.values())
    total_out = sum(v["out"] for v in tokens.values())
    cost = None
    if settings.price_input and settings.price_output:
        cost = round(total_in / 1e6 * settings.price_input
                     + total_out / 1e6 * settings.price_output, 4)
    return {
        "elapsed_sec": round(time.time() - started, 1),
        "tokens": tokens,
        "total_tokens": {"in": total_in, "out": total_out, "budget_cap": llm.budget.cap},
        "cost_yuan": cost,
        "sources": len(final.get("materials", [])),
        "subq_total": len(final.get("subquestions", [])),
        "subq_failed": len(final.get("failed_subqs", [])),
        "rewrite_count": final.get("rewrite_count", 0),
        "report_file": path.name,
    }


def _persist_failed(task: Task, payload: dict, error: str, failed_subqs: list) -> None:
    """失败任务同样落库，保证历史与成功率统计真实（v1.3）。"""
    try:
        from backend.core.storage import get_store
        get_store().save_failed(task.id, payload["topic"], error, failed_subqs,
                                payload.get("subquestions", []))
    except Exception:
        LOG.exception("任务 %s 失败状态落库异常", task.id)


async def run_task(task: Task, settings, payload) -> None:
    """研究任务入口：构建图 -> 执行 -> 落盘 -> 推送 done / L4 error。"""
    RUNNING.add(task.id)
    started = time.time()
    try:
        task.emit({"type": "stage", "stage": "research", "detail": "研究任务启动"})
        LOG.info("任务 %s 启动：%s", task.id, payload["topic"])
        llm = LLMClient(settings, task)
        ctx = Ctx(settings=settings, task=task, llm=llm)
        graph = build_graph(ctx)

        state0: ResearchState = {
            "topic": payload["topic"], "angle": payload["angle"],
            "depth": payload["depth"], "length": payload["length"],
            "subquestions": payload["subquestions"],
            "materials": [], "failed_subqs": [], "rewrite_count": 0,
        }
        try:
            final = await graph.ainvoke(state0)
        except Exception as e:                                      # L4：全局失败
            LOG.exception("任务 %s 全局失败", task.id)
            task.status = "failed"
            task.emit({"type": "error", "level": "L4", "message": f"研究任务失败：{e}"})
            _persist_failed(task, payload, str(e), [])
            return

        if final.get("deliver_mode") == "failed":                   # L4：无任何素材
            LOG.warning("任务 %s 无任何素材（L4）", task.id)
            task.status = "failed"
            task.emit({"type": "error", "level": "L4",
                       "message": "所有子问题均未获取到资料（L4）",
                       "failed_subqs": final.get("failed_subqs", [])})
            _persist_failed(task, payload, "所有子问题均未获取到资料（L4）",
                            final.get("failed_subqs", []))
            return

        path = write_report(final["topic"], final["report_md"])
        if settings.export_html:                                    # 同步导出独立 HTML
            try:
                from backend.agent.export import export_html
                export_html(final["report_md"], path, final["topic"])
            except Exception:
                LOG.warning("任务 %s HTML 导出失败（不影响 Markdown 交付）", task.id)
        stats = _stats(llm, started, final, path, settings)
        task.materials = final["materials"]          # 供追问/局部重写使用
        task.result = {
            "topic": final["topic"],
            "subquestions": final.get("subquestions", []),
            "report_md": final["report_md"],
            "report_file": path.name,
            "sources": [{"title": m["title"], "url": m["url"]} for m in final["materials"]],
            "failed_subqs": final.get("failed_subqs", []),
            "review": final.get("review", {}),
            "deliver_mode": final["deliver_mode"],
            "stats": stats,
        }
        task.status = "partial" if final["deliver_mode"] == "partial" else "done"
        from backend.core.storage import get_store
        try:
            get_store().save_finished(task)                          # 持久化：重启不丢历史
        except Exception:
            LOG.exception("任务 %s 落库失败", task.id)
        LOG.info("任务 %s 完成：%s | 来源 %d | Token %d | 耗时 %.1fs",
                 task.id, task.status, stats["sources"],
                 stats["total_tokens"]["in"] + stats["total_tokens"]["out"],
                 stats["elapsed_sec"])
        task.emit({"type": "done", "deliver_mode": final["deliver_mode"],
                   "report_file": path.name, "status": task.status})
    finally:
        RUNNING.discard(task.id)
