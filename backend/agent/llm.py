"""分层模型调用层 —— 对应《产品设计方案》5.1 / 5.2 / 第 6 节。

职责：
- 按节点（planner/researcher/writer/reviewer）使用对应档位模型
- L2 兜底：指数退避重试 -> 降级到 fallback_models（设计方案 7.1）
- Token 预算硬顶：超限抛 BudgetExceeded，由上层走 L3 部分交付
- Mock 模式：无 API Key 时使用确定性模拟，保证离线可跑通全流程
"""
import asyncio
import json
import random
import re
from collections import defaultdict

from openai import AsyncOpenAI

from backend.config import Settings
from backend.core.events import Task
from backend.core.log import get_logger

LOG = get_logger("llm")


class LLMError(Exception):
    """模型调用失败（重试与降级均耗尽，L2）。"""


class BudgetExceeded(Exception):
    """Token 预算触顶，上层应走 L3 部分交付。"""


class Budget:
    def __init__(self, cap: int) -> None:
        self.cap = cap
        self.used = 0
        self.calls = 0

    def check(self) -> None:
        if self.used >= self.cap:
            raise BudgetExceeded(f"Token 预算触顶（{self.cap}），中止后续调用")

    def add(self, tin: int, tout: int) -> None:
        self.used += tin + tout
        self.calls += 1


def _zh_tokens(text: str) -> int:
    """中文粗略估 Token：约 2 字符/Token（仅 Mock/流式无 usage 时使用）。"""
    return max(1, len(text) // 2)


def _backoff_sleep(attempt: int) -> float:
    """指数退避 + 随机抖动（jitter）：0.5s / 1s 基础上叠加 0~0.3s 随机量。

    抖动的作用：多个任务同时失败时，若 Everyone 都按固定节奏重试，会形成
    周期性的重试脉冲（thundering herd），反而更容易再次触发限流。随机化
    重试时刻可把脉冲摊平。
    """
    return 0.5 * (2 ** attempt) + random.uniform(0, 0.3)


class LLMClient:
    def __init__(self, settings: Settings, task: Task) -> None:
        self.s = settings
        self.task = task
        self.mock = settings.mock_llm
        self.budget = Budget(settings.max_total_tokens)
        self.stats: dict[str, dict] = defaultdict(lambda: {"calls": 0, "in": 0, "out": 0})
        self._client = None if self.mock else AsyncOpenAI(
            base_url=settings.base_url, api_key=settings.api_key, timeout=120
        )

    async def chat(self, node: str, messages: list[dict], tools: list | None = None) -> dict:
        """返回统一结构：{"content": str, "tool_calls": [{"id","name","args"}], "usage": {"in","out"}}"""
        cfg = self.s.nodes[node]
        candidates = [cfg.model] + [m for m in cfg.fallback_models if m != cfg.model]
        last_err: Exception | None = None

        for model in candidates:                      # L2 第二层：同档位模型互换
            for attempt in range(3):                  # L2 第一层：指数退避重试
                try:
                    self.budget.check()
                    if self.mock:
                        content, tool_calls = _mock_chat(node, messages)
                        usage = {"in": _zh_tokens(str(messages)), "out": _zh_tokens(content or "")}
                    else:
                        content, tool_calls, usage = await self._real(model, messages, tools)
                    self.budget.add(usage["in"], usage["out"])
                    st = self.stats[node]
                    st["calls"] += 1
                    st["in"] += usage["in"]
                    st["out"] += usage["out"]
                    self.task.emit({
                        "type": "llm", "node": node, "model": model,
                        "tokens_in": usage["in"], "tokens_out": usage["out"],
                    })
                    return {"content": content, "tool_calls": tool_calls, "usage": usage}
                except BudgetExceeded:
                    raise
                except Exception as e:                # 限流/超时/网络等
                    last_err = e
                    LOG.warning("[%s] %s 第 %d 次调用失败：%s", node, model, attempt + 1, e)
                    if attempt < 2:                   # 最后一轮不再空等，直接进入下一候选模型
                        await asyncio.sleep(0.5 * (2 ** attempt))
        raise LLMError(f"[{node}] 模型调用失败（已重试并尝试降级模型）：{last_err}")

    async def stream_chat(self, node: str, messages: list[dict], on_chunk=None) -> str:
        """流式调用：逐块回调 on_chunk(text)，返回完整文本。

        复用与 chat() 完全一致的 L2 重试/降级与 Token 预算记账 —— 流式路径不能成为
        预算硬顶和模型降级的缺口。全部候选耗尽时抛 LLMError（由上层走 L3 降级）。
        """
        cfg = self.s.nodes[node]
        candidates = [cfg.model] + [m for m in cfg.fallback_models if m != cfg.model]
        last_err: Exception | None = None

        for model in candidates:
            for attempt in range(3):
                try:
                    self.budget.check()
                    if self.mock:
                        content, _ = _mock_chat(node, messages)
                        for i in range(0, len(content), 80):   # 模拟流式节奏
                            if on_chunk:
                                on_chunk(content[i:i + 80])
                            await asyncio.sleep(0.02)
                    else:
                        content = await self._stream(model, messages, on_chunk)
                    usage = {"in": _zh_tokens(str(messages)), "out": _zh_tokens(content)}
                    self.budget.add(usage["in"], usage["out"])
                    st = self.stats[node]
                    st["calls"] += 1
                    st["in"] += usage["in"]
                    st["out"] += usage["out"]
                    self.task.emit({
                        "type": "llm", "node": node, "model": model,
                        "tokens_in": usage["in"], "tokens_out": usage["out"],
                    })
                    return content
                except BudgetExceeded:
                    raise
                except Exception as e:
                    last_err = e
                    LOG.warning("[%s] %s 流式第 %d 次失败：%s", node, model, attempt + 1, e)
                    if attempt < 2:
                        await asyncio.sleep(_backoff_sleep(attempt))
        raise LLMError(f"[{node}] 流式输出失败（已重试并尝试降级模型）：{last_err}")

    async def _stream(self, model: str, messages: list[dict], on_chunk=None) -> str:
        assert self._client is not None, "流式调用仅在真实模式下可用"
        stream = await self._client.chat.completions.create(
            model=model, messages=messages,  # type: ignore[arg-type]
            stream=True, temperature=0.4,
        )
        parts: list[str] = []
        async for chunk in stream:  # type: ignore[union-attr]
            delta = chunk.choices[0].delta.content if chunk.choices else None
            if delta:
                parts.append(delta)
                if on_chunk:
                    on_chunk(delta)
        return "".join(parts)

    async def _real(self, model: str, messages: list[dict], tools: list | None):
        assert self._client is not None, "普通调用仅在真实模式下可用"
        resp = await self._client.chat.completions.create(
            model=model,
            messages=messages,  # type: ignore[arg-type]
            tools=tools or None,  # type: ignore[arg-type]
            temperature=0.3,
        )
        msg = resp.choices[0].message
        content = msg.content or ""
        tool_calls = [
            {"id": tc.id, "name": tc.function.name,  # type: ignore[union-attr]
             "args": json.loads(tc.function.arguments or "{}")}  # type: ignore[union-attr]
            for tc in (msg.tool_calls or [])
        ]
        usage = {
            "in": getattr(resp.usage, "prompt_tokens", 0) or 0,
            "out": getattr(resp.usage, "completion_tokens", 0) or 0,
        }
        return content, tool_calls, usage


# ======================= Mock 模式（离线演示/测试） =======================

def _last_user(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m["role"] == "user":
            return m.get("content", "")
    return ""


def _mock_chat(node: str, messages: list[dict]):
    if node == "planner":
        topic = _last_user(messages).split("主题：")[-1].split("\n")[0].strip()
        hit = re.search(r"拆解为 (\d) 个", _last_user(messages))
        n = int(hit.group(1)) if hit else 4
        pool = [
            f"{topic}的整体发展现状与最新进展",
            f"{topic}的核心技术路线与代表方案",
            f"{topic}的典型应用场景与落地案例",
            f"{topic}当前面临的主要挑战与争议",
            f"{topic}的未来发展趋势与机会",
        ]
        return json.dumps(pool[:n], ensure_ascii=False), []

    if node == "researcher":
        tool_msgs = [m for m in messages if m["role"] == "tool"]
        if not tool_msgs:
            subq = _last_user(messages).replace("研究子问题：", "").split("\n")[0].strip()
            return "", [{"id": "mock_call_1", "name": "web_search", "args": {"query": subq}}]
        if len(tool_msgs) == 1:
            try:
                first = json.loads(tool_msgs[0]["content"])[0]
                return "", [{"id": "mock_call_2", "name": "read_page", "args": {"url": first["url"]}}]
            except Exception:
                pass
        return f"已围绕该子问题完成资料收集，共执行 {len(tool_msgs)} 次工具调用。", []

    if node == "writer":
        joined = " ".join(str(m.get("content", "")) for m in messages)
        if "【重写小节】" in joined:                    # 局部重写：仅返回该小节
            hit = re.search(r"【重写小节】(.+)", joined)
            title = hit.group(1).split("\n")[0].strip() if hit else "该小节"
            return (f"## {title}\n（已按反馈重写 · Mock）本节根据用户要求补充了更具体的说明与数据支撑[1]，"
                    "并修正了原表述中不够严谨的部分。"), []

        topic = ""
        for m in messages:
            if m["role"] == "user" and "主题：" in m.get("content", ""):
                topic = m["content"].split("主题：")[-1].split("\n")[0].strip()
        n = 2
        hit = re.search(r"可用来源共 (\d+) 条",
                        messages[0].get("content", "") if messages else "")
        if hit:
            n = max(1, min(int(hit.group(1)), 3))
        cites = "".join(f"[{i}]" for i in range(1, n + 1))
        report = (
            f"# {topic} 研究报告（Mock 演示版）\n\n"
            f"## 摘要\n本报告由 Mock 模式生成，用于在无 API Key 环境下演示完整研究流水线："
            f"围绕主题完成了检索、阅读、撰写与自检四个阶段{cites}。\n\n"
            f"## 核心要点\n- 要点一：{topic}整体处于快速发展期，公开资料较为丰富[1]。\n"
            f"- 要点二：主要技术路线与应用场景在检索结果中均有覆盖[2]。\n\n"
            f"## 分析（以下为分析推论）\n基于以上资料可推断该主题具备持续跟踪价值。\n\n"
            f"## 结论\nMock 流水线验证通过：规划-检索-撰写-自检链路完整，报告附来源角标。"
        )
        return report, []

    if node == "reviewer":
        return '{"verdict":"pass","reasons":["引用角标均在来源范围内","要点均有来源支撑"]}', []

    if node == "followup":
        return ("根据现有素材（Mock 演示）：该问题可以从来源[1]与[2]中得到支撑——主题整体处于快速发展期，"
                "主要技术路线与应用场景在素材中均有覆盖；更细粒度的数据需要进一步联网检索确认。"), []

    return "", []
