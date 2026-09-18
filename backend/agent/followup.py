"""报告追问与局部重写 —— 对应《产品设计方案》3.2④ 的交付后交互动作。

- 追问：仅基于已收集素材回答，标注来源角标；素材不足时明确说明（幻觉兜底 7.2）
- 局部重写：按小节粒度重写，其余小节与参考来源保持不变；复用 Writer 档位模型
"""
import re

from backend.agent.llm import LLMClient
from backend.agent.writer import digest_materials
from backend.core.events import Task

FORBIDDEN_SECTIONS = {"参考来源"}

FOLLOWUP_SYS = (
    "你是研究助理。仅根据提供的编号素材回答用户问题：事实性陈述标注来源角标 [n]；"
    "素材不足以回答时明确说明「素材中未覆盖」，不得编造。回答控制在 300 字以内。"
)

REWRITE_SYS = (
    "你是研究报告撰写人。根据用户要求重写报告的指定小节：只输出该小节的 Markdown 内容"
    "（从 ## 标题开始，不要输出报告其他部分）；事实性陈述标注来源角标 [n]，不得编造来源。"
)


class SectionNotFound(Exception):
    """报告中不存在指定小节。"""


def list_sections(report_md: str) -> list[str]:
    return [ln[3:].strip() for ln in report_md.splitlines()
            if ln.startswith("## ") and ln[3:].strip()]


def _find_bounds(lines: list[str], title: str) -> tuple[int, int]:
    start = next((i for i, ln in enumerate(lines)
                  if ln.startswith("## ") and ln[3:].strip() == title), None)
    if start is None:
        raise SectionNotFound(title)
    end = next((j for j in range(start + 1, len(lines)) if lines[j].startswith("## ")),
               len(lines))
    return start, end


def _replace_section(report_md: str, title: str, new_md: str) -> str:
    lines = report_md.splitlines()
    start, end = _find_bounds(lines, title)
    merged = lines[:start] + new_md.strip().splitlines() + [""] + lines[end:]
    return "\n".join(merged).rstrip() + "\n"


class FollowupService:
    def __init__(self, task: Task, settings) -> None:
        self.task = task
        self.settings = settings
        self.llm = LLMClient(settings, task)

    async def answer(self, question: str) -> dict:
        materials = self.task.materials
        user = (f"用户问题：{question}\n\n编号素材（来自报告研究过程）：\n"
                f"{digest_materials(materials, per_source=600)}")
        resp = await self.llm.chat("followup", [
            {"role": "system", "content": FOLLOWUP_SYS},
            {"role": "user", "content": user},
        ])
        answer = resp["content"].strip()
        cited = sorted({int(x) for x in re.findall(r"\[(\d+)\]", answer)
                        if 1 <= int(x) <= len(materials)})
        return {"answer": answer,
                "sources": [{"index": i, "title": materials[i - 1]["title"],
                             "url": materials[i - 1]["url"]} for i in cited]}

    async def rewrite(self, section: str, feedback: str) -> str:
        if section in FORBIDDEN_SECTIONS:
            raise ValueError("参考来源小节不支持重写")
        report_md = self.task.result["report_md"]
        if section not in list_sections(report_md):
            raise SectionNotFound(section)
        new_md = await self._rewrite_section(report_md, section, feedback)
        updated = _replace_section(report_md, section, new_md)
        self.task.result["report_md"] = updated
        return updated

    async def _rewrite_section(self, report_md: str, section: str, feedback: str) -> str:
        lines = report_md.splitlines()
        start, end = _find_bounds(lines, section)
        old_section = "\n".join(lines[start:end]).strip()
        user = (f"【重写小节】{section}\n"
                f"重写要求：{feedback or '使内容更具体、更有依据'}\n"
                f"原小节内容：\n{old_section}\n\n"
                f"编号素材：\n{digest_materials(self.task.materials, per_source=600)}")
        resp = await self.llm.chat("writer", [
            {"role": "system", "content": REWRITE_SYS},
            {"role": "user", "content": user},
        ])
        return resp["content"].strip()
