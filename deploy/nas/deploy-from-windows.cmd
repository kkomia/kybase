@echo off
REM kybase frontend deploy to NAS -- one double-click.
REM Wrapper around deploy/nas/deploy-from-windows.sh: finds Git Bash, cd's to the repo root,
REM and keeps the window open so you can read the output.
REM
REM NOTE: keep this file ASCII-only. cmd.exe parses .cmd/.bat in the OEM codepage (GBK on this
REM machine), so UTF-8 Chinese here turns into garbage bytes and breaks the parser.
REM
REM Usage: deploy-from-windows.cmd [branch] [host] [ssh user]
REM   defaults: master / 192.168.31.18 / yumao   (yumao = the NAS login user,
REM   NOT this PC's Windows user -- ssh without a user would try that one and fail)
REM   deploy-from-windows.cmd --dry-run   : print what it would do, touch nothing
setlocal
REM UTF-8 console: the .sh prints Chinese, and a fresh cmd window defaults to GBK (936) --
REM without this the whole output is mojibake.
chcp 65001 >nul
cd /d "%~dp0..\.."

set "BASH=%ProgramFiles%\Git\bin\bash.exe"
if not exist "%BASH%" set "BASH=%ProgramFiles(x86)%\Git\bin\bash.exe"
if not exist "%BASH%" goto :nobash

REM CHERE_INVOKING: Git for Windows honors this to keep the login shell in the current
REM directory instead of cd-ing back to HOME -- otherwise the relative path below breaks.
set "CHERE_INVOKING=1"
"%BASH%" -lc "sh deploy/nas/deploy-from-windows.sh %*"
echo.
pause
exit /b 0

:nobash
echo Git for Windows not found: no bash.exe under Program Files\Git\bin.
echo Install Git for Windows, then double-click this file again.
echo.
pause
exit /b 2
