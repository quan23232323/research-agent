"""历史/统计/持久化集成测试：完成的研究任务落库，重启模拟后仍可访问与追问。"""
import json
import os

os.environ["MOCK_LLM"] = "1"
os.environ["MOCK_SEARCH"] = "1"
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="ra-test-"))

import pytest
from fastapi.testclient import TestClient

from backend.core import events
from backend.main import app

client = TestClient(app)


@pytest.fixture(scope="module")
def finished_task_id():
    """跑一个真实（Mock）研究任务并等其完成，返回 task_id。"""
    with TestClient(app) as c:
        subqs = c.post("/api/plan", json={"topic": "持久化验证主题", "depth": "quick"}
                       ).json()["subquestions"]
        tid = c.post("/api/research", json={
            "topic": "持久化验证主题", "depth": "quick", "length": "medium",
            "subquestions": subqs}).json()["task_id"]
        with c.stream("GET", f"/api/tasks/{tid}/events") as resp:
            for line in resp.iter_lines():
                if line.startswith("data: ") and json.loads(line[6:])["type"] in ("done", "error"):
                    break
        yield tid
    # 清理：删除任务与报告文件，避免污染后续运行
    c.delete(f"/api/tasks/{tid}")


def test_history_contains_task(finished_task_id):
    r = client.get("/api/history?limit=50").json()
    assert r["total"] >= 1
    item = next((x for x in r["items"] if x["id"] == finished_task_id), None)
    assert item is not None
    assert item["topic"] == "持久化验证主题"
    assert "report_md" not in item                 # 列表不携带大字段


def test_stats_updated(finished_task_id):
    s = client.get("/api/stats").json()
    assert s["total_tasks"] >= 1
    assert s["success_tasks"] >= 1
    assert "uptime_sec" in s and "running_tasks" in s


def test_html_export_generated(finished_task_id):
    import re
    r = client.get(f"/api/tasks/{finished_task_id}").json()
    html_name = re.sub(r"\.md$", ".html", r["result"]["report_file"])
    dl = client.get(f"/api/reports/{html_name}")
    assert dl.status_code == 200
    assert "<html" in dl.text.lower()


def test_archived_task_fallback(finished_task_id):
    """模拟服务重启：清空内存任务后，详情/追问/小节仍可用（归档回退）。"""
    events.TASKS.clear()
    r = client.get(f"/api/tasks/{finished_task_id}").json()
    assert r["archived"] is True
    assert "## 参考来源" in r["result"]["report_md"]

    secs = client.get(f"/api/tasks/{finished_task_id}/sections").json()["sections"]
    assert "核心要点" in secs

    fu = client.post(f"/api/tasks/{finished_task_id}/followup",
                     json={"question": "发展趋势如何？"})
    assert fu.status_code == 200
    assert fu.json()["answer"]

    # 归档任务重写同样可用并同步持久化
    rw = client.post(f"/api/tasks/{finished_task_id}/rewrite",
                     json={"section": "摘要", "feedback": "更精炼"})
    assert rw.status_code == 200
    assert "已按反馈重写" in rw.json()["report_md"]
    # 重启模拟下再次读取（从库），重写内容仍在
    events.TASKS.clear()
    r2 = client.get(f"/api/tasks/{finished_task_id}").json()
    assert "已按反馈重写" in r2["result"]["report_md"]


def test_versions_and_restore(finished_task_id):
    """v1.3：重写自动产生版本快照，可查看并一键恢复（归档任务同样适用）。"""
    tid = finished_task_id
    before = client.get(f"/api/tasks/{tid}").json()["result"]["report_md"]
    n0 = len(client.get(f"/api/tasks/{tid}/versions").json()["versions"])

    # 重写 -> 产生一个新版本快照（内容为重写前）
    r = client.post(f"/api/tasks/{tid}/rewrite",
                    json={"section": "核心要点", "feedback": "补充数据"})
    assert r.status_code == 200
    versions = client.get(f"/api/tasks/{tid}/versions").json()["versions"]
    assert len(versions) == n0 + 1
    latest = versions[0]
    assert latest["version"] == n0 + 1

    # 查看版本内容 = 重写前内容
    v1 = client.get(f"/api/tasks/{tid}/versions/{latest['version']}").json()
    assert v1["report_md"] == before

    # 恢复到该版本 -> 报告回到重写前（恢复动作自身也入档）
    r = client.post(f"/api/tasks/{tid}/restore", json={"version": latest["version"]})
    assert r.status_code == 200
    assert r.json()["report_md"] == before
    now = client.get(f"/api/tasks/{tid}").json()["result"]["report_md"]
    assert now == before
    assert len(client.get(f"/api/tasks/{tid}/versions").json()["versions"]) == n0 + 2

    # 边界：不存在的版本 -> 404
    assert client.get(f"/api/tasks/{tid}/versions/999").status_code == 404
    assert client.post(f"/api/tasks/{tid}/restore",
                       json={"version": 999}).status_code == 404


def test_history_search(finished_task_id):
    r = client.get("/api/history?q=持久化验证").json()
    assert r["total"] >= 1
    assert any(x["id"] == finished_task_id for x in r["items"])
    r = client.get("/api/history?q=绝对不存在的主题xyz").json()
    assert r["total"] == 0 and r["items"] == []


def test_rewrite_failure_leaves_no_phantom_version(finished_task_id):
    """v1.3.1：重写失败不应留下与当前内容相同的「伪版本」污染版本列表。"""
    from backend.agent.llm import LLMClient, LLMError

    tid = finished_task_id
    n0 = len(client.get(f"/api/tasks/{tid}/versions").json()["versions"])
    orig = LLMClient.chat

    async def boom(self, node, messages, tools=None):
        if node == "writer":
            raise LLMError("模拟模型调用失败")
        return await orig(self, node, messages, tools)

    LLMClient.chat = boom
    try:
        r = client.post(f"/api/tasks/{tid}/rewrite",
                        json={"section": "核心要点", "feedback": "补充数据"})
    finally:
        LLMClient.chat = orig

    assert r.status_code == 502
    assert len(client.get(f"/api/tasks/{tid}/versions").json()["versions"]) == n0


def test_delete_task(finished_task_id):
    assert client.delete(f"/api/tasks/{finished_task_id}").status_code == 200
    assert client.get(f"/api/tasks/{finished_task_id}").status_code == 404
