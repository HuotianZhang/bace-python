@echo off
REM The shutter as one switch, in a browser. Double-click, use the page, close
REM the window when you are done.
REM
REM     http://127.0.0.1:8910/     opened for you once the server is up
REM
REM Nothing else on the bench is opened: no VISA, no scope, no generators, no
REM service - this program owns the Deditec DIO module and serves one page.
REM
REM ONE PROCESS OWNS THE MODULE
REM     While this console runs, the BACE service and tools\shutter.py cannot
REM     open the shutter line, and vice versa. Stop the service (and close the
REM     LabVIEW VI) before starting this. The service does not yet know how to
REM     ask this console the way it asks the 1918-C and the 331 consoles;
REM     until it does, run one or the other.
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
REM     A 32-bit Python with nothing installed in it is enough: the console is
REM     standard library only.
REM
REM WITHOUT A BROWSER
REM     tools\shutter.py does the same three things from the command line
REM     (read, --open, --shut). Run Shutter.bat is its double-click form.

setlocal
cd /d "%~dp0.."
set PY=py -3

%PY% -m bace.drivers.shutter_console --browser
echo.
pause
