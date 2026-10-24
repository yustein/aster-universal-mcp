@echo off
setlocal
if not exist "%~dp0issue-pairing-windows.ps1" (
  echo ERROR: Keep this launcher in the repository scripts directory.
  exit /b 2
)
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0issue-pairing-windows.ps1"
set "ASTER_RESULT=%ERRORLEVEL%"
if not "%ASTER_RESULT%"=="0" echo PAIRING WAS NOT COMPLETED. Resolve the reported deployment problem before retrying.
pause
exit /b %ASTER_RESULT%
