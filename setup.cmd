@echo off
cd /d "%~dp0"
python -m venv .venv
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto fail
echo Setup complete. Run start.cmd and open http://127.0.0.1:8765
pause
exit /b 0
:fail
echo Setup failed. Install Python 3.10-3.13 from python.org and retry.
pause
exit /b 1
