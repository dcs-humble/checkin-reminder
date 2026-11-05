@echo off
cd /d "%~dp0"
py -3.12 -m venv --copies runtime\python
if errorlevel 1 exit /b 1
runtime\python\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 exit /b 1
copy /y runtime\python\Scripts\python.exe runtime\python\python.exe >nul
copy /y runtime\python\Scripts\pythonw.exe runtime\python\pythonw.exe >nul
echo Setup complete.
pause
