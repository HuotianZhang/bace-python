@echo off
REM Install this checkout on the lab PC -- the Windows machine that has the
REM instruments. One command, once, and every other .bat in this folder works.
REM
REM WHAT IT INSTALLS
REM     pip install -e .[lab,dev] -- the measurement core numpy, scipy and
REM     h5py; the service the console talks to, fastapi and uvicorn and
REM     websockets; the VISA layer for the real rig, pyvisa and pyvisa-py;
REM     and the test suite, pytest and httpx2.
REM
REM     Editable, the -e, so this folder stays the code that runs: edit a
REM     file and the next run has it, with no reinstall. That matters here
REM     because there is more than one copy of this tree on the bench.
REM
REM     pyvisa uses the NI-VISA already installed for LabVIEW, so GPIB works.
REM     Nothing here touches an instrument.
REM
REM WHICH OF THE TWO DO I WANT
REM     This one on the lab PC. On a laptop or a desk machine with no
REM     instruments, "Setup (sim).bat" installs the same thing without the
REM     VISA layer, which is all the console work needs.
REM
REM IT INSTALLS INTO py -3
REM     the same interpreter every other .bat in this folder calls. No
REM     virtualenv, and that is deliberate: a venv none of them look in would
REM     install perfectly and leave every button in this folder still broken.
REM
REM     scripts\setup.sh is the Linux equivalent and does build a venv there,
REM     because nothing on that side is pinned to a system interpreter.
REM
REM AFTER THIS
REM     "Run Service.bat" is the real bench. "Run Service (sim).bat" needs no
REM     hardware and is the safe thing to click first. "Run Bench Check.bat"
REM     exercises the port against the rig and writes a report.

setlocal
cd /d "%~dp0.."

where py >nul 2>&1
if errorlevel 1 goto :nopy

py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 goto :oldpy

echo == installing into py -3, with the VISA layer for the real rig
py -3 -m pip install -e ".[lab,dev]"
if errorlevel 1 goto :pipfailed

echo.
echo == what came in
py -3 -c "import bace.service.app, uvicorn, importlib.metadata as md; print('   bace ' + md.version('bace') + '; app and uvicorn import, which is what Run Service needs')"
if errorlevel 1 goto :notook
py -3 -c "import pyvisa, sys; lib = str(pyvisa.ResourceManager().visalib.library_path); print('   pyvisa ' + pyvisa.__version__ + ', VISA backend ' + lib); sys.exit(2 if lib == 'py' else 0)"
if errorlevel 2 goto :nobackend
if errorlevel 1 goto :novisa

echo.
echo == ready
echo.
echo    "Run Service (sim).bat"  the console on a simulated rig, no hardware
echo    "Run Service.bat"        the real bench
echo    "Run Bench Check.bat"    exercise the port against the rig, write a report
echo.
pause
exit /b 0

:nopy
echo.
echo   No "py" launcher on PATH.
echo   Install Python 3.11 or newer from python.org, with the py launcher
echo   ticked, or run pip yourself with whichever python you have.
goto :stop

:oldpy
echo.
echo   bace needs Python 3.11 or newer, because config loading uses tomllib.
py -3 -c "import platform; print('   py -3 here is ' + platform.python_version())"
goto :stop

:pipfailed
echo.
echo   The install failed. The lines above say why. The usual causes are
echo   no network, or a proxy pip has not been told about.
goto :stop

:notook
echo.
echo   The install reported success but bace.service will not import, so
echo   something did not take. The traceback above names it.
goto :stop

:nobackend
echo.
echo   Installed, but this machine has no vendor VISA. pyvisa fell back to
echo   pyvisa-py, which cannot talk to GPIB, so the instruments are not
echo   reachable from here even though every package is in place.
echo.
echo   Install NI-VISA -- the same one LabVIEW uses -- and run this again.
echo   "Run Bench Check.bat" is what reports the backend properly once it is.
goto :stop

:novisa
echo.
echo   Everything but the VISA layer is in: bace.service imports, so the
echo   simulated rig and the console will work. pyvisa will not import,
echo   and the traceback above says why, so this machine cannot reach the
echo   real instruments yet.
echo.
echo   Nothing else can tell you this. bace.service imports its VISA
echo   drivers only when a real rig is built, so it comes up clean on a
echo   machine with no pyvisa at all -- which is the whole point of --sim,
echo   and the reason this line is checked separately.
goto :stop

:stop
echo.
pause
exit /b 1
