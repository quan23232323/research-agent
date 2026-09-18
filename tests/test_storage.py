"""持久化层单元测试：TaskStore 读写 / 归档适配 / 统计聚合（临时库，不影响真实数据）。"""
import os

os.environ["MOCK_LLM"] = "1"
os.environ["MOCK_SEARCH"] = "1"

import pytest

from backend.core.storage import ArchivedTask, TaskStore


class FakeTask:
    """模拟已完成任务（结构与 events.Task 一致的最小子集）。"""

    def __init__(self, tid="t1", topic="测试主题"):
        self.id = tid
        self.status = "done"
        self.materials = [{"title": "来源A", "url": "https://a.example.com",
                           "snippet": "s", "content": "c"}]
        self.result = {
            "topic": topic,
            "report_md": "# 报告\n## 核心要点\n- 要点[1]\n\n## 参考来源\n[1] 来源A",
            "report_file": "test.md",
            "sources": [{"title": "来源A", "url": "https://a.example.com"}],
            "failed_subqs": [],
            "review": {"verdict": "pass", "reasons": []},
            "deliver_mode": "full",
            "stats": {"total_tokens": {"in": 100, "out": 20, "budget_cap": 150000},
                      "cost_yuan": 0.01, "elapsed_sec": 1.5, "sources": 1,
                      "subq_total": 2, "subq_failed": 0, "rewrite_count": 0},
        }


@pytest.fixture()
def store(tmp_path):
    return TaskStore(tmp_path / "test.db")


def test_save_and_get_roundtrip(store):
    store.save_finished(FakeTask())
    row = store.get("t1")
    assert row is not None
    assert row["topic"] == "测试主题"
    assert row["tokens_in"] == 100 and row["sources_count"] == 1
    assert "参考来源" in row["report_md"]
    assert json_len(row["sources"]) == 1


def json_len(s):
    import json
    return len(json.loads(s))


def test_get_missing_returns_none(store):
    assert store.get("nope") is None


def test_list_fields_and_total(store):
    for i in range(3):
        store.save_finished(FakeTask(tid=f"t{i}", topic=f"主题{i}"))
    items, total = store.list(limit=2, offset=0)
    assert total == 3 and len(items) == 2
    assert "report_md" not in items[0]            # 列表不携带大字段
    items2, _ = store.list(limit=2, offset=2)
    assert len(items2) == 1


def test_update_report(store):
    store.save_finished(FakeTask())
    store.update_report("t1", "# 新报告")
    assert "新报告" in store.get("t1")["report_md"]


def test_delete(store):
    store.save_finished(FakeTask())
    assert store.delete("t1") == "test.md"
    assert store.get("t1") is None
    assert store.delete("t1") is None


def test_stats_aggregation(store):
    store.save_finished(FakeTask(tid="a"))
    t = FakeTask(tid="b")
    t.status = "failed"
    store.save_finished(t)
    s = store.stats()
    assert s["total_tasks"] == 2
    assert s["success_tasks"] == 1
    assert s["total_tokens"]["in"] == 200
    assert s["success_rate"] == 0.5


def test_archived_task_adapter(store):
    store.save_finished(FakeTask())
    arch = ArchivedTask(store.get("t1"))
    assert arch.finished and arch.status == "done"
    assert arch.materials[0]["title"] == "来源A"
    assert arch.result["report_file"] == "test.md"
    assert arch.result["topic"] == "测试主题"          # v1.3：归档任务回填 topic
    assert arch.result["subquestions"] == []           # v1.3：归档任务带子问题清单
    arch.emit({"x": 1})                                # 不抛异常即可


def test_save_failed_and_stats(store):
    store.save_failed("f1", "失败主题", "所有子问题均未获取到资料（L4）",
                      [{"subq": "子问题", "reason": "搜索失败"}], ["子问题"])
    row = store.get("f1")
    assert row["status"] == "failed" and row["report_md"] == ""
    s = store.stats()
    assert s["total_tasks"] == 1 and s["success_tasks"] == 0
    assert s["success_rate"] == 0.0


def test_versions_roundtrip(store):
    store.save_finished(FakeTask())
    assert store.list_versions("t1") == []
    assert store.add_version("t1", "核心要点", "# 旧版内容") == 1
    assert store.add_version("t1", "摘要", "# 更旧内容") == 2
    versions = store.list_versions("t1")
    assert [v["version"] for v in versions] == [2, 1]   # 倒序
    assert store.get_version("t1", 1) == "# 旧版内容"
    assert store.get_version("t1", 99) is None


def test_list_search(store):
    store.save_finished(FakeTask(tid="a", topic="电力行业研究"))
    store.save_finished(FakeTask(tid="b", topic="机器人产业研究"))
    items, total = store.list(q="电力")
    assert total == 1 and items[0]["topic"] == "电力行业研究"
    _, total_all = store.list()
    assert total_all == 2
