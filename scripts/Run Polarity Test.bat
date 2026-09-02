@echo off
REM Which output polarity does BACE actually want?
REM
REM First light on 2026-09-01 gave Q = -2.07e-10 C where the archive, at the
REM same Vcoll and the same delay, gives +3.65e-10 C. Opposite sign.
REM
REM The 81150A's LOW level is what the device sits at BETWEEN pulses. The
REM original computes LoLvl = Vcoll, HiLvl = Vpre (MathScript, confirmed), so
REM with :OUTP1:POL NORM the device is held in extraction at Vcoll and pulsed
REM up to Vpre. With INV it is held at Vpre and stepped to Vcoll -- which is
REM BACE as the physics describes it, and matches the archive's near-zero
REM standing current (+4.6e-5 A, i.e. sitting at Voc).
REM
REM The generator VI holds BOTH constants -- Polarity 0 (Normal) and
REM Polarity 1 (Inverted) -- in the two frames of a case structure, so the
REM original sets one or the other and the binary does not say which.
REM
REM This runs the same measurement both ways, back to back, same sample.
REM Whichever gives Q of the archive's sign is the right one.
REM
REM LEAVE THE SAMPLE CONNECTED. It will ask you to type "yes" twice.

setlocal
cd /d "%~dp0.."

echo.
echo  ===== 1 of 2:  :OUTP1:POL NORM  (held at Vcoll, stepped to Vpre) =====
echo.
py -3 -m bace.bench --measure --vpre 0.92 --vcoll -1.0 --averages 16 --loops 2

echo.
echo  ===== 2 of 2:  :OUTP1:POL INV   (held at Vpre, stepped to Vcoll) =====
echo.
py -3 -m bace.bench --measure --vpre 0.92 --vcoll -1.0 --averages 16 --loops 2 --invert-output

echo.
echo  Two reports in bench-reports\ -- compare their Q.
pause
