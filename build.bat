@echo off
REM ============================================================
REM  GadgetOfTheFuture - launcher for build.ps1 (v1.5.0-beta, Lightweight)
REM
REM  This file is intentionally ASCII-only: cmd.exe parses .bat files
REM  using the current console code page, and a "chcp" switch inside a
REM  batch file makes cmd re-read it with a wrong offset, breaking
REM  multi-line commands on large files. All build logic (with Russian
REM  messages) lives in build.ps1, which PowerShell reads as UTF-8.
REM ============================================================
setlocal
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build.ps1"
set "BUILD_RC=%ERRORLEVEL%"

endlocal & exit /b %BUILD_RC%
