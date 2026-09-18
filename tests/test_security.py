"""安全测试：令牌鉴权 / 按 IP 限流 / 配置校验。"""
import os

os.environ["MOCK_LLM"] = "1"
os.environ["MOCK_SEARCH"] = "1"
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="ra-test-"))

import pytest
from fastapi.testclient import TestClient

from backend.api import routes
from backend.config import load_settings, validate_settings
from backend.main import app

client = TestClient(app)


def test_config_validation_ok():
    assert validate_settings(load_settings()) == []


def test_config_validation_bad():
    s = load_settings()
    s.max_total_tokens = 100
    s.max_react_rounds = 99
    errors = validate_settings(s)
    assert any("max_total_tokens" in e for e in errors)
    assert any("max_react_rounds" in e for e in errors)


def test_auth_disabled_by_default():
    assert client.get("/api/meta").status_code == 200
    assert client.get("/api/health").status_code == 200


def test_auth_enforced(monkeypatch):
    monkeypatch.setattr(routes.settings, "auth_token", "secret-token")
    try:
        # 未带令牌 -> 401（health 豁免）
        assert client.get("/api/meta").status_code == 401
        assert client.get("/api/health").status_code == 200
        assert client.post("/api/plan", json={"topic": "测试"}).status_code == 401
        # 正确令牌 -> 200
        r = client.get("/api/meta", headers={"X-API-Key": "secret-token"})
        assert r.status_code == 200
        assert r.json()["auth_enabled"] is True
        # 错误令牌 -> 401
        assert client.get("/api/meta",
                          headers={"X-API-Key": "wrong"}).status_code == 401
    finally:
        monkeypatch.setattr(routes.settings, "auth_token", "")


def test_rate_limit(monkeypatch):
    monkeypatch.setattr(routes.settings, "rate_limit_per_min", 2)
    codes = [client.post("/api/plan", json={"topic": f"主题{i}"}).status_code
             for i in range(4)]
    assert codes[:2] == [200, 200]
    assert codes[2] == 429 and codes[3] == 429      # 第 3 次起限流


def test_rate_limit_not_bypassed_by_changing_path(monkeypatch):
    """v1.3.1：限流按客户端 IP 合计计数，换接口不应获得新配额。"""
    monkeypatch.setattr(routes.settings, "rate_limit_per_min", 1)
    assert client.post("/api/plan", json={"topic": "主题甲"}).status_code == 200
    # 换到另一条写接口：旧实现按 ip:path 分桶会放行，现在必须 429
    assert client.post("/api/research", json={
        "topic": "主题乙", "subquestions": ["子问题"]}).status_code == 429


def test_forwarded_for_ignored_without_trust_proxy(monkeypatch):
    """默认不信任 X-Forwarded-For：伪造该头不能绕过限流。"""
    monkeypatch.setattr(routes.settings, "rate_limit_per_min", 1)
    monkeypatch.setattr(routes.settings, "trust_proxy", False)
    assert client.post("/api/plan", json={"topic": "主题甲"},
                       headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 200
    assert client.post("/api/plan", json={"topic": "主题乙"},
                       headers={"X-Forwarded-For": "2.2.2.2"}).status_code == 429


def test_forwarded_for_used_when_trust_proxy(monkeypatch):
    """位于可信反代之后时，按 X-Forwarded-For 首跳识别客户端（各自独立配额）。"""
    monkeypatch.setattr(routes.settings, "rate_limit_per_min", 1)
    monkeypatch.setattr(routes.settings, "trust_proxy", True)
    assert client.post("/api/plan", json={"topic": "主题甲"},
                       headers={"X-Forwarded-For": "1.1.1.1, 10.0.0.1"}).status_code == 200
    assert client.post("/api/plan", json={"topic": "主题乙"},
                       headers={"X-Forwarded-For": "2.2.2.2, 10.0.0.1"}).status_code == 200


def test_doc_paths_require_auth():
    """v1.3.1：接口文档端点也纳入鉴权判定（不能只看 /api/ 前缀）。"""
    from backend.main import _needs_auth
    assert _needs_auth("/api/meta") is True
    assert _needs_auth("/api/health") is False
    assert _needs_auth("/openapi.json") is True
    assert _needs_auth("/docs") is True
    assert _needs_auth("/docs/oauth2-redirect") is True
    assert _needs_auth("/redoc") is True
    assert _needs_auth("/js/app.js") is False


def test_docs_endpoints_closed_by_default():
    """默认 expose_docs=false：/docs 与 /openapi.json 不存在，接口契约不外泄。"""
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/docs").status_code == 404


def test_config_validation_bound_widened():
    s = load_settings()
    s.reviewer_rewrite_limit = 99
    s.max_sources = 0
    s.rate_limit_per_min = -1
    errors = validate_settings(s)
    assert any("reviewer_rewrite_limit" in e for e in errors)
    assert any("max_sources" in e for e in errors)
    assert any("rate_limit_per_min" in e for e in errors)


def test_config_validation_weak_token():
    s = load_settings()
    s.auth_token = "123"
    assert any("AUTH_TOKEN" in e for e in validate_settings(s))


def test_config_missing_section_raises(tmp_path, monkeypatch):
    """配置文件结构不合法时给出可读错误，而不是裸 KeyError。"""
    from backend import config as cfg
    bad = tmp_path / "bad.yaml"
    bad.write_text("models: {}\n", encoding="utf-8")
    monkeypatch.setattr(cfg, "CONFIG_PATH", bad)
    with pytest.raises(cfg.ConfigError):
        cfg.load_settings()
