@echo off
REM Just the acquisition. Talks only to the oscilloscope and enables no output.
REM Needs the 81150A sync arriving on CHAN3, so leave the generators running.

setlocal
cd /d "%~dp0.."
py -3 -m bace.bench --read --configure --acquire --averages 16
echo.
pause
