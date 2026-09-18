@echo off
rem 智能研究助手 Agent 一键启动（Windows）
cd /d %~dp0
if not exist .env copy .env.example .env
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
