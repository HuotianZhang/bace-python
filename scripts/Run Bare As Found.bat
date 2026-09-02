@echo off
REM One BACE point at the voltages the VI left, shutter the only thing moving.
REM
REM     1. Run the LabVIEW VI. Leave the bench exactly as it finished.
REM     2. Run this. Do not touch anything in between.
REM
REM WHAT IT DOES
REM     Reads back every setting on the scope, the 81150A and the 33220A, prints
REM     them, and writes them into the run folder as 0_instrument_state*.txt and
REM     .json - so the state that produced the data is recorded WITH the data.
REM
REM     Then, with --as-found, it does not write the levels either. The 81150A
REM     keeps the voltages the VI left on it. Both traces are taken at those
REM     same voltages. The shutter is the only thing in the building that moves:
REM
REM         shutter open  -> settle -> :DIGitize -> light
REM         shutter shut  -> settle -> :DIGitize -> dark
REM
REM     Any difference between the two is photocurrent by construction. No level
REM     translation, no polarity convention, no per-point arithmetic, no numbers
REM     typed in by anyone. One point.
REM
REM WHY THIS ONE
REM     The last run (bare --invert) matched the VI to 0.08 % on the displacement
REM     spike, 0.9988 correlation on the dark trace, and reached 78 % of its
REM     photocurrent peak - but its tail decayed 2.6x faster, and that is where
REM     the whole charge difference sits. This removes the last few things the
REM     previous run still did: the level arithmetic and the polarity flag.
REM
REM     --settle 5 also matches the panel's "LED stab. time 5 s", in case the
REM     short tail is the device not having had time to fill at the prebias.
REM
REM It prints everything first and asks you to type yes before anything moves.
REM Nothing is restored on the way out except the shutter, which is shut.

setlocal
cd /d "%~dp0.."
py -3 tools\bare.py --as-found --settle 5
echo.
pause
