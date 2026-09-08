@echo off
REM The Keithley 2400's front panel, in a browser. Double-click, use the page,
REM close the window when you are done. The page opens by itself.
REM
REM     http://127.0.0.1:8924/
REM
REM This folder is standalone: it imports nothing from bace\, and the BACE
REM service knows nothing about it. To see the page with no instrument at all,
REM add --sim to the line at the bottom - that needs no VISA either.
REM
REM ONE PROCESS OWNS THE INSTRUMENT
REM     While this console runs, the BACE service cannot open GPIB0::24, and
REM     while the service holds it, this cannot: the second one gets a VISA
REM     error that reads like a cable fault. Stop one before starting the
REM     other. The port says which instrument answers here - 8924 for GPIB 24,
REM     the way the 1918-C console is on 8918 and the 331 on 8331.
REM
REM THE OUTPUT GOES OFF ON THE WAY OUT
REM     Closing this window, Ctrl-C, or the machine sending this process a
REM     stop signal all switch the source off first. It can take a moment: a
REM     reading at NPLC 10 with a 100-deep filter is 80 seconds of integration
REM     and the off waits behind it. Pressing Ctrl-C again will not hurry it
REM     and will not skip it.
REM
REM     What cannot be caught is End Task's hard kill and a power cut. If you
REM     need the source off and this window is not responding, the 2400's own
REM     OUTPUT key is the answer.
REM
REM CONFIGURATION
REM     rig.toml gives the GPIB address and the two bench ceilings; run.toml
REM     gives the compliance, integration time and terminals the panel opens
REM     on. This file cd's to its own folder, so the search goes: here, then
REM     every folder above - which finds a checkout's rig.toml two levels up,
REM     and finds a copy sitting beside this file on a bench PC. The console
REM     prints which files it used; if it says "not found", it is running on
REM     built-in ceilings and you should say so out loud before sourcing
REM     anything.
REM
REM NEEDS PYVISA
REM     For the real instrument only. pip install pyvisa, plus a VISA runtime
REM     (NI-VISA or Keysight). --sim needs neither.

setlocal
cd /d "%~dp0"
set PY=py -3

%PY% keithley_console.py --browser
echo.
pause
