@echo off
cd /d "%~dp0"
python -u monitor.py --port 8787
pause
