# Writing style

These rules apply to the manual, the other documents, code comments and docstrings, the console's text, and the comments in `run.toml` and `rig.toml`. Terms are defined in the glossary, `manual/4.4-glossary.md`. Where a text and the glossary disagree, the text is wrong.

## Register

| Rule | Example |
|---|---|
| Define the subject, then give its physical or engineering basis | "The dark trace repeats the voltage step without light, so its displacement current cancels in the difference." |
| No history. Do not say who found something, when, or in what order | Not: "On 26 October a null run showed …" |
| Give a measured value as data, with its source | "Amplifier drift between the two traces: −1.2 ± 2.9 µA per shot (`analysis/2026-10-26/scope-window`)." |
| Present tense and third person in description; the imperative in instructions; no "we", "I", "now", "new" | "The window starts 2 ns before the field arrives." · "Repeat the point." |
| One idea per sentence; cut words that carry nothing | Not: "It is important to note that the window is in fact pinned …" |
| A table or an equation instead of a paragraph where it is clearer | |
| Plain noun headings | "Integration window", not "How we chose the integration window" |
| No emphasis for its own sake: little bold, no exclamation, few dashes | |

## Quantities

| Rule | Example |
|---|---|
| One name, one symbol and one unit per quantity, as in the glossary | V_coll, never "extraction voltage" in one place and "collection bias" in another |
| SI units, with a space before the unit and the symbol upright | 6 µs, 5.192 Ω, −1.2 µA |
| In code, the unit is a suffix of the name | `t_int_width_s`, `delay_ns`, `sense_resistor_ohm` |
| Uncertainty as value ± uncertainty, and say which | "0.210 ± 0.003 nC (standard error of the mean, 6 loops)" |
| Significant figures that the measurement supports | |
| The sign convention is stated where a sign matters | "Q carries the sign of the measured current after `current_sign` is applied." |

## Code comments and docstrings

| Rule | |
|---|---|
| A docstring opens with one sentence that says what the object is or does | |
| A comment explains why, or a constraint the code cannot show; it does not narrate the code | |
| An empirical constant names its source document, not its history | `# 47.1 ns; manual 2.2, "Timing"` |
| A known limitation is stated as a limitation, with the condition under which it matters | |
| No dates, names, ticket numbers or "fixed" in comments; that record is `git log` | |

## The console

| Rule | |
|---|---|
| Labels are nouns or short noun phrases; buttons are verbs | "Integration window", "Start" |
| A warning states the evidence and the remedy, in that order | "Spike edge 11 ns (limit 8 ns): the trigger moved during the shot. Repeat the point." |
| Numbers follow `ui-rules.md` §2 | |

## Review

Each rewritten text is checked twice before it is merged. A language pass by Codex (`codex exec`, read-only) proposes edits, and the author accepts or rejects each. A mechanical check confirms that only text changed. For Python, the syntax tree without comments and docstrings must be identical before and after. For the console, the tests and the linter must pass.
