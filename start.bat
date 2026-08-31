@echo off
REM Double-click this to run the spelling trainer in a proper console window.
REM It picks Python 3 explicitly and keeps the window open if anything fails.

cd /d "%~dp0"
title IELTS Spelling Trainer

where py >nul 2>nul
if %errorlevel%==0 (
    py -3 "spell trainer.py" %*
) else (
    python "spell trainer.py" %*
)

echo.
echo ------------------------------------------------------------
echo Session ended. Close this window, or run start.bat again.
echo If you saw an error above, that text is what to report.
echo ------------------------------------------------------------
pause
