# What goes in the name, and what goes in the record

2026-09-02. A proposal, not yet implemented — for review before any code
moves. It answers one instruction:

> *the journal record must not depend on the file name; the file name should be
> more regular — if it has to carry measurement parameters, the unknown ones
> should hold their place, so the structure stays the same.*

Two rules follow, and they are not independent: the name can only be allowed to
lose information once the record has all of it.

1. **Whatever the folder name can say, the journal node record already says.**
   The name is then a convenience, which is what `bace/storage/naming.py`'s
   docstring already claims it is and what it is not yet.
2. **Every folder name has the same shape.** Eight fields, always present,
   `na` where a value is unknown.

`docs/service-contract.md` §7 and `naming.py`'s module docstring both state
that the folder name keeps the 2026-08-07 archive convention *exactly*. Rule 2
amends that, deliberately, and narrowly: a run whose fields are all known keeps
the 2026-08-07 string to the character. Only the under-specified runs change,
and they change from silently shorter to explicitly place-held.

Writing this turned up a live defect on the way, in §2: `sample`, `material`
and `pixel` reach the folder name unreduced, so the contract's own example
`material = "PTQ10:IT-4F"` builds a path segment containing `:` — which fails
on the lab PC and passes on Linux and the simulator. That one is worth fixing
whatever is decided about the rest.

---

## 1 · The record does not have the metadata

Measured on a `--sim --fast` session that ran `jv_bace` then `bace`. Counting
occurrences in the session's own journal file:

```
sample: 0   material: 0   pixel: 0   operator: 0   comment: 0
```

`SessionStarted` carries `mode`, `rig_toml`, `run_toml`, `out`, `fingerprint`,
`python`, `version`, `fast`, `startup_writes`, `session_id` — and no `[sample]`
block. No later line carries one either.

**Reading last week's journal, nothing says which device a run was on.** The
only record of it is the folder name (`s4_PTQ10IT4F_pxa_…`) and the HDF5
metadata inside the folder. The results tab is a per-device view fed by
`GET /runs?session=all`, which walks up to twenty journal files and opens no
HDF5. It cannot group by device without parsing the path.

Three specific holes, each verified:

| what | where it is | where it is not |
|---|---|---|
| `sample`, `material`, `pixel`, `operator`, `comment`, `offset_corrected` | HDF5 `/metadata`, the folder name | the journal, anywhere |
| `temperature_k` + `temperature_how` + `temperature_source` on a node with no temperature loop above it — every manual run, and every pipeline with no temperature node | HDF5 `/metadata` (`290.0`, `typed`, `""`) | the journal node record (all three absent) |
| `led_drive_v` on a `jv_bace` node outside an illumination loop | HDF5 `/metadata`, the folder name (`1000mVLED`) | the journal node record (`led_v` absent) |

The temperature hole is not a design decision. `RunContext.temperature()`
(`bace/service/modules.py:278`) is the resolver written for exactly this — *"the
tree's binding when a temperature node made one, the session's typed number
otherwise. The three travel together on purpose"* — the two recorders call it
(`modules.py:976`, `modules.py:1075`), which is why the HDF5 is right, and
`tests/test_service_modules.py:196` asserts its fallback. `executor._node_detail`
(`executor.py:604`) does not call it; it reads the raw `ctx.temperature_k`
field, which only a temperature node ever sets.

The pipeline path is fine: a tree with a temperature loop, resumed by hand at
250.1 K, journals `temperature_k=250.1 · how=operator · source=operator` on
both the loop node and the module node.

### What changes

- `SessionStarted` gains the `[sample]` block — `sample`, `material`, `pixel`,
  `operator`, `comment`. It is session-scoped and already in `GET /session`.

  **The header alone is not enough, and merging it into the rows is not
  either.** Three paths defeat it, each verified:

  1. `_Parsed` keeps `header` beside `runs` and never joins them;
     `run_index()` is `out.extend(run.summary() for run in
     reversed(parsed.runs))`; `_Run.summary()` emits no identity; and
     `GET /runs` returns that list unchanged. A grid reading `?session=all`
     would still be parsing folder names.
  2. `Session.run_record()` returns `rec.as_wire()` for any run this process
     still holds, and `RunRecord.as_wire()` carries no identity either — so a
     header merge in the journal reader would light up `GET /runs/{id}` after
     a restart and not while the run is live.
  3. A header is a property of the *file*, not of the runs in it. `Journal`
     resumes an existing file (`self.resumed = os.path.isfile(self.path) and
     os.path.getsize(self.path) > 0`) and writes `SessionStarted` only
     `if not self.resumed`. Two processes started in the same second share a
     `session_id`, hence a file; the second one's `[sample]` is never recorded
     and its runs would be attributed to the first one's device.

  So the identity travels **with each run**, not only in the header:
  `RunQueued` gains the `[sample]` block beside the `params`/`resolved` it
  already carries, `RunRecord` keeps it, and both `_Run.summary()` and
  `RunRecord.as_wire()` expose it — so `GET /runs` and `GET /runs/{id}` agree
  whether the answer comes from this process or from a journal file. Per-run is
  what makes paths 2 and 3 go away rather than being worked around.
  `SessionStarted` keeps the block too: it is the session's own context, it
  costs one line, and it is what a file with no runs in it can still say.
- `executor._node_detail` builds its metadata fields from the same
  `RunMetadata` the recorder wrote, rather than assembling them from whichever
  `RunContext` fields happen to be set. In practice: call `ctx.temperature()`
  for the temperature triple, and record `led_drive_v` and `offset_corrected`
  unconditionally.
- `journal.py`'s node record gains `led_drive_v` and `offset_corrected` beside
  the fields it already copies.

Blast radius: `detail["temperature_k"]` has exactly one reader in the whole
tree, `journal.py:564`, and it copies the value on. Nothing branches on the
field's absence.

---

## 2 · The name has seven conditional fields out of eight

`RunMetadata.folder_name()` appends each field only when it is set, and joins
with `_`. Measured:

| case | `_`-parts | name |
|---|---|---|
| all known (the 2026-08-07 original) | 9 | `s4_PTQ10IT4F_pxa_290K_1020mVLED_906mVVOC_offsetcorr_20260902_183355` |
| `jv_bace` — no V_oc | 5 | `290K_1000mVLED_offsetcorr_20260902_183355` |
| offset correction off | 4 | `290K_1000mVLED_20260902_183355` |
| nothing known | 3 | `offsetcorr_20260902_183355` |
| a sample named `290K` | 6 | `290K_250K_1000mVLED_offsetcorr_20260902_183355` |

(The stamp is two `_`-parts, `YYYYMMDD` and `HHMMSS`.)

Three consequences:

- **Position carries no meaning.** A reader must match suffixes — `K`,
  `mVLED`, `mVVOC` — and a missing field shifts everything after it.
- **`offsetcorr` is a boolean spelled as presence.** "Offset correction was
  off" and "written before the field existed" are the same name.
- **Fields collide.** A sample named `290K` is indistinguishable from the
  temperature segment, as the table shows.

### The identity fields are not reduced, and one of them is already illegal

`sample`, `material` and `pixel` go into the name raw — `folder_name()` appends
them straight from `[sample]`, while only the comment passes through `slug()`.
So the three fields that name the device can hold anything the operator typed:

| `[sample]` value | folder name | what it does |
|---|---|---|
| `sample = "a_b"` | `a_b_290K_1000mVLED_offsetcorr_…` | forges a field boundary — 7 parts where the grid wants 9 |
| `sample = "s4 pixel a"` | `s4 pixel a_290K_offsetcorr_…` | a space in a directory name — the 2026-09-01 bug that `slug()` was written to stop, still open on this path |
| `material = "PTQ10:IT-4F"` | `s4_PTQ10:IT-4F_a_290K_…` | **`:` is illegal in a Windows path segment** |

The last row is not hypothetical: it is `docs/service-contract.md` §4's own
example of `session.sample`, and `PTQ10:IT-4F` is the material the archive
folder spells `PTQ10IT4F` because someone stripped the punctuation by hand when
typing it. `naming.py` already knows the character is illegal —
`_PATH_UNSAFE = '<>:"/|?*' + chr(92)` contains it and `slug("PTQ10:IT-4F")`
returns `PTQ10IT-4F` — the knowledge just is not applied here. On the lab PC
that run fails at folder creation; on Linux and in the simulator it succeeds,
which is why no test caught it.

So the grid needs the three identity fields to pass through `slug()` too, with
a shorter limit than a comment's 64. That is not an extra: without it, "always
nine parts" is not true.

### The grid

Eight fields, every one present, in the 2026-08-07 order:

```
<sample>_<material>_<pixel>_<T>K_<LED>mVLED_<VOC>mVVOC_<offset>_<YYYYMMDD>_<HHMMSS>
```

Splitting on `_` gives nine parts, of which 8 and 9 are the stamp — **for a run
folder.** One caller appends to that name and the invariant has to say so:
`SeriesRecorder` builds `f"{metadata.folder_name()}_series"`
(`storage/series.py:185`), which is ten parts with `series` last. That suffix
is a folder *class* marker, and it stays: the intensity series writes a parent
holding one run folder per level, and the name is how you tell the two apart in
a directory listing.

So the rule, stated so it covers both: **parts 1–9 are the grid, and a trailing
`_series` marks a series parent.** Read the stamp at parts 8–9 rather than from
the end.

| field | known | unknown |
|---|---|---|
| sample, material, pixel | `s4`, `PTQ10IT4F`, `pxa` | `na` |
| temperature | `290K` | `naK` |
| LED drive | `1020mVLED` | `namVLED` |
| V_oc | `906mVVOC` | `namVVOC` |
| offset correction | `offsetcorr` | `offsetraw` |

`offsetraw` is the point of that row: a real negative rather than an absence.

```
s4_PTQ10IT4F_pxa_290K_1020mVLED_906mVVOC_offsetcorr_20260807_111521   all known — unchanged
na_na_na_290K_1000mVLED_namVVOC_offsetcorr_20260902_183355            jv_bace on an unnamed device
na_na_na_naK_namVLED_namVVOC_offsetraw_20260902_183355                nothing known
```

The placeholders are shorter than the values they stand in for, so the
`COMMENT_MAX` budget against Windows' 260-character path limit only loosens.

**The sentinel is reserved.** An operator who types `na` into an identity field
would otherwise produce the same folder name as one who typed nothing, and the
grid makes that a genuine collision where today it is not — `sample = "na"`
gives `na_290K_…` and `sample = ""` gives `290K_…`, two different names. So a
literal `na` in `sample`, `material` or `pixel` is escaped on the way into the
name (`na-`), and `as_dict()` keeps what was typed.

### Colliding folders are made unique, and that is part of this proposal

Reserving the sentinel is not enough, because **`slug()` is many-to-one** and
the grid is what makes every identity pass through it. Measured, at the
24-character limit:

| two identities | both become |
|---|---|
| `a_b` and `a b` | `a-b` |
| `s4 pixel` and `s4_pixel` | `s4-pixel` |
| `PTQ10IT4F-batch-2026-08-A` and `…-B` | `PTQ10IT4F-batch-2026-08` |

The first two are contrived; the third is not — two batches of one material,
differing after character 24, is an ordinary thing for a lab to have. Today
those pairs produce different folder names. Both of those names are broken (one
forges a field boundary, one puts a space in a directory), which is why they
are being slugged — but "different and broken" becoming "identical" is a
regression, and it is this proposal's to own.

Underneath it is a hazard that predates the grid: **two runs whose metadata
matches to the second already overwrite each other.** `RunRecorder._start()` is
`os.makedirs(self.folder, exist_ok=True)` and every file inside is named from
`metadata.stamp` — `<stem><stamp>.dat`, `run<stamp>.h5` — so the same folder
means the same file names and the second run lands on the first. Under
`--sim --fast` a run takes milliseconds and same-second pairs are ordinary; on
the rig they are not.

So a folder that already holds a run is not reused: the next free `-2`, `-3`
suffix is taken instead. The suffix rides on the last part of *the grid*
(`…_20260902_183355-2`), so the count stays nine and the stamp stays readable
at parts 8–9; the files inside keep the plain LabVIEW stamp, because they are
in a different directory and never clash.

**On the grid, not on the whole name.** A series parent is
`folder_name() + "_series"`, so suffixing the string would give
`…_183355_series-2` and break the rule two paragraphs up that an exact trailing
`_series` names the folder class. The allocator takes the grid and the class
marker separately and puts the disambiguator between them —
`…_20260902_183355-2_series` — so both invariants survive a collision: parts
8–9 carry the stamp with its suffix, and `_series` is still last.

**One helper, called by every `folder_name()` consumer** — not a change inside
`RunRecorder`. All three callers listed in §3 allocate their own directory and
all three would otherwise stay exposed:

| caller | how it lands there |
|---|---|
| `RunRecorder._start()` | `os.makedirs(self.folder, exist_ok=True)` (`recorder.py:96`) |
| `jv_*`, via `modules.py:981` | builds the path itself, then `JVRecorder` does its own `os.makedirs(..., exist_ok=True)` (`jv.py:97`, `:211`) and writes HDF5 with mode `"w"` — a truncating open |
| `SeriesRecorder` | `os.makedirs(self.folder, exist_ok=True)` (`series.py:187`) on the `_series` parent |

The J-V path is the sharpest of the three: `h5py.File(path, "w")` does not
merely reuse the directory, it truncates the file. So the allocation is a
function in `storage/naming.py` that reserves a directory and returns the name
it actually got, and the three call it rather than calling `makedirs`
themselves. That closes the sentinel case, the three slug cases, and the
same-second case that predates the grid, on every path that writes a run.

An injective encoding was the alternative and is the wrong trade: it buys
uniqueness by making every name unreadable, when the name exists to be read.
Uniqueness belongs at the directory, and the record — which is now per-run and
carries all three identity fields verbatim — is what says which run is which.

`sample`, `material` and `pixel` are reduced with `slug()` on the way in, for
the reason above: whitespace and `_` become `-`, the characters a Windows path
segment may not hold are dropped, anything merely non-ASCII is kept. A limit of
24 characters each keeps the identity block under the 66 the 2026-08-07 name
budgets for it. `as_dict()` keeps all three verbatim, the same split the comment
already has.

**A field that reduces to nothing takes the placeholder too.** `slug(" :: ")`,
`slug("///")` and `slug("...")` all return `""`, and today `folder_name()`
handles that by dropping the field — which the grid cannot do without producing
`s4__na_290K_…` and losing the arity it exists to guarantee. So the rule is
*unknown **or reduced to nothing** → `na`*, and that is the case the new test
has to carry alongside the three in the table above.

`slug()` does not normalise Windows' reserved device names (`CON`, `PRN`,
`AUX`, `NUL`, `COM1`…), and does not need to: those are reserved only as a
*whole* path component, and `folder_name()` never emits one. Even
`sample = "CON"` with everything else unknown builds
`CON_na_na_naK_namVLED_namVVOC_offsetraw_20260902_183355`. Nothing in the
metadata reaches a name of its own — the `.dat` stems and `run<stamp>.h5` are
fixed strings — so there is no path on which a bare `CON` can be created.

### The comment leaves the name

Today `[sample] comment` is slugged into a segment between `offsetcorr` and the
stamp, and it is the main source of variable length: `290K_1000mVLED_offsetcorr_
LabVIEW-panel-replica-combination-4-shutter-only-dark_20260902_012223`.

It moves out of the name entirely and lives in the file header. It is already
there: HDF5 `/metadata` carries `comment` verbatim, which is the split
`naming.py` documents — *the name is a convenience, the file is the record*.
`docs/ui-kickoff.md` already requires the console to show the sentence and not
the slug, so the console reads the metadata either way.

`slug()` stays. It is still what a comment must pass through if it ever reaches
a path, and its tests are the record of which characters a Windows path segment
cannot hold.

**The `.dat` files cannot take a metadata header.** `bace/storage/legacy_dat.py`
writes them byte-for-byte as LabVIEW did, and `tests/test_storage.py` reads the
2026-08-07 archive, writes it back through the module and compares bytes.
The format's own docstring records that *"scripts that skip exactly one header
line"* read the blank line as a row — adding lines would break the parsers on
the other side. So "the header" here means the HDF5 `/metadata` group, and the
`.dat` files stay exactly as they are.

One edge: `RunRecorder` writes the `.dat` files first *"so a run survives a
missing h5py"* (`recorder.py:55`). On such a run the comment would have no home
once it leaves the name. `h5py` is a hard dependency in `pyproject.toml`, not an
extra, so this is a degraded case rather than a normal one — worth knowing, not
worth designing around unless you want a sidecar JSON.

---

## 3 · What this costs

**No production code parses a folder name.** `folder_name()` has three callers
and all three build a path: `storage/recorder.py:95`, `storage/series.py:186`,
`service/modules.py:981`. Every other match is a test asserting a substring.

Tests that change:

| test | why |
|---|---|
| `test_storage.py:312` (`test_the_name_gets_the_slug_and_the_record_keeps_the_sentence`) | the comment leaves the name; the assertion becomes that it is *not* in the name and *is* in `as_dict()` |
| `test_storage.py:320` (`blank.folder_name()`) | `s4_290K_20260902_012223` → the full grid |
| `test_service_modules.py:287` | asserts `"LED" not in name and "offsetcorr" not in name` — the exact pattern being removed; becomes `namVLED` / `offsetraw` |

Tests that stay green, and are the reason the change is narrow:

| test | why |
|---|---|
| `test_storage.py:251` `test_folder_name_matches_the_archive_convention` | all fields known → the same string |
| `test_storage.py:262` `test_temperature_provenance_stays_out_of_the_name` | provenance still never enters the name; `290K` is `290K` however it was arrived at |
| the substring assertions in `test_service_executor.py`, `test_service_session.py`, `test_intensity_series.py`, `test_service_modules.py` (`"1020mVLED" in name`, `"900mVVOC" in name`, `"250K" in name`) | those substrings still appear |
| `test_regression_20260807.py` | mentions the archive name in a docstring; asserts nothing about it |

Reducing `sample`/`material`/`pixel` with `slug()` changes no existing test:
every sample in the suite is already `s4`, `SIM`, `PTQ10IT4F`, `pxa`, `a` —
values `slug()` returns unchanged. It wants one new test of its own, with the
three rows of the table above.

Folders already written keep their names. Nothing renames them, and nothing
reads them, so the archive becomes mixed-format on disk and single-format from
the date of the change. That is the honest trade for not touching data that has
already been analysed.

---

## 4 · Left open

**`[sample] temperature_k = 290.0` is hard-coded in seven recipes** — `run.toml`
and all six of `recipes/*.toml`. `RunMetadata.temperature_k` defaults to `None`
with `how = ""`, so *"nobody said"* is representable; the recipes are what turn
it into *"someone typed 290"*.

Once rule 1 lands, a manual run at 220 K on a bench whose `run.toml` nobody
edited is journalled as `290 K · typed` — a wrong number wearing an honest
label. `typed` cannot separate that from a deliberate 290. The console cannot
fix it either: `docs/ui-rules.md` §6 forbids re-deriving provenance.

The fix is on the data side — drop `temperature_k` from `[sample]` in the
recipes so it resolves to `None`/`""` and the console renders "not recorded".
The cost is that every manual run then shows a blank temperature until someone
fills it in, which is a change to bench habit, not to code. Not decided here.

**Migration of existing folders**: not proposed. See above.
