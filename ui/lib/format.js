// Rendering numbers, per `docs/ui-rules.md` §2 — significant figures carry
// meaning, because they are what the measurement resolves, and two zeros in
// the archive are not zeros at all.
//
// Every function here returns a string for the screen, and returns the em dash
// for an absence. None of them invents a value: a formatter that turned `null`
// into `0.0000` would be the same lie the two zeros are.

export const ABSENT = '—';           // an em dash: nothing was recorded

/** Significant figures, in fixed or scientific notation as the magnitude asks. */
export function sig(value, figures) {
  if (value === null || value === undefined || Number.isNaN(value)) return ABSENT;
  if (value === 0) return '0';
  const exponent = Math.floor(Math.log10(Math.abs(value)));
  if (exponent < -4 || exponent >= 6) return scientific(value, figures);
  const decimals = Math.max(0, figures - 1 - exponent);
  return value.toFixed(Math.min(decimals, 20));
}

/** `3.65257e-10` — the archive's own spelling for a charge. */
export function scientific(value, figures = 6) {
  if (value === null || value === undefined || Number.isNaN(value)) return ABSENT;
  const text = value.toExponential(Math.max(0, figures - 1));
  return text.replace(/e([+-])(\d)$/, 'e$1$2');
}

/** Charge to 5–6 significant figures: `3.65257e-10 C`. */
export function charge(q, { unit = true } = {}) {
  if (q === null || q === undefined) return ABSENT;
  return scientific(q, 6) + (unit ? ' C' : '');
}

/**
 * σ_Q. **A zero is not a zero**: most of the 9 x 5 grid carries `0.000000`
 * because the loops were never recorded, and a zero-length error bar drawn as
 * a bare dot is a lie (`docs/ui-rules.md` §2). So this answers `null` for it,
 * and the caller renders the absence — the legend's hollow marker, not a dot.
 */
export function sigmaQ(std) {
  return (std === null || std === undefined || std === 0) ? null : scientific(std, 3);
}

/** V_oc and every other potential: four decimals. */
export function volts(v, { decimals = 4, unit = true } = {}) {
  if (v === null || v === undefined || Number.isNaN(v)) return ABSENT;
  return v.toFixed(decimals) + (unit ? ' V' : '');
}

/**
 * Current density to 3 significant figures, in **mA/cm²** — the unit the
 * service sends and the only one J–V is shown in anywhere in this project.
 * `JVCurveDone.density` is `current × 1000 / pixel_area_cm2`
 * (`experiment.jv.current_density`), so there is nothing to convert here and,
 * more to the point, nothing to choose: an SI prefix picked per value printed
 * one sweep as `20.0 mA/cm²` at one end and `1.90 nA/cm²` at the other, which
 * is a column no reader can compare down and an axis nobody can label.
 *
 * `pixel_area_cm2 = 0` means there is no density at all — the service sends
 * `null` and the caller reports amps instead, rather than defaulting to 1 cm²
 * and silently mislabelling A as mA/cm² (`docs/ui-rules.md` §6).
 */
export function density(j, { unit = true } = {}) {
  if (j === null || j === undefined || Number.isNaN(j)) return ABSENT;
  return sig(j, 3) + (unit ? ' mA/cm²' : '');
}

/**
 * A current, prefixed where it is natural: `50.0 mA`, `1.20 µA`. The
 * compliance the rail shows is a ceiling on what a 2400 may push into a small
 * cell (`docs/ui-rules.md` §6), so it is never rendered in bare amps.
 */
export function amps(a) {
  if (a === null || a === undefined || Number.isNaN(a)) return ABSENT;
  if (a === 0) return '0 A';
  const [value, prefix] = prefixed(a);
  return sig(value, 3) + ' ' + prefix + 'A';
}

/** Temperature to one decimal. */
export function kelvin(k, { unit = true } = {}) {
  if (k === null || k === undefined || Number.isNaN(k)) return ABSENT;
  return k.toFixed(1) + (unit ? ' K' : '');
}

/**
 * The power meter's reading, in **watts at the meter**. Never mW/cm²: the
 * beam-splitter/area factor is not recoverable, so an irradiance here would be
 * a number that looks calibrated and is not (`docs/ui-rules.md` §2).
 */
export function intensity(watts) {
  if (watts === null || watts === undefined || Number.isNaN(watts)) return ABSENT;
  if (watts === 0) return '0 W';
  const [value, prefix] = prefixed(watts);
  return sig(value, 3) + ' ' + prefix + 'W';
}

const PREFIXES = [[1e-12, 'p'], [1e-9, 'n'], [1e-6, 'µ'], [1e-3, 'm'], [1, ''], [1e3, 'k']];

/** SI prefixes where they are natural (mA, ns, mW); the caller adds the unit. */
export function prefixed(value) {
  const magnitude = Math.abs(value);
  let chosen = PREFIXES[0];
  for (const step of PREFIXES) if (magnitude >= step[0]) chosen = step;
  return [value / chosen[0], chosen[1]];
}

/** Seconds as the three time scales read them: `2 h 14 min`, `27 min`, `4.2 s`. */
export function duration(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return ABSENT;
  if (seconds < 1) return sig(seconds, 2) + ' s';
  if (seconds < 90) return (seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)) + ' s';
  const minutes = Math.round(seconds / 60);
  if (minutes < 90) return minutes + ' min';
  const hours = Math.floor(minutes / 60);
  return hours + ' h' + (minutes % 60 ? ' ' + (minutes % 60) + ' min' : '');
}

/** A wall clock, for a `finish_at`. */
export function clock(ts) {
  if (!ts) return ABSENT;
  const d = new Date(ts * 1000);
  return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
}

/** `20260902_153722` -> `15:37:22`, for the session log. */
export function time(ts) {
  if (!ts) return ABSENT;
  const d = new Date(ts * 1000);
  return d.toTimeString().slice(0, 8);
}

/**
 * The slash-unit convention the rest of the project uses: `V_pre / V`.
 * The label is the **real parameter name** — the operator already knows
 * `v_pre` and `n_averages`, and a prettier synonym is a second vocabulary
 * to learn (`docs/ui-rules.md` §1).
 */
export function label(name, unit) {
  return unit ? `${name} / ${unit}` : name;
}

/** `100 loops requested, 20 completed` — a truncated run is normal (§9). */
export function keptOf(kept, requested) {
  if (kept === null || kept === undefined) return ABSENT;
  if (requested === null || requested === undefined) return String(kept);
  return `${kept}/${requested}`;
}

/**
 * `1 folder`, `4 folders` — a count and the thing it counts.
 *
 * The schedule footer said "writes 1 folders" (#42), and it was not alone:
 * every counter on the pipeline tab was a template literal with a hard `s`
 * on the end. A count of one is the ordinary case for most of them — one
 * module, one level, one shot — so it is not a corner worth spelling out
 * inline eight times.
 *
 * English, and only where English already agrees with itself: the irregular
 * plurals this console needs are none, so the rule is the `s`, with `many`
 * there for a word that would not take it.
 */
export function plural(n, one, many = `${one}s`) {
  return `${n} ${Math.abs(n) === 1 ? one : many}`;
}
