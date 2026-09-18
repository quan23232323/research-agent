"""任务与进度事件总线：SSE 进度反馈的数据源（设计方案 3.2③）。"""
import time
import uuid

MAX_TASKS_IN_MEMORY = 200
MAX_CHUNK_EVENTS = 2000     # 单任务流式预览事件上限：正文以 result 为准，防长报告把事件表撑爆

TASKS: dict[str, "Task"] = {}
RUNNING: set[str] = set()            # 正在运行的任务 id（并发上限控制）


class Task:
    def __init__(self) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.status = "running"          # running | done | partial | failed
        self.history: list[dict] = []    # 全量事件（SSE 重放用）
        self.result: dict = {}
        self.materials: list[dict] = []  # 编号素材（追问/局部重写的依据）
        self._chunk_events = 0

    def emit(self, ev: dict) -> None:
        ev.setdefault("ts", round(time.time(), 3))
        if ev.get("type") == "chunk":
            # 流式预览只是过程展示，超出上限后丢弃即可（最终正文在 task.result 中）
            self._chunk_events += 1
            if self._chunk_events > MAX_CHUNK_EVENTS:
                return
        self.history.append(ev)

    @property
    def finished(self) -> bool:
        return self.status in ("done", "partial", "failed")


def new_task() -> Task:
    t = Task()
    TASKS[t.id] = t
    if len(TASKS) > MAX_TASKS_IN_MEMORY:     # 内存保护：优先清理已结束的旧任务
        for tid in list(TASKS):
            if len(TASKS) <= MAX_TASKS_IN_MEMORY:
                break
            if TASKS[tid].finished:
                TASKS.pop(tid, None)
    return t


def get_task(task_id: str) -> Task | None:
    return TASKS.get(task_id)
