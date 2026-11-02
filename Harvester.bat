@echo off
rem Double-click to open the VPin Frame Harvester app.
cd /d "%~dp0"
where pythonw >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw "%~dp0app.py"
) else (
  python "%~dp0app.py"
  if errorlevel 1 pause
)
