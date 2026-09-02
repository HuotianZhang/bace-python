@echo off
REM Trigger chain: is the 81150A armed when the light goes OFF, or when it
REM comes ON? Half an LED period apart -- 1 ms -- and both give a transient
REM that integrates to a plausible charge. Nothing has ever checked it.
REM
REM WIRING
REM     CHAN1  33220A MAIN OUTPUT, through a tee, scope input 1 Mohm
REM     CHAN2  device current (already there, untouched)
REM     CHAN3  81150A Sync      (already there)
REM     CHAN4  81150A MAIN OUTPUT, through a tee, 1 Mohm  (optional)
REM
REM CHAN1 goes on the 33220A MAIN OUTPUT, not its Sync. What the Sync means
REM is the thing being tested, so looking at the drive itself settles it
REM without trusting any polarity setting or manual. 1 Mohm, not 50 ohm:
REM a 50 ohm input would load the generator outputs.
REM
REM BEFORE YOU RUN
REM     1. Leave the relay on the SourceMeter side. That is the SAFE
REM        position for this: the amplifier is disconnected from the device,
REM        so the 81150A can run with nothing reaching the sample.
REM     2. Keithley output OFF. The stage refuses to go on if it is not.
REM
REM --sync-drive puts the 33220A into pulse mode at the run.toml frequency
REM and levels and enables both generators, then restores exactly what they
REM were -- including DC mode, if a J-V scan left it there. Without it the
REM stage is scope-only and assumes the generators are already running.
REM
REM Every scope setting it changes is saved and put back, CHAN1 and CHAN4
REM included.

setlocal
cd /d "%~dp0.."
py -3 -m bace.bench --sync --sync-drive
echo.
pause
