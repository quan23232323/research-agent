"""API 集成测试（TestClient + Mock 模式）：L0 校验 / 全流程 / 追问 / 局部重写 / 下载。

注意：研究任务是请求内 asyncio.create_task 的后台协程，必须在 TestClient 的
with 上下文中调用（事件循环跨请求存活），否则 SSE 流收不到事件。
"""
import json
import os

os.environ["MOCK_LLM"] = "1"
os.environ["MOCK_SEARCH"] = "1"
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="ra-test-"))

from fastapi.testclient import TestClient

from backend.main import app

client = TestClient(app)


def test_health_and_meta():
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    r = client.get("/api/meta")
    assert r.status_code == 200
    assert "models" in r.json()


def test_l0_validation():
    assert client.post("/api/plan", json={"topic": "   "}).status_code == 400
    assert client.post("/api/plan", json={"topic": "枪械制造入门"}).status_code == 400
    assert client.post("/api/plan", json={"topic": "x" * 500}).status_code == 400
    assert client.post("/api/plan", json={"topic": "测试", "depth": "bad"}).status_code == 400
    # 空子问题列表违反 pydantic schema（min_length=1），返回 422
    assert client.post("/api/research", json={"topic": "测试", "subquestions": []}).status_code == 422


def test_full_flow_with_followup_and_rewrite():
    with TestClient(app) as c:                      # 事件循环跨请求存活
        # 阶段①②：规划
        r = c.post("/api/plan", json={"topic": "大模型在电力行业的应用", "depth": "quick"})
        assert r.status_code == 200
        subqs = r.json()["subquestions"]
        assert 1 <= len(subqs) <= 5

        # 启动研究 + 消费 SSE 直到结束
        tid = c.post("/api/research", json={
            "topic": "大模型在电力行业的应用", "depth": "quick",
            "length": "medium", "subquestions": subqs}).json()["task_id"]

        done = None
        with c.stream("GET", f"/api/tasks/{tid}/events") as resp:
            for line in resp.iter_lines():
                if line.startswith("data: "):
                    ev = json.loads(line[6:])
                    if ev["type"] in ("done", "error"):
                        done = ev
                        break
        assert done is not None and done["type"] == "done"

        # 结果与报告结构
        res = c.get(f"/api/tasks/{tid}").json()
        assert res["status"] == "done"
        report = res["result"]["report_md"]
        assert "## 参考来源" in report and "[1]" in report
        assert res["result"]["stats"]["sources"] >= 1

        # 小节列表（不含参考来源）
        secs = c.get(f"/api/tasks/{tid}/sections").json()["sections"]
        assert "核心要点" in secs
        assert "参考来源" not in secs

        # 追问：基于素材作答并返回可点击来源
        r = c.post(f"/api/tasks/{tid}/followup", json={"question": "主要挑战是什么？"})
        assert r.status_code == 200
        body = r.json()
        assert body["answer"] and body["sources"]

        # 局部重写：仅替换目标小节
        r = c.post(f"/api/tasks/{tid}/rewrite",
                   json={"section": "核心要点", "feedback": "补充数据"})
        assert r.status_code == 200
        updated = r.json()["report_md"]
        assert "已按反馈重写" in updated
        assert "## 参考来源" in updated           # 来源小节保持不变

        # 下载内容与重写后一致
        dl = c.get(f"/api/reports/{res['result']['report_file']}")
        assert dl.status_code == 200
        assert "已按反馈重写" in dl.text

        # 边界：参考来源不可重写、不存在的小节 404、空问题 422
        assert c.post(f"/api/tasks/{tid}/rewrite",
                      json={"section": "参考来源"}).status_code == 400
        assert c.post(f"/api/tasks/{tid}/rewrite",
                      json={"section": "不存在的小节"}).status_code == 404
        assert c.post(f"/api/tasks/{tid}/followup",
                      json={"question": ""}).status_code == 422

        # 前端静态资源可达
        assert c.get("/").status_code == 200
        assert c.get("/js/app.js").status_code == 200
        assert c.get("/css/style.css").status_code == 200
