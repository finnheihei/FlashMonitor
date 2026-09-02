@echo off
cd /d "%~dp0"
echo Installing PyInstaller if needed...
python -m pip install -q pyinstaller
if errorlevel 1 (
  echo pip / PyInstaller install failed.
  exit /b 1
)
echo Building FlashMonitor.exe ...
python -c "from webview_win import prepare_loader_for_build; prepare_loader_for_build()"
if errorlevel 1 (
  echo WebView2Loader.dll download failed.
  exit /b 1
)
echo Building FlashMonitor.exe ...
python -m PyInstaller --noconfirm --clean --onefile --windowed --name FlashMonitor --icon icon.ico --add-data "ui.html;." --add-data "icon.ico;." --add-binary "native/WebView2Loader.dll;." monitor.py
if errorlevel 1 (
  echo Build failed.
  exit /b 1
)
echo.
echo Done: dist\FlashMonitor.exe
echo Double-click it. Traffic logs stay in the same folder\data\
echo.
