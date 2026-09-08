@echo off
REM The shutter as one switch, in a browser. Double-click, use the page, close
REM the window when you are done. The page opens by itself.
REM
REM     http://127.0.0.1:8910/
REM
REM This folder is standalone: it imports nothing from bace\, needs nothing
REM installed, and the BACE service knows nothing about it. To see the page
REM with no hardware at all, add --sim to the line at the bottom.
REM
REM ONE PROCESS OWNS THE MODULE
REM     While this console runs, the BACE service, tools\shutter.py and the
REM     LabVIEW VI cannot open the Deditec module - and while any of them holds
REM     it, this cannot: DapiOpenModule answers 0, which reads like missing
REM     hardware. Stop one before starting the other.
REM
REM THE LINE IS LEFT WHERE YOU PUT IT
REM     Closing this window does not move the shutter. Shut it on the page
REM     first if that is what you want.
REM
REM BITNESS
REM     ctypes loads only the DELIB build matching the interpreter. If this
REM     machine has just the 32-bit DELIB (delib.dll in SysWOW64, or the copy
REM     under D:\BACE\shutter\builds\data\), change the line below to
REM
REM         set PY=py -3.11-32
REM
REM     A 32-bit Python with nothing installed in it is enough.

setlocal
cd /d "%~dp0"
set PY=py -3

%PY% shutter_console.py --browser
echo.
pause
