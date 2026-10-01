@echo off
setlocal EnableExtensions
REM ---------------------------------------------------------------------------
REM  Mailex - Windows build. Double-click this file.
REM  Installs Python if needed, installs every dependency, and produces
REM  dist\Mailex.exe as a single standalone file.
REM ---------------------------------------------------------------------------
cd /d "%~dp0"
set "LOG=%~dp0build-win-log.txt"
set "VENV=%~dp0.venv-build"
echo Mailex build started %DATE% %TIME% > "%LOG%"

echo.
echo ===============================================
echo   Building Mailex for Windows
echo   Full log: build-win-log.txt
echo ===============================================
echo.

echo [1/6] Finding or installing Python...
for /f "usebackq delims=" %%P in (`powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0ensure_python.ps1" 2^>^> "%LOG%"`) do set "PY=%%P"
if not defined PY goto :fail
if not exist "%PY%" goto :fail
echo       using %PY%
echo Using Python: %PY% >> "%LOG%"

echo [2/6] Creating a clean build environment...
if exist "%VENV%" rmdir /s /q "%VENV%"
"%PY%" -m venv "%VENV%" >> "%LOG%" 2>&1
if errorlevel 1 goto :fail
set "VPY=%VENV%\Scripts\python.exe"

echo [3/6] Installing dependencies (wheels only, no compiling)...
"%VPY%" -m pip install --upgrade pip wheel >> "%LOG%" 2>&1
"%VPY%" -m pip install --only-binary :all: -r "%~dp0requirements.txt" >> "%LOG%" 2>&1
if errorlevel 1 goto :fail
"%VPY%" -m pip install --only-binary :all: pyinstaller >> "%LOG%" 2>&1
if errorlevel 1 goto :fail
REM  Optional: PyMuPDF reads complex PDFs better for the content index. No wheel
REM  for this Python is not an error - the built-in PDF reader takes over.
set "PDF_EXTRA="
"%VPY%" -m pip install --only-binary :all: -r "%~dp0requirements-optional.txt" >> "%LOG%" 2>&1
"%VPY%" -c "import pymupdf" >> "%LOG%" 2>&1
if errorlevel 1 (
  echo       PyMuPDF has no wheel for this Python - PDFs will use the built-in reader.
) else (
  echo       PyMuPDF installed ^(better PDF text for the index^).
  set "PDF_EXTRA=--collect-all pymupdf --hidden-import fitz"
)

echo [4/6] Checking the code before packaging...
REM  The working directory is already this folder, so '.' is the source tree.
REM  %~dp0 must NOT go inside the Python string: it always ends in a backslash,
REM  which would escape the closing quote and make it an unterminated literal.
"%VPY%" -c "import sys; sys.path.insert(0,'.'); import mailexlib.ui, mailexlib.export, mailexlib.pdfout, mailexlib.indexer; print('imports ok')" >> "%LOG%" 2>&1
if errorlevel 1 goto :fail

echo [5/6] Packaging (this takes a minute or two)...
if exist "%~dp0build" rmdir /s /q "%~dp0build"
if exist "%~dp0dist" rmdir /s /q "%~dp0dist"
"%VPY%" -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name "Mailex" ^
  --collect-all reportlab ^
  --collect-all PIL ^
  --collect-all tkinterdnd2 ^
  --collect-submodules mailexlib ^
  --hidden-import tkinter ^
  --hidden-import tkinter.ttk ^
  --hidden-import tkinter.filedialog ^
  --hidden-import tkinter.messagebox ^
  --hidden-import sqlite3 ^
  --exclude-module pytest ^
  %PDF_EXTRA% ^
  "%~dp0mailex_app.py" >> "%LOG%" 2>&1
if errorlevel 1 goto :fail
if not exist "%~dp0dist\Mailex.exe" goto :fail

echo [6/6] Running the self-test on the built program...
echo       It builds three small PST files, reads them back and exports every
echo       format. About half a minute; the window stays hidden while it works.
if exist "%~dp0dist\mailex-selftest.txt" del /q "%~dp0dist\mailex-selftest.txt"
pushd "%~dp0dist"
REM  A windowed build has no console, so a plain invocation returns at once and
REM  the report would be read before it exists. start /wait blocks until the
REM  self-test has actually finished.
start "Mailex self-test" /wait "%~dp0dist\Mailex.exe" selftest
popd
if exist "%~dp0dist\mailex-selftest.txt" (
  type "%~dp0dist\mailex-selftest.txt"
  findstr /C:"PROBLEMS FOUND" "%~dp0dist\mailex-selftest.txt" >nul && goto :selftestfail
) else (
  echo       ^(no self-test report was written - check it by hand^)
)

echo.
echo ===============================================
echo   DONE.  dist\Mailex.exe is ready.
echo ===============================================
echo.
pause
exit /b 0

:selftestfail
echo.
echo ===============================================
echo   BUILT, BUT THE SELF-TEST REPORTED PROBLEMS.
echo   See dist\mailex-selftest.txt above.
echo ===============================================
echo.
pause
exit /b 2

:fail
echo.
echo ===============================================
echo   BUILD FAILED.  Last 40 lines of the log:
echo ===============================================
powershell -NoProfile -Command "Get-Content -Tail 40 '%LOG%'"
echo.
echo Full log: %LOG%
echo.
pause
exit /b 1
