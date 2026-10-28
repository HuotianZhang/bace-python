# Bench check-out

The harness checks the package and instruments in stages. It records each
SCPI command and checks the error queue after commands. Timing-sensitive
acquisition blocks suspend those queries with `quiet(res)` so extra bus
traffic does not change the acquisition. The transcript shows accepted
commands, refusals and instrument responses.

Three files land in `bench-reports/`: a `.txt` to read, a `.html` to look at,
and a `.json` with the full command transcript.

## Setup

From the repository root:

```
py -3 -m pip install -e .[lab,dev]
```

`scripts/Setup.bat` does the same by double-click. In PowerShell quote it,
`pip install -e '.[lab,dev]'`, or the brackets are read as an index.

`h5py` is a declared package dependency. If HDF5 writing fails, the recorder
still writes `.dat` files first. The VISA backend must be installed for the
real bench; the driver can use NI-VISA or another configured backend.
The harness writes reports under `bench-reports\`. Offline checks use
temporary directories for simulated runs and file round trips.

## Code fingerprint

There may be more than one copy of this tree on the bench, edited in one
folder and executed from another. Pass 1 therefore reports a code
fingerprint: a hash over every `.py` in the package, path and content, ignoring
`__pycache__`.

```
package            = <checkout>\bace
code fingerprint   = 3279a559bfa8
newest source file = bench/checks.py (2026-09-01 00:02)
```

Reports with the same fingerprint use identical Python source paths and
contents. Check the `package` path when the fingerprint differs from the
intended checkout. Identical source does not establish identical settings,
dependencies or bench conditions.

## Pass 1 — no instruments touched

```
python -m bace.bench
```

The numerical regression uses the `.dat` run in `bench-archive/` (540 kB).
It recomputes photocurrent and Q and checks byte-exact `.dat` round trips.
The regression tolerance follows the files' six significant figures
([Testing](manual/3.5-testing.md)). Without an archive the check skips.

`--archive <folder>` overrides it, and accepts either a single run folder or the
directory that holds them; it descends one level to find one.

Checks the package on your Python, runs the simulated transient / J–V / series
end to end, re-runs the numerical regression against a real archive on your
machine, and confirms the legacy `.dat` files still round-trip byte-for-byte
there. Then enumerates VISA, asks `*IDN?` at each configured address, and looks
for `delib.dll` and the two consoles. Read-only throughout.

If an instrument answers with something unexpected — say the 33220A at the
81150A's address — the report flags it rather than carrying on.

## Pass 2 — read and configure. Outputs stay off

```
python -m bace.bench --read --configure
```

`--read` reads back what each instrument is currently set to and sends no
settings. `--configure` sends every configuration command the measurement uses
and checks the error queue after each one. No output is enabled.

The report checks `:TIM:RANG = timebase_ns / 1e8`: 200 ns/div gives a
2 µs record. It also reports `:ACQ:POIN?`, `:ACQ:SRAT?` and the implied
sample count. `record_length` is a request. A measured 2 µs record at
2 GSa/s contains 4000 samples despite `:ACQ:POIN? = 5000` (`notes.md`).
Disagreements in these hypothesis checks produce warnings with the values.

## Pass 3 — one acquisition

```
python -m bace.bench --read --configure --acquire --averages 16
```

Turn the instrument outputs off first, or stop the LabVIEW program. The
configure stage refuses to change the settings of an instrument whose output
is already enabled — changing levels on a live output connected to a sample is
the mistake this harness exists to avoid. `--force-configure` overrides it, and
should only be used with nothing connected.

The digitiser needs its trigger source running: 81150A Sync on CHAN3.
Acquisition uses `:DIG` and `*OPC?`; a timeout reports the missing-trigger
condition. The compatibility diagnostics can include `:ADER?` replies.

Reports the point count, `dt`, the auto-ranged vertical range, the sequence of
windows the auto-range tried on its way there, and the first few samples — enough to tell whether the timebase arithmetic is right. It also
reports where the trigger falls inside the record and where `t0_int` sits
relative to it, which is what decides whether the integral starts before the
transient does.

The configure stage sets CHAN2 to 0.15 V (`bace/bench/checks.py`). Auto-range starts from the active
range. Clipped extremes are lower bounds, so a clipped range grows by ×1.8,
anchored on the edge that does not clip. The first fitting trace supplies
the fitted range. The limit is six passes.

Bench comparisons in `notes.md` give these acquisition counts from 0.15 V:

| Signal peak | Repeated fit formula | Geometric growth |
|---|---|---|
| 164 mV | 5 | 2 |
| 127 mV | 4 | 2 |
| 336 mV | 8 | 3 |

If the report ends with `still clipping`, every charge from that run is an
underestimate — start the channel wider.

A range inside the 2 % deadband stays unchanged. This avoids repeated range
writes and settling queries and keeps quantisation stable.
`notes.md` records a range-query cost of about 145 ms.

The `raw waveform transfer` check reads `:WAV:DATA?` byte by byte. It reports
the header, declared payload length and actual length. These distinguish an
indefinite block from a truncated transfer or empty fetch.

## Optional — what the DIO lines do

```
python -m bace.bench --dio
```

Leave the sample connected and the LED on. With no device there is nothing
to tell the states apart.

The measured DIO states (`notes.md`) are:

```
module 0=0, 1=0 : V(I=0) -0.0360 V  I(V=0) -1.09e-11 A  I(+0.5V) +4.75e-11 A
module 0=0, 1=1 : V(I=0) +0.0001 V  I(V=0) +5.54e-11 A  I(+0.5V) +4.76e-07 A
module 0=1, 1=0 : V(I=0) +0.0109 V  I(V=0) -1.70e-11 A  I(+0.5V) +4.76e-11 A
module 0=1, 1=1 : V(I=0) +0.9222 V  I(V=0) -1.13e-04 A  I(+0.5V) -7.78e-05 A
```

Module 0 is the shutter (1 = open): shutting it removes the photocurrent without removing forward conduction. Module 1 is the path switch (1 = Keithley):
flipping it drops conduction to the open-input floor. The two open-input current
readings, 4.7513e-11 and 4.7638e-11 A, differ by about 0.26 % — that is the SMU reading
itself, with nothing attached — and the nearest connected reading is four decades
above. Not a close call.

`routing.Relay` uses module 1 as the path switch.

Re-run it after any rewiring; it takes about a minute.

Listening to a click settles none of that — it does not say whether a line
switched an optical path or an electrical one. The Keithley does. This walks all
four states of the two lines and takes three readings in each:

| state | V at I = 0 | I at V = 0 | I at +0.5 V |
|---|---|---|---|
| connected, light | V_oc (~0.9 V) | short-circuit current | conducts |
| connected, dark | ~0 V | ~0 | conducts |
| not connected | undefined | ~0 | leakage floor |

Photocurrent marks the one state where the device is on the Keithley *and*
light reaches it. That fixes the values of both lines but not
which line is which — both are set the same way there, so the pair is
symmetric. Forward conduction breaks the symmetry: flipping one line at a
time from the photocurrent state, the flip that stops conduction moved the
electrical path, and the flip that keeps it but loses the photocurrent moved the
light. If `rig.toml` has them the wrong way up the report says so in as many
words — that error would open the relay on every step where a run meant to open
the shutter.

Sourcing 0 A into an open circuit does not prove disconnection by voltage
compliance: 0 A flows at any voltage. Measured disconnected voltages of
0.0082 and 0.0682 V are below the 2 V limit. A small forward bias distinguishes
the paths (`notes.md`).

The stage sources 0 A for one reading, 0 V for the
next, and +0.5 V for the probe — below the ~0.92 V V_oc this cell shows, so it
is a point on its own J–V curve, under the recipe's compliance. The output is
enabled only for each reading. Both generator outputs are
off for the whole stage and put back at the end, and each DIO line is returned
to its read-back position at stage entry. If a line's position cannot be read back, the
outputs are left OFF and the report says so — re-enabling a source into a path
that may have moved underneath it is the hazard the interlock exists for.
`--leave-outputs-off` skips the restore entirely; `--listen` adds the
what-did-you-hear prompts when someone is in the room.

## Optional — outputs, with nothing connected

```
python -m bace.bench --outputs
```

Disconnect the sample first. Enables the 81150A and the Keithley at 0 V,
reads back the output state and the error queue, then disables them. It asks you
to type `yes` before doing anything.

The Keithley check also reports the current it sees at 0 V into what should be an
open circuit — more than 1 µA means something is still connected.

## Optional — first light

```
python -m bace.bench --measure --manual-shutter --vpre 0.9 --vcoll -1 --loops 2 --averages 16
```

One real transient at a single prebias, with a sample. This is the only thing
the simulator cannot answer: whether an acquisition triggered by the real sync,
through the real sense resistor, produces a photocurrent that integrates to a
sensible charge.

`--manual-shutter` prompts you to open and close the shutter by hand — four
prompts for a two-loop run — which permits operation without a working DIO backend. Matching 64-bit
DELIB uses `delib64.dll`; the package also supports a 32-bit bridge.

The report carries the light, dark and photocurrent traces downsampled to ~400
points each, the charge per loop, where the peak sits in the record relative to
`t0_int`, and the tail noise. It also writes a normal run folder under
`bench-reports\first-light\`, so the result opens in exactly the same analysis
as any other measurement.

Set `--vcoll` and `--vpre` to values you would normally use on that sample.
The defaults are the archive's, not yours.

## Output

`bench-reports\bench_<stamp>.json` is the record of a session. If `--measure`
ran, the run folder under `bench-reports\first-light\` is too.

## Failures

Every check is caught, so one broken instrument never ends the session — the
report just records the traceback and continues. A half-complete report from a
rig with one dead instrument is still a report; keep the `.json`.
