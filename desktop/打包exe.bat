@echo off
chcp 65001 >nul
cd /d "%~dp0.."
echo [1/2] Generating icon ...
python -X utf8 desktop\make_icon.py
echo [2/2] Building GoldDesk.exe ...
python -m PyInstaller --noconfirm --clean desktop\GoldDesk.spec
if errorlevel 1 (
  echo BUILD FAILED. Make sure PyInstaller is installed: python -m pip install pyinstaller
  pause
  exit /b 1
)
echo.
echo DONE: dist\GoldDesk.exe
echo Data folder will be created next to the exe on first run: dist\data
pause
