@echo off
rem Double-click once: puts a "VPin Frame Harvester" icon on your desktop.
cd /d "%~dp0"
set "HV_DIR=%~dp0"
set "HV_TARGET=%~dp0Harvester.bat"
set "HV_ARGS="
for /f "delims=" %%p in ('where pythonw 2^>nul') do if not defined HV_PYW set "HV_PYW=%%p"
if defined HV_PYW set "HV_TARGET=%HV_PYW%"
if defined HV_PYW set "HV_ARGS=%~dp0app.py"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop') + '\VPin Frame Harvester.lnk'); $s.TargetPath=$env:HV_TARGET; if ($env:HV_ARGS) { $s.Arguments='\"' + $env:HV_ARGS + '\"' }; $s.WorkingDirectory=$env:HV_DIR; $s.IconLocation=$env:HV_DIR + 'assets\icon.ico'; $s.Save()"
if errorlevel 1 (echo Could not make the shortcut. & pause & exit /b)
echo Done - look for "VPin Frame Harvester" on your desktop.
pause
