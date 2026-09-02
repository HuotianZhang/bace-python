@echo off
REM LabVIEW panel replica -- combination 4 polarity, every other parameter
REM copied off the panel screenshot of 2026-09-01.
REM
REM WHAT IT IS FOR
REM     Does the port reproduce the number the LabVIEW engine gets? The panel
REM     gives three things to check against:
REM         Charge           ~ -9.2e-10 C   (three Vpre points within 0.7 %)
REM         PhotoCurrent     peak ~ -3.7 mA, back to zero inside 2 us
REM         raw light/dark   peak ~ -27 mA
REM
REM     Those are the 23:16 panel numbers and they are NOT a baseline.
REM     LabVIEW ran again at 02:16 with not one parameter changed and gave
REM     -3.3e-10 C, tau 71 ns against 190 ns: the device drifted 2.8x in three
REM     hours. Compare only against a LabVIEW run taken minutes away, never
REM     against these. (HANDOVER-2026-09-02.md)
REM
REM PANEL VALUES COPIED  (full list and reasoning in recipes\run-labview.toml)
REM     Vpre 1.00354 -> 1.02354 step 0.01     Vcoll -1 V      Delay 90 ns
REM     Timebase 200 ns   Record Length 5e3   t0 Integration 320e-9 (record)
REM     Averages 200      Loops 1             Pulse Width 5e3 ns
REM     LED 1.000 / 0.400 V, 500 Hz, 50 %     offset corr ON
REM
REM POLARITY  (corrected 2026-09-02 -- this file used to say the opposite)
REM     invert_polarity = true + :OUTP1:POL NORM -- combination 3, and it is
REM     the validated pair. "Combination 4 = INV" was overturned on the rig:
REM     the 02:20 bit-by-bit read-back showed LabVIEW itself finishing in
REM     NORM, and INV parks the device at v_coll through the inverting
REM     amplifier, collapsing the photocurrent peak to about 0.5 mA -- that is
REM     what the "3x too small" hunt of 23:41-01:22 was chasing.
REM     recipes\run-labview.toml is corrected; the overturned
REM     table is in docs\README.md.
REM
REM SAMPLING
REM     200 ns/div with 5000 points is 0.4 ns a sample, four times finer than
REM     the 500 ns/div runs, which the scope answered with 3125 points
REM     (1.6 ns). Check the sample interval the runner prints: :ACQ:POIN is a
REM     request, not a promise.
REM
REM --set-led IS REQUIRED
REM     The panel drives the LED at 1.000 V; every run before this one used
REM     1.020 V, and the extracted charge scales with intensity. Without
REM     --set-led the generator keeps whatever the previous run left. The
REM     preflight now reads :VOLT:HIGH? / :VOLT:LOW? back and refuses to start
REM     if they disagree with the recipe -- nobody checked those two numbers
REM     until 2026-09-01.
REM
REM SHUTTER SETTLING
REM     shutter_settle_s = 0.7 s, applied after the shutter moves in BOTH
REM     branches. Until 2026-09-01 the dark trace was acquired the instant the
REM     shutter closed, with no settling at all, while the light trace got
REM     settle_s. A difference of two traces means nothing when only one of
REM     them was allowed to finish its transient.
REM
REM BEFORE YOU RUN
REM     Relay on the AMPLIFIER side, Keithley output OFF. The preflight checks
REM     both and refuses otherwise. It will ask you to type "yes".

setlocal
cd /d "%~dp0.."
py -3 tools\scan.py --run recipes\run-labview.toml --set-led
echo.
pause
