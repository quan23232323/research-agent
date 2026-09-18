"""进程内滑动窗口限流：按客户端 IP 限制写入类接口频率（0 = 关闭）。

调用方传入的 key 应只包含客户端标识（IP），不要把请求路径拼进来 ——
否则调用方换个 URL 就能拿到一份全新配额，限流形同虚设。
"""
import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = __import__("threading").Lock()

    def allow(self, key: str, limit_per_min: int) -> bool:
        if limit_per_min <= 0:
            return True
        now = time.time()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= limit_per_min:
                return False
            q.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


rate_limiter = RateLimiter()
