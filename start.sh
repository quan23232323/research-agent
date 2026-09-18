#!/usr/bin/env bash
# 智能研究助手 Agent 一键启动（Linux/macOS）
cd "$(dirname "$0")"
[ -f .env ] || cp .env.example .env
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
