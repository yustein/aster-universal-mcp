@echo off
setlocal
set "ROOT=%USERPROFILE%\AsterClaudeBeta"
set "DB=%ROOT%\state\windows-oauth.sqlite"
set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not exist "%DB%" (echo ERROR: OAuth DB missing & pause & exit /b 2)
if not exist "%PY%" (echo ERROR: Python missing & pause & exit /b 3)
if not exist "%ROOT%\source\pairing_admin.py" (echo ERROR: Source missing & pause & exit /b 4)
echo ASTER CLAUDE FREE - SINGLE USE OWNER PAIRING
"%PY%" "%ROOT%\source\pairing_admin.py" --db "%DB%" issue --ttl 900
if errorlevel 1 (echo PAIRING ISSUE FAILED & pause & exit /b 5)
echo Copy the short code into Claude's pairing page within 15 minutes.
pause
