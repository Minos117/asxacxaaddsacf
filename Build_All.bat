@echo off
cd /d "%~dp0"
set "PY="
for %%V in (3.12 3.13 3.11 3.10 3.14) do if not defined PY call :try py -%%V-64
if not defined PY call :try python
if not defined PY goto nopy
echo Build with: %PY%
%PY% build_all.py
pause
exit /b %errorlevel%

:try
%* -c "import sys,struct;sys.exit(0 if (3,10)<=sys.version_info[:2]<=(3,14) and struct.calcsize('P')==8 else 1)" >nul 2>&1
if not errorlevel 1 set "PY=%*"
exit /b 0

:nopy
echo Need 64-bit Python 3.10 - 3.14 on THIS build PC.
echo Install:  winget install Python.Python.3.12
pause
exit /b 1
