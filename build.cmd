@echo off
setlocal
cd /d "%~dp0"

echo [1/4] Running tests...
python -m unittest discover -s tests
if errorlevel 1 (
  echo Tests failed. Nothing was built.
  exit /b 1
)

echo [2/4] Generating versioninfo.txt from flashmonitor\__init__.py ...
python tools\gen_versioninfo.py
if errorlevel 1 exit /b 1

echo [3/4] Preparing PyInstaller and WebView2Loader.dll ...
python -m pip install -q pyinstaller==6.20.0
if errorlevel 1 (
  echo pip / PyInstaller install failed.
  exit /b 1
)
python -c "from webview_win import prepare_loader_for_build; prepare_loader_for_build()"
if errorlevel 1 (
  echo WebView2Loader.dll download failed.
  exit /b 1
)

echo [4/4] Building dist\FlashMonitor.exe ...
python -m PyInstaller --noconfirm --clean --onefile --windowed --name FlashMonitor --icon icon.ico --version-file versioninfo.txt --add-data "ui.html;." --add-data "icon.ico;." --add-binary "native/WebView2Loader.dll;." monitor.py
if errorlevel 1 (
  echo Build failed.
  exit /b 1
)
echo.
echo Done: dist\FlashMonitor.exe
echo Records go to %%LOCALAPPDATA%%\FlashMonitor\data
echo.
