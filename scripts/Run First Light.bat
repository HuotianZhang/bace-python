@echo off
REM First light: one real transient into the connected sample.
REM
REM This is the only question the simulator cannot answer -- does an
REM acquisition triggered by the real 81150A sync, through the real sense
REM resistor, give a photocurrent that integrates to a sensible charge?
REM
REM What it drives, at the device (after the x4 amplifier):
REM     prebias  +0.92 V   -- the measured V_oc of this cell
REM     collect  -1.00 V   -- generator sends +0.230 / -0.250 V
REM     delay 88 ns, 2 loops, 16 averages, one axis point
REM For comparison the rig currently free-runs at +5.06 / 0 V at the device,
REM so this is the gentler setting.
REM
REM LEAVE THE SAMPLE CONNECTED. The shutter is driven in software (DIO
REM module 0, confirmed by measurement 2026-09-01); the relay stays where it
REM is, on the amplifier path. It will ask you to type "yes" before it
REM enables anything.

setlocal
cd /d "%~dp0.."
py -3 -m bace.bench --measure --vpre 0.92 --vcoll -1.0 --averages 16 --loops 2
echo.
pause
