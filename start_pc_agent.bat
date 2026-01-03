@echo off
setlocal

REM One-click PC Agent start.
REM This wrapper keeps a simple filename for users.

set "REPO=%~dp0"
cd /d "%REPO%" || exit /b 1

call "%REPO%run_pc_agent.bat"
