// Formatters use measurement precision and render missing values as an em dash
// (`docs/ui-rules.md` §2). Sentinel zeros must not become measured zeros, and `null`
// must not become a numeric value.

export const ABSENT = '—';           // The em dash denotes an unrecorded value.

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
 * The formatter treats zero Q standard deviation as unrecorded uncertainty
 * (`docs/ui-rules.md` §2). It returns `null` so the caller uses the hollow marker
 * rather than a zero-length error bar that implies a measurement.
 */
export function sigmaQ(std) {
  return (std === null || std === undefined || std === 0) ? null : scientific(std, 3);
}

/** Potential values, including V_oc, use four decimal places. */
export function volts(v, { decimals = 4, unit = true } = {}) {
  if (v === null || v === undefined || Number.isNaN(v)) return ABSENT;
  return v.toFixed(decimals) + (unit ? ' V' : '');
}

/**
 * Current density uses three significant figures in mA/cm², the service's wire unit.
 * No conversion or per-value SI prefix applies, so a column and axis keep one
 * comparable unit (`docs/service-contract.md` §6). Without area, the service sends
 * `null` and the caller reports A rather than assuming 1 cm² (`docs/ui-rules.md` §6).
 */
export function density(j, { unit = true } = {}) {
  if (j === null || j === undefined || Number.isNaN(j)) return ABSENT;
  return sig(j, 3) + (unit ? ' mA/cm²' : '');
}

/**
 * The current formatter uses readable SI prefixes. Compliance is the source-meter's
 * ceiling and must remain legible beside the field (`docs/ui-rules.md` §6).
 */
export function amps(a) {
  if (a === null || a === undefined || Number.isNaN(a)) return ABSENT;
  if (a === 0) return '0 A';
  const [value, prefix] = prefixed(a);
  return sig(value, 3) + ' ' + prefix + 'A';
}

/** Temperature uses one decimal place. */
export function kelvin(k, { unit = true } = {}) {
  if (k === null || k === undefined || Number.isNaN(k)) return ABSENT;
  return k.toFixed(1) + (unit ? ' K' : '');
}

/**
 * Power is watts at the meter. The beam-splitter and area factor is unavailable, so
 * reporting irradiance would imply an unsupported calibration (`docs/ui-rules.md`
 * §2).
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

/** The formatter extracts the session's time of day from its timestamp identifier. */
export function time(ts) {
  if (!ts) return ABSENT;
  const d = new Date(ts * 1000);
  return d.toTimeString().slice(0, 8);
}

/**
 * Axis labels use the parameter name and slash-unit convention, such as V_pre / V. A
 * second display vocabulary would make identifiers harder to recognize
 * (`docs/ui-rules.md` §1).
 */
export function label(name, unit) {
  return unit ? `${name} / ${unit}` : name;
}

/**
 * The formatter reports requested and completed loops for truncated runs
 * (`docs/ui-rules.md` §9).
 */
export function keptOf(kept, requested) {
  if (kept === null || kept === undefined) return ABSENT;
  if (requested === null || requested === undefined) return String(kept);
  return `${kept}/${requested}`;
}

/**
 * The helper formats a count with its singular or plural noun. One module, level or
 * shot is a common case, so shared handling avoids repeated plural rules. English
 * regular plurals append s; `many` permits a different plural.
 */
export function plural(n, one, many = `${one}s`) {
  return `${n} ${Math.abs(n) === 1 ? one : many}`;
}
