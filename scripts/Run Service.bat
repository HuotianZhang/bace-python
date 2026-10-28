@echo off
REM The BACE service and its console in a terminal, for diagnosis: open
REM http://127.0.0.1:8900/ in a browser once this window says the service is
REM running. Operators start it with BACE.pyw in the repository root, which
REM runs the same command without a window and opens the console.
REM
REM WHAT IT DOES
REM     Loads rig.toml and run.toml from the repo root, opens the journal
REM     (runs\journal\<session>.jsonl), builds the real rig over VISA, reads
REM     every instrument back once, and then waits for the console. Nothing
REM     moves until a run is started. Every run ends parked -- bias and
REM     source-meter outputs off, shutter shut -- whether it finished, was
REM     stopped or failed.
REM
REM     Runs land under runs\ exactly as tools\scan.py leaves them (HDF5 plus
REM     the legacy .dat set); a pipeline gets one parent folder with one
REM     folder per module run inside it.
REM
REM BEFORE YOU RUN
REM     1. Nothing else may hold the instruments: close the LabVIEW VI and any
REM        other bace tool first. One process owns the bus, and this is it.
REM     2. The service opens the 1918-C power meter and the Lake Shore 331
REM        itself. Their own console programs must not run beside it: two
REM        programs on one instrument read each other's replies.
REM     3. The manual, docs\manual\1.3-first-measurement.md, walks through
REM        a first measurement.
REM
REM Leave this window open. Ctrl-C, or shut down in the console, aborts any
REM active run, parks the bench and closes the journal. The HTTP routes are listed at
REM http://127.0.0.1:8900/docs.

setlocal
cd /d "%~dp0.."
py -3 -m bace.service --rig rig.toml --run run.toml --ui ui
echo.
pause
