@echo off
REM The BACE service: the bench, the modules, the runs and the pipeline tree
REM over HTTP and WebSocket at http://127.0.0.1:8900/ -- the console's back end.
REM
REM WHAT IT DOES
REM     Loads rig.toml and run.toml from the repo root, opens the journal
REM     (runs\journal\<session>.jsonl), builds the real rig over VISA, reads
REM     every instrument back once, and then waits for the console. Nothing
REM     moves until a run is posted. Every run ends parked -- outputs off,
REM     shutter shut -- whether it finished, was stopped or failed.
REM
REM     Runs land under runs\ exactly as tools\scan.py leaves them (HDF5 plus
REM     the legacy .dat set); a pipeline gets one parent folder with one
REM     folder per module run inside it.
REM
REM BEFORE YOU RUN
REM     1. Nothing else may hold the instruments: close the LabVIEW VI and any
REM        other bace tool first. One process owns the bus, and this is it.
REM     2. Start the 1918-C console ("Start Console.bat" in the power meter
REM        project) if intensity is to be recorded. Without it the preflight
REM        says the console is silent (a warn, not a refusal) and intensity
REM        is NaN.
REM     3. Temperature: with [temperature] console set in rig.toml (the 331
REM        console at http://127.0.0.1:8331, started from its own folder),
REM        a temperature loop sets the setpoint through it and waits for
REM        the band; the console keeps its 350 K ceiling and the heater
REM        range. With it empty (the default) the loop pauses at each
REM        setpoint until you set the cryostat by hand and click resume.
REM
REM Leave this window open. Ctrl-C aborts the run in flight, parks the bench
REM and closes the journal. The routes are listed at http://127.0.0.1:8900/
REM and bace\service\README.md says how to drive them by hand.

setlocal
cd /d "%~dp0.."
py -3 -m bace.service --rig rig.toml --run run.toml
echo.
pause
