# 筛查：测试流程中不够人性化的设计

The measurement flow, walked end to end and screened for what it asks of the
person driving it — 2026-09-05, against `b8975ab`.

Everything here is about the **operator's** experience of a run, not the
console's rendering, its performance or its correctness: those have their own
documents (`docs/ui-rules.md` for the register, `docs/ui-plan.md` for the
milestones, `docs/service-contract.md` for every shape). What this file is for
is the other axis — where the design, having been careful about the sample and
the instruments, was careless about the human being.

The flow screened is the one the console actually offers:

```
name the device → read the bench, fix what the chain says → fire one module
→ compose a tree → dry run → Start → watch it → answer its pauses
→ stop it or let it end → find the run again in results
```

---

## The rule this was judged against

`ui-rules` already says what the screen owes the operator. Reading the code
against it turned up one thing the rules do not state, and it is what most of
the findings below are instances of:

> **Everything that costs the *sample* something arms and confirms. Nothing
> that costs the *operator* something did.**

Park on a busy bench arms (`app.js: park`), Abort arms (`app.js: abortRun`),
Run and Start are guarded against the double-click that would start two
experiments (`views/bench.js: submitting`, `views/pipeline.js: submitting`),
and the whole of `views/bench.js`'s edit chain exists so that a click cannot
drive the LED at a level nobody chose. That care is right and it should stay.

It just stopped at the edge of the person. The pipeline tree — the one thing on
screen that is composed by hand, and the only thing the service holds no layer
underneath — could be destroyed in one click three different ways with nothing
offering it back; the only file this console writes was overwritten without
being mentioned; a run that had stopped and was waiting for somebody had no way
at all to reach the somebody; and the console said `no sample named` in the top
bar while giving nobody any way to name it.

That asymmetry is backwards. The sample is expensive and *not* recoverable, so
it is worth arming. The operator's work is cheap and *is* recoverable — which
is precisely why it was worth giving back rather than protecting.

---

## Fixed

### 1 · The rig waits for a person who has no way to know

**The worst of them, and the cheapest to answer.**

A temperature step is 14 minutes to 2 hours; a nine-temperature sweep measured
4.5 hours, of which the measuring was a small fraction (`ui-rules` §5). Where
the 331 is not on the bus, **every one of those steps stops and waits for a
person** — the executor pauses with `NeedsOperator` and holds until somebody
answers (contract §7).

The console says so, correctly and in detail, on the monitor under the rail:
what to set, to what tolerance, how long it will hold, what the cryostat is
reading while you decide, and what a blank resume will bind (`lib/monitor.js:
pausePrompt`). All of it inside a window the operator is by construction **not
watching**, because the thing they are waiting for takes half an hour. Nothing
reached the taskbar, the tab strip or the window switcher: `document.title` was
the string in `index.html` and never moved, and there is no other channel in
the code — no notification, no sound, no favicon change.

So the gap between the rig stopping and somebody noticing was however long it
took them to look, on a sample that is cold and drifting, inside a run whose
whole cost is the waiting.

**Now:** `ui/lib/title.js`. The window title carries what the run needs.

| | |
|---|---|
| `⏸ needs you · set 250.0 K — BACE console` | a pause, until it is answered |
| `⏵ bace · T 250 K · 1 of 9 — BACE console` | running |
| `✓ done · jv — BACE console`, `⏹ stopped · bace · 39/60`, `✕ failed · bace` | it ended while nobody was looking |

Three decisions inside that:

- **`document.title` alone.** It needs no permission, works behind another
  window, survives a minimised console, and costs nothing. The Notification API
  would ask for a grant, and a lab PC that has denied it once denies it
  silently for good.
- **The outermost loop counter and no faster one.** `ui-rules` §5's three
  scales are the monitor's job, on the screen; a taskbar entry that rewrote
  itself once a shot would be noise, not a glance. The title takes the one
  counter whose iteration is measured in hours.
- **An ending is announced only while nobody has seen it.** A pause is a
  standing fact and says so with the window wide open; an ending is a fact
  about the past — it should be waiting when they come back, and gone once they
  have. The shell latches it on `blur`/`visibilitychange` and drops it on
  focus (`app.js: drawTitle`, `seen`).

The model is `monitorModel`'s, re-rendered — never a second opinion about what
the run is doing, so the title cannot disagree with the monitor.
`ui/tests/title.test.mjs` holds it against the same recordings the monitor is
held against, including the only one with a `NeedsOperator` in it.

### 2 · The tree could be destroyed in one click, three ways, with no way back

Composing the canonical 9 T × 5 level tree is dozens of deliberate clicks. Three
controls threw it away instantly and irrecoverably:

| | was |
|---|---|
| `✕` on a tree row | `change(tree.removeAt(typed, row.path), …)` — the node **and everything under it**, no confirmation (`views/pipeline.js:395`) |
| the saved-recipes picker | `change(found.tree, …)` on the `select`'s `change` — the whole unsaved tree replaced, silently (`:862`) |
| `↺ bench` on a node form | every override that node types, dropped (`:453`) |

There was no undo anywhere in the console.

**Now:** `ui/lib/undo.js`, hung off `change()` — which the file already
established as the one funnel every mutation goes through, so all three are
covered, along with every field edit and the two that are not buttons on this
screen. `↶ undo` sits at the head of the button row and Ctrl/⌘-Z does the same
(Ctrl-Shift-Z puts it back). It holds whole snapshots — tree, name, selection
and the recipe it was opened from — because a `✕` that cleared the selection
has to undo to the *screen* the operator was looking at, not only to the tree.

Two details that are the difference between an undo and a working one:

- **The button carries the name of what it will take back** —
  `↶ undo · remove the temperature loop`. After four clicks and a look away, a
  button that only says it *can* take something back asks the operator to
  remember what they did.
- **Ctrl-Z is ignored while the caret is in a field.** There the browser's own
  undo is the one they mean, and stealing it would take back the whole node
  instead of the digit they mistyped.

One bound worth knowing: the history lives in the view, so it is empty again
after a tab switch — which is the same rule the tree itself follows, since that
comes back from `GET /pipelines/last` rather than from the browser. Undo is for
the composing session, and a composing session is what a stray `✕` interrupts.

Because undo covers it, the recipe picker still acts on one click rather than
asking first: making it *recoverable* is a better answer than making it
*harder*, and this is the one place in the console where that trade is
available.

### 3 · Save wrote over an existing recipe without mentioning it

`POST /pipelines/save` writes `<out>/recipes/<name>.json` unconditionally
(`app.py:536`) and the console never checked (`views/pipeline.js:970`). Typing
a name that matched an existing recipe destroyed it — and unlike everything in
finding 2, that one is outside the undo history: the file that was there is
gone.

**Now:** a Save that would overwrite arms and says what it will replace —
`overwrite screening-probe?`, with the strip naming the file and when it was
saved. Six seconds, then it disarms, exactly as Park does. The arm is scoped to
**one name**, not a flag: a name changed between the two clicks would otherwise
carry the arming to a different file and overwrite *that* one with no
confirmation at all — the rule `monitor.scopedTo` already applies to an armed
Abort.

### 4 · The one message line had no clock on it and no way out

The strip at the foot of every view is the console's single place for a
sentence, and it was replaced only by the next one (`app.js:144`,
`lib/rail.js:419`). A `queued … → folder` from three hours ago and a refusal
the operator dealt with twenty minutes ago both kept reading as news, and the
only way to be rid of one was to provoke another.

**Now:** a `✕` that clears it. Dismissing is the operator's and never a
timer's — a refusal that fades on its own is the failure mode this sits beside,
not a milder version of it.

---

### 5 · The device could not be named from the console

`GET /session` answered `"sample": {"sample": "", "material": "", "pixel": "",
"operator": "", "comment": ""}` on a fresh checkout, because `run.toml`'s
`[sample]` block is empty by design (`run.toml:131`) and **there was no route
that set it** — `session.py:494` read `self.run_toml["sample"]`, and `app.py`
had no `PUT`. The top bar knew: it said `no sample named` (`app.js:111`), and
offered nothing.

So an operator who mounted a device and started measuring filed every run of
that session under a name with no device in it — `290K_1000mVLED_offsetcorr_
20260905_…` — and the only way to fix it was to edit `run.toml` on disk and
restart the process that owns every instrument. `naming-plan.md` is explicit
that the folder name is the record and that renaming afterwards is a hazard,
so this was the one finding whose cost was **permanent**.

**Now:** `PUT /session/sample` (contract §3a) and `ui/lib/identity.js` — the
chip in the bar is the button that answers it, and the panel under the bar is
where the block is typed. It opens itself once when nothing names the device,
because a console that knows the next runs are about to be filed under no name
and waits to be asked is the finding, not a smaller version of it.

Five decisions, and the input box is none of them:

- **It shows the consequence, not the field.** Three of the five go into every
  folder name this session writes, in that order, so the panel draws the stem
  the next run will be filed under — `s4_PTQ10IT4F_pxa_…`, or, with nothing
  named, `290K_… — no device in the name`. The operator is typing a filename;
  the panel says so.
- **It separates the ugly from the impossible**, because `naming-plan.md`
  §"Fields collide" lists both and they are not the same thing. *Ugly*:
  `sample = "a_b"` forges a field boundary, `sample = "s4 pixel a"` puts a
  space in a directory name — the 2026-09-01 bug. Nothing refuses either,
  `run.toml` may hold them, so the warning at the field is the whole of the
  protection. *Impossible*: `sample = "a/b"` is two directories,
  `"../../etc"` is a folder above `<out>`, and `material = "PTQ10:IT-4F"` —
  **the contract's own example** — is a colon in a Windows path segment, which
  fails on the lab PC and passes here. Those the route refuses, with the value
  it would take instead (`slug()`'s own answer, offered in the panel as a
  click: *"⚠ ":" cannot be in a folder name · use PTQ10IT-4F"*).

  That last part was not optional. `naming-plan.md` §2 has carried this defect
  since 2026-09-04 and it was reachable only by editing `run.toml` — the
  operator's own file, on their own machine. Adding a text field to the
  console is a different reachability, and a route that opens one without
  closing it is a regression, so the refusal is part of the feature rather
  than a follow-up. **What is still open is that file's own remedy**: reducing
  all three fields with `slug()` inside `folder_name()`, which would settle it
  for `run.toml` too and is what its "always nine parts" needs. It changes
  names on disk and wants a length limit chosen, so it is not taken here.
- **`temperature_k` is not offered**, though the route accepts it. `run.toml`'s
  own comment (2026-09-04) says why: every recipe said 290, and a run at 220 K
  was filed as "290 K, typed". A field here would rebuild that defect with a
  nicer surface, so the panel says the number is read from the 331 instead.
- **A merge, and `null` is the way back** — to what `run.toml` opened with, the
  same meaning `null` has for a parameter. `sample_file` is on the wire so the
  panel can offer the `↺` on exactly the rows where it would change something.
- **Allowed while a run is going, and it says what that means.** An operator
  who notices at hour one should not have to choose between abandoning the
  sweep and mislabelling everything after it, so the answer reads
  `sample · …-003 keeps the name it was queued under`.

That last one needed a change under the console: `_ctx_factory` built each
node's `RunMetadata` from `catalogue.base_metadata()` — **the session's block
as it is when that node runs**. Nothing mutated the block before now, so it
could not disagree with `RunQueued.sample`; the moment a rename exists it can,
and a four-hour sweep renamed at hour one would have filed its remaining nodes
under the new name inside a folder named for the old one. A run's identity is
now fixed when it is queued (`base_metadata(rec.sample)`), which is what
`RunQueued.sample` already claimed to be.

---

## Found, and left for you

### 6 · Naming the device is not the same as naming a *run*

The block is per **session**, and a session is one process lifetime. Mounting
a second device without restarting the service is now possible — type the new
name, keep measuring — and the journal says exactly when it changed. What it
does not do is *stop* you doing it halfway through a tree that was composed for
the first device, and nothing checks that a recipe reopened three days later is
being run on what it was saved against (`lib/recipe.js` compares the bench
values, not the identity).

Whether that wants a check at Start ("this recipe was saved against `s4`, the
bench says `s7`") is a question about how the recipes are actually used, which
is yours. It is cheap once asked: `POST /pipelines/save` already records the
bench block beside the tree, and adding the identity to it is one line.

### 7 · A run that ends well says nothing on screen

`RunFailed` and `RunAborted` reach the strip (`app.js: afterFrame`); a clean
finish does not. The monitor simply disappears when the bench parks, and the
only trace is a new row on the results tab. For an operator who *is* watching,
"it stopped and I do not know whether that was the end or a failure" is a
question the screen should not make them ask.

Finding 1 covers the case where they are away. The on-screen half is a line on
the strip and is trivial to add — it is left only because "what a completed run
should say, and for how long" is a sentence for the person whose console this
is to write, and §10 is unusually specific about voice.

---

## Found in passing, outside this screening

**The Python suite is red on `main`.**
`tests/test_service_api.py::test_unknown_things_are_404s` fails: it posts a
`bace` with `smu_current_compliance_a = 0.1` against a bench ceiling of
0.05 A and expects a 422 with `smu.ceiling`, and gets a 202. The cause is
deliberate and recent — `d3536fd` ("a parameter the run will not read is inert")
made `_c_smu_ceiling` skip a `bace` node unless `measure_dc` is set
(`service/pipeline.py:1349`), which is right, and the test was not moved with
it. Almost certainly `measure_dc: True` belongs in that test's params, but it
is somebody's call whether the ceiling should also be checked on a `bace` that
will not source the 2400, and `ui-rules` §6 flags this particular check for a
reason ("A 2400 will happily push 1 A into a small cell"). 720 pass, 7 skip
beside it; the UI suite is green (294 tests).

**`ui-rules` §12 is answered and still says it is not.** *"One thing nothing
implements yet — Check if Cursor 1 matches the start of the transient"* was
implemented on 2026-09-05 as `core.diagnostics`'s alignment rule: `spike_lag_ns`
is the light-to-dark lag of the displacement spike by cross-correlation, beyond
`SPIKE_LAG_NS = 0.25` the shot is `warn`, and the console draws the verdict
beside the trace (`monitor.js: lastShotLine`). Corrected in place, keeping what
the paragraph was for — a design doc that still lists a solved problem as the
strongest open candidate will get it solved twice.
