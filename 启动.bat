@echo off
setlocal

set "PY=%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe"
if exist "%PY%" goto run

for /f "delims=" %%i in ('where pythonw.exe 2^>nul') do (
  set "PY=%%i"
  goto run
)

for /f "delims=" %%i in ('where pyw.exe 2^>nul') do (
  start "" "%%i" -3 "%~dp0extkeeper.pyw" %*
  exit /b 0
)

echo pythonw.exe / pyw.exe not found. Please install Python 3 (with tkinter).
pause
exit /b 1

:run
start "" "%PY%" "%~dp0extkeeper.pyw" %*
exit /b 0
