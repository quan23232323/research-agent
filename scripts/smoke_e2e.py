"""端到端冒烟测试：真起 uvicorn，验证鉴权边界 + 鉴权下的 SSE 实时流。

与 pytest 的区别：走真实 HTTP 连接，能暴露中间件注册顺序、静态托管、
启动期配置校验等 TestClient 可能掩盖的问题。

用法：
    python scripts/smoke_e2e.py [--port 8123]

退出码 0 = 全部通过，非 0 = 存在失败项（可直接用于 CI）。
需先安装 requirements.txt（含 requests）。
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "test-token-123"          # 长度需 ≥8，否则触发启动期配置校验告警

fails: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  -> {detail}" if detail else ""))
    if not ok:
        fails.append(label)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8123)
    args = ap.parse_args()
    base = f"http://127.0.0.1:{args.port}"

    env = {
        **os.environ,
        "AUTH_TOKEN": TOKEN,
        "MOCK_LLM": "1",
        "MOCK_SEARCH": "1",
        "DATA_DIR": tempfile.mkdtemp(prefix="ra-smoke-"),
        "PYTHONUNBUFFERED": "1",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app", "--port", str(args.port)],
        cwd=str(ROOT), env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True,
    )
    try:
        deadline = time.time() + 40
        up = False
        while time.time() < deadline:
            try:
                if requests.get(f"{base}/api/health", timeout=2).status_code == 200:
                    up = True
                    break
            except Exception:
                time.sleep(0.4)
        check("服务启动 + /api/health 可达", up)
        if not up:
            return 1

        h = {"X-API-Key": TOKEN}

        # 鉴权中间件先于路由执行：无令牌访问文档端点应被拦下（不泄露端点是否存在）
        check("/docs 无令牌被拦下", requests.get(f"{base}/docs").status_code in (401, 404))
        check("/openapi.json 无令牌被拦下",
              requests.get(f"{base}/openapi.json").status_code in (401, 404))
        # 带合法令牌仍不可访问 -> 证明默认确实关闭（而非仅靠鉴权遮住）
        check("/docs 默认关闭（带令牌仍 404）",
              requests.get(f"{base}/docs", headers=h).status_code == 404)
        check("/openapi.json 默认关闭（带令牌仍 404）",
              requests.get(f"{base}/openapi.json", headers=h).status_code == 404)
        check("/api/meta 无令牌 401", requests.get(f"{base}/api/meta").status_code == 401)
        check("/api/meta 带令牌 200",
              requests.get(f"{base}/api/meta", headers=h).status_code == 200)
        check("前端静态资源免鉴权", requests.get(f"{base}/").status_code == 200)
        check("health 豁免鉴权", requests.get(f"{base}/api/health").status_code == 200)

        r = requests.post(f"{base}/api/plan", headers=h,
                          json={"topic": "大模型在电力行业的应用", "depth": "quick"})
        check("鉴权下 /api/plan 可用", r.status_code == 200, str(r.status_code))
        subqs = r.json()["subquestions"]

        tid = requests.post(f"{base}/api/research", headers=h, json={
            "topic": "大模型在电力行业的应用", "depth": "quick",
            "length": "medium", "subquestions": subqs}).json()["task_id"]

        seen, done, chunk_chars = set(), None, 0
        with requests.get(f"{base}/api/tasks/{tid}/events", headers=h,
                          stream=True, timeout=60) as resp:
            for line in resp.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data: "):
                    continue
                ev = json.loads(line[6:])
                seen.add(ev["type"])
                if ev["type"] == "chunk":
                    chunk_chars += len(ev.get("text", ""))
                if ev["type"] in ("done", "error"):
                    done = ev
                    break

        check("鉴权下 SSE 建立并跑完", done is not None and done["type"] == "done",
              str(done)[:80] if done else "无终态事件")
        check("SSE 收到流式 chunk 事件（实时预览）", chunk_chars > 0, f"{chunk_chars} 字符")
        check("SSE 事件类型齐全", {"stage", "tool", "llm", "chunk"} <= seen,
              str(sorted(seen)))

        res = requests.get(f"{base}/api/tasks/{tid}", headers=h).json()
        check("任务完成且报告含来源角标",
              res["status"] == "done" and "## 参考来源" in res["result"]["report_md"])

        # 无令牌访问 SSE 必须 401（前端据此提示填令牌，而非静默失败）
        check("SSE 无令牌 401",
              requests.get(f"{base}/api/tasks/{tid}/events", stream=True).status_code == 401)
    finally:
        proc.terminate()
        try:
            out, _ = proc.communicate(timeout=10)
        except Exception:
            proc.kill()
            out = ""
        if out and fails:
            print("\n--- 服务端日志（仅失败时输出） ---")
            print(out[:2000])

    print(f"\n结果：{'全部通过' if not fails else '失败项 ' + str(fails)}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
