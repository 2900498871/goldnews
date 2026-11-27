@echo off
chcp 65001 >nul
cd /d "%~dp0.."
REM Win7 兼容版：必须用 Python 3.8（最后一个支持 Win7 的版本）+ PyInstaller 5.13.1 的专用虚拟环境
set PY38=C:\SoftWare\Python38\venv-golddesk\Scripts\python.exe
if not exist "%PY38%" (
  echo [ERROR] Python 3.8 build environment not found: %PY38%
  echo         See README "Win7 兼容版" for how to set it up.
  pause
  exit /b 1
)
echo [1/2] Generating icon ...
"%PY38%" -X utf8 desktop\make_icon.py
echo [2/2] Building dist\win7\日常小助手.exe (Python 3.8 runtime) ...
"%PY38%" -X utf8 -m PyInstaller --noconfirm --clean --distpath dist\win7 --workpath build\win7 desktop\GoldDesk-Win7.spec
if errorlevel 1 (
  echo BUILD FAILED. Check that the venv has: pip install pywebview pyinstaller==5.13.1
  pause
  exit /b 1
)
echo.
echo DONE: dist\win7\日常小助手.exe
echo Copy the whole "win7" folder to the Windows 7 PC. Data folder is created next to the exe on first run.
pause
