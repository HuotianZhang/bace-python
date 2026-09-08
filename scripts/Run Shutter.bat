@echo off
REM The shutter, on its own. Reads the line, then opens or shuts it if you say
REM so. Nothing else on the bench is opened: no VISA, no scope, no generators,
REM no service - this is the Deditec DIO line and nothing more.
REM
REM WHY IT NEEDS NO INTERLOCK
REM     The other DIO line is the relay, and throwing that under a live source
REM     is the one software mistake here that costs hardware rather than a
REM     dataset; tools\relay.py is the program for it, and it proves both
REM     outputs off before it moves. The shutter only carries light: shut is
REM     its safe state, and the worst it can do is a trace taken in the wrong
REM     illumination. So this asks no other instrument for permission. The one
REM     thing it refuses is --module-nr 1, which is the relay.
REM
REM ONE PROCESS OWNS THE MODULE
REM     Stop the service (Run Service.bat) and close the LabVIEW VI first.
REM     While another program holds the Deditec module, DapiOpenModule answers
REM     0 and the message reads like missing hardware.
REM
REM BITNESS
REM     ctypes loads only the DELIB build matching the interpreter. If this
REM     machine has just the 32-bit DELIB (delib.dll in SysWOW64, or the copy
REM     under D:\BACE\shutter\builds\data\), change the line below to
REM
REM         set PY=py -3.11-32
REM
REM     A 32-bit Python with nothing installed in it is enough: the tool
REM     imports the standard library and bace\drivers\shutter.py, and reads
REM     [dio] out of rig.toml itself. Nothing here needs numpy or pyvisa.
REM
REM The line is left where you put it. Opening the shutter and closing this
REM window leaves the shutter open - that is the point of the program.

setlocal
cd /d "%~dp0.."
set PY=py -3

%PY% tools\shutter.py
echo.
set ANS=
set /p ANS="  open / shut / leave it?  [o/s/N] "
if /i "%ANS%"=="o" goto :open
if /i "%ANS%"=="s" goto :shut
echo   left where it was.
goto :end

:open
%PY% tools\shutter.py --open
goto :end

:shut
%PY% tools\shutter.py --shut

:end
echo.
pause
