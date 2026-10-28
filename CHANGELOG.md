# Changelog

Dated, newest first. Details and evidence are in `docs/`.

## 2026-10-28

- The assistant answers with any OpenAI-compatible model as well as Claude:
  OpenAI, DeepSeek, Qwen, Kimi, GLM, or a model on this PC through Ollama,
  which needs no key. Provider, address and model are chosen in the panel's
  settings; `openai` 3.40 joins the `service` extra.

- **The console's assistant.** The `ask` chip in the bar opens a panel where
  the physics, the chain, a parameter's value and the console's controls can
  be asked about in plain language. A Claude model reads the bench, the
  modules with each value's layer, the runs, the event ring and the project's
  documents, and in this version changes nothing. It runs on the asker's own
  Anthropic API key, which the service does not keep. `anthropic` 2.3 joins
  the `service` extra; without it the console works as before.
- `docs/bace-principle.md`: what BACE measures, from the papers that
  introduced it. The 2026-09-06 split of Q into photogenerated and injected
  parts is withdrawn; at V_oc the two are not additive.

## 2026-10-27

- **The record was too short and the baseline read part of the signal.** At
  200 ns/div the record ended while Q was still rising, and the last-10 %
  offset subtracted the slow tail: Q read 10–20 % low. `run.toml` now takes
  1 µs/div, a 6 µs window and a 10 µs pulse, and the offset comes from the
  record before the trigger (`offset_region = "pre-trigger"`). Evidence in
  `docs/analysis/2026-10-26/scope-window/`.
- `alternate_order`: the dark trace first on even loops, so drift between the
  two acquisitions cancels over loop pairs. That pair is taken in the vertical
  window the light trace set at the same point a loop earlier, so a light
  trace larger than the dark one does not clip.
- Settle times 0.5 / 0.5 / 1 s instead of 2 / 2 / 5 s: the same Q, 6.3 s a
  shot instead of 17.3, and half the scatter per loop.
- Every bace run writes the 33220A `:OUTP:POL` itself before it pulses.
- The 2400 reads again once when GPIB garbles a reply.
- Per-shot diagnostics in the file: the pre-trigger levels of both traces,
  the light − dark offset after the window, and which trace came first.
- The run summary is Q in nC with the standard error of the mean.

## 2026-09-09

- `introduction.html` at the repository root: how to run it and how the code
  is arranged, with the rig schematic and the architecture diagrams. A single
  file; opens from disk with the fonts in `ui/fonts/`.

## 2026-09-08

- Repository reorganised for publication: `docs/figures/`, `docs/history/`,
  `docs/notes.md`, `examples/`, `ui/studies/`. The session hand-off file is
  gone; its engineering record is `docs/notes.md`.
- Standalone Keithley 2400 console, `examples/keithley_console/`: one
  instrument, one process, one port, importing nothing from the package. The
  shutter console moved out of the package the same way.
- `scripts/Setup.bat` and `Setup (sim).bat` install on Windows into `py -3`;
  `scripts/setup.sh` builds a `.venv`, runs the suite, and starts the service
  once to prove the run command.
- CI runs the suite on Ubuntu and Windows.

## 2026-09-07

- Console: an undeclared name in the bench view's `dispose` took the router
  down; ESLint `no-undef` now runs over `ui/` in the suite.

## 2026-09-06

- **Scope averager was never emptied between acquisitions.** Every dark trace
  was part light and Q read about half its value. The driver now clears the
  averager, acquires with `:DIG` + `*OPC?`, and checks the folded count against
  the recipe. Runs before this date were taken the old way.
- Console: the rail is one row; the bench can be saved and reopened; the power
  trace folds away without stopping the monitor.

## 2026-09-05

- The integration window is measured from the field's arrival at the device,
  not from the trigger, so a delay scan integrates every point alike.
  `docs/integration-window.md`.
- Sample identity is typed in the console and carried on every run record;
  identity fields are slugged into folder names (a colon in `material` had
  built a path that failed on Windows).
- Temperature monitor beside a run; the console's window title reports a pause.
- Console: pipeline undo, arming Save, results tab (M6).

## 2026-09-04

- Console: pipeline tab (M5), with the schedule drawn as a length of time.

## 2026-09-03

- `jv_dark` split into `jv` (sweeps, touches nothing else) and `light` (owns
  the shutter and LED). Current density is mA/cm² everywhere; HDF5 `bace-jv/4`.
- The 1918-C and Lake Shore 331 drivers moved into the package; the service
  opens both itself.
- Sync-to-field latency measured: 47.1 ns (49-point delay scan).

## 2026-09-02

- The port reproduces the LabVIEW engine: two runs on the same device eight
  minutes apart agree on peak to 6.0 %, τ to 2.8 %, Q to 4.0 %.
- `current_sign = -1` in `rig.toml`, applied once in the digitiser fetch.
- `bace/service/` built: one process owning the bench, HTTP + WebSocket, the
  pipeline tree with validation, dry run and execution, an append-only journal.
  Run end to end on the lab PC the same evening; the trigger calibration was
  found circular and rebuilt.
- Output polarity `INV` tested on the rig and rejected; `NORM` stands.

## 2026-09-01

- First bench sessions. DIO line identity measured rather than inferred. Nine
  defects found by hardware and fixed; `docs/notes.md`.

## 2026-08-31

- Rig constants confirmed on the bench and recorded in `rig.toml`.

## 2026-08-07

- The reference run that `tests/test_regression_20260807.py` reproduces
  (`bench-archive/`).
