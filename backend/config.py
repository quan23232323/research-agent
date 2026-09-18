"""配置加载：config.yaml（分层模型/预算/搜索链）+ .env（密钥与演示开关）。"""
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.yaml"
load_dotenv(ROOT / ".env")


class ConfigError(Exception):
    """配置文件缺失或结构不合法（启动期 fail fast）。"""


@dataclass
class NodeModel:
    model: str
    fallback_models: list = field(default_factory=list)


@dataclass
class Settings:
    base_url: str
    api_key: str
    mock_llm: bool
    mock_search: bool
    nodes: dict                 # 节点名 -> NodeModel
    max_total_tokens: int
    max_react_rounds: int
    reviewer_rewrite_limit: int
    search_concurrency: int
    max_sources: int
    max_concurrent_tasks: int
    rate_limit_per_min: int
    trust_proxy: bool
    auth_token: str
    expose_docs: bool
    export_html: bool
    search_primary: str
    request_timeout: int
    page_max_chars: int
    topic_max_chars: int
    question_max_chars: int
    sensitive_words: list
    price_input: float | None
    price_output: float | None


def _env_flag(name: str) -> bool:
    return os.getenv(name, "0").strip() in ("1", "true", "True")


def _env_bool(name: str, default: bool) -> bool:
    """环境变量优先于 config.yaml；未设置时回落到配置文件取值。"""
    return _env_flag(name) if name in os.environ else default


def _mock_search_flag(mock_llm: bool) -> bool:
    """MOCK_SEARCH 未显式设置时跟随最终生效的 mock_llm（含"无 API Key 自动 Mock"路径），
    保证无 Key 演示环境全离线一致；显式设置 MOCK_SEARCH 则优先。"""
    if "MOCK_SEARCH" in os.environ:
        return _env_flag("MOCK_SEARCH")
    return mock_llm


def load_settings() -> Settings:
    if not CONFIG_PATH.is_file():
        raise ConfigError(f"配置文件不存在：{CONFIG_PATH}")
    try:
        raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(f"config.yaml 解析失败：{e}") from e
    if not isinstance(raw, dict):
        raise ConfigError("config.yaml 顶层结构必须是键值映射")

    for key in ("models", "budget", "search"):
        if key not in raw:
            raise ConfigError(f"config.yaml 缺少必需配置段：{key}")
    if not isinstance(raw["models"], dict) or not raw["models"]:
        raise ConfigError("config.yaml 的 models 段不能为空")

    nodes = {}
    for name, cfg in raw["models"].items():
        if not isinstance(cfg, dict) or not cfg.get("model"):
            raise ConfigError(f"models.{name} 缺少 model 字段")
        nodes[name] = NodeModel(cfg["model"], list(cfg.get("fallback_models", [])))

    budget = raw["budget"]
    search = raw["search"]
    inp = raw.get("input", {})
    server = raw.get("server", {})
    security = raw.get("security", {})
    export = raw.get("export", {})
    prices = raw.get("prices") or {}

    mock_llm = _env_flag("MOCK_LLM") or not os.getenv("LLM_API_KEY")
    mock_search = _mock_search_flag(mock_llm)

    def _num(section: dict, key: str, default: int) -> int:
        try:
            return int(section.get(key, default))
        except (TypeError, ValueError) as e:
            raise ConfigError(f"配置项 {key} 必须是整数，当前值：{section.get(key)!r}") from e

    return Settings(
        base_url=os.getenv("LLM_BASE_URL", "https://api.deepseek.com"),
        api_key=os.getenv("LLM_API_KEY", ""),
        mock_llm=mock_llm,
        mock_search=mock_search,
        nodes=nodes,
        max_total_tokens=_num(budget, "max_total_tokens", 150000),
        max_react_rounds=_num(budget, "max_react_rounds", 6),
        reviewer_rewrite_limit=_num(budget, "reviewer_rewrite_limit", 2),
        search_concurrency=_num(budget, "search_concurrency", 3),
        max_sources=_num(budget, "max_sources", 12),
        max_concurrent_tasks=_num(server, "max_concurrent_tasks", 3),
        rate_limit_per_min=_num(server, "rate_limit_per_min", 10),
        trust_proxy=_env_bool("TRUST_PROXY", bool(server.get("trust_proxy", False))),
        auth_token=os.getenv("AUTH_TOKEN", "") or str(security.get("auth_token", "") or ""),
        expose_docs=_env_bool("EXPOSE_DOCS", bool(security.get("expose_docs", False))),
        export_html=bool(export.get("html", True)),
        search_primary=search.get("primary", "duckduckgo"),
        request_timeout=_num(search, "request_timeout", 15),
        page_max_chars=_num(search, "page_max_chars", 4000),
        topic_max_chars=_num(inp, "topic_max_chars", 200),
        question_max_chars=_num(inp, "question_max_chars", 500),
        sensitive_words=list(inp.get("sensitive_words", [])),
        price_input=prices.get("input"),
        price_output=prices.get("output"),
    )


def validate_settings(s: Settings) -> list[str]:
    """启动前配置校验（fail fast）：返回错误列表，空列表表示通过。"""
    errors = []
    required_nodes = {"planner", "researcher", "writer", "reviewer", "followup"}
    missing = required_nodes - set(s.nodes)
    if missing:
        errors.append(f"缺少模型档位配置：{sorted(missing)}")
    if s.max_total_tokens < 10000:
        errors.append(f"max_total_tokens 过小（{s.max_total_tokens}），至少 10000")
    if not 1 <= s.max_react_rounds <= 20:
        errors.append(f"max_react_rounds 超出范围（{s.max_react_rounds}），应为 1~20")
    if not 1 <= s.search_concurrency <= 10:
        errors.append(f"search_concurrency 超出范围（{s.search_concurrency}），应为 1~10")
    if not 1 <= s.max_concurrent_tasks <= 50:
        errors.append(f"max_concurrent_tasks 超出范围（{s.max_concurrent_tasks}），应为 1~50")
    if not 0 <= s.reviewer_rewrite_limit <= 5:
        errors.append(f"reviewer_rewrite_limit 超出范围（{s.reviewer_rewrite_limit}），应为 0~5")
    if not 1 <= s.max_sources <= 50:
        errors.append(f"max_sources 超出范围（{s.max_sources}），应为 1~50")
    if s.rate_limit_per_min < 0:
        errors.append(f"rate_limit_per_min 不能为负（{s.rate_limit_per_min}），0 表示关闭限流")
    if s.page_max_chars < 500:
        errors.append(f"page_max_chars 过小（{s.page_max_chars}），至少 500")
    if not s.search_primary:
        errors.append("search_primary 不能为空")
    if s.auth_token and len(s.auth_token) < 8:
        errors.append(f"AUTH_TOKEN 过短（{len(s.auth_token)} 字符），建议至少 8 位以免被暴力猜测")
    return errors
