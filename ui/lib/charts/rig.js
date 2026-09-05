// The rig tab's two drawings, ported from the design pack as they stand.
//
// `docs/ui-plan.md`'s component table lists `ch-rig` as "static, reusable as
// it stands", and R3·4 puts it beside `ch-timing` — the one shot at four
// scales, with the chain timing measured on 2026-09-01. Both are reference,
// not a work surface: the numbers in them are the rig day's, baked in, and
// they are meant to be read against the live drawing on the `bace` card
// (`lib/charts/timing.js`), which is the code's own arithmetic over the form.
//
// The source is `docs/design/bace-charts-r3.js`, which builds its SVG as
// markup strings inside a custom element. That is kept: the geometry is the
// design's, hand-placed, and re-expressing three hundred coordinates through
// `svg.s()` would be a transcription with nothing to check it against. What
// changed is only the wrapper — a pure function returning the markup, so it
// can be tested without a browser, and `root()` from `lib/svg.js` around it
// so it resizes with its card like every other chart here.
//
// The palette is the console's own (`style.css` `:root`), written as hex
// because `<marker>` fills and `stroke-opacity` do not take `var()` in every
// engine the lab PC might run.

import { root } from '../svg.js';

const F = "'IBM Plex Mono',monospace";
const FS = "'IBM Plex Sans',sans-serif";
const A = '#ec3013';    // --accent
const AD = '#ae1800';   // --accent-dark
const K = '#201e1d';    // --ink
const G = '#7d7979';    // --grey
const L = '#d7d3d3';    // --rule
const LL = '#eae7e7';   // --fill
const W = '#fff';       // --paper

const esc = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const t = (x, y, s, o) => `<text x="${x}" y="${y}" style="font:${(o && o.f) || `400 9.5px ${F}`};fill:${(o && o.c) || G}"`
  + ((o && o.a) ? ` text-anchor="${o.a}"` : '') + ((o && o.tr) ? ` transform="${o.tr}"` : '') + `>${esc(s)}</text>`;
const line = (x1, y1, x2, y2, c, w, d) => `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="${c || L}" stroke-width="${w || 1}"`
  + (d ? ` stroke-dasharray="${d}"` : '') + '/>';
const path = (d, c, w, dash, o) => `<path d="${d}" fill="none" stroke="${c}" stroke-width="${w}"`
  + (dash ? ` stroke-dasharray="${dash}"` : '') + ((o && o.cap) ? ` stroke-linecap="${o.cap}"` : '')
  + ((o && o.m) ? ` marker-end="url(#${o.m})"` : '') + ((o && o.op) ? ` stroke-opacity="${o.op}"` : '') + ' stroke-linejoin="miter"/>';
const rect = (x, y, w, h, f, o) => `<rect x="${x}" y="${y}" width="${w}" height="${h}" fill="${f}"`
  + (o && o.op != null ? ` fill-opacity="${o.op}"` : '')
  + (o && o.s ? ` stroke="${o.s}" stroke-width="${o.sw || 1}"${o.d ? ` stroke-dasharray="${o.d}"` : ''}` : '') + '/>';

// The arrowheads: `ak` in ink for drive, DC and signal; `aa` in the accent for
// trigger and bias.
const markers = '<defs>'
  + `<marker id="ak" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M0 0L10 5L0 10z" fill="${K}"/></marker>`
  + `<marker id="aa" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M0 0L10 5L0 10z" fill="${A}"/></marker>`
  + '</defs>';

function box(x, y, w, h, lines, o) {
  let s = rect(x, y, w, h, (o && o.f) || W, { s: (o && o.s) || K, sw: (o && o.sw) || 1, d: o && o.d });
  s += t(x + 9, y + 17, lines[0], { f: `600 11.5px ${FS}`, c: (o && o.c) || K });
  if (lines[1]) s += t(x + 9, y + 31, lines[1], { f: `400 9.5px ${FS}`, c: G });
  if (lines[2]) s += t(x + 9, y + 45, lines[2], { f: `400 9.5px ${F}`, c: (o && o.c2) || G });
  return s;
}

/** The chain: who is wired to what. The design's `ch-rig`, 960 × 470. */
export const CHAIN_SIZE = { width: 960, height: 470 };

export function chainMarkup() {
  let s = markers;
  // instruments
  s += box(44, 30, 220, 58, ['33220A', 'LED drive · master clock', '1 kHz · 50 % · low 0.400 V']);
  s += box(44, 118, 220, 58, ['81150A', 'bias pulser', 'ARM EXT POS · :PULS:DEL1 = delay_ns'], { s: A, sw: 1.4 });
  s += box(44, 206, 220, 58, ['Keithley 2400', 'SMU · DC', 'V_oc · J_sc · J_sat · cc ≤ 50 mA']);
  s += box(44, 294, 220, 58, ['Infiniium', 'digitiser', 'CHAN1 I(t) · CHAN3 trigger · avg 100']);
  s += box(44, 382, 220, 58, ['Deditec DIO', 'shutter = module 0 · relay = module 1', 'ID 9'], { s: G, d: '3 3' });
  // bench
  s += box(344, 30, 90, 58, ['LED amp', 'fixed gain', '≈ 83 ns']);
  s += box(464, 30, 70, 58, ['LED', '530 nm', '']);
  s += box(564, 30, 120, 58, ['shutter → fibre', 'DIO 0 · open / shut', '85 m fibre · 419 ns']);
  s += box(714, 30, 70, 58, ['splitter', 'meter +', 'device']);
  s += box(814, 30, 130, 58, ['1918-C', 'optical power', 'W · :8918']);
  s += box(344, 118, 90, 58, ['× 4 amp', 'bias', ''], { s: A, sw: 1.4 });
  s += box(514, 190, 150, 84, ['relay · DIO 1', '2400  ↔  amplifier · never both', ''], { sw: 1.4 });
  s += box(734, 190, 210, 70, ['device in cryostat', 's4 · PTQ10:IT-4F · pixel a', 'V_pre held · pulsed to V_coll'], { sw: 2 });
  s += box(734, 300, 210, 50, ['R_sense 5.192 Ω', 'I = V / R', ''], {});
  s += box(734, 390, 210, 50, ['Lake Shore 331', 'cryostat temperature · not wired', 'console :8331'], { s: G, d: '3 3' });
  // relay poles
  s += `<circle cx="530" cy="246" r="3" fill="${K}"/><circle cx="530" cy="264" r="3" fill="${K}"/><circle cx="648" cy="255" r="3" fill="${K}"/>`;
  s += line(530, 264, 648, 255, K, 1.6);
  s += t(538, 243, 'amp', { f: `400 8.5px ${FS}` }) + t(538, 270, '2400', { f: `400 8.5px ${FS}` });
  // drive
  s += path('M264 59H342', K, 1.6, null, { m: 'ak' }); s += path('M434 59H462', K, 1.6, null, { m: 'ak' });
  s += t(268, 54, 'drive', { f: `400 9px ${FS}` });
  // light
  s += path('M534 59H562', K, 2, '1 4', { cap: 'round', m: 'ak' }); s += path('M684 59H712', K, 2, '1 4', { cap: 'round', m: 'ak' }); s += path('M784 59H812', K, 2, '1 4', { cap: 'round', m: 'ak' });
  s += path('M749 88V188', K, 2, '1 4', { cap: 'round', m: 'ak' });
  s += t(755, 140, 'light · 502 ns', { f: `400 9px ${FS}` });
  // trigger 33220A SYNC -> 81150A ARM
  s += path('M264 78H290V128H266', A, 1.6, '6 3', { m: 'aa' });
  s += t(294, 106, 'SYNC ↑ arms', { f: `500 9px ${FS}`, c: AD });
  // 81150A SYNC -> scope CHAN3 (left gutter)
  s += path('M44 165H22V340H42', A, 1.6, '6 3', { m: 'aa' });
  s += t(14, 252, 'SYNC → CHAN3', { f: `500 9px ${FS}`, c: AD, a: 'middle', tr: 'rotate(-90 14 252)' });
  // bias
  s += path('M264 147H342', A, 2, null, { m: 'aa' }); s += path('M434 147H474V246H512', A, 2, null, { m: 'aa' });
  s += t(438, 142, 'V_pre → V_coll', { f: `500 9px ${FS}`, c: AD });
  // DC
  s += path('M264 264H512', K, 1.6, null, { m: 'ak' }); s += t(300, 260, 'DC', { f: `400 9px ${FS}` });
  // relay -> device
  s += path('M664 255H732', K, 1.8, null, { m: 'ak' });
  // device -> R_sense -> scope
  s += path('M839 260V298', K, 1.6, null, { m: 'ak' });
  s += path('M734 325H266', K, 1.6, null, { m: 'ak' });
  s += t(600, 320, 'I(t) → CHAN1', { f: `400 9px ${FS}` });
  s += t(44, 462, 'host: setup and read-back only · the 1 kHz loop closes in hardware', { f: `400 9.5px ${FS}` });
  return s;
}

/** One shot at four scales, chain timing measured 2026-09-01. The design's `ch-timing`, 1000 × 760. */
export const SHOT_SIZE = { width: 1000, height: 760 };

export const AXES = ['vpre', 'delay_ns', 'vcoll'];

export function shotMarkup(axis = 'vpre') {
  // The swept axis is in the accent, the rest ghosted — `ui-rules` §3, as
  // the design drew it. An axis the console has never heard of highlights
  // nothing rather than throwing.
  const hv = axis === 'vpre', hd = axis === 'delay_ns', hc = axis === 'vcoll';
  let s = '';
  // A · one shot
  const XA = (sec) => 90 + sec / 0.8 * 890;
  s += t(8, 18, 'A · one shot = one Q · ≈ 0.8 s', { f: `600 11px ${FS}`, c: K });
  s += t(980, 18, 'one loop = one shot per axis point · zero-width axis: loop = shot · n_loops repeats', { a: 'end', f: `400 10px ${FS}` });
  s += rect(90, 26, 890, 134, W, { s: L });
  s += t(8, 52, 'shutter', { f: `600 10px ${FS}`, c: K }); s += t(8, 80, 'LED drive', { f: `600 10px ${FS}`, c: K }); s += t(8, 91, '1 kHz · 50 %', { f: `400 8.5px ${FS}` }); s += t(8, 112, 'bias', { f: `600 10px ${FS}`, c: K }); s += t(8, 123, 'pulsed each cycle', { f: `400 8.5px ${FS}` }); s += t(8, 148, 'acquire', { f: `600 10px ${FS}`, c: K });
  s += path(`M90 56H${XA(0.03)}V40H${XA(0.43)}V56H980`, K, 1.6);
  s += t(XA(0.06), 37, 'open', { f: `400 9px ${FS}` }); s += t(XA(0.46), 66, 'shut', { f: `400 9px ${FS}` });
  const per = 890 / 80;
  let dled = 'M90 74';
  for (let i = 0; i < 80; i += 1) { const xa = 90 + i * per; dled += `H${(xa + per / 2).toFixed(1)}V92H${(xa + per).toFixed(1)}V74`; }
  s += path(dled, K, 1);

  const xs = XA(0.43), yhL = 104, ybL = 122, yhD = 108, ybD = 126;
  s += path(`M90 ${yhL}H${xs}`, hv ? A : K, 1.4); s += path(`M${xs} ${yhD}H980`, K, 1.4);
  for (let j = 0; j < 80; j += 1) { const xb = 90 + j * per + per / 2, lt = xb < xs; s += line(xb, lt ? yhL : yhD, xb, lt ? ybL : ybD, hc ? A : K, 1); }
  s += t(90, 170, 'LED drive never stops · the light at the sample follows 502 ns later   ·   bias: V_pre ⇄ V_coll (1.0423 → −4.00 V) for 5 µs at every LED-off edge   ·   dark half: 0 ⇄ −5.04 V, the same swing shifted by −V_oc', { f: `400 9px ${FS}` });
  s += rect(XA(0.25), 136, XA(0.35) - XA(0.25), 18, A); s += rect(XA(0.65), 136, XA(0.75) - XA(0.65), 18, K);
  s += t(XA(0.30), 148, 'light · 100 cycles', { a: 'middle', f: `500 9px ${FS}`, c: W }); s += t(XA(0.70), 148, 'dark · 100 cycles', { a: 'middle', f: `500 9px ${FS}`, c: W });
  s += t(XA(0.14), 148, 'settle_s 0.20', { a: 'middle' }); s += t(XA(0.54), 148, 'dark_settle_s 0.20', { a: 'middle' }); s += t(XA(0.775), 148, 'Q', { a: 'middle', f: `600 10px ${F}`, c: K });
  s += rect(XA(0.295) - 2, 72, 5, 22, 'none', { s: A, d: '2 2' });
  s += path(`M${XA(0.295) - 2} 94L90 200`, A, 1, '3 3', { op: 0.6 }); s += path(`M${XA(0.295) + 3} 94L980 200`, A, 1, '3 3', { op: 0.6 });

  // B · one cycle
  const XB = (ms) => 90 + ms * 890;
  s += t(8, 194, 'B · one LED cycle · 1 ms', { f: `600 11px ${FS}`, c: K });
  s += t(980, 194, 'one of n_averages 100 · summed in the scope', { a: 'end', f: `400 10px ${FS}` });
  s += rect(90, 200, 890, 156, W, { s: L });
  s += t(8, 224, 'LED drive', { f: `600 10px ${FS}`, c: K }); s += t(8, 253, 'light at sample', { f: `600 10px ${FS}`, c: K }); s += t(8, 279, 'SYNC', { f: `600 10px ${FS}`, c: K }); s += t(8, 310, 'bias', { f: `600 10px ${FS}`, c: K }); s += t(8, 342, 'scope', { f: `600 10px ${FS}`, c: K });
  s += path(`M90 212H${XB(0.5)}V230H980`, K, 1.6);
  s += t(XB(0.05), 208, 'on · led_v 1.020 V · 500 µs', { f: `400 9px ${FS}` }); s += t(XB(0.75), 226, 'off · led_low_v 0.400 · 500 µs', { f: `400 9px ${FS}` });
  s += path(`M90 242H${XB(0.5)}V256H980`, K, 1.6, '5 3');
  s += t(XB(0.05), 239, 'same square, 502 ns later · LED amp 83 ns + 85 m fibre 419 ns', { f: `400 9px ${FS}` });
  s += path(`M90 282H${XB(0.5)}V268H980`, A, 1.6);
  s += t(XB(0.52), 265, 'SYNC ↑ · 380 ns after the drive-off edge · arms 81150A · scope trigger', { f: `500 9px ${FS}`, c: AD });
  const xp = XB(0.5) + 0.5;
  s += path(`M90 296H${xp}`, hv ? A : K, hv ? 2.4 : 1.6); s += path(`M${xp} 296V318H${xp + 5}V296`, hc ? A : K, hc ? 2.4 : 1.6); s += path(`M${xp + 5} 296H980`, hv ? A : K, hv ? 2.4 : 1.6);
  s += t(XB(0.05), 291, 'V_pre = V_oc + 0.000 → 1.0423 V', { f: `${hv ? '600' : '500'} 9.5px ${F}`, c: hv ? A : K });
  s += t(xp + 20, 307, ':PULS:DEL1 = delay_ns 60 · V_coll −4.00 V for 5 µs', { f: `${(hd || hc) ? '600' : '400'} 9px ${F}`, c: hd ? A : (hc ? A : G) });
  s += t(xp + 20, 319, 'the 5 µs record starts 0.48 µs before the Sync', { f: `400 9px ${F}`, c: G });
  s += line(xp + 1, 330, xp + 1, 346, A, 1.6);
  s += t(xp + 20, 341, 'trigger ← 81150A SYNC · one record per cycle', { f: `400 9px ${FS}` });
  s += rect(xp - 8, 288, 22, 60, 'none', { s: A, d: '2 2' });
  s += path(`M${xp - 8} 348L90 390`, A, 1, '3 3', { op: 0.6 }); s += path(`M${xp + 14} 348L980 390`, A, 1, '3 3', { op: 0.6 });

  // C · the edges · zero = 81150A Sync
  const XC = (ns) => 90 + (ns + 480) / 780 * 890;
  s += t(8, 384, 'C · the edges · −480 … +300 ns · zero = 81150A Sync = scope trigger · measured 2026-09-01', { f: `600 11px ${FS}`, c: K });
  s += rect(90, 390, 890, 170, W, { s: L });
  s += t(8, 409, 'LED drive', { f: `600 10px ${FS}`, c: K }); s += t(8, 433, 'SYNC · CH3', { f: `600 10px ${FS}`, c: K }); s += t(8, 463, 'bias', { f: `600 10px ${FS}`, c: K }); s += t(8, 493, 'light at sample', { f: `600 10px ${FS}`, c: K }); s += t(8, 530, 'photocurrent', { f: `600 10px ${FS}`, c: K });
  [-380, 0, 60, 122].forEach((v) => { s += line(XC(v), 392, XC(v), 558, LL, 1); });
  s += path(`M90 402H${XC(-380)}V416H980`, K, 1.6);
  s += t(XC(-380) + 5, 413, '−380 · drive off', { f: `500 9px ${F}`, c: K });
  s += path(`M90 442H${XC(0)}V428H980`, A, 1.6);
  s += t(XC(0) - 5, 439, '0 · Sync = trigger', { a: 'end', f: `500 9px ${F}`, c: AD });
  s += path(`M90 450H${XC(60)}`, hv ? A : K, hv ? 2.4 : 1.6); s += path(`M${XC(60)} 450V472H980`, hc ? A : K, hc ? 2.4 : 1.6);
  s += t(XC(-460), 447, 'V_pre 1.0423 V', { f: `${hv ? '600' : '500'} 9px ${F}`, c: hv ? A : K });
  s += t(XC(60) + 8, 458, '+60 · :PULS:DEL1 = delay_ns · V_coll −4.00 V', { f: `${(hd || hc) ? '600' : '500'} 9px ${F}`, c: hd ? A : (hc ? A : K) });
  s += path(`M90 484H${XC(122)}V498H980`, K, 1.6, '5 3');
  s += t(XC(107) - 8, 495, 'light off at the sample · +122 · 502 ns after the drive edge', { a: 'end', f: `500 9px ${F}`, c: K });
  s += rect(XC(107), 506, XC(122) - XC(107), 42, A, { op: 0.14 });
  let dc = `M90 540H${XC(107)}L${XC(122)} 512`;
  for (let k = 1; k <= 12; k += 1) { const tn = 122 + k * 15, y = 512 + 28 * (1 - Math.exp(-(tn - 122) / 700)); dc += `L${XC(tn).toFixed(1)} ${y.toFixed(1)}`; }
  s += path(dc, K, 1.6);
  s += t(XC(107) - 8, 522, 'response +107 … 122 · t_bias 47–62 ns after the output edge', { a: 'end', f: `400 9px ${F}`, c: G });
  s += line(XC(118.5), 506, XC(118.5), 548, A, 1.5);
  s += t(XC(122) + 8, 546, 't0_int +118.5', { f: `600 9px ${F}`, c: A });
  [-400, -300, -200, -100, 0, 100, 200].forEach((v) => { s += line(XC(v), 560, XC(v), 564, K, 1) + t(XC(v), 574, String(v).replace('-', '−'), { a: 'middle' }); });
  s += t(980, 574, '300 ns', { a: 'end' });

  // D · the record
  const XD = (ns) => 90 + (ns + 480) / 5000 * 890;
  s += t(8, 594, 'D · the record · 5 µs · timebase 500 ns/div · 3125 pts · dt 1.60 ns', { f: `600 11px ${FS}`, c: K });
  s += rect(90, 600, 890, 130, W, { s: L });
  s += t(8, 626, 'bias', { f: `600 10px ${FS}`, c: K }); s += t(8, 680, 'photocurrent', { f: `600 10px ${FS}`, c: K });
  s += path(`M90 614H${XD(60)}`, hv ? A : K, hv ? 2.4 : 1.6); s += path(`M${XD(60)} 614V634H980`, hc ? A : K, hc ? 2.4 : 1.6);
  s += t(XD(-460), 610, 'V_pre', { f: `500 9.5px ${F}`, c: hv ? A : K });
  s += t(976, 610, 'V_coll −4.00 V · back to V_pre at +5.06 µs, after the record ends', { a: 'end', f: `${hc ? '600' : '500'} 9.5px ${F}`, c: hc ? A : K });
  s += rect(XD(118.5), 642, 980 - XD(118.5), 80, A, { op: 0.07 });
  s += line(XD(118.5), 642, XD(118.5), 722, A, 1.5);
  let dd = `M90 712H${XD(107)}L${XD(122)} 660`;
  for (let m = 1; m <= 60; m += 1) { const tm = 122 + m * 73, ym = 712 - 52 * Math.exp(-(tm - 122) / 700); dd += `L${XD(tm).toFixed(1)} ${ym.toFixed(1)}`; }
  dd += 'H980';
  s += path(dd, K, 1.6);
  s += t(XD(118.5) + 6, 652, 't0_int 118.5 ns from trigger', { f: `600 9.5px ${F}`, c: A });
  s += t(980 - 6, 656, 'Q = ∫ (light − dark) dt', { a: 'end', f: `500 9.5px ${F}`, c: A });
  [0, 1000, 2000, 3000, 4000].forEach((v) => { s += line(XD(v), 730, XD(v), 734, K, 1) + t(XD(v), 744, String(v), { a: 'middle' }); });
  s += t(980, 744, 'ns from trigger', { a: 'end', f: `400 9px ${FS}` });
  s += rect(90, 602, XD(300) - 90, 126, 'none', { s: A, d: '2 2' });
  s += path('M90 602L90 560', A, 1, '3 3', { op: 0.6 }); s += path(`M${XD(300)} 602L980 560`, A, 1, '3 3', { op: 0.6 });
  return s;
}

/** The markup as an `<svg>` that scales with its card. */
function svgOf({ width, height }, markup) {
  const el = root(width, height);
  el.innerHTML = markup;
  return el;
}

export const chainSvg = () => svgOf(CHAIN_SIZE, chainMarkup());
export const shotSvg = (axis) => svgOf(SHOT_SIZE, shotMarkup(axis));
