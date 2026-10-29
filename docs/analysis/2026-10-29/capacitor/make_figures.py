"""A 56 pF capacitor in the device's place: the current conversion factor and the step's timing.

Run 1 steps the capacitor by three swings at a fixed delay; run 2 steps it by one swing at six
delays. Both have the shutter shut for both traces. The charge of one trace is integrated from 30 ns
before the step's onset for 500 ns, against the trace's own pre-trigger baseline.

The capacitor's current is C dV/dt, so its own half-rise and peak mark the current spike, not the
voltage. The voltage step is the trace's cumulative charge over its final value, V(t) = Q(t)/C: its
10, 50 and 90 % crossings time the step as the digitiser sees it, through the measurement chain's own
response. Usage: make_figures.py RUN_1 RUN_2
"""
import glob, os, sys
import numpy as np
import h5py
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = r"Q:/Huotian/bace-python/runs"
RUN_1, RUN_2 = (os.path.join(ROOT, a) for a in sys.argv[1:3])
OUT = os.path.dirname(os.path.abspath(__file__))
C_NOMINAL = 56e-12
SPAN = (-30e-9, 500e-9)          # integration of one trace, from the step's onset

plt.rcParams.update({
    "figure.dpi": 100, "savefig.dpi": 120, "font.size": 10, "axes.titlesize": 11,
    "axes.grid": True, "grid.alpha": 0.3, "legend.fontsize": 8, "legend.framealpha": 0.9,
})


def load(path):
    with h5py.File(glob.glob(os.path.join(path, "run*.h5"))[0]) as h:
        d = dict(t=h["traces/time"][:], L=h["traces/light"][:], D=h["traces/dark"][:],
                 t0=h["traces/trace_t0"][:], win=h["traces/window"][:],
                 vpre=h["axis/vpre"][:], vcoll=h["axis/vcoll"][:], delay=h["axis/delay_ns"][:],
                 q=h["charge/mean"][:])
    d["dt"] = d["t"][1] - d["t"][0]
    return d


def step(d, i, y):
    """Onset (10 sigma), half-rise and peak of trace `y` at point `i`, in record time."""
    trig = -d["t0"][i]
    pre = d["t"] < trig - 10e-9
    y = y - y[pre].mean()
    after = np.where(d["t"] > trig + d["delay"][i] * 1e-9 - 20e-9)[0]
    yy = y[after]
    k = np.argmax(np.abs(yy))
    onset = after[np.argmax(np.abs(yy) > 10 * y[pre].std())]
    j = after[np.argmax(np.abs(yy) >= abs(yy[k]) / 2)]
    a, b = abs(y[j - 1]), abs(y[j])
    half = d["t"][j - 1] + (abs(yy[k]) / 2 - a) / (b - a) * d["dt"]
    return y, trig, d["t"][onset], half, d["t"][after[k]]


def voltage_crossings(d, i, y, levels=(0.1, 0.5, 0.9)):
    """Record times at which the cumulative charge reaches `levels` of its value 120-200 ns after
    the step: the voltage step reconstructed as V = Q/C."""
    trig = -d["t0"][i]
    start = trig + d["delay"][i] * 1e-9
    w = (d["t"] > start - 10e-9) & (d["t"] < start + 200e-9)
    tt, c = d["t"][w], np.cumsum(y[w]) * d["dt"]
    f = c / c[tt > start + 120e-9].mean()
    out = []
    for level in levels:
        j = np.argmax(f >= level)
        out.append(tt[j - 1] + (level - f[j - 1]) / (f[j] - f[j - 1]) * d["dt"])
    return out


def charge(d, y, onset):
    w = (d["t"] > onset + SPAN[0]) & (d["t"] <= onset + SPAN[1])
    return y[w].sum() * d["dt"]


r1, r2 = load(RUN_1), load(RUN_2)
lines = [f"run 1 {os.path.basename(RUN_1)}", f"run 2 {os.path.basename(RUN_2)}", ""]

# -- run 1: Q against the swing ---------------------------------------------------------
lines.append("run 1: single-trace charge against the swing dV = V_pre - V_coll (light and dark traces)")
lines.append("  dV/V   Q_light/pC  Q_dark/pC  light-dark in the run's window/pC")
dv, ql, qd = [], [], []
for i in range(len(r1["vcoll"])):
    yl, _, on, _, _ = step(r1, i, r1["L"][i])
    yd, _, ond, _, _ = step(r1, i, r1["D"][i])
    dv.append(r1["vpre"][i] - r1["vcoll"][i])
    ql.append(charge(r1, yl, on))
    qd.append(charge(r1, yd, ond))
    lines.append(f"  {dv[-1]:5.3f}  {ql[-1] * 1e12:9.1f}  {qd[-1] * 1e12:9.1f}  {r1['q'][i] * 1e12:9.2f}")
dv, ql, qd = map(np.array, (dv, ql, qd))
fits = {}
for name, q in (("light", ql), ("dark", qd)):
    k, b = np.polyfit(dv, q, 1)
    fits[name] = (k, b)
    lines.append(f"  {name}: |slope| {abs(k) * 1e12:.2f} pF, intercept {b * 1e12:+.2f} pC; "
                 f"ratio to {C_NOMINAL * 1e12:.0f} pF nominal {abs(k) / C_NOMINAL:.3f}")

# -- run 2: the step's timing against the delay ------------------------------------------
lines += ["", "run 2: the step at each delay, in ns after the trigger (light trace)",
          "  delay | current: onset  half   peak | voltage: V10   V50   V90  V50-delay | window start  Q/pC"]
rows = []
for i in range(len(r2["delay"])):
    y, trig, on, half, peak = step(r2, i, r2["L"][i])
    v10, v50, v90 = voltage_crossings(r2, i, y)
    rows.append((r2["delay"][i], (on - trig) * 1e9, (half - trig) * 1e9, (peak - trig) * 1e9,
                 (v10 - trig) * 1e9, (v50 - trig) * 1e9, (v90 - trig) * 1e9,
                 (r2["win"][i, 0] - trig) * 1e9, charge(r2, y, on)))
    r = rows[-1]
    lines.append(f"  {r[0]:5.0f} |   {r[1]:6.1f} {r[2]:6.1f} {r[3]:6.1f} |  {r[4]:6.1f} {r[5]:6.1f} "
                 f"{r[6]:6.1f}  {r[5] - r[0]:6.2f}  | {r[7]:7.1f}  {r[8] * 1e12:6.1f}")
delay = np.array([r[0] for r in rows])
for name, col in (("current half-rise", 2), ("voltage 50 %", 5)):
    v = np.array([r[col] for r in rows])
    k, b = np.polyfit(delay, v, 1)
    lines.append(f"  {name} = {k:.4f} x delay + {b:.2f} ns, residual rms "
                 f"{np.std(v - (k * delay + b)):.2f} ns")
lines.append(f"  voltage 10-90 % rise: {np.mean([r[6] - r[4] for r in rows]):.1f} ns")
lines.append(f"  window start - voltage 50 %: {np.mean([r[7] - r[5] for r in rows]):.1f} ns; "
             f"- voltage 90 %: {np.mean([r[7] - r[6] for r in rows]):.1f} ns")

with open(os.path.join(OUT, "numbers.txt"), "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines) + "\n")
print("\n".join(lines))

# -- figures -------------------------------------------------------------------------------
fig, ax = plt.subplots(1, 2, figsize=(10, 3.8))
x = np.linspace(0, dv.max() * 1.05, 50)
for name, q, m in (("light", ql, "o"), ("dark", qd, "s")):
    ax[0].plot(dv, -q * 1e12, m, label=f"{name} trace")
    k, b = fits[name]
    ax[0].plot(x, -(k * x + b) * 1e12, lw=0.8)
ax[0].plot(x, C_NOMINAL * x * 1e12, "k--", lw=0.8, label="56 pF × ΔV")
ax[0].set(xlabel="ΔV = V_pre − V_coll (V)", ylabel="−Q of one trace (pC)",
          title="Charge against the swing")
ax[0].legend()
for i in range(len(r2["delay"])):
    y, trig, _, _, _ = step(r2, i, r2["L"][i])
    t = (r2["t"] - trig) * 1e9
    sel = (t > -10) & (t < 260)
    ax[1].plot(t[sel], y[sel] * 1e3, lw=0.9, label=f"delay {r2['delay'][i]:.0f} ns")
    ax[1].axvline((r2["win"][i, 0] - trig) * 1e9, lw=0.6, ls=":", color=f"C{i}")
ax[1].set(xlabel="time after the trigger (ns)", ylabel="current (mA)",
          title="The step at each delay; dotted: window start")
ax[1].legend(ncol=2)
fig.tight_layout()
fig.savefig(os.path.join(OUT, "capacitor.png"))
