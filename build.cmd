@echo off
cd /d "%~dp0"
echo Installing PyInstaller if needed...
python -m pip install -q pyinstaller
if errorlevel 1 (
  echo pip / PyInstaller install failed.
  exit /b 1
)
echo Building FenGuang.exe ...
python -m PyInstaller --noconfirm --clean --onefile --windowed --name FenGuang --add-data "ui.html;." --hidden-import tkinter monitor.py
if errorlevel 1 (
  echo Build failed.
  exit /b 1
)
echo.
echo Done: dist\FenGuang.exe
echo Double-click it. Traffic logs stay in the same folder\data\
echo.
