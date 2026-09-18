"""离线冒烟测试：Mock 模式跑通四节点工作流 + L1/L4 兜底路径。

运行：cd research-agent && python -m pytest tests/ -q
"""
import asyncio
import os

os.environ["MOCK_LLM"] = "1"
os.environ["MOCK_SEARCH"] = "1"
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="ra-test-"))


from backend.agent import tools
from backend.agent.graph import run_task
from backend.agent.llm import BudgetExceeded, LLMClient, LLMError
from backend.agent.planner import plan_subquestions
from backend.config import load_settings
from backend.core.events import TASKS, Task


def _settings():
    return load_settings()


def _payload(subqs, topic="大模型在电力行业的应用"):
    return {"topic": topic, "angle": "", "depth": "standard", "length": "medium",
            "subquestions": subqs}


def _run(sync_fn):
    return asyncio.run(sync_fn)


def test_config_loads():
    s = _settings()
    assert s.mock_llm and s.mock_search
    assert {"planner", "researcher", "writer", "reviewer", "followup"} <= set(s.nodes)
    assert s.max_total_tokens == 150000


def test_planner_returns_subquestions():
    async def main():
        s = _settings()
        t = Task()
        llm = LLMClient(s, t)
        subqs = await plan_subquestions("固态电池产业化进展", "standard", "", llm)
        assert 3 <= len(subqs) <= 5
        assert all(isinstance(x, str) and x for x in subqs)
    _run(main())


def test_full_pipeline_done():
    """正常路径：done + 报告文件 + 来源 + 角标。"""
    async def main():
        s = _settings()
        t = Task()
        TASKS[t.id] = t
        await run_task(t, s, _payload([
            "大模型在电力行业的应用现状",
            "电力大模型的技术路线",
            "典型落地案例",
            "面临的挑战",
        ]))
        assert t.status == "done"
        assert t.result["deliver_mode"] == "full"
        assert "[1]" in t.result["report_md"]
        assert len(t.result["sources"]) >= 1
        assert "## 参考来源" in t.result["report_md"]
        assert t.result["stats"]["sources"] >= 1
        assert os.path.isfile(os.path.join(str(tools.REPORTS_DIR), t.result["report_file"]))
    _run(main())


def test_l1_partial_subq_failure():
    """L1 兜底：单个子问题搜索失败 -> 跳过并标记，整体仍 done。"""
    async def main():
        s = _settings()
        real_search = tools.web_search

        async def flaky_search(query, ctx):
            if "技术路线" in query:
                raise tools.ToolError("模拟搜索源全部失败")
            return await real_search(query, ctx)

        tools.web_search = flaky_search
        try:
            t = Task()
            TASKS[t.id] = t
            await run_task(t, s, _payload([
                "应用现状", "电力大模型的技术路线", "落地案例",
            ]))
            assert t.status == "done"
            failed = t.result["failed_subqs"]
            assert any("技术路线" in f["subq"] for f in failed)
        finally:
            tools.web_search = real_search
    _run(main())


def test_l4_all_search_failed():
    """L4 兜底：全部子问题失败 -> 任务 failed + error 事件。"""
    async def main():
        s = _settings()

        async def dead_search(query, ctx):
            raise tools.ToolError("模拟全部搜索源不可用")

        real = tools.web_search                 # 先备份原函数再覆盖
        tools.web_search = dead_search
        try:
            t = Task()
            TASKS[t.id] = t
            await run_task(t, s, _payload(["子问题一", "子问题二"]))
            assert t.status == "failed"
            assert any(e.get("type") == "error" and e.get("level") == "L4"
                       for e in t.history)
            # v1.3：失败任务同样落库，成功率统计真实
            from backend.core.storage import get_store
            row = get_store().get(t.id)
            assert row is not None and row["status"] == "failed"
            assert row["topic"] == "大模型在电力行业的应用"
        finally:
            tools.web_search = real
    _run(main())


def test_l3_budget_exceeded_partial():
    """L3 兜底：撰写阶段预算触顶 -> 素材汇编版部分交付。

    v1.3.1 起 Writer 走 stream_chat（统一重试/降级/预算记账），故在此拦截流式入口。
    BudgetExceeded 不进普通调用兜底，应直接落到 L3。
    """
    async def main():
        s = _settings()
        orig = LLMClient.stream_chat

        async def patched(self, node, messages, on_chunk=None):
            if node == "writer":
                raise BudgetExceeded("模拟预算触顶")
            return await orig(self, node, messages, on_chunk)

        LLMClient.stream_chat = patched
        try:
            t = Task()
            TASKS[t.id] = t
            await run_task(t, s, _payload(["应用现状", "技术路线"]))
            assert t.status == "partial"
            assert t.result["deliver_mode"] == "partial"
            assert "素材汇编" in t.result["report_md"]
        finally:
            LLMClient.stream_chat = orig
    _run(main())


def test_l2_stream_falls_back_to_plain_call():
    """L2 兜底：流式不可用（服务端不支持 stream）-> 降级普通调用，仍完整交付。"""
    async def main():
        s = _settings()
        orig = LLMClient.stream_chat

        async def patched(self, node, messages, on_chunk=None):
            if node == "writer":
                raise LLMError("模拟服务端不支持流式")
            return await orig(self, node, messages, on_chunk)

        LLMClient.stream_chat = patched
        try:
            t = Task()
            TASKS[t.id] = t
            await run_task(t, s, _payload(["应用现状", "技术路线"]))
            assert t.status == "done"
            assert t.result["deliver_mode"] == "full"
            assert any("降级为普通调用" in str(ev.get("detail", "")) for ev in t.history)
        finally:
            LLMClient.stream_chat = orig
    _run(main())


def test_l3_stream_and_plain_both_fail():
    """L3 兜底：流式与普通调用双双失败 -> 素材汇编版部分交付。"""
    async def main():
        s = _settings()
        orig_stream = LLMClient.stream_chat
        orig_chat = LLMClient.chat

        async def dead_stream(self, node, messages, on_chunk=None):
            raise LLMError("模拟流式失败")

        async def dead_chat(self, node, messages, tools=None):
            if node == "writer":
                raise LLMError("模拟普通调用失败")
            return await orig_chat(self, node, messages, tools)

        LLMClient.stream_chat = dead_stream
        LLMClient.chat = dead_chat
        try:
            t = Task()
            TASKS[t.id] = t
            await run_task(t, s, _payload(["应用现状", "技术路线"]))
            assert t.status == "partial"
            assert "素材汇编" in t.result["report_md"]
        finally:
            LLMClient.stream_chat = orig_stream
            LLMClient.chat = orig_chat
    _run(main())


def test_reviewer_rewrite_loop():
    """幻觉兜底：首审不通过 -> 打回重写 -> 复审通过。"""
    async def main():
        s = _settings()
        orig = LLMClient.chat
        calls = {"reviewer": 0}

        async def patched(self, node, messages, tools=None):
            if node == "reviewer":
                calls["reviewer"] += 1
                if calls["reviewer"] == 1:
                    return {"content": '{"verdict":"rewrite","reasons":["要点缺乏来源支撑"]}',
                            "tool_calls": [], "usage": {"in": 10, "out": 5}}
            return await orig(self, node, messages, tools)

        LLMClient.chat = patched
        try:
            t = Task()
            TASKS[t.id] = t
            await run_task(t, s, _payload(["应用现状", "技术路线"]))
            assert t.status == "done"
            assert calls["reviewer"] >= 2
            assert t.result["stats"]["rewrite_count"] == 1
        finally:
            LLMClient.chat = orig
    _run(main())
