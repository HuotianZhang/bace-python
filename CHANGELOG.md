# Changelog

Dated, newest first. Details and evidence are in `docs/`.

## 2026-10-28

- **The light source is a 1 W, 520 nm laser diode** (Nichia NDG7475), driven
  through the ×4 light amplifier; the code keeps calling it the LED. The
  1918-C now reads at its wavelength, 520 nm, by default (`rig.toml`, and the
  `power` module's field for another source). Intensities recorded at the
  earlier 530 nm setting are low by one constant factor, the ratio of the
  detector's responsivities at the two wavelengths.
- **R_s = 5.192 is the current chain's conversion factor**, set by the
  sample-holder amplifier, not a resistor in series with the device: the
  I_d cable reaches CHAN2 through a 50 Ω feed-through, with CHAN2 at 1 MΩ.
  Q is unchanged; the manual and `rig.toml`'s comments are corrected.
- The manual takes in the group's training material (the BACE Quick Guide,
  the TDCF guides and the training video, read frame by frame and
  transcribed): the parts the software does not address, the limits, the
  checks before a measurement, troubleshooting, and seven figures of the
  wiring and the bench. `tools/build_manual.py` embeds figures in the book.

- **The whole repository's prose follows `docs/style.md`**: the comments
  and docstrings of every package, the console's comments and stylesheet,
  the configuration files and the current documents. No history, units and
  reasons kept, measured values cited to the document that holds them. The
  first sentence of each parameter's docstring is its help line in the
  console and in manual 4.1. Rewriting the documents against the code
  corrected 39 statements that no longer held. `tools/check_text_only.py`
  confirms that a rewrite changed only text: Python by its syntax tree,
  TOML by its tables, JavaScript by its tokens, CSS by its rules.
  `docs/notes.md`, `docs/ui-plan.md`, `history/`, `analysis/` and `tests/`
  keep their dates as records.
- A dark `jv` on a bench with no shutter or LED runs, with a warning that
  its illumination is unknown; it was refused.
- `python -m bace.service` reports a missing or malformed `--rig`/`--run`
  before it takes the port, so a port held by another program no longer
  hides the configuration error.

- The manual's Part IV: every parameter, every check and every bench action,
  generated from the code by `tools/build_manual.py`, which also builds the
  whole manual as one printable HTML file (`--book`). The checks and
  actions carry their description, level and remedy in the code
  (`CHECK_DOCS`, `SHOT_CHECKS`, `ACTION_DOCS`); `tests/test_manual.py`
  fails when the chapters fall behind.

- The manual's Part III, maintenance: the layers and one run end to end,
  each step of a shot from physics to SCPI to code, the files the bench
  writes, how to add a module, an instrument, a check or an assistant
  tool, and the tests.
- The assistant reads the manual, chapter by chapter, and starts from it.
  Its instructions named a 50 Ω current resistor; the bench's is 5.192 Ω.

- The manual's Part II, principle: what the extracted charge is, the
  transient and its timing, the measurement chain with an uncertainty
  budget, and the tests that show Q complete and n a bulk density. The
  glossary gains the terms they use.

- **`BACE.pyw`, one double-click.** It starts the service without a
  terminal, logging to `runs/logs/`, and opens the console in a window of its
  own; a service already running is reused. Closing the console leaves the
  service and its run alone; `shut down` in the bar parks the bench and ends
  it (`POST /service/shutdown`). A second service on the port now exits
  before it opens an instrument: on Windows the port was bound with
  `SO_REUSEADDR`, which let two services share it.

- The manual's Part I, operation: measurement overview, the bench, a first
  measurement, results, troubleshooting.
- `Run Service.bat` and `Run Service (sim).bat` serve the console
  (`--ui ui`); before, they started the service without it. Their notes no
  longer tell the operator to start the 1918-C and 331 consoles, which the
  service now opens itself.

- `docs/style.md`, the rules every text here follows, and the outline of the
  bench manual in `docs/manual/`: operation, principle, maintenance,
  reference, with a glossary that fixes one name, symbol and unit per term.

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
