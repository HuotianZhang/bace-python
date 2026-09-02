# BACE port — hand-off

Converting `BACE_Mehrdad.vi` (LabVIEW, 907 VIs) to Python. The measurement half
is written, tested and proven on the rig; the service over it is built and
proven on the simulator. The UI is not started.

Read `docs/bace-status.html` first — it is the full narrative with the evidence.
This file is the operational summary.

---

## 2026-09-02 — 端口复现了 LabVIEW 引擎

**当前状态见 `HANDOVER-2026-09-02.md`.** 相隔八分钟的两轮对照（LabVIEW 02:16
vs 端口 02:08）：峰值差 6.0%、τ 差 2.8%、Q 差 4.0%，尖峰都在记录的 328 ns。
测试 212 passed / 7 skipped。

下一程第一件事：`rig.toml` 加 `current_sign = -1`，乘在 `Infiniium._fetch`，
同时进协议和模拟器。端口全正、LabVIEW 全负，是取号约定，不是物理。

**2026-09-02，稍后：** 上面这件事做了，`docs/service-plan.md` 里的 P0 清理
全部完成——`current_sign = -1` 进了 `rig.toml`，只在 `Infiniium._fetch` 乘一次，
协议和模拟器同步；事件信封（`Envelope`、`Progress.node_path`、服务层的事件词汇）
定在 `experiment/events.py`；`configure_trigger` 进了 `BiasSource` 协议，
`run_transient_scan` 自己设；`LedSource` 协议；`core/sequence.py` 删了；`run_jv`
自己开关快门（亮曲线开、暗曲线关）。`bace/service/` 已建，在模拟器上跑通了
整条路（bench 读回与动作、模块目录、单模块 run、pipeline 树的校验/试跑/执行、
两个停法、journal），还没上过真台子：怎么跑见 `bace/service/README.md`，契约见
`docs/service-contract.md`。测试 528 passed / 7 skipped。

以下内容写于 2026-09-01 之前，其中 `timebase 500`、`trigger_offset 47 ns`、
「光根本没关」三条已被数据推翻，以 `HANDOVER-2026-09-02.md` 为准。

---

## 1. Where everything is

| path | what |
|---|---|
| `Q:\Huotian\bace-python` | **the copy the lab PC runs.** `bench-reports/` lives only here |
| `D:\BACE\bace-python` | the same tree on sternwarte, beside the source material |
| `D:\BACE\_decompiled` | recovered block diagrams, constants, SCPI, MathScript |
| `D:\BACE\instr.lib`, `D:\BACE\BACE_Mehrdad Folder` | the original VIs |
| `D:\BACE\DELIB` | Deditec DELIB (32-bit only; the 64-bit `delib64.dll` was installed separately into `C:\Windows\System32`) |
| `Q:\Huotian\2026\BACE\20260831\220K` | **LabVIEW reference data for the device currently mounted**, 220 K. Not yet connected to a session — ask for folder access |
| `bench-archive/` | a copy of the 2026-08-07 run, used by the numerical regression |

**These two trees are NOT one store.** They looked like it for a while and I said
so; that was wrong. Write to `D:` with `device_commit_files`, then mirror with
`device_bash` (a commit straight to the `Q:` path fails):

```bash
D="$HOME/mnt/BACE/bace-python"; Q="$HOME/mnt/bace-python"
cd "$D"
while IFS= read -r -d '' f; do
  if [ ! -f "$Q/$f" ] || ! cmp -s "$f" "$Q/$f"; then
    mkdir -p "$Q/$(dirname "$f")"; cp "$f" "$Q/$f"; echo "  -> $f"
  fi
done < <(find . -type f -not -path "*__pycache__*" -not -path "./bench-reports/*" -print0)
```

Never touch `bench-reports/` — those are the user's run outputs.

Every bench report carries a **code fingerprint**. Compare it before analysing
anything; if it does not match the local tree, the report came from stale code.

```bash
python3 -c "import sys,os; sys.path.insert(0,'.'); \
from bace.bench.checks import fingerprint; import bace; \
print(fingerprint(os.path.dirname(bace.__file__)))"
```

Reports can be read directly from `Q:\Huotian\bace-python\bench-reports` — the
user does not need to upload them.

---

## 2. What is built

528 tests pass (7 skipped), including a numerical regression against a real
2026-08-07 run (worst relative difference 1.6e-06) and a byte-exact `.dat`
round-trip. Both now run on the lab PC too, against the deployed copy. On
sternwarte one `test_bench` DIO test fails because that machine has
`delib64.dll` installed; that is the environment, not the code.

```
params.py   parameter provenance — default, run.toml, last-used, edited,
            inherited, derived
core/       axis, pulses, process, illumination, simulate
drivers/    protocols (7 contracts), infiniium, agilent81150, agilent33220a,
            keithley2400, newport1918c, shutter, delib, routing, simulated,
            win32bridge
experiment/ events, wire, rig, transient, jv, intensity_series
storage/    numbers, legacy_dat, naming, hdf5, recorder, jv, series
bench/      report, session, checks, __main__
service/    __main__, session, rigs, modules, journal, worker, wire, pipeline,
            executor, monitors, app
```

**`service/`** — FastAPI + WebSocket around the engine, built 2026-09-02 to
`docs/service-contract.md`, itself derived from `docs/service-plan.md` and the
Round 3 console design. One process owns every VISA instrument; one worker
thread is the bench lock, and everything that touches the bus is a job on it.
Every event a run yields is enveloped (`seq`, `ts`, `run_id`, `node_path`),
fanned out over `WS /events` under a payload policy that decimates traces on
the wire and keeps scalars in the journal, and appended to
`<out>/journal/<session>.jsonl`. `/bench` is the cached read-back plus the
explicit by-hand actions (the service never fixes anything on its own);
`/modules` is the catalogue with each parameter's provenance; `/runs` is a
manual run, which is a one-node pipeline on the same code path; `/pipelines`
validates, dry-runs and executes the tree with the three bindings and a
`NeedsOperator` pause at each temperature. Two stop verbs, `after_shot` and
`abort`; every ending parks the bench. It rewrites no measurement logic and
imports `pyvisa` only inside `rigs.py`, so `--sim --fast` runs on a machine
with no VISA backend. `bace/service/README.md` is how to run and drive it. It
has run on the simulator only.

Design rules that are load-bearing:

- `core/` imports nothing outward. Dependencies point inward.
- Instruments are `typing.Protocol` contracts, so the simulated rig satisfies the
  same interface as the real one. Every module runs end to end on the simulator.
- The run is a **synchronous generator** yielding typed events. Not async: VISA
  blocks and its sessions are not thread-safe. The service adapts at its edge.
- Storage reproduces the legacy `.dat` byte for byte — CRLF, tabs, `%10.5e` with
  an unpadded exponent, `NaN` right-aligned in 10 characters — and writes HDF5
  alongside. An HDF5 failure never costs the `.dat` files.

---

## 3. What is NOT built

**`ui/`** — browser front end. **Explicit instruction from the user, 2026-09-01:**

> *"I do not want the same shape as JV console and the 331 controller. I want a
> suitable shape for this measurement itself (the ui should be demonstrated and
> modified by Claude Design)."*

So: do not copy the existing consoles. Design for what a BACE run actually is —
a scan over a bias axis, repeated in loops, each point producing a light trace, a
dark trace, their difference and one charge. Draft it in **Claude Design** and
iterate with the user there. The Round 3 canvas (`docs/bace-console-round3.html`,
four tabs: bench, pipeline, results, rig) is where that stands, and the service
was built to serve it; `python -m bace.service --ui DIR` serves what gets built.
The results tab is not designed on either side.

**Temperature** — wired since this handover was written (superseded here by
`docs/service-contract.md` section 7). The Lake Shore 331 is at
`GPIB0::7::INSTR` with its own console on `127.0.0.1:8331`, and
`bace/drivers/lakeshore331.py` is an HTTP client to that console, never a
second GPIB session. `RigConfig.temperature_console` names it (empty in the
default `rig.toml`); named and answering at Start, a temperature node settles
through it, otherwise the executor pauses with `NeedsOperator` as before. No
endpoint changed. `temperature_k` is a metadata field. Temperature is **not** an `Axis` —
it settles in minutes, the axis quantities are per-shot and set in nanoseconds.
It is an outer loop of the pipeline tree beside the LED level.

---

## 4. The rig, as measured

| instrument | address | notes |
|---|---|---|
| Infiniium DSO9054H | `TCPIP0::PwM-DSO9054H.local::inst0::INSTR` | LAN, not GPIB |
| Agilent 81150A | `GPIB0::12::INSTR` | collection field, through a ×4 amplifier |
| Keithley 2400 | `GPIB0::24::INSTR` | DC side of the relay |
| Agilent 33220A | `GPIB0::15::INSTR` | LED drive |
| Lake Shore 331 | `GPIB0::7::INSTR` | through its console on `127.0.0.1:8331` (contract section 7); never opened directly |
| Deditec DIO | module ID 9, channel 0 | module 0 = shutter, module 1 = relay |

**Record geometry**, stable across every session: 4000 points, dt 0.5 ns,
t0 −199.5 ns, the trigger 199.5 ns into the record, `t0_int` 118.5 ns after it.
`:TIM:POS` is the screen **centre**. `:ACQ:POIN` is a **request** — the record is
window × sample rate, which is where the archive's 4000 comes from, not 5000.

**DIO identity, measured 2026-09-01** (not inferred — see §6):
module 0 = shutter, 1 = open. Module 1 = path switch, 1 = Keithley.

---

## 5. Bench harness

`python -m bace.bench` plus opt-in stages. Every SCPI string is sent once and the
error queue read immediately after, so a report is an exact list of what each
instrument accepted. Four `.bat` files sit beside the package.

| stage | what it does |
|---|---|
| offline, discover, local | always. Package, simulated runs, regression, `*IDN?`, DELIB, consoles |
| `--read` | read back instrument state |
| `--configure` | send configuration. **Skips any instrument whose output is already ON** |
| `--acquire` | one scope acquisition |
| `--dio` | identify the two DIO lines by measurement |
| `--outputs` | enable outputs with **no sample connected** |
| `--measure` | one real transient |

`--dio` and `--measure` are the ones that move hardware. `--dio` turns both
outputs off itself, **reads them back to prove they went off**, works, then
restores exactly what was on; if a DIO line's position cannot be read back it
leaves the outputs OFF rather than re-enabling into a path that may have moved.

---

## 6. Defects the hardware found that simulation could not

Nine, and they are the reason the harness exists. Each is documented at its site.

1. **Auto-range computed the range from amps, not volts** — would have set
   `:CHAN2:RANG` 5.192× too small and clipped every light trace.
2. **Phantom error queue** — the no-error prefix list did not match the
   DSO9054H's bare `0`; 28 false rejections in report 1.
3. **Configure changed levels on a LIVE output.** Now reads `:OUTP?` from the
   instrument, not from the driver's own flag.
4. **The empty fetch was not a block-header problem** (two wrong diagnoses
   first) — it was fetching while the scope was still running. `:STOP` first.
5. **`:ADER?` is a latch cleared on read**, and answered `+1` ~1 ms after `:RUN`.
6. **Auto-range was single-pass**, then capped at 4 — both too few. See §7.
7. **The scope rounds `:CHAN2:RANG?` to three significant figures.** A readback
   added for accuracy was degrading it, and an equality-based ceiling test would
   have read display rounding as a refusal.
8. **That readback costs 145 ms** (timed on the rig). Removing it was wrong — the
   clip test is computed from range and offset, so a clamped scope would leave
   the driver testing a window that does not exist. Fix was to *write* less: a
   2 % deadband, so a correct range is left alone.
9. **`--dio` bypassed the relay interlock.** `routing.Relay` refuses to move the
   relay while a source drives; `stage_dio` drove the DIO line directly and never
   went through that class.
10. **`current_sign = -1` broke the trigger calibration.** `calibrate_trigger`
    took `max()` of the CHAN3 trace as `acquire` returns it -- amps, in the
    rig's sign convention -- so the day the sign flipped, the positive sync
    became a negative pulse whose maximum is the baseline: the first service
    run on the rig (2026-09-02, session 103857) set a 0.25 mV threshold, and
    with `trigger_sweep = AUTO` the scope averaged twenty untriggered records
    into a flat trace and Q = 1e-12 C. The calibration now works in scope
    volts, and a channel swinging under 0.1 V refuses to run under AUTO
    (`transient.SyncError`) rather than measure noise. The 1/R factor had
    been there all along, unnoticed because 0.115 V still triggers a 1.2 V
    sync.

---

## 7. Deliberate deviations from the original

Each is a decision, not an accident, and each is documented where it lives.

- **Geometric auto-range.** While a trace is clipped its max and min carry no
  information about how far past the rail the signal went, so the window grows
  ×1.8 anchored on whichever edge did not clip; the original formula is applied
  once, to the first acquisition that fits. Acquisitions from the 0.15 V start
  (a hard constant inside `scale to maximum.vi`): archive signal 5 → 2,
  reports 7–8 4 → 2, report 9 8 → 3.
- **Compliance before output enable** on the Keithley. The original enabled the
  output first, leaving the SMU at whatever `*RST` had set.
- **`:SENS:VOLT:PROT:LEV`** for the V_oc branch, where the original set the
  current protection.
- **`FUNC:PULS:HOLD DCYC`** before `:FREQ` on the 33220A, to avoid a real
  `-221 Settings conflict`.
- **A 2 % deadband** on the auto-range, so a correct range is not rewritten.

---

## 8. Open questions

**None of these blocks the service or the UI.**

1. **Pulse width.** `run.toml` has 5 µs against a 2 µs record, so the record only
   ever sees the rising edge. Compare against `Q:\Huotian\2026\BACE\20260831\220K`
   — same device, LabVIEW — to see what the original actually used.
2. **J_sc settle time.** It moved 2.14× between two runs ten minutes apart while
   V_oc moved 3.5 mV. An intensity change of that size would move V_oc ~20 mV
   (n = 1, 300 K), so the light did not change: either the device drifts or
   500 ms is not enough. The intensity series reads J_sc at every level.
3. **`trigger_sweep`.** `AUTO` is the original's, and what the rig still reads.
   It free-runs when no trigger arrives — a dead sync gives a flat trace, not an
   error. `TRIG` would fail loudly. Undecided.
4. **Why three prebias points** rather than three repeats at V_oc. Mechanism
   settled, intent not. With the axis a parameter this is a `run.toml` choice.
5. **The intensity calibration `Factor`** — a recovered panel constant of unknown
   provenance. Scales logged intensity and nothing else.
6. **How many iterations `scale to maximum.vi`'s FOR loop runs.** At least five,
   measured from the archive; the binary's count terminal came back unwired.

### Closed, so nobody re-opens them

- **`New Sample?`** — MathScript node 9 swaps and negates the two levels, and
  nothing else. Also drives `Configure Output Polarity.vi`, which is why the two
  looked like they might cancel.
- **`positive jSC?`** — dead. The label is on every front panel; the string
  appears in no block diagram.
- **The power meter is on a beam splitter**, so intensity can be read during a
  run. No bridge needed: it goes through the existing 1918-C console over HTTP.
- **Output polarity.** `:OUTP1:POL INV` was proposed, tested on the rig and
  **rejected** — it moves the transient to 68.5 ns, outside the integration
  window. NORM stands.
- **The 2026-08-07 archive's Q sign.** Not a discrepancy: **that is a different
  device.** Do not try to reconcile it. It remains valid as a *numerical*
  regression (same traces in, same numbers out) and nothing more.

---

## 9. Corrections I had to make to my own claims

Kept because the reasoning failed the same way twice, and a successor is likely
to reach for the same shortcuts.

- **"Q: and D: are one store."** Inferred from a 62-second timing coincidence.
  They are not. Verify, do not infer.
- **"The tail improving proves the clipping was distorting the baseline."**
  One sample. The next run undercut it. Shot-to-shot tail scatter is
  ±1.3e-5 A at 16 averages.
- **"Run the 32-bit helper with `py -3.11-32`."** A command nobody had run, on an
  interpreter that does not exist there, missing its required `--delib`.
- **"There is no 64-bit DELIB."** The 64-bit build ships as `delib64.dll`. The
  absence of a 64-bit `delib.dll` proved nothing.
- **"An open circuit rails at the voltage compliance."** It does not — sourcing
  0 A into an open circuit is a degenerate loop. A small forward bias is the
  decisive probe.
- **A test that mocked the driver hid a real crash** (`RecordingResource(res, c)`
  where the second argument is a list). Mocking the thing that constructs the
  defect is how it reached the bench. The tests now wire the real driver and the
  real recording layer against a fake pyvisa resource.

---

## 10. Suggested order

1. **`service/`** — done, 2026-09-02, on the simulator. The first thing on the
   lab PC is `scripts\Run Service.bat`, a `jv_dark` from `curl`, and a read of
   `/bench` against what the instruments actually say
   (`bace/service/README.md`, "Driving it by hand").
2. **`ui/`** — in Claude Design, shaped for this measurement. See §3. The
   contract it talks to is `docs/service-contract.md`; `--ui DIR` serves it.
3. **Side-by-side** — same device, same settings, LabVIEW and Python back to
   back, against `Q:\Huotian\2026\BACE\20260831\220K`. Agreement on Q within
   noise is the acceptance test.
4. **Temperature** — done since (contract section 7): the 331 console is a
   `TemperatureController` on the rig, a temperature node settles through it
   when `RigConfig.temperature_console` names it and it answers, and pauses
   for the operator otherwise. What is left is the first run against the real
   console on the rig.
