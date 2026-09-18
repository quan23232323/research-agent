"""FastAPI 入口：安全中间件（鉴权 + 限流）+ API 路由 + 前端静态托管。

启动：python -m uvicorn backend.main:app --port 8000
访问：http://127.0.0.1:8000
"""
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.api.routes import APP_VERSION, router, settings
from backend.config import validate_settings
from backend.core.log import get_logger, setup_logging
from backend.core.ratelimit import rate_limiter

LOG = get_logger("main")

# 接口文档端点：默认关闭（security.expose_docs=false）。
# 开启时同样纳入鉴权范围 —— 否则 /openapi.json 会把完整接口契约对外暴露。
_DOC_PATHS = ("/docs", "/redoc", "/openapi.json")


def _needs_auth(path: str) -> bool:
    """哪些路径需要令牌：/api/*（health 豁免）+ 接口文档端点。"""
    if path == "/api/health":
        return False
    if path.startswith("/api"):
        return True
    return any(path.startswith(p) for p in _DOC_PATHS)


def _client_ip(request: Request) -> str:
    """限流用客户端标识。

    仅当 server.trust_proxy=true 时才信任 X-Forwarded-For —— 直连部署下该头可被伪造，
    盲信等于把限流开关交给调用方。
    """
    if settings.trust_proxy:
        fwd = request.headers.get("X-Forwarded-For", "")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(os.getenv("LOG_LEVEL", "INFO"))
    errors = validate_settings(settings)
    if errors:
        raise SystemExit("配置校验失败：\n- " + "\n- ".join(errors))
    mode = "MOCK（离线演示）" if (settings.mock_llm or settings.mock_search) else "真实调用"
    auth = "已启用" if settings.auth_token else "未启用"
    docs = "开放" if settings.expose_docs else "关闭"
    print(f"[research-agent v{APP_VERSION}] 就绪 | LLM: {settings.nodes['planner'].model} "
          f"| 模式: {mode} | 鉴权: {auth} | 接口文档: {docs} | 前端: http://127.0.0.1:8000")
    if settings.auth_token and _origins == ["*"]:
        LOG.warning("已启用鉴权但 ALLOW_ORIGINS 仍为通配符 *，生产环境请显式指定允许来源")
    if settings.trust_proxy:
        LOG.warning("TRUST_PROXY 已开启：请确认服务确实位于可信反向代理之后")
    yield


app = FastAPI(
    title="智能研究助手 Agent",
    version=APP_VERSION,
    lifespan=lifespan,
    docs_url="/docs" if settings.expose_docs else None,
    redoc_url="/redoc" if settings.expose_docs else None,
    openapi_url="/openapi.json" if settings.expose_docs else None,
)

_origins = [o.strip() for o in os.getenv("ALLOW_ORIGINS", "*").split(",") if o.strip()]


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    """可选令牌鉴权（/api/health 豁免）+ 写入类接口按 IP 限流。"""
    path = request.url.path
    if _needs_auth(path):
        if settings.auth_token and not secrets.compare_digest(
                request.headers.get("X-API-Key", ""), settings.auth_token):
            return JSONResponse({"detail": "未授权：请在页面设置中配置访问令牌"},
                                status_code=401)
        rate_limit = (request.method == "POST" and settings.rate_limit_per_min > 0
                      and not rate_limiter.allow(_client_ip(request),
                                                 settings.rate_limit_per_min))
        if rate_limit:
            # 限流键只取客户端 IP：带上 path 会让调用方靠换 URL 绕过配额
            return JSONResponse({"detail": "请求过于频繁，请稍后再试"}, status_code=429)
    return await call_next(request)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


app.include_router(router)

# CORS 最后注册 = 位于中间件栈最外层，保证 401/429 这类提前返回的响应也带上 CORS 头，
# 否则跨域前端只会看到不透明的网络错误、读不到真实状态码。
app.add_middleware(CORSMiddleware, allow_origins=_origins, allow_methods=["*"],
                   allow_headers=["*"])

_frontend = Path(__file__).resolve().parents[1] / "frontend"


class NoCacheStaticFiles(StaticFiles):
    """静态资源协商缓存：文件变更立即生效，未变更返回 304（v1.3 修复更新不生效问题）。"""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


app.mount("/", NoCacheStaticFiles(directory=_frontend, html=True), name="frontend")
