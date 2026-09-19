"""评估脚本的指标口径测试（离线，不调用模型）。

重点回归 v1.4.0 修掉的两个问题：
1. 要点解析只认 `-` / `*` 项目符号 —— 而真实模型更多输出有序列表 `1.`，
   曾导致 5 篇报告里 4 篇的支撑率静默变成 None（要点总数被少算 80%）；
2. 均值掩盖样本缺失 —— 仅 1 个样本可测时，汇总仍写成「平均要点支撑率 1.0」。
"""
import importlib.util
import os
import sys
from pathlib import Path

os.environ["MOCK_LLM"] = "1"
os.environ["MOCK_SEARCH"] = "1"

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _load_evaluate():
    """evaluate.py 在 scripts/ 下且带有模块级副作用，按文件路径加载。"""
    spec = importlib.util.spec_from_file_location("ra_evaluate", ROOT / "scripts" / "evaluate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ev = _load_evaluate()


def _report(points: list[str], n_sources: int = 3) -> str:
    body = "\n".join(points)
    sources = "\n".join(f"[{i}] 来源 {i}\n    https://example.com/{i}" for i in range(1, n_sources + 1))
    return (
        "## 摘要\n摘要正文 [1]。\n\n"
        f"## 核心要点\n{body}\n\n"
        "## 结论与展望\n结论 [1]。\n\n"
        f"## 参考来源\n\n{sources}\n"
    )


def test_ordered_list_is_counted():
    """回归：有序列表（真实模型的主要写法）必须被统计到。"""
    md = _report([
        "1. vLLM 提供 PagedAttention 与连续批处理 [1]。",
        "2. TensorRT-LLM 通过 Python API 优化推理 [2]。",
        "3. 两者定位不同，前者偏服务框架 [3]。",
    ])
    m = ev.citation_metrics(md, 3)
    assert m["points"] == 3
    assert m["point_support_rate"] == 1.0
    assert m["cite_valid_rate"] == 1.0


def test_bullet_list_is_counted():
    md = _report([
        "- MemGPT 提出 virtual context management [1]。",
        "- Letta 是 MemGPT 的工程化版本 [2]。",
    ])
    m = ev.citation_metrics(md, 3)
    assert m["points"] == 2
    assert m["point_support_rate"] == 1.0


def test_mixed_and_alternate_markers():
    """兼容 `*` `+` 以及 `1、` `1)` `1．` 等中文/变体分隔符。"""
    md = _report([
        "* 第一条要点内容较长因此应被识别 [1]。",
        "+ 第二条要点内容较长因此应被识别 [2]。",
        "3、第三条要点内容较长因此应被识别 [3]。",
        "4) 第四条要点内容较长因此应被识别 [1]。",
        "5．第五条要点内容较长因此应被识别 [2]。",
    ])
    assert ev.citation_metrics(md, 3)["points"] == 5


def test_plain_sentence_with_decimal_is_not_a_point():
    """`3.5 倍` 这类正文不应被误判为列表项。"""
    md = _report([
        "1. 第一条要点内容较长因此应被识别 [1]。",
        "3.5 倍",
        "2. 第二条要点内容较长因此应被识别 [2]。",
    ])
    assert ev.citation_metrics(md, 3)["points"] == 2


def test_unsupported_point_citation_lowers_support_rate():
    md = _report([
        "1. 这条有依据 [1]。",
        "2. 这条同样有依据 [2]。",
    ], n_sources=2)
    md = md.replace("这条同样有依据 [2]。", "这条同样有依据，但没有挂角标。")
    assert ev.citation_metrics(md, 2)["point_support_rate"] == 0.5


def test_out_of_range_citation_is_invalid():
    """角标有效率按整篇报告统计：越界角标计入分母但不计入有效。"""
    md = (
        "## 核心要点\n"
        "1. 角标越界的情形应被记为无效 [9]。\n"
        "2. 这条在来源范围内 [2]。\n\n"
        "## 参考来源\n\n[1] a\n[2] b\n[3] c\n"
    )
    m = ev.citation_metrics(md, 3)
    assert m["points"] == 2
    assert m["cite_valid_rate"] == 0.5   # [9] 越界无效，[2] 有效


def test_source_list_labels_are_not_counted_as_citations():
    """参考来源小节的 [n] 是列表标签，不应计入角标统计（否则指标系统性偏松）。"""
    md = (
        "## 核心要点\n"
        "1. 这条挂了一个越界角标 [9]，应拉低有效率。\n\n"
        "## 参考来源\n\n[1] a\n[2] b\n[3] c\n[4] d\n[5] e\n"
    )
    m = ev.citation_metrics(md, 5)
    assert m["points"] == 1
    assert m["cite_valid_rate"] == 0.0   # 唯一被统计的角标是 [9]，越界
    assert m["point_support_rate"] == 1.0


def test_missing_section_returns_none_without_crash():
    md = "## 摘要\n只有摘要，没有核心要点小节 [1]。\n"
    m = ev.citation_metrics(md, 3)
    assert m["points"] == 0
    assert m["point_support_rate"] is None
    assert m["cite_valid_rate"] is None


def test_avg_cov_flags_partial_coverage():
    """回归：可测样本不足时必须显式标注，不能让 1/5 的均值冒充全量指标。"""
    assert ev._avg_cov([1.0, 1.0, 1.0], 3) == "1.0"
    assert ev._avg_cov([1.0], 5) == "1.0（仅 1/5 个主题可测）"
    assert ev._avg_cov([], 5) == "—（0/5 个主题可测）"
    assert ev._avg_cov([0.5, 1.0], 2) == "0.75"


def test_render_report_marks_unmeasurable_rows():
    rows = [
        {"id": "a", "topic": "主题甲", "status": "done", "elapsed": 10.0, "tokens": 1000,
         "cost": 0.1, "sources": 3, "subq_failed": 0, "rewrites": 0,
         "points": 2, "point_support_rate": 1.0, "cite_valid_rate": 1.0},
        {"id": "b", "topic": "主题乙", "status": "done", "elapsed": 20.0, "tokens": 2000,
         "cost": 0.2, "sources": 4, "subq_failed": 0, "rewrites": 1,
         "points": 0, "point_support_rate": None, "cite_valid_rate": None},
    ]
    out = ev.render_report(rows, mock=False)
    assert "仅 1/2 个主题可测" in out      # 覆盖度必须写出来
    assert "| 核心要点总数 | 2 |" in out   # 分母可核对
    assert "| 主题乙 | done | 20.0 | 2000 | 4 | — | — |" in out
