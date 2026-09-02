@echo off
REM How much light actually reaches the sample? Measure it, do not infer it.
REM
REM     dark    shutter shut, 33220A output OFF     - the meter's background
REM     light   33220A pulsing 1.000 / 0.400 V, 500 Hz, 50 %, :OUTP:POL INV,
REM             shutter OPEN                        - what the sample sees
REM     off     33220A held at 0.400 V DC, shutter still open
REM     dark    shutter shut again                  - did the background move?
REM
REM The "off" phase settles a question open since 2026-09-01: every recipe calls
REM 0.400 V the LED's off level, and nothing has ever checked that it is below
REM the LED's turn-on. If it is not, the light never stops, and a BACE delay
REM axis is measuring the delay to an event that does not happen.
REM
REM BEFORE YOU RUN
REM     1. Start the 1918-C console first ("Start Console.bat" in the power
REM        meter project) and leave its window open. Only one process can hold
REM        the meter over USB; everything here goes through its HTTP API.
REM     2. Put the meter head where the sample sits, in the beam.
REM
REM This does NOT touch the 81150A or the relay. It moves the shutter and the
REM 33220A, and puts both back exactly as it found them - shape, polarity,
REM frequency, both levels, and the output state.
REM
REM Writes runs\lightpower<stamp>.csv: one row per sample, with the phase, so
REM the shutter transition itself is in the file too.

setlocal
cd /d "%~dp0.."
py -3 tools\lightpower.py --off-level --seconds 15
echo.
pause
