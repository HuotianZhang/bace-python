@echo off
REM The Keithley 2400's front panel, in a browser. Double-click, use the page,
REM press Ctrl-C in this window when you are done. The page opens by itself.
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
REM STOP IT WITH Ctrl-C, NOT THE CLOSE BUTTON
REM     Ctrl-C in this window switches the source off before exiting. So does
REM     Ctrl-Break, and so does `taskkill` without /F. It can take a moment: a
REM     reading at NPLC 10 with a 100-deep filter is 80 seconds of integration
REM     and the off waits behind it. Pressing Ctrl-C again will not hurry it
REM     and will not skip it.
REM
REM     THE WINDOW'S X BUTTON IS NOT ONE OF THOSE. Windows sends the console
REM     CTRL_CLOSE_EVENT, which Python does not deliver as a signal, so the
REM     process is killed without running its shutdown and the 2400 is left
REM     driving whatever it was driving. The same goes for End Task, a logoff,
REM     and a power cut.
REM
REM     If the source needs to be off and this window will not take a Ctrl-C,
REM     the 2400's own OUTPUT key is the answer and it always works.
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
