@echo off
REM BACE with nothing initialised - inherit the VI's setup, move three things.
REM
REM RUN THE LabVIEW VI FIRST and leave the bench exactly as it finished.
REM
REM This program configures NOTHING: not the timebase, not the trigger, not the
REM averaging or its count, not the vertical range, not the 33220A, not
REM :OUTP:POL, not the arming, not the pulse delay or width. It reads all of it
REM back and prints it, then does only the three things that cannot be
REM inherited because they change per point:
REM
REM     1. :VOLT1:HIGH / :VOLT1:LOW on the 81150A
REM     2. the shutter
REM     3. :DIGitize + *OPC? and fetch CHAN2
REM
REM plus one arithmetic step - light minus dark, baseline corrected the panel's
REM way (mean of the last 200 samples), integrated from 320 ns of record time.
REM
REM WHY
REM     The port and the VI now agree on the displacement spike to 1 % and
REM     disagree on the photocurrent by about 4x. Between them sit two dozen
REM     settings the port writes. This removes all of them at once:
REM       - number comes out right -> the fault is in something the port sets
REM       - number comes out like the port's -> the fault is in one of the three
REM
REM :DIGitize is what lets it claim to configure nothing: it acquires until the
REM acquisition is complete, which with averaging already on means the whole
REM average, and leaves the scope stopped. No averager restart, so no setting
REM is touched - and a fresh acquisition every time, so a light trace can never
REM be a blend with the dark one before it.
REM
REM LEVELS
REM     light = (vpre, vcoll), dark = that swing translated to start at 0 V,
REM     i.e. (0, vcoll - vpre). No --invert: the VI writes its levels in the
REM     plain convention and its :OUTP:POL is being inherited.
REM
REM     --dark same     leaves the levels alone for the dark trace too, so the
REM                     shutter is the only thing that moves at all.
REM
REM Nothing is restored on the way out except the shutter, which is shut. The
REM levels are left where the last point put them, on purpose.

setlocal
cd /d "%~dp0.."
py -3 tools\bare.py --dry
echo.
echo ----------------------------------------------------------------------
echo The above is what it read back and what it would write. Ctrl-C now to
echo stop, or press a key to run it for real.
echo ----------------------------------------------------------------------
pause
py -3 tools\bare.py --invert
echo.
pause
