@echo off
setlocal
cd /d "%~dp0"

REM Build wrapper for PC Agent desktop EXE.
REM Avoids PyInstaller WinError 5 when dist\JarvisPCAgent.exe is still running/locked.

set "TARGET=dist\JarvisPCAgent.exe"

echo [1/3] Stopping running JarvisPCAgent.exe (if any)...
taskkill /F /IM "JarvisPCAgent.exe" >nul 2>&1

echo [2/3] Releasing previous output file...
if exist "%TARGET%" (
  for /l %%I in (1,1,8) do (
    del /F /Q "%TARGET%" >nul 2>&1
    if not exist "%TARGET%" goto file_released
    timeout /t 1 /nobreak >nul
  )
)

:file_released
if exist "%TARGET%" (
  echo ERROR: %TARGET% is still locked.
  echo Close any running JarvisPCAgent windows/processes and retry.
  exit /b 1
)

if exist ".venv\Scripts\python.exe" (
  set "PY=.venv\Scripts\python.exe"
) else if exist "venv\Scripts\python.exe" (
  set "PY=venv\Scripts\python.exe"
) else (
  set "PY=python"
)

echo [3/3] Running PyInstaller...
"%PY%" -m PyInstaller --noconfirm --clean JarvisPCAgent.spec
exit /b %ERRORLEVEL%
