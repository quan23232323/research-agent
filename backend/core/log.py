"""统一日志：任务生命周期 / 兜底触发 / 工具失败均有迹可查（设计方案第 8 节可观测）。"""
import logging


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"research-agent.{name}")


def setup_logging(level: str = "INFO") -> None:
    root = logging.getLogger("research-agent")
    if root.handlers:                       # 避免重复初始化
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    root.addHandler(handler)
    root.setLevel(level.upper())
