"""SQLite 持久化层：任务历史、结果归档与统计聚合。

设计要点：
- 内存 TASKS 只服务"运行中"任务的进度与 SSE；任务结束后落库（商业级交付：重启不丢历史）
- 归档任务通过 ArchivedTask 适配器支持追问/局部重写
- 单写者模型（SQLite），配套 DATA_DIR 环境变量便于挂卷
"""
import json
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.getenv("DATA_DIR", str(ROOT / "data")))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    status TEXT NOT NULL,
    deliver_mode TEXT,
    created_at TEXT,
    finished_at TEXT,
    report_file TEXT,
    report_md TEXT,
    sources TEXT,
    failed_subqs TEXT,
    review TEXT,
    stats TEXT,
    subquestions TEXT,
    materials TEXT,
    tokens_in INTEGER DEFAULT 0,
    tokens_out INTEGER DEFAULT 0,
    cost_yuan REAL,
    elapsed_sec REAL,
    sources_count INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tasks_finished ON tasks(finished_at DESC);
"""

_VERSIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS report_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    section TEXT,
    report_md TEXT NOT NULL,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_versions_task ON report_versions(task_id, version);
"""

_LIST_FIELDS = ("id", "topic", "status", "deliver_mode", "created_at", "finished_at",
                "report_file", "tokens_in", "tokens_out", "cost_yuan",
                "elapsed_sec", "sources_count")


class ArchivedTask:
    """归档任务的轻量适配器：支持追问/局部重写，无进度事件。"""

    def __init__(self, row: dict) -> None:
        self.id = row["id"]
        self.status = row["status"]
        self.finished = True
        self.history: list = []
        self.materials = json.loads(row["materials"] or "[]")
        stats = json.loads(row["stats"] or "{}")
        self.result = {
            "topic": row["topic"],
            "report_md": row["report_md"] or "",
            "report_file": row["report_file"] or "",
            "sources": json.loads(row["sources"] or "[]"),
            "failed_subqs": json.loads(row["failed_subqs"] or "[]"),
            "review": json.loads(row["review"] or "{}"),
            "deliver_mode": row["deliver_mode"] or "full",
            "subquestions": json.loads(row["subquestions"] or "[]"),
            "stats": stats,
        }

    def emit(self, ev: dict) -> None:      # 归档任务无 SSE 订阅者
        self.history.append(ev)


class TaskStore:
    def __init__(self, path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL：读不阻塞写，避免在事件循环里同步写大字段时把 SSE 读取卡住
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA + _VERSIONS_SCHEMA)
        self._conn.commit()

    # ---------- 写入 ----------

    def save_finished(self, task) -> None:
        r = task.result or {}
        stats = r.get("stats") or {}
        tt = stats.get("total_tokens") or {}
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO tasks
                   (id, topic, status, deliver_mode, created_at, finished_at, report_file,
                    report_md, sources, failed_subqs, review, stats, subquestions, materials,
                    tokens_in, tokens_out, cost_yuan, elapsed_sec, sources_count)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (task.id, r.get("topic", ""), task.status, r.get("deliver_mode", "full"),
                 datetime.now().isoformat(timespec="seconds"),
                 datetime.now().isoformat(timespec="seconds"),
                 r.get("report_file", ""), r.get("report_md", ""),
                 json.dumps(r.get("sources", []), ensure_ascii=False),
                 json.dumps(r.get("failed_subqs", []), ensure_ascii=False),
                 json.dumps(r.get("review", {}), ensure_ascii=False),
                 json.dumps(stats, ensure_ascii=False),
                 json.dumps(r.get("subquestions", []), ensure_ascii=False),
                 json.dumps(task.materials, ensure_ascii=False),
                 tt.get("in", 0), tt.get("out", 0), stats.get("cost_yuan"),
                 stats.get("elapsed_sec"), stats.get("sources", 0)),
            )
            self._conn.commit()

    def update_report(self, task_id: str, report_md: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE tasks SET report_md = ? WHERE id = ?",
                               (report_md, task_id))
            self._conn.commit()

    def save_failed(self, task_id: str, topic: str, error: str,
                    failed_subqs: list | None = None,
                    subquestions: list | None = None) -> None:
        """L4 失败任务也落库，保证成功率统计真实。"""
        now = datetime.now().isoformat(timespec="seconds")
        stats = {"error": str(error)[:300], "total_tokens": {"in": 0, "out": 0},
                 "sources": 0}
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO tasks
                   (id, topic, status, deliver_mode, created_at, finished_at, report_file,
                    report_md, sources, failed_subqs, review, stats, subquestions, materials,
                    tokens_in, tokens_out, cost_yuan, elapsed_sec, sources_count)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (task_id, topic, "failed", "failed", now, now, "", "",
                 "[]", json.dumps(failed_subqs or [], ensure_ascii=False), "{}",
                 json.dumps(stats, ensure_ascii=False),
                 json.dumps(subquestions or [], ensure_ascii=False), "[]",
                 0, 0, None, None, 0),
            )
            self._conn.commit()

    # ---------- 版本历史（局部重写快照与恢复） ----------

    def add_version(self, task_id: str, section: str, report_md: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(version), 0) v FROM report_versions WHERE task_id = ?",
                (task_id,)).fetchone()
            version = row["v"] + 1
            self._conn.execute(
                "INSERT INTO report_versions (task_id, version, section, report_md, created_at)"
                " VALUES (?,?,?,?,?)",
                (task_id, version, section, report_md,
                 datetime.now().isoformat(timespec="seconds")))
            self._conn.commit()
            return version

    def list_versions(self, task_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT version, section, created_at FROM report_versions"
                " WHERE task_id = ? ORDER BY version DESC", (task_id,)).fetchall()
        return [dict(r) for r in rows]

    def get_version(self, task_id: str, version: int) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT report_md FROM report_versions WHERE task_id = ? AND version = ?",
                (task_id, version)).fetchone()
        return row["report_md"] if row else None

    def delete(self, task_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT report_file FROM tasks WHERE id = ?",
                                     (task_id,)).fetchone()
            if not row:
                return None
            self._conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            # 一并清理版本快照，否则孤儿快照会让库无限增长（v1.3.1）
            self._conn.execute("DELETE FROM report_versions WHERE task_id = ?", (task_id,))
            self._conn.commit()
            return row["report_file"]

    # ---------- 查询 ----------

    def get(self, task_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM tasks WHERE id = ?",
                                     (task_id,)).fetchone()
        return dict(row) if row else None

    def list(self, limit: int = 20, offset: int = 0, q: str | None = None) -> tuple[list[dict], int]:
        with self._lock:
            if q:
                like = f"%{q}%"
                total = self._conn.execute(
                    "SELECT COUNT(*) c FROM tasks WHERE topic LIKE ?", (like,)).fetchone()["c"]
                rows = self._conn.execute(
                    f"SELECT {', '.join(_LIST_FIELDS)} FROM tasks WHERE topic LIKE ?"
                    " ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (like, limit, offset)).fetchall()
            else:
                total = self._conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"]
                rows = self._conn.execute(
                    f"SELECT {', '.join(_LIST_FIELDS)} FROM tasks"
                    " ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (limit, offset)).fetchall()
        return [dict(r) for r in rows], total

    def stats(self) -> dict:
        with self._lock:
            row = self._conn.execute(
                """SELECT COUNT(*) AS total,
                          SUM(CASE WHEN status IN ('done','partial') THEN 1 ELSE 0 END) AS ok,
                          COALESCE(SUM(tokens_in), 0) AS tin,
                          COALESCE(SUM(tokens_out), 0) AS tout,
                          COALESCE(SUM(cost_yuan), 0) AS cost,
                          COALESCE(AVG(elapsed_sec), 0) AS avg_elapsed,
                          COALESCE(SUM(sources_count), 0) AS sources
                   FROM tasks""").fetchone()
        total = row["total"]
        return {
            "total_tasks": total,
            "success_tasks": row["ok"] or 0,
            "success_rate": round((row["ok"] or 0) / total, 4) if total else None,
            "total_tokens": {"in": row["tin"], "out": row["tout"]},
            "total_cost_yuan": round(row["cost"], 4),
            "avg_elapsed_sec": round(row["avg_elapsed"], 1),
            "total_sources": row["sources"],
        }


_store: TaskStore | None = None
_store_lock = threading.Lock()


def get_store() -> TaskStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = TaskStore(Path(DATA_DIR) / "research.db")
        return _store
