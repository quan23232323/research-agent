"""单元测试：配置 / 解析 / 规则核验 / 素材摘要 / 事件总线内存保护（全部离线）。"""
import os

os.environ["MOCK_LLM"] = "1"
os.environ["MOCK_SEARCH"] = "1"
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="ra-test-"))

import pytest

from backend.agent.llm import _mock_chat
from backend.agent.planner import _fallback_plan, _parse_json_list
from backend.agent.reviewer import _rule_check
from backend.agent.writer import _append_sources, compile_partial, digest_materials
from backend.config import load_settings as cfg_load_settings
from backend.core import events


def test_settings_fields():
    s = cfg_load_settings()
    assert s.mock_llm and s.mock_search
    assert {"planner", "researcher", "writer", "reviewer", "followup"} <= set(s.nodes)
    assert s.max_sources >= 1
    assert s.max_concurrent_tasks >= 1
    assert s.question_max_chars >= 100


def test_parse_json_list():
    assert _parse_json_list('["a","b"]') == ["a", "b"]
    assert _parse_json_list('```json\n["a"]\n```') == ["a"]
    with pytest.raises(ValueError):
        _parse_json_list("不是数组")
    with pytest.raises(ValueError):
        _parse_json_list("[]")            # 空数组视为异常


def test_parse_json_list_numbering_stripped():
    """v1.3：去除模型自带的序号前缀。"""
    out = _parse_json_list('["1. 现状如何", "2、技术路线", "（3）应用案例"]')
    assert out == ["现状如何", "技术路线", "应用案例"]


def test_rule_check_body_only():
    """v1.3：参考来源小节的角标不能作为"有引用"的证据。"""
    report = "## 摘要\n无引用正文\n\n## 参考来源\n\n[1] 来源A\n[2] 来源B"
    problems = _rule_check(report, 2)
    assert any("没有任何来源角标" in p for p in problems)


def test_fallback_plan():
    plan = _fallback_plan("主题X", 3)
    assert len(plan) == 3
    assert all("主题X" in p for p in plan)


def test_rule_check():
    assert _rule_check("毫无引用的一段话", 2) != []            # 无角标
    assert _rule_check("要点[3]越界", 2) != []                  # 角标越界
    assert _rule_check("要点[1]与[2]正常", 2) == []


def test_digest_and_sources():
    mats = [{"title": "T1", "url": "u1", "snippet": "s", "content": "c" * 50}]
    d = digest_materials(mats, per_source=10)
    assert "[1] T1" in d and "u1" in d
    out = _append_sources("# R", mats)
    assert "## 参考来源" in out and "[1] T1" in out


def test_compile_partial():
    md = compile_partial("主题", [{"title": "T", "url": "u", "snippet": "s", "content": ""}],
                         [{"subq": "子问题A", "reason": "搜索失败"}], "撰写失败")
    assert "降级交付 L3" in md
    assert "检索失败项" in md and "子问题A" in md


def test_mock_chat_followup():
    content, calls = _mock_chat("followup", [{"role": "user", "content": "为什么？"}])
    assert calls == [] and "[1]" in content


def test_mock_writer_rewrite():
    content, _ = _mock_chat("writer", [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "【重写小节】核心要点\n重写要求：更具体\n原小节：x"}])
    assert content.startswith("## 核心要点")
    assert "已按反馈重写" in content


def test_tasks_memory_cap():
    for _ in range(events.MAX_TASKS_IN_MEMORY + 50):
        t = events.new_task()
        t.status = "done"
    assert len(events.TASKS) <= events.MAX_TASKS_IN_MEMORY + 1


def test_mock_search_follows_mock_llm(monkeypatch):
    """MOCK_SEARCH 未显式设置时跟随最终生效的 mock_llm（含"无 API Key 自动 Mock"）。"""
    from backend import config as cfg
    monkeypatch.delenv("MOCK_SEARCH", raising=False)
    assert cfg._mock_search_flag(mock_llm=True) is True      # 跟随生效值
    assert cfg._mock_search_flag(mock_llm=False) is False
    monkeypatch.setenv("MOCK_SEARCH", "0")                   # 显式设置则优先
    assert cfg._mock_search_flag(mock_llm=True) is False


def test_no_api_key_implies_full_mock(monkeypatch):
    """无 API Key 且未显式设置开关：LLM 与搜索均进入 Mock（容器/演示场景）。"""
    monkeypatch.delenv("MOCK_LLM", raising=False)
    monkeypatch.delenv("MOCK_SEARCH", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    s = cfg_load_settings()
    assert s.mock_llm is True and s.mock_search is True
