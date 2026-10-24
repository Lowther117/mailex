@echo off
setlocal EnableExtensions
REM Run Mailex from source on Windows, setting up a virtual environment
REM the first time. Use build-exe.bat if you want a standalone .exe instead.
cd /d "%~dp0"
set "VENV=%~dp0.venv"
if exist "%VENV%\Scripts\python.exe" goto :run

echo First run - setting Python up. This happens once.
REM  %PY% is set and used in separate statements on purpose: inside one
REM  parenthesised block cmd expands variables before the block runs.
for /f "usebackq delims=" %%P in (`powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0ensure_python.ps1"`) do set "PY=%%P"
if not defined PY goto :nopython
if not exist "%PY%" goto :nopython
"%PY%" -m venv "%VENV%"
if errorlevel 1 ( echo Could not create the environment. & goto :setupfail )
"%VENV%\Scripts\python.exe" -m pip install --upgrade pip >nul
"%VENV%\Scripts\python.exe" -m pip install --only-binary :all: -r "%~dp0requirements.txt"
if errorlevel 1 goto :setupfail
REM  optional, better PDF text for the index; fine if there is no wheel
"%VENV%\Scripts\python.exe" -m pip install --only-binary :all: -r "%~dp0requirements-optional.txt" >nul 2>&1

:run
start "" "%VENV%\Scripts\pythonw.exe" "%~dp0mailex.py" %*
exit /b 0

:setupfail
REM  a half-built .venv would be trusted by the next run; remove it so that
REM  run sets Python up again from the start
if exist "%VENV%" rmdir /s /q "%VENV%"
pause
exit /b 1

:nopython
echo Could not find or install Python.
pause
exit /b 1
