@echo off
chcp 65001 >nul
cd /d %~dp0
title GameAgent 一键启动
echo ==============================================
echo  GameAgent 一键启动（全链路 + 前端）
echo  前端: http://localhost:8501
echo  部署: http://localhost:8080
echo ==============================================
.venv\Scripts\python.exe -X utf8 run_demo.py %*
pause
