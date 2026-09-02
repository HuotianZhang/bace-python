@echo off
REM BACE bench check. Passes 1-3: nothing here enables an instrument output.
REM Just double-click. The numerical regression runs against the copy of the
REM 2026-08-07 run in bench-archive\, so it re-proves this machine's copy of
REM the code, not only the author's. To use a different run instead, set
REM ARCHIVE below and add --archive "%ARCHIVE%" to the lines that need it.

setlocal
cd /d "%~dp0.."
set ARCHIVE=

echo.
echo  Pass 1  package, simulated runs, archive regression, instrument identity
echo  ----------------------------------------------------------------------
py -3 -m bace.bench

echo.
echo  Pass 2  read instrument state, send configuration (outputs stay OFF)
echo  ----------------------------------------------------------------------
echo  If a generator's output is already ON its configure is SKIPPED, on
echo  purpose. Turn it off, or re-run with --force-configure if nothing is
echo  connected to the device.
echo.
py -3 -m bace.bench --read --configure

echo.
echo  Pass 3  one acquisition. Talks only to the scope; enables no output.
echo  ----------------------------------------------------------------------
echo  Needs the 81150A sync on CHAN3, so leave the generators running.
echo.
py -3 -m bace.bench --read --configure --acquire --averages 16

echo.
echo  Reports are in bench-reports\ -- send the newest .json back.
echo.
pause
