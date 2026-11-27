@echo off
chcp 65001 >nul
title 日常小助手 - Windows 桌面程序（独立实例）
cd /d "%~dp0.."
where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 未找到 python，请安装 Python 3.10+ 并加入 PATH。
  pause
  exit /b 1
)
echo 正在启动桌面程序独立实例（配置/数据库/密钥仅在本端）...
python -X utf8 desktop\run.py %*
echo.
echo 服务已退出。按任意键关闭窗口。
pause >nul
