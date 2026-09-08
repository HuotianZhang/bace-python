@echo off
REM Install this checkout on a Windows machine with no instruments -- a laptop,
REM a desk PC, anywhere the console is being worked on rather than the bench.
REM One command, once.
REM
REM WHAT IT INSTALLS
REM     pip install -e .[service,dev] -- the measurement core numpy, scipy and
REM     h5py; the service the console talks to, fastapi and uvicorn and
REM     websockets; and the test suite, pytest and httpx2.
REM
REM     Deliberately NOT the rig extra. --sim builds the rig from
REM     bace.drivers.simulated and never imports pyvisa, so a machine with no
REM     VISA backend needs none: you get the whole API, the whole event stream
REM     and the whole journal, with a cryostat that settles in the time a click
REM     takes. "Setup.bat" is the one for the lab PC, and adds VISA.
REM
REM     Editable, the -e, so this folder stays the code that runs: edit a file
REM     and the next run has it, with no reinstall.
REM
REM IT INSTALLS INTO py -3
REM     the same interpreter every other .bat in this folder calls. No
REM     virtualenv, and that is deliberate: a venv none of them look in would
REM     install perfectly and leave every button in this folder still broken.
REM
REM AFTER THIS
REM     "Run Service (sim).bat" starts the service on the simulated rig and
REM     touches nothing. The console is at http://127.0.0.1:8900/ once it is
REM     up, and bace\service\README.md walks the operator flow by hand.
REM
REM     The console's own test suite is Node's, not Python's, and Node is not
REM     installed by any of this. Without it those suites skip rather than
REM     fail, which is the right answer on the lab PC and a hole on a desk
REM     machine -- install Node 22 or newer if you are working on ui\.

setlocal
cd /d "%~dp0.."

where py >nul 2>&1
if errorlevel 1 goto :nopy

py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 goto :oldpy

echo == installing into py -3, no VISA -- the simulated rig needs none
py -3 -m pip install -e ".[service,dev]"
if errorlevel 1 goto :pipfailed

echo.
echo == what came in
py -3 -c "import bace.service.app, uvicorn, importlib.metadata as md; print('   bace ' + md.version('bace') + '; app and uvicorn import, which is what Run Service needs')"
if errorlevel 1 goto :notook

echo.
echo == ready
echo.
echo    "Run Service (sim).bat"  the console on a simulated rig, no hardware
echo.
echo    Or run the suite, which is all simulator and recorded transcripts:
echo        py -3 -m pytest -q
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

:stop
echo.
pause
exit /b 1
