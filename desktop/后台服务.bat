@echo off
chcp 65001 >nul
title 日常小助手 - 桌面后台服务（不打开窗口）
cd /d "%~dp0.."
echo 仅启动后台服务：定时采集、研判与发信照常运行。
echo 可在浏览器打开 http://127.0.0.1:8765/ 查看界面。
python -X utf8 desktop\run.py --no-window %*
pause
