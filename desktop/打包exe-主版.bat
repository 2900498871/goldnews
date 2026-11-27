@echo off
chcp 65001 >nul
cd /d "%~dp0.."
REM 主版（Win10/11，pywebview + WebView2）打包脚本
REM 注意：Win7 版是 Python 3.8 构建，不含 pywebview；如果它被写进 dist\日常小助手.exe，
REM       双击后后台能起来但窗口会一闪就退（回退 Edge --app 又被单实例接管）。
REM       所以打包顺序：先跑「打包exe-Win7.bat」，最后再跑本脚本。
echo [1/2] Building dist\日常小助手.exe (Python 3.13 + pywebview) ...
python -X utf8 -m PyInstaller --noconfirm --clean desktop\GoldDesk.spec
if errorlevel 1 (
  echo [ERROR] 打包失败，请看上面的 PyInstaller 输出。
  pause
  exit /b 1
)
for %%F in (dist\日常小助手.exe) do set SZ=%%~zF
if %SZ% LSS 12000000 (
  echo [ERROR] dist\日常小助手.exe 只有 %SZ% 字节，说明这次的包没有打进 pywebview（多半是 Win7 版覆盖了主版）。
  echo         请确认用的是 Python 3.13 的 python.exe，并且已 pip install pywebview pythonnet。
  pause
  exit /b 1
)
echo [2/2] OK: dist\日常小助手.exe %SZ% bytes
echo 启动验证：双击 exe，窗口标题栏应出现「日常小助手」，日志里会有「已打开 WebView2 无边框窗口」。
pause
