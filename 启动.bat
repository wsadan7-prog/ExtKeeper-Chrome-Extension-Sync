@echo off
setlocal
chcp 65001 >nul 2>&1

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

echo 未找到 pythonw.exe 或 pyw.exe，请先安装带 tkinter 的 Python 3。
pause
exit /b 1

:run
start "" "%PY%" "%~dp0extkeeper.pyw" %*
