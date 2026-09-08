# Bench check — what to run on the lab PC

Nothing in this port has ever spoken to an instrument. Every SCPI string was
recovered from a LabVIEW binary. This harness sends each one and reads the
instrument's error queue **immediately afterwards**, so the report comes back
with an exact list of what worked, what was rejected, and where the recovered
behaviour differs from what the instruments actually do.

Three files land in `bench-reports/`: a `.txt` to read now, a `.html` to look
at, and a `.json` with the full command transcript. **Send the `.json` back** —
that is the one with everything in it.

## Where to run it from

**Run the copy I commit to, or tell me which copy is canonical.** The second
bench session ran `Q:\Huotian\bace-python` while the fixes had gone to
`D:\BACE\bace-python`, so the session repeated three defects that were already
fixed. Either re-copy before each run, or say which path to write to.

## Setup, once

Double-click **`scripts\Setup.bat`**. It installs into `py -3` -- the
interpreter every other `.bat` here calls -- checks that `bace.service`
imports, and says what came in.

By hand it is one line, from the repo root:

```
cd D:\BACE\bace-python
py -3 -m pip install -e .[lab,dev]
```

In PowerShell quote it, `pip install -e '.[lab,dev]'`, or the brackets are
read as an index.

`lab` is the rig and the service together, which is what a bench PC needs:
numpy, scipy and h5py for the measurement; fastapi, uvicorn and websockets
for the service the console talks to; pyvisa for the instruments. `dev` adds
pytest and httpx2, so the suite runs here too. The `-e` is what makes this
folder the code that runs -- edit a file and the next run has it -- which
matters on a bench that keeps more than one copy of the tree.

On a machine with no instruments, `scripts\Setup (sim).bat` installs the same
thing without pyvisa: `--sim` never imports it, so the console and the whole
API work with no VISA backend at all.

`pyvisa` uses the NI-VISA already installed for LabVIEW, so GPIB works. Nothing
is installed system-wide by the harness and nothing is written outside
`bench-reports\`.

## Which copy am I running?

There is more than one copy of this tree on the bench — it is edited in one
folder and executed from another — and on 2026-09-01 the executed copy sat ten
files behind for an hour with nothing saying so. So pass 1 reports a **code
fingerprint**: a hash over every `.py` in the package, path and content, ignoring
`__pycache__`.

```
package            = Q:\Huotian\bace-python\bace
code fingerprint   = 3279a559bfa8
newest source file = bench/checks.py (2026-09-01 00:02)
```

Two reports with the same fingerprint ran the same code; two that disagree about
a result while sharing a fingerprint disagree about the *rig*. If the fingerprint
does not match what was just sent, the copy being run is stale — check the folder
in the `package` line.

## Pass 1 — no instruments touched

```
python -m bace.bench
```

The numerical regression needs a real measurement folder, and a copy of the
2026-08-07 run (`s4_PTQ10IT4F_pxa_290K_...111521`, .dat files only, 540 kB) now
sits in `bench-archive/` for it, so it runs with no argument. That matters more
than it sounds: it is the check that reproduces a real run's photocurrent and
charge to 0.6 ulp and round-trips the legacy files byte for byte, and it means
every report proves *this machine's* copy of the code rather than the author's.
Delete the folder if you would rather not keep data in the code tree -- the
check goes back to skipping.

`--archive <folder>` overrides it, and accepts either a single run folder or the
directory that holds them; it descends one level to find one.

Checks the package on your Python, runs the simulated transient / J–V / series
end to end, re-runs the numerical regression against a real archive **on your
machine**, and confirms the legacy `.dat` files still round-trip byte-for-byte
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
and checks the error queue after each one. **No output is enabled.**

This is the pass that earns the harness. It also settles two things the archive
implies but nobody has watched happen:

- whether `:TIM:RANG` really wants `timebase_ns / 1e8` (200 → 2 µs full screen);
- where the archive's 4000 points come from. The first session settled this:
  `:ACQ:POIN?` answers 5000, so the reduction is not there — the record is the
  time window times the sample rate, and 2 µs at 2 GSa/s is 4000. The check now
  reports `:ACQ:SRAT?` and the implied count, so `record_length` is visibly a
  request rather than a promise.

Both are reported as *warnings with the numbers*, not failures — a disagreement
is information.

## Pass 3 — one acquisition

```
python -m bace.bench --read --configure --acquire --averages 16
```

**Turn the instrument outputs off first**, or stop the LabVIEW program. The
configure stage now refuses to change the settings of an instrument whose output
is already enabled — changing levels on a live output connected to a sample is
the mistake this harness exists to avoid. `--force-configure` overrides it, and
should only be used with nothing connected.

Needs whatever normally triggers the scope to be running (the 81150A putting
sync edges on CHAN3). If nothing is triggering, the check says so and shows what
`:ADER?` returned, rather than failing silently.

Reports the point count, `dt`, the auto-ranged vertical range, the sequence of
windows the auto-range tried on its way there, and the first few samples — enough to tell whether the timebase arithmetic is right. It also
reports where the trigger falls inside the record and where `t0_int` sits
relative to it, which is what decides whether the integral starts before the
transient does.

The auto-range iterates, and the window sequence is worth reading. The channel
always starts at **0.15 V** — that is a hard constant inside
`scale to maximum.vi`, so it is where every LabVIEW run begins too — and a range
computed from a clipped trace is a lower bound, which is why the original's plain
formula creeps. Measured against three real signals, counting acquisitions from
that 0.15 V start:

| signal | peak | original formula | geometric growth |
|---|---|---|---|
| archive, 2026-08-07 | 164 mV | 5 | 2 |
| reports 7–8 | 127 mV | 4 | 2 |
| report 9, original generator settings | 336 mV | 8 | 3 |

Note the middle column against the old `passes = 4` ceiling: on the archive's own
signal that would have given up while still clipping. It is 6 now, and with
geometric growth even the largest of the three needs 3.

If the report ends with `still clipping`, every charge from that run is an
underestimate — start the channel wider.

A range that is already right is **left alone** (2 % deadband). That saves a
`:CHAN2:RANG?` per step, which is not free: report 9 timed it at 145, 149 and
135 ms, because the query blocks while the front end settles. It also keeps
every loop of a run on one quantisation instead of a hundred slightly different
ones.

A separate `raw waveform transfer` check reads `:WAV:DATA?` byte by byte and
reports the block header, the declared payload length and the actual one. That
is there because the first acquisition attempt got a preamble promising 4000
points and then an empty array; the probe distinguishes an indefinite-length
block from a truncated transfer from something else entirely.

## Optional — what the DIO lines do

```
python -m bace.bench --dio
```

**Leave the sample connected and the LED on.** With no device there is nothing
to tell the states apart.

**Answered on 2026-09-01. `rig.toml` was right, and it is now measured rather
than inferred:**

```
module 0=0, 1=0 : V(I=0) -0.0360 V  I(V=0) -1.09e-11 A  I(+0.5V) +4.75e-11 A
module 0=0, 1=1 : V(I=0) +0.0001 V  I(V=0) +5.54e-11 A  I(+0.5V) +4.76e-07 A
module 0=1, 1=0 : V(I=0) +0.0109 V  I(V=0) -1.70e-11 A  I(+0.5V) +4.76e-11 A
module 0=1, 1=1 : V(I=0) +0.9222 V  I(V=0) -1.13e-04 A  I(+0.5V) -7.78e-05 A
```

**Module 0 is the shutter** (1 = open): flipping it keeps the device conducting
but loses the photocurrent. **Module 1 is the path switch** (1 = Keithley):
flipping it drops conduction to the open-input floor. The two open-circuit
readings, 4.7513e-11 and 4.7638e-11 A, agree to 0.26 % — that is the SMU reading
itself, with nothing attached — and the nearest connected reading is four decades
above. Not a close call.

Before that, the evidence was: `shutter_lv2012.vi` hard-codes `module nr = 0`
(good), plus my reading of `open/close shutter 2` in `BACE_Mehrdad.vi` as the
relay (a guess — the binary never calls it one). `routing.Relay` guards module 1
as the path switch, so that guess was load-bearing. It is now a measurement.

Re-run it after any rewiring; it takes about a minute.

Listening to a click settles none of that — it does not say whether a line
switched an optical path or an electrical one. The Keithley does. This walks all
four states of the two lines and takes three readings in each:

| state | V at I = 0 | I at V = 0 | I at +0.5 V |
|---|---|---|---|
| connected, light | V_oc (~0.9 V) | **J_sc** | conducts |
| connected, dark | ~0 V | ~0 | conducts |
| not connected | undefined | ~0 | leakage floor |

**Photocurrent** marks the one state where the device is on the Keithley *and*
light reaches it (Huotian's rule). That fixes the values of both lines but not
which line is which — both are set the same way there, so the pair is
symmetric. **Forward conduction** breaks the symmetry: flipping one line at a
time from the photocurrent state, the flip that stops conduction moved the
electrical path, and the flip that keeps it but loses the photocurrent moved the
light. If `rig.toml` has them the wrong way up the report says so in as many
words — that error would open the relay on every step where a run meant to open
the shutter.

> **The first version used the SMU's voltage compliance as the disconnect
> signature, and it was wrong.** Sourcing 0 A into an open circuit is a
> degenerate loop — 0 A flows at any voltage — so the SMU settles wherever it
> likes rather than railing. The 2026-09-01 run proved it: 0.0082 V and
> 0.0682 V in the two disconnected states, nowhere near the 2 V limit. A small
> forward bias is decisive instead, and does not depend on the light.

Nothing meaningful is forced into the device: 0 A for one reading, 0 V for the
next, and +0.5 V for the probe — below the ~0.92 V V_oc this cell shows, so it
is a point on its own J–V curve, under the recipe's compliance. The output is
enabled only for each reading. Both generator outputs are
off for the whole stage and put back at the end, and each DIO line is returned
to the position it was found in. If a line's position cannot be read back, the
outputs are left OFF and the report says so — re-enabling a source into a path
that may have moved underneath it is the hazard the interlock exists for.
`--leave-outputs-off` skips the restore entirely; `--listen` adds the
what-did-you-hear prompts when someone is in the room.

## Optional — outputs, with nothing connected

```
python -m bace.bench --outputs
```

**Disconnect the sample first.** Enables the 81150A and the Keithley at 0 V,
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
prompts for a two-loop run — which is what makes this possible on 64-bit Python
where `delib.dll` cannot load.

The report carries the light, dark and photocurrent traces downsampled to ~400
points each, the charge per loop, where the peak sits in the record relative to
`t0_int`, and the tail noise. It also writes a normal run folder under
`bench-reports\first-light\`, so the result opens in exactly the same analysis
as any other measurement.

**Set `--vcoll` and `--vpre` to values you would normally use on that sample.**
The defaults are the archive's, not yours.

## What to send back

`bench-reports\bench_<stamp>.json`. If `--measure` ran, the run folder under
`bench-reports\first-light\` too.

## If something goes wrong

Every check is caught, so one broken instrument never ends the session — the
report just records the traceback and continues. A half-complete report from a
rig with one dead instrument is far more useful than no report, so if a pass
errors, send the `.json` anyway.
