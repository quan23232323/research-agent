"""工具层：web_search / read_page / write_report —— 对应《产品设计方案》第 4 节工具层与 7.1 L1 兜底。

L1 兜底链：重试 2 次 -> 切换搜索源（主源 -> tavily/bocha（有 Key 才启用）-> bing -> duckduckgo）
-> 抛 ToolError 由上层跳过并标记。
成本防线（第 6 节）：URL/查询缓存复用；read_page 截断到 page_max_chars。
"""
import asyncio
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import requests
from bs4 import BeautifulSoup

from backend.config import Settings
from backend.core.events import Task

if TYPE_CHECKING:
    from backend.agent.llm import LLMClient

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

REPORTS_DIR = Path(__file__).resolve().parents[2] / "reports"

_page_cache: dict[str, dict] = {}
_query_cache: dict[str, list] = {}
_CACHE_MAX = 200                # 缓存上限：长时运行防内存泄漏（v1.3）


def _cache_put(cache: dict, key, value) -> None:
    cache[key] = value
    while len(cache) > _CACHE_MAX:
        cache.pop(next(iter(cache)))


class ToolError(Exception):
    """工具失败（重试与换源均耗尽，L1）。"""


@dataclass
class Ctx:
    settings: Settings
    task: Task
    llm: "LLMClient"


# ---------------- 搜索（含 L1 兜底链） ----------------

def _ddgs_class():
    try:
        from ddgs import DDGS  # 新包名
        return DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS  # type: ignore[no-redef]  # 旧包名兼容
            return DDGS
        except ImportError:
            return None


def _search_provider(provider: str, query: str, s: Settings) -> list[dict]:
    """同步执行单源搜索，返回 [{title,url,snippet}]（由 asyncio.to_thread 调度）。"""
    if provider == "duckduckgo":
        DDGS = _ddgs_class()
        if DDGS is None:
            raise ToolError("未安装 ddgs 搜索依赖")
        # 只取同步的 text()：atext() 是协程，在线程里无法 await，
        # 旧写法 `getattr(DDGS(), "atext", None) or DDGS().text` 一旦命中就会拿到协程而崩。
        client = DDGS()
        fn = getattr(client, "text", None)
        if fn is None:
            raise ToolError("ddgs 版本不兼容：缺少同步 text() 方法")
        raw = fn(query, max_results=5)
        return [{"title": r.get("title", ""), "url": r.get("href", ""),
                 "snippet": r.get("body", "")} for r in raw]

    if provider == "tavily":
        key = os.getenv("TAVILY_API_KEY", "")
        r = requests.post(
            "https://api.tavily.com/search",
            json={"query": query, "max_results": 5},
            headers={"Authorization": f"Bearer {key}"},
            timeout=s.request_timeout,
        )
        r.raise_for_status()
        return [{"title": x.get("title", ""), "url": x.get("url", ""),
                 "snippet": (x.get("content") or "")[:300]} for x in r.json().get("results", [])]

    if provider == "bocha":
        key = os.getenv("BOCHA_API_KEY", "")
        r = requests.post(
            "https://api.bochaai.com/v1/web-search",
            json={"query": query, "summary": True, "count": 5},
            headers={"Authorization": f"Bearer {key}"},
            timeout=s.request_timeout,
        )
        r.raise_for_status()
        pages = r.json().get("data", {}).get("webPages", {}).get("value", [])
        return [{"title": x.get("name", ""), "url": x.get("url", ""),
                 "snippet": (x.get("summary") or "")[:300]} for x in pages]

    if provider == "bing":
        # 必应网页结果（无需 Key）。存在的意义：DuckDuckGo 在国内网络下经常整体不可达，
        # 只留 ddg 会导致整条兜底链直接失败。代价是它属于非官方抓取，
        # 必应改版时需同步调整下面的选择器。
        # 显式标注为 dict[str, str]：混合字面量会被 mypy 推成 dict[str, object]，
        # 与 requests 期望的查询参数类型不兼容
        params: dict[str, str] = {"q": query, "setlang": "zh-CN", "count": "10"}
        r = requests.get(
            "https://www.bing.com/search",
            params=params,
            headers={**UA, "Accept-Language": "zh-CN,zh;q=0.9"},
            timeout=s.request_timeout,
        )
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        out: list[dict] = []
        for li in soup.select("li.b_algo"):
            a = li.select_one("h2 a")
            if a is None or not a.get("href"):
                continue
            caption = li.select_one(".b_caption p") or li.select_one("p")
            out.append({
                "title": a.get_text(" ", strip=True),
                "url": a["href"],
                "snippet": (caption.get_text(" ", strip=True) if caption else "")[:300],
            })
        return out[:5]

    raise ToolError(f"未知搜索源：{provider}")


def _provider_chain(s: Settings) -> list[str]:
    """L1 第二层：搜索源切换顺序。

    bing 排在 duckduckgo 之前——它是国内网络下唯一无需 Key 又可达的网页源，
    放在链条前段可以让兜底真正生效，而不是等一个必然失败的源重试两轮。
    """
    chain = [s.search_primary]
    for p in ("tavily", "bocha", "bing", "duckduckgo"):
        if p not in chain:
            key_env = {"tavily": "TAVILY_API_KEY", "bocha": "BOCHA_API_KEY"}.get(p)
            if key_env is None or os.getenv(key_env):
                chain.append(p)
    return chain


def _mock_results(query: str) -> list[dict]:
    slug = re.sub(r"\W+", "-", query)[:20] or "topic"
    return [
        {"title": f"《{query}》2026 年度综述（Mock）", "url": f"https://mock.example.com/{slug}/review",
         "snippet": f"关于{query}的系统性综述，覆盖发展现状、技术路线与应用场景。"},
        {"title": f"{query}技术路线解析（Mock）", "url": f"https://mock.example.com/{slug}/tech",
         "snippet": f"梳理{query}的主流技术路线与代表性方案对比。"},
        {"title": f"{query}应用案例集（Mock）", "url": f"https://mock.example.com/{slug}/cases",
         "snippet": f"{query}在各行业的落地案例与效果数据。"},
    ]


async def web_search(query: str, ctx: Ctx) -> list[dict]:
    if query in _query_cache:                       # 成本防线：查询缓存
        ctx.task.emit({"type": "tool", "name": "web_search", "query": query,
                       "status": "ok", "detail": "命中缓存", "n": len(_query_cache[query])})
        return _query_cache[query]

    ctx.task.emit({"type": "tool", "name": "web_search", "query": query, "status": "running"})
    if ctx.settings.mock_search:
        await asyncio.sleep(0.2)
        results = _mock_results(query)
        _cache_put(_query_cache, query, results)
        ctx.task.emit({"type": "tool", "name": "web_search", "query": query,
                       "status": "ok", "detail": f"Mock 搜索返回 {len(results)} 条", "n": len(results)})
        return results

    errors: list[str] = []
    for provider in _provider_chain(ctx.settings):     # L1 第二层：切换搜索源
        for attempt in (1, 2):                          # L1 第一层：重试 2 次
            try:
                results = await asyncio.to_thread(_search_provider, provider, query, ctx.settings)
                if results:
                    _cache_put(_query_cache, query, results)
                    ctx.task.emit({"type": "tool", "name": "web_search", "query": query,
                                   "status": "ok", "detail": f"{provider} 返回 {len(results)} 条",
                                   "n": len(results)})
                    return results
                errors.append(f"{provider}: 空结果")
            except Exception as e:
                errors.append(f"{provider}: {e}")
            await asyncio.sleep(0.4 * attempt)
    raise ToolError(f"搜索失败（已重试并切换搜索源）：{'; '.join(errors[-3:])}")


# ---------------- 网页阅读 ----------------

def _fetch_page(url: str, s: Settings) -> dict:
    r = requests.get(url, headers=UA, timeout=s.request_timeout)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    text = re.sub(r"\n{2,}", "\n", soup.get_text(separator="\n").strip())
    return {"title": (soup.title.string.strip() if soup.title and soup.title.string else url),
            "content": text[: s.page_max_chars]}


async def read_page(url: str, ctx: Ctx) -> dict:
    if url in _page_cache:                          # 成本防线：URL 缓存
        ctx.task.emit({"type": "tool", "name": "read_page", "url": url,
                       "status": "ok", "detail": "命中缓存"})
        return _page_cache[url]

    ctx.task.emit({"type": "tool", "name": "read_page", "url": url, "status": "running"})
    if ctx.settings.mock_search:
        await asyncio.sleep(0.2)
        page = {"title": f"Mock 页面：{url}", "content":
                f"这是关于该主题的模拟网页正文（截断至 {ctx.settings.page_max_chars} 字符以控制 Token 预算）。"
                "内容覆盖背景、方法、数据与结论四部分，供研究流水线演示使用。" * 8}
        _cache_put(_page_cache, url, page)
        ctx.task.emit({"type": "tool", "name": "read_page", "url": url, "status": "ok",
                       "detail": f"Mock 正文 {len(page['content'])} 字"})
        return page

    last_err = None
    for attempt in (1, 2):
        try:
            page = await asyncio.to_thread(_fetch_page, url, ctx.settings)
            _cache_put(_page_cache, url, page)
            ctx.task.emit({"type": "tool", "name": "read_page", "url": url, "status": "ok",
                           "detail": f"正文 {len(page['content'])} 字"})
            return page
        except Exception as e:
            last_err = e
            await asyncio.sleep(0.4 * attempt)
    raise ToolError(f"网页读取失败：{url} ({last_err})")


# ---------------- 报告落盘 ----------------

def write_report(topic: str, content: str) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[\\/:*?\"<>|\s]+", "-", topic)[:24].strip("-") or "report"
    path = REPORTS_DIR / f"{safe}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
    path.write_text(content, encoding="utf-8")
    return path
