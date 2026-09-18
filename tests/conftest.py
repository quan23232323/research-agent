"""pytest 全局夹具：隔离进程内全局状态，避免用例之间互相污染。

限流计数器按「客户端 IP」累计写入次数（v1.3.1 起不再按路径分桶），
而所有用例共享同一个测试来源 IP —— 不逐条清零就会互相耗尽配额，
表现为后续用例收到莫名其妙的 429。
"""
import pytest

from backend.core.ratelimit import rate_limiter


@pytest.fixture(autouse=True)
def _isolate_rate_limiter():
    rate_limiter.reset()
    yield
    rate_limiter.reset()
