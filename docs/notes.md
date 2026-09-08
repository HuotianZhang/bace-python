# Engineering notes

What was measured, what was found, and what was decided. Kept so that nobody
has to discover any of it twice. `CHANGELOG.md` says when; this says what and
why.

## The rig, as measured

| instrument | address | notes |
|---|---|---|
| Infiniium DSO9054H | `TCPIP0::PwM-DSO9054H.local::inst0::INSTR` | LAN, not GPIB |
| Agilent 81150A | `GPIB0::12::INSTR` | collection field, through a ×4 amplifier |
| Keithley 2400 | `GPIB0::24::INSTR` | DC side of the relay |
| Agilent 33220A | `GPIB0::15::INSTR` | LED drive |
| Lake Shore 331 | `GPIB0::7::INSTR` | opened by the service; `[temperature] console` hands the bus to the 331 console instead |
| Deditec DIO | module ID 9 | module 0 = shutter (1 = open), module 1 = relay (1 = Keithley) |

Record geometry, stable across every session: 4000 points, dt 0.5 ns,
t0 −199.5 ns, the trigger 199.5 ns into the record. `:TIM:POS` is the screen
centre. `:ACQ:POIN` is a request; the record is window × sample rate, which is
where the archive's 4000 comes from, not 5000.

The DIO identity was measured on 2026-09-01 by walking all four line states
with the Keithley reading the device (`docs/bench-checkout.md`). Re-run
`python -m bace.bench --dio` after any rewiring.

Sync-to-field latency: 47.1 ns, from a 49-point delay scan on 2026-09-03
(1.5 ns residual). It positions the integration window and is never added to a
command (`docs/integration-window.md`).

## Defects only the hardware found

Each is fixed and documented at its site in the code. They are the reason the
check-out harness exists.

1. **Auto-range computed the range from amps, not volts.** Would have set
   `:CHAN2:RANG` 5.192× too small and clipped every light trace.
2. **Phantom error queue.** The no-error prefix list did not match the
   DSO9054H's bare `0`; 28 false rejections in the first report.
3. **Configure changed levels on a live output.** It now reads `:OUTP?` from
   the instrument, not from the driver's own flag.
4. **The empty fetch was not a block-header problem.** It was fetching while
   the scope was still running. `:STOP` first.
5. **The averager is never emptied** by the LabVIEW double configure, nor by
   `:STOP`/`:RUN`, only by a channel-range write, `:CDIS`, or averaging
   off/on. `:WAV:COUN?` ran on from the light acquisition into the dark one,
   so every dark trace was half light and Q was half its value; a step whose
   auto-range sat inside the deadband inherited the previous step's dark and
   read a third. `:ADER?` is a latch cleared on read, set per acquisition, not
   per completed average; `:WAV:COUN?` read while running answers the
   configured count. The driver now clears the averager, takes the auto-range
   passes un-averaged, acquires with `:DIG` + `*OPC?`, and checks the folded
   count after the fetch. The LabVIEW original had the same behaviour.
   Measured by `tools/probe_averager.py`, 2026-09-06.
6. **Auto-range was single-pass**, then capped at 4 passes; both too few.
7. **The scope rounds `:CHAN2:RANG?` to three significant figures.** A
   readback added for accuracy was degrading it.
8. **That readback costs 145 ms.** Removing it was wrong, because the clip
   test is computed from range and offset. The fix was to write less: a 2 %
   deadband, so a correct range is left alone.
9. **`--dio` bypassed the relay interlock.** `routing.Relay` refuses to move
   the relay while a source drives; the DIO stage had driven the line directly.
10. **`current_sign = -1` broke the trigger calibration.** `calibrate_trigger`
    took `max()` of the sync trace in amps, in the rig's sign convention, so
    the positive sync became a negative pulse whose maximum is the baseline.
    The calibration now works in scope volts, and a channel swinging under
    0.1 V refuses to run under `AUTO` sweep (`transient.SyncError`).
11. **The trigger calibration was circular.** A 2 µs record only sees the
    5 µs sync if it starts on it, and the scope only starts on it if it is
    already triggering on it, so every calibration inherited the previous
    session's level. The run now triggers at a provisional 0.5 V, measures the
    sync from triggered records, then sets the measured half-amplitude. It
    also reads `:OUTP1?` back after `:OUTP1 ON` and stops if the instrument
    says 0.

## Deliberate deviations from the LabVIEW original

- **Geometric auto-range.** A clipped trace's extremes carry no information
  about how far past the rail the signal went, so the window grows ×1.8
  anchored on the edge that did not clip; the original formula is applied
  once, to the first acquisition that fits. From the 0.15 V start (a hard
  constant inside `scale to maximum.vi`): archive signal 5 → 2 acquisitions,
  reports 7–8 4 → 2, report 9 8 → 3.
- **Compliance before output enable** on the Keithley. The original enabled
  the output first, leaving the SMU at whatever `*RST` had set.
- **`:SENS:VOLT:PROT:LEV`** for the V_oc branch, where the original set the
  current protection.
- **`FUNC:PULS:HOLD DCYC`** before `:FREQ` on the 33220A, avoiding a real
  `-221 Settings conflict`.
- **A 2 % deadband** on the auto-range, so a correct range is not rewritten.
- **The LED generator is never switched off by a module; the shutter is the
  light switch.** A `bace` leaves the 33220A pulsing, a J–V leaves it at DC,
  and every unwind shuts the shutter. After DC → pulse a `bace` opens the
  shutter and waits for the power meter to read stable (three readings 0.5 s
  apart within `led_settle_tolerance`, at least `led_settle_s`, at most
  `led_settle_max_s`) rather than a fixed 2 s: a generator that is cycled
  loses its thermal steady state. The read-back also refuses a 33220A whose
  Sync output is off, since that is what arms the 81150A.
- **The run is a synchronous generator, not async.** VISA blocks and its
  sessions are not thread-safe. The service adapts at its edge.
- **HDF5 failure never costs the `.dat` files.** They are written first.

## Open questions

None of these blocks a measurement.

1. **Pulse width.** `run.toml` has 5 µs against a 2 µs record, so the record
   only ever sees the rising edge. The original's value is unconfirmed.
2. **J_sc settle time.** It moved 2.14× between two runs ten minutes apart
   while V_oc moved 3.5 mV. Either the device drifts or 500 ms is not enough.
3. **`trigger_sweep`.** `AUTO` is the original's, and free-runs when no
   trigger arrives: a dead sync gives a flat trace, not an error. `TRIG`
   would fail loudly. Undecided.
4. **Why three prebias points** rather than three repeats at V_oc. Mechanism
   settled, intent not. With the axis a parameter this is a `run.toml` choice.
5. **The intensity calibration `Factor`**, a recovered panel constant of
   unknown provenance. Scales logged intensity and nothing else.
6. **How many iterations `scale to maximum.vi`'s loop runs.** At least five,
   from the archive; the binary's count terminal was unwired.
7. **`keithley2400.PanelSetup`, `apply_panel` and `read_panel`** have no
   caller inside `bace/` since the Keithley console moved to `examples/` with
   its own copy of the driver. Kept and tested; a candidate for deletion if
   nothing takes it up.

## Closed questions

- **`New Sample?`** swaps and negates the two levels, and nothing else. It also
  drives `Configure Output Polarity.vi`, which is why the two looked like they
  might cancel.
- **`positive jSC?`** is dead. The label is on every front panel; the string
  appears in no block diagram.
- **The power meter is on a beam splitter**, so intensity can be read during a
  run. No bridge and no 32-bit helper needed: only `delib.dll` needs that, and
  the 1918-C is opened in-process.
- **Output polarity.** `:OUTP1:POL INV` was proposed, tested on the rig and
  rejected: through the inverting ×4 amplifier it rests the device at V_coll so
  extraction never stops; every INV run showed the photo peak collapsed from
  ~3 mA to ~0.5 mA. The validated pair is `invert_polarity = true` with `NORM`,
  which is also where the LabVIEW program leaves the generator.
- **The 2026-08-07 archive's Q sign.** That is a different device. The archive
  is a numerical regression (same traces in, same numbers out) and nothing
  more.

## Claims that were overturned by measurement

Each page that made one carries a dated correction where the claim appears.

| claim | what is true |
|---|---|
| trigger offset = 47 ns on the scan path | 0. At the same `Delay(ns) = 90`, LabVIEW and the port both put the displacement spike at 328 ns of record time. |
| `timebase_ns_per_div` = 500 | 200. On the panel, Timebase 200 ns and Pulse Width 5e3 ns sit side by side; they are unrelated. |
| the LED never goes dark | It does. The power meter reads its off level at 0.009 % of the on level. |
| the device may be damaged | It is not. V_oc 1.028 V, J_sc −126 A/m², FF 0.55. |
| light and dark sharing an extreme means clipping | They should share it. Clipping shows as 7–8 consecutive samples at one float inside a single averaged trace. |
| the port is 3× low against LabVIEW | The reference moved: LabVIEW's own τ fell from 190 ns to 71 ns over three hours, so runs hours apart are not a comparison. The pair eight minutes apart is in `history/HANDOVER-2026-09-02.md`. |
| combination ④ means `invert_polarity` + `:OUTP1:POL INV` | See *Output polarity* above. |
| `Q:` and `D:` are one store over the network | Two separate trees. The harness prints a code fingerprint so a stale copy is visible. |

## Inferences that were wrong

Kept because the reasoning failed the same way twice.

- **"Q: and D: are one store."** Inferred from a 62-second timing coincidence.
  Verify, do not infer.
- **"The tail improving proves the clipping was distorting the baseline."**
  One sample. The next run undercut it; shot-to-shot tail scatter is
  ±1.3e-5 A at 16 averages.
- **"There is no 64-bit DELIB."** The 64-bit build ships as `delib64.dll`. The
  absence of a 64-bit `delib.dll` proved nothing.
- **"An open circuit rails at the voltage compliance."** It does not: sourcing
  0 A into an open circuit is a degenerate loop. A small forward bias is the
  decisive probe.
- **A test that mocked the driver hid a real crash.** Mocking the thing that
  constructs the defect is how it reached the bench. The driver tests now wire
  the real driver and the real recording layer against a fake pyvisa resource.

## The criterion

A BACE curve is usable when light minus dark returns to zero in the steady
state, not when V_oc lands somewhere expected. A non-zero plateau disqualifies
the curve however plausible the charge looks.
