@echo off
setlocal
cd /d "%~dp0"
set "VENV=%~dp0.venv"
if exist "%~dp0runtime\pythonw.exe" goto runtime
if exist "%VENV%\ok" goto run

rem ---- pick a 64-bit Python 3.10-3.14 (PySide6 6.12 / PyMuPDF 1.26 wheels need it)
set "PY="
for %%V in (3.12 3.13 3.11 3.10 3.14) do if not defined PY call :try py -%%V-64
if not defined PY call :try python
if not defined PY goto nopy

echo Using: %PY%
%PY% -c "import sys,struct;print(sys.version);print(struct.calcsize('P')*8,'bit')"
if exist "%VENV%" rmdir /s /q "%VENV%"
%PY% -m venv "%VENV%"
if errorlevel 1 goto failvenv

if exist "%~dp0wheels\" goto offline
echo Installing (internet needed)...
"%VENV%\Scripts\python.exe" -m pip install PySide6-Essentials==6.12.0 PyMuPDF==1.26.7
goto chk
:offline
echo Installing from bundled wheels (offline)...
"%VENV%\Scripts\python.exe" -m pip install --no-index --find-links "%~dp0wheels" PySide6-Essentials==6.12.0 PyMuPDF==1.26.7
:chk
if errorlevel 1 goto failpip
echo ok>"%VENV%\ok"

:run
start "" "%VENV%\Scripts\pythonw.exe" "%~dp0zaia_pdf.py" %*
exit /b 0

:runtime
start "" "%~dp0runtime\pythonw.exe" "%~dp0zaia_pdf.py" %*
exit /b 0

:try
%* -c "import sys,struct;sys.exit(0 if (3,10)<=sys.version_info[:2]<=(3,14) and struct.calcsize('P')==8 else 1)" >nul 2>&1
if not errorlevel 1 set "PY=%*"
exit /b 0

:nopy
echo No 64-bit Python 3.10 - 3.14 found.
echo Install Python 3.12 64-bit:  winget install Python.Python.3.12
echo Then run ZAIA PDF again.
pause
exit /b 1

:failvenv
echo Could not create .venv
pause
exit /b 1

:failpip
echo Install failed (see error above). Removing .venv so next run retries.
rmdir /s /q "%VENV%"
pause
exit /b 1
