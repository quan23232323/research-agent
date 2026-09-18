"""API 路由：规划 / 研究任务 / SSE 进度 / 结果与报告 / 追问与局部重写 / 历史 / 统计 / 健康检查。"""
import asyncio
import contextlib
import json
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from backend.agent.export import export_html, html_sibling
from backend.agent.followup import FollowupService, list_sections
from backend.agent.graph import run_task
from backend.agent.llm import LLMClient, LLMError
from backend.agent.planner import plan_subquestions
from backend.agent.tools import REPORTS_DIR
from backend.config import load_settings
from backend.core.events import RUNNING, TASKS, get_task, new_task
from backend.core.storage import ArchivedTask, get_store

router = APIRouter(prefix="/api")
settings = load_settings()

APP_VERSION = "1.3.1"
STARTED_AT = time.time()

# 后台任务的强引用集合：asyncio 只对 Task 持弱引用，不保存引用可能被 GC 提前回收
_BACKGROUND: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    """启动后台任务并持有强引用，结束后自动释放。"""
    task = asyncio.get_running_loop().create_task(coro)
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)


# ---------- L0 输入校验（设计方案 7.1） ----------

def validate_topic(topic: str) -> str:
    topic = (topic or "").strip()
    if not topic:
        raise HTTPException(400, "主题不能为空")
    if len(topic) > settings.topic_max_chars:
        raise HTTPException(400, f"主题过长（>{settings.topic_max_chars} 字），请精简")
    _check_sensitive(topic)
    return topic


def _check_sensitive(text: str) -> None:
    for w in settings.sensitive_words:
        if w in text:
            raise HTTPException(400, "内容包含不适宜词汇，请修改后重试")


# ---------- 请求模型 ----------

class PlanRequest(BaseModel):
    topic: str
    depth: str = "standard"            # quick | standard | deep
    length: str = "medium"             # short | medium | long
    angle: str = ""


class ResearchRequest(PlanRequest):
    subquestions: list[str] = Field(min_length=1, max_length=8)


class FollowupRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)


class RewriteRequest(BaseModel):
    section: str = Field(min_length=1, max_length=100)
    feedback: str = Field(default="", max_length=500)


class RestoreRequest(BaseModel):
    version: int = Field(ge=1)


# ---------- 健康检查与服务信息 ----------

@router.get("/health")
async def health():
    return {"status": "ok", "version": APP_VERSION,
            "running_tasks": len(RUNNING),
            "mock": settings.mock_llm or settings.mock_search}


@router.get("/meta")
async def meta():
    return {"mock_llm": settings.mock_llm, "mock_search": settings.mock_search,
            "models": {k: v.model for k, v in settings.nodes.items()},
            "max_total_tokens": settings.max_total_tokens,
            "auth_enabled": bool(settings.auth_token),
            "version": APP_VERSION}


# ---------- 规划（阶段②：可干预的任务规划） ----------

@router.post("/plan")
async def make_plan(req: PlanRequest):
    topic = validate_topic(req.topic)
    if req.depth not in ("quick", "standard", "deep"):
        raise HTTPException(400, "depth 取值：quick / standard / deep")
    probe = new_task()                 # 规划阶段的事件不进入 SSE，仅用于 LLMClient 接口对齐
    llm = LLMClient(settings, probe)
    try:
        subqs = await plan_subquestions(topic, req.depth, req.angle, llm)
    finally:
        TASKS.pop(probe.id, None)
    return {"topic": topic, "subquestions": subqs,
            "mock": settings.mock_llm or settings.mock_search}


# ---------- 启动研究（异步任务） ----------

@router.post("/research")
async def start_research(req: ResearchRequest):
    topic = validate_topic(req.topic)
    subqs = [s.strip() for s in req.subquestions if s.strip()]
    if not subqs:
        raise HTTPException(400, "子问题列表不能为空")
    if len(RUNNING) >= settings.max_concurrent_tasks:
        raise HTTPException(429, f"已有 {len(RUNNING)} 个研究任务在运行，请稍后再试")
    task = new_task()
    # 在同一同步块内占位：否则「检查上限」与「实际开始运行」之间存在窗口，
    # 并发请求会同时通过校验（TOCTOU）。
    RUNNING.add(task.id)
    try:
        _spawn(run_task(task, settings,
                        req.model_dump() | {"topic": topic, "subquestions": subqs}))
    except Exception:
        RUNNING.discard(task.id)
        TASKS.pop(task.id, None)
        raise
    return {"task_id": task.id}


# ---------- SSE 进度流（3.2③ 实时反馈） ----------

@router.get("/tasks/{task_id}/events")
async def task_events(task_id: str):
    task = get_task(task_id)
    if task:
        async def stream():
            idx = 0
            idle = 0
            while True:
                while idx < len(task.history):
                    ev = task.history[idx]
                    idx += 1
                    yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                    if ev.get("type") in ("done", "error"):
                        return
                if task.finished:
                    return
                await asyncio.sleep(0.25)
                idle += 1
                if idle > 3600:        # 15 分钟无事件保护
                    yield 'data: {"type":"error","level":"L4","message":"进度流超时"}\n\n'
                    return
        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    # 归档任务：重放单条结束事件（前端据此直接拉取结果）
    row = get_store().get(task_id)
    if row:
        async def replay():
            ev = {"type": "done", "deliver_mode": row["deliver_mode"],
                  "report_file": row["report_file"], "status": row["status"]}
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        return StreamingResponse(replay(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    raise HTTPException(404, "任务不存在")


# ---------- 任务结果（内存任务 + 归档任务统一视图） ----------

@router.get("/tasks/{task_id}")
async def task_result(task_id: str):
    task = get_task(task_id)
    if task:
        return {"task_id": task.id, "status": task.status, "result": task.result,
                "archived": False}
    row = get_store().get(task_id)
    if row:
        arch = ArchivedTask(row)
        return {"task_id": arch.id, "status": arch.status, "result": arch.result,
                "archived": True}
    raise HTTPException(404, "任务不存在")


# ---------- 历史记录（持久化） ----------

@router.get("/history")
async def history(limit: int = 20, offset: int = 0, q: str = ""):
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    items, total = get_store().list(limit=limit, offset=offset, q=q.strip() or None)
    return {"total": total, "items": items, "q": q}


@router.delete("/tasks/{task_id}")
async def delete_task(task_id: str):
    task = get_task(task_id)
    if task and not task.finished:
        raise HTTPException(409, "任务正在运行，无法删除")
    report_file = get_store().delete(task_id)
    if report_file is None:
        raise HTTPException(404, "任务不存在")
    if task:
        TASKS.pop(task_id, None)
    # 同时清理 Markdown 与 HTML 两种导出文件（v1.3 修复 .md.html 路径错误）
    for name in (report_file, html_sibling(report_file).name):
        (Path(REPORTS_DIR) / name).unlink(missing_ok=True)
    return {"deleted": task_id}


# ---------- 全局统计 ----------

@router.get("/stats")
async def stats():
    s = get_store().stats()
    s["running_tasks"] = len(RUNNING)
    s["uptime_sec"] = round(time.time() - STARTED_AT, 1)
    return s


# ---------- 报告下载 ----------

@router.get("/reports/{filename}")
async def download_report(filename: str):
    path = Path(REPORTS_DIR) / Path(filename).name
    if not path.is_file():
        raise HTTPException(404, "报告不存在")
    media = ("text/html; charset=utf-8" if path.suffix == ".html"
             else "text/markdown; charset=utf-8")
    return FileResponse(path, filename=path.name, media_type=media)


# ---------- 追问与局部重写（3.2④ 交付后交互，支持归档任务） ----------

def _require_task(task_id: str):
    task = get_task(task_id)
    if task:
        if not task.finished or not task.result or not task.materials:
            raise HTTPException(400, "任务尚未完成或无可用素材")
        return task
    row = get_store().get(task_id)
    if row:
        arch = ArchivedTask(row)
        if not arch.result["report_md"]:
            raise HTTPException(400, "归档任务无报告内容")
        return arch
    raise HTTPException(404, "任务不存在")


@router.get("/tasks/{task_id}/sections")
async def task_sections(task_id: str):
    task = _require_task(task_id)
    sections = [t for t in list_sections(task.result["report_md"]) if t != "参考来源"]
    return {"sections": sections}


@router.post("/tasks/{task_id}/followup")
async def task_followup(task_id: str, req: FollowupRequest):
    task = _require_task(task_id)
    _check_sensitive(req.question)
    try:
        return await FollowupService(task, settings).answer(req.question.strip())
    except LLMError as e:
        raise HTTPException(502, f"模型调用失败：{e}"[:200]) from e


@router.post("/tasks/{task_id}/rewrite")
async def task_rewrite(task_id: str, req: RewriteRequest):
    task = _require_task(task_id)
    section = req.section.strip()
    if section == "参考来源":
        raise HTTPException(400, "参考来源小节不支持重写")
    if section not in list_sections(task.result["report_md"]):
        raise HTTPException(404, f"未找到小节：{section}")
    svc = FollowupService(task, settings)
    before = task.result["report_md"]      # 快照内容先取出来（rewrite 会就地改写该字段）
    try:
        updated = await svc.rewrite(section, req.feedback.strip())
    except LLMError as e:
        raise HTTPException(502, f"模型调用失败：{e}"[:200]) from e
    # 仅在重写成功后存档：否则失败的请求会留下一条与当前内容相同的「伪版本」污染版本列表
    get_store().add_version(task_id, section, before)
    # 覆盖报告文件并同步持久化（内存任务 / 归档任务一致）
    path = Path(REPORTS_DIR) / Path(task.result["report_file"]).name
    path.write_text(updated, encoding="utf-8")
    if settings.export_html:
        with contextlib.suppress(Exception):       # HTML 导出失败不影响主交付
            export_html(updated, path, task.result.get("topic", path.stem))
    get_store().update_report(task_id, updated)
    return {"report_md": updated, "section": req.section.strip()}


# ---------- 报告版本历史（v1.3：重写快照 + 一键恢复） ----------

@router.get("/tasks/{task_id}/versions")
async def task_versions(task_id: str):
    _require_task(task_id)
    return {"versions": get_store().list_versions(task_id)}


@router.get("/tasks/{task_id}/versions/{version}")
async def task_version_detail(task_id: str, version: int):
    _require_task(task_id)
    md = get_store().get_version(task_id, version)
    if md is None:
        raise HTTPException(404, f"版本不存在：{version}")
    return {"version": version, "report_md": md}


@router.post("/tasks/{task_id}/restore")
async def task_restore(task_id: str, req: RestoreRequest):
    task = _require_task(task_id)
    md = get_store().get_version(task_id, req.version)
    if md is None:
        raise HTTPException(404, f"版本不存在：{req.version}")
    # 恢复前同样快照当前版本，保证任何操作可回退
    get_store().add_version(task_id, f"恢复到 v{req.version} 前",
                            task.result["report_md"])
    task.result["report_md"] = md
    path = Path(REPORTS_DIR) / Path(task.result["report_file"]).name
    path.write_text(md, encoding="utf-8")
    if settings.export_html:
        with contextlib.suppress(Exception):       # HTML 导出失败不影响主交付
            export_html(md, path, task.result.get("topic", path.stem))
    get_store().update_report(task_id, md)
    return {"report_md": md, "restored_from": req.version}
