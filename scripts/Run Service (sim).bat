@echo off
REM The BACE service on the simulated rig, every settle a no-op: for developing
REM the console away from the bench. Touches no instrument; pyvisa is never
REM imported, so it runs on a machine with no VISA backend at all.
REM
REM Same routes, same journal, same folders as the real thing, all under
REM runs\ -- but a "cryostat" that settles in the time a click takes and
REM shots that take a millisecond. The history queries know the difference:
REM a sim session's settle and shot times never feed the lab PC's estimates,
REM and the parameters you type here are still what the real bench sees as
REM last-used, on purpose.
REM
REM --fast is refused without --sim. On the real rig it would measure before
REM the device has settled, with no symptom in the data.
REM
REM Ctrl-C to stop. http://127.0.0.1:8900/ lists the routes.

setlocal
cd /d "%~dp0.."
py -3 -m bace.service --sim --fast
echo.
pause
