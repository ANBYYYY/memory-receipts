@echo off
REM ====================================================================
REM  START-HERE.cmd -- the fastest path in, for Windows.
REM
REM  A reader who only has the git URL has no idea what to do first.
REM  This file finds a REAL Python, then hands over to tools\start_here.py,
REM  which runs the four README steps in a scratch directory OUTSIDE this
REM  package and proves the package itself was not modified.
REM
REM  !! THIS FILE IS ASCII-ONLY ON PURPOSE !!
REM  cmd.exe parses .cmd in the OEM codepage; non-ASCII bytes can break the
REM  quoting and parsing of LATER lines. All Chinese text is printed either
REM  by tools\start_here.py or, best effort, by tools\no_python.ps1.
REM
REM  Optional override -- affects the SEARCH ORDER only, never a judge:
REM      set MEMORY_PACK_PYTHON=C:\full\path\to\python.exe
REM ====================================================================
setlocal EnableExtensions
set "PKG=%~dp0"
if "%PKG:~-1%"=="\" set "PKG=%PKG:~0,-1%"
set "PYC="
set "PYA="

if defined MEMORY_PACK_PYTHON call :probe_path "%MEMORY_PACK_PYTHON%"
if not defined PYC call :probe_name py -3
if not defined PYC call :probe_name python
if not defined PYC call :probe_name python3
if defined PYC goto :have

echo.
echo ======================================================================
echo   Python was not found on this computer.
echo.
echo   This is NOT a broken environment and it is not your mistake.
echo   The demo simply needs a Python to run with.
echo.
echo   Option 1 - install it. One line, copy and paste:
echo.
echo       winget install -e --id Python.Python.3.12
echo.
echo   Option 2 - you already have a Python somewhere. Point this file at it:
echo.
echo       set MEMORY_PACK_PYTHON=C:\path\to\python.exe
echo       START-HERE.cmd
echo.
echo   AFTER INSTALLING: close this window, open a NEW one, then run
echo   START-HERE.cmd again. A new window matters: the PATH of this window
echo   will not change.
echo.
echo   Chinese explanation: see the section at the top of README.md in this
echo   folder.
echo ----------------------------------------------------------------------
echo.

REM Best effort only. If PowerShell is missing or blocked, the ASCII block
REM above is already self-sufficient -- this must never become "printed
REM nothing at all".
where powershell >nul 2>nul
if not errorlevel 1 if exist "%~dp0tools\no_python.ps1" powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\no_python.ps1"

endlocal
exit /b 2

:have
"%PYC%" %PYA% -B -X utf8 "%PKG%\tools\start_here.py" %*
set "RC=%ERRORLEVEL%"
endlocal & exit /b %RC%

REM --------------------------------------------------------------------
REM  A candidate counts ONLY if it prints "Python <digit>" on -V.
REM
REM  We read that output IN MEMORY, with for /f, and never write a file.
REM  A redirect into %TEMP% adds a failure surface that has nothing to do
REM  with Python. Reported from a machine where %TEMP% is not writable:
REM  py answered rc=0 and printed "Python 3.10.9", the REDIRECT failed, no
REM  file appeared, findstr could not open it -- and this script told the
REM  user "Python was not found". That translated "cannot write a temp
REM  file" into "you have not installed Python", and the wrong translation
REM  sends people off to install something they already have.
REM
REM  Why not "the command exists": the Microsoft Store ships a python.exe
REM  placeholder that answers -V with EMPTY output. Why not "rc=0" alone:
REM  it cannot tell "printed a version" from "printed nothing". Why not
REM  "the path has no \WindowsApps\": winget itself lives there.
REM --------------------------------------------------------------------
:probe_path
set "PYV="
for /f "delims=" %%v in ('"%~1" -V 2^>nul') do set "PYV=%%v"
echo %PYV% | findstr /R "^Python [0-9]" >nul || exit /b
set "PYC=%~1"
exit /b

:probe_name
set "PYV="
for /f "delims=" %%v in ('"%~1" %~2 -V 2^>nul') do set "PYV=%%v"
echo %PYV% | findstr /R "^Python [0-9]" >nul || exit /b
set "PYC=%~1"
set "PYA=%~2"
exit /b
