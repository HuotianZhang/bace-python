@echo off
REM What do DIO module 0 and module 1 actually do? Answered by measurement,
REM with nobody in the room.
REM
REM LEAVE THE SAMPLE CONNECTED AND THE LED ON. With no device there is
REM nothing to tell the four states apart.
REM
REM It walks all four states of the two lines and measures the Keithley in
REM each: V at I=0, then I at V=0. Photocurrent means the device is on the
REM Keithley and light is reaching it; the voltage compliance rail means an
REM open circuit. That says which line is the shutter and which switches the
REM electrical path.
REM
REM Nothing is forced into the sample -- 0 A for one reading, 0 V for the
REM other. Both generator outputs are turned off for the whole stage and put
REM back at the end, and each DIO line is returned to where it was found.

setlocal
cd /d "%~dp0.."
py -3 -m bace.bench --dio
echo.
pause
