# Documents

Local copies of the figures written during the port. Each was also published as
an artifact on claude.ai; these are the same files, kept here so the project is
self-contained and readable with no account and no network.

Open them in a browser. They are single files with no external dependencies
beyond a web font, and they fall back cleanly without it.

## Start here

| file | what it is |
|---|---|
| `../HANDOVER-2026-09-02.md` | **Start here.** The closing summary: what was proved, what was fixed, what is still open, and how to run it. |
| `bace-wrapup.html` | 《BACE 移植收官》 — the same summary as a figure page, with the four runs charted. |
| `bace-drift.html` | 《参考本身在漂》 — the evidence that the port reproduces the LabVIEW engine, and that the earlier 4× gap was the sample drifting. |

## The rest, by subject

| file | what it is |
|---|---|
| `bace-timing.html` | 《BACE 触发链时序》 — the trigger chain in time: five edges, the 502 ns optical path, and where the delay axis' zero really is. |
| `bace-polarity.html` | 《四种极性组合的瞬态》 — the four combinations of `output_polarity` × `invert_polarity`, and why only ④ is valid. |
| `bace-polarity-raw.html` | 《四种组合的原始亮暗电流》 — the same four, as raw light and dark traces rather than differences. |
| `bace-delay-scan-1.html` | 《第一次 delay 扫描》 — the first attempt to calibrate the zero of the delay axis. |
| `bace-delay-scan-2.html` | 《第二次 delay 扫描》 — the second, and the baseline-correction trap it exposed. |
| `bace-chain.html` | The instrument chain — what is wired to what, and which command reaches which box. |
| `bace-anatomy.html` | What one BACE run actually does, step by step. |
| `bace-architecture.html` | The module layout and why the dependencies point the way they do. |

## Historical — accurate for their date, not for today

| file | what it is |
|---|---|
| `bace-status.html` | The running record of the port **through 31 August 2026**. Carries a 2026-09-02 banner listing what has since changed. |
| `port-plan.html` | The original plan, written before any of it was built. Kept for the reasoning. It is where the 47 ns error started; that paragraph is corrected in place. |

## The UI design canvas

| file | what it is |
|---|---|
| `ui-rules.md` | What a mockup cannot show and a contract has no place for: significant figures and the two zeros that mean *not recorded* and *not calibrated*, colour with a job, the three time scales and why “step 412 of 8400” is useless, provenance and inheritance on screen, the ten segments of one shot, the failures that look like results, and the questions the design has already answered. Distilled from the design pack, which is now historical. |
| `ux-screening.md` | 《筛查：测试流程中不够人性化的设计》 — the measurement flow walked end to end and screened for what it asks of the *person* driving it, 2026-09-05. The rule it turned up: everything that costs the sample something arms and confirms, and nothing that cost the operator something did. Four findings fixed (the window title, so a run that has stopped and is waiting can reach somebody who is not looking; an undo for the pipeline tree; an arming Save that would overwrite a recipe; a dismissable strip), two left with what they would take (naming the device from the console, which needs a route and a contract line; what a run that ends *well* should say on screen). |
| `ui-plan.md` | How `ui/` gets built and in what order: the four structural decisions (generated forms, the screen as a projection of the event stream, six chart components on one scale module, four fixtures), then M0–M6 cut vertically. `../ui/README.md` says what is built. |
| `bace-console-round3.html` | 《BACE console — Round 3 · B》 — the console mockup: home is the five modules as cards, each card parameters + Run + result; the bench rail across the top; the pipeline composed from the cards as they stand; the rig diagram on its own tab. Three turns — cold bench, the same bench mid-check, and the rig tab. **Self-contained**: all 33 assets it needs (React, the stylesheet, the web font) are inlined, so it opens from disk with no network and no account. This is the current version — its chain-timing lane carries the corrected light path, 502 ns with delay zero at 0, and (2026-09-02) its 81150A `:OUTP1:POL` reads **NORM** in all four artboards, where the mockup had it INV from the overturned ④ below. The bace card's second polarity row is now `output_polarity` → NORM: NORM/INV is `output_polarity`'s pair, while `inverted_output` is a boolean. |
| `design/` | The Claude Design project the mockups were made in, exported whole and openable from a local server: the three rounds’ canvas sources, `BenchRail.dc.html`, the icon set, the design system — and `bace-charts*.js`, which is where the chart decisions actually live, as framework-free custom elements with the measured numbers baked in. **Round 2’s R2·3 is the only results design that exists**; Round 1 is archived uncorrected. `design/README.md` says what each file is and what was corrected. |

## The service layer

Markdown, not figure pages. Read in this order.

| file | what it is |
|---|---|
| `service-plan.md` | 《BACE 服务层规划》 — why the service is a thin shell around the engine: one process, one worker thread as the bench lock, the API by tab, the three pipeline bindings, `NeedsOperator` for a cryostat that is not wired, the verdict rule, and the P0 core cleanups to make first. Chinese. |
| `service-contract.md` | The contract `bace/service/` was built to, derived from the plan and the Round 3 canvas: package layout, the concurrency and stop semantics, every endpoint's shape, the wire and journal payload policy, the tree schema, the check catalogue and the cost model. Where it and the plan disagree the plan wins; where it and the code disagree, fix one and say so there. |
| `../bace/service/README.md` | How to run the service and drive it by hand with `curl`, `httpx` and a WebSocket client. |
| `ui-kickoff.md` | The brief for the session that builds `ui/`: the design inputs, the dev backend (`--sim --fast`), what the service guarantees the UI, and the known gaps. Start there — then `ui-rules.md` and `design/`. |

## What has been overturned

Every page below was written in good faith and then contradicted by
measurement. Each carries a dated correction where the claim appears, but the
list is here too, so nobody has to discover it twice:

| claim | where it was | what is true |
|---|---|---|
| trigger offset = 47 ns | `port-plan.html`, and from there everywhere | **0** on the scan path. At the same `Delay(ns) = 90`, LabVIEW and the port both put the displacement spike at 328 ns of record time. |
| `timebase_ns_per_div` = 500 | the 2026-09-01 backlog | **200**. On the panel, Timebase 200 ns and Pulse Width 5e3 ns sit side by side; they are unrelated. |
| the LED never goes dark | `bace-delay-scan-1.html`, `bace-delay-scan-2.html`, `bace-polarity.html` | It does. The power meter reads its off level at **0.009 %** of the on level. |
| the device may be damaged | `bace-polarity.html` | It is not. V_oc 1.02771 V, J_sc −126.127 A/m², FF 0.5505. |
| light and dark sharing an extreme means clipping | `bace-delay-scan-1.html`, `bace-polarity.html` | They should share it. Clipping shows as 7–8 consecutive samples at one float inside a single averaged trace. |
| the port is 3× low against LabVIEW | working notes, 2026-09-01 | The reference moved. LabVIEW's own τ fell from 190 ns to 71 ns over three hours; against a run eight minutes away the port agrees to 4 % on charge. |
| `Q:` and `D:` are one store over the network | `bace-status.html` (already withdrawn there) | Two separate trees. Mirror with `D:\BACE\sync-bace.sh` and verify by fingerprint. |
| combination ④ means `invert_polarity` + `:OUTP1:POL INV` | `recipes/run-labview.toml`, `recipes/run-bace.toml` (the "④" comments), `bace-polarity.html`'s labels as read into the recipes | The validated pair is `invert_polarity = true` with **NORM**: `bare.py --invert` matched LabVIEW to 4 % at 02:08 with the generator read back at NORM, and the 02:20 bit-exact read-back shows LabVIEW itself finishes at NORM. Through the inverting ×4 amplifier, INV rests the device at v_coll so extraction never stops: every INV run (the four replicas of 23:41–01:22 and the service run of 2026-09-02 14:52) shows the photo peak collapsed from ~3 mA to ~0.5 mA and the displacement spike with the opposite sign. |

## The criterion, since it was got wrong repeatedly

A BACE curve is usable when **light minus dark returns to zero in the steady
state** — not when V_oc lands somewhere expected. A non-zero plateau disqualifies
the curve however plausible the charge looks.
