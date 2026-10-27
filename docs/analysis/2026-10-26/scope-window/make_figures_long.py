"""Figures for the 20 us pulse test run (2 us/div, record 20 us, step at ~2.04 us, window 17.5 us).

Same cell and conditions as make_figures.py (294 K, LED 1.000 V, Voc +/- 1 mV, Vcoll -4 V), with the
pulse and the record four times longer, to find where Q stops growing. offset_correct is off.
"""
import glob, os
import numpy as np
import h5py
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

RUN = r"Q:/Huotian/bace-python/runs/test_294K_1000mVLED_483mVVOC_20261026_133541"
SHORT = r"Q:/Huotian/bace-python/runs/test_293.99K_1000mVLED_484mVVOC_20261026_125108"
OUT = r"D:/BACE/bace-python/docs/analysis/2026-10-26/scope-window"
FIT = (8e-6, 17e-6)       # after the step: where light - dark is flat, fitted as a constant offset

plt.rcParams.update({
    "figure.dpi": 100, "savefig.dpi": 120, "font.size": 10, "axes.titlesize": 11,
    "axes.grid": True, "grid.alpha": 0.3, "legend.fontsize": 8, "legend.framealpha": 0.9,
})
COL = ["C0", "C1", "C2"]

def save(fig, name):
    fig.tight_layout()
    fig.savefig(os.path.join(OUT, name))
    plt.close(fig)

def load(path):
    with h5py.File(glob.glob(os.path.join(path, "run*.h5"))[0]) as h:
        d = dict(t=h["traces/time"][:], L=h["traces/light"][:], D=h["traces/dark"][:],
                 w=h["traces/window"][0], vpre=h["axis/vpre"][:], voc=float(h["metadata"].attrs["voc_v"]))
    d["dt"] = d["t"][1] - d["t"][0]
    head = d["t"] < d["w"][0] - 0.1e-6
    d["ph"] = np.array([(d["L"][i] - d["D"][i]) - (d["L"][i] - d["D"][i])[head].mean() for i in range(3)])
    m = d["t"] >= d["w"][0]
    d["ts"] = d["t"][m] - d["w"][0]
    d["q"] = np.array([-np.cumsum(p[m]) * d["dt"] for p in d["ph"]])     # -Q(t), C
    d["lbl"] = [f"Vpre = Voc {1e3 * (v - d['voc']):+.0f} mV" for v in d["vpre"]]
    return d

r = load(RUN)
s = load(SHORT)
ts = r["ts"]
fm = (ts >= FIT[0]) & (ts <= FIT[1])
fits = [np.polyfit(ts[fm], q[fm], 1) for q in r["q"]]          # slope (A), intercept (C)

# ----------------------------------------------------------------------------- 1 Q(t), 17.5 us
fig, ax = plt.subplots(figsize=(7.5, 4.5))
for i in range(3):
    ax.plot(ts * 1e6, r["q"][i] * 1e9, color=COL[i], lw=1.2, label=r["lbl"][i])
    sl, ic = fits[i]
    ax.plot(ts * 1e6, (ic + sl * ts) * 1e9, color=COL[i], ls=":", lw=1)
ax.axvline(4.3, color="C3", ls="--", label="4.3 us (500 ns/div window)")
ax.axvline(1.5, color="0.4", ls="--", label="1.5 us (old window)")
ax.axvspan(FIT[0] * 1e6, FIT[1] * 1e6, color="0.5", alpha=0.08, label="linear fit: constant offset")
ax.set_xlabel("integration time after step / us"); ax.set_ylabel("-Q(t) / nC")
ax.set_title("Q vs integration time, 20 us pulse, pre-trigger baseline")
ax.set_ylim(0, None)
ax.legend(loc="lower right")
save(fig, "long-cumulative-q.png")

# ----------------------------------------------------------------------------- 2 light - dark, log
fig, ax = plt.subplots(figsize=(7.5, 4.5))
edges = np.logspace(np.log10(5e-9), np.log10(17.4e-6), 60)
for i in range(3):
    m = r["t"] >= r["w"][0]
    y = -r["ph"][i][m]
    tc = np.sqrt(edges[:-1] * edges[1:])
    yc = np.array([y[(ts >= a) & (ts < b)].mean() if ((ts >= a) & (ts < b)).any() else np.nan
                   for a, b in zip(edges[:-1], edges[1:])])
    ax.loglog(tc * 1e6, np.where(yc > 0, yc, np.nan) * 1e3, "o-", ms=3, lw=1, color=COL[i], label=r["lbl"][i])
    ax.axhline(fits[i][0] * 1e3, color=COL[i], ls=":", lw=1)
ax.set_xlabel("time after step / us"); ax.set_ylabel("-(light - dark) / mA, log-binned")
ax.set_title("Extraction current falls to a flat offset (dotted) after ~5-8 us")
ax.legend(loc="lower left")
save(fig, "long-photocurrent-loglog.png")

# ----------------------------------------------------------------------------- 3 Q(t) minus offset, as fraction
fig, ax = plt.subplots(figsize=(7.5, 4.5))
for i in range(3):
    sl, ic = fits[i]
    qc = r["q"][i] - sl * ts
    ax.plot(ts * 1e6, qc / ic, color=COL[i], lw=1.2, label=f"{r['lbl'][i]}  (Q_inf = {ic * 1e9:.3f} nC)")
for f in (0.9, 0.95, 0.98):
    ax.axhline(f, color="0.5", lw=0.7, ls=":")
ax.axvline(4.3, color="C3", ls="--", label="4.3 us")
ax.set_xlim(0, 12); ax.set_ylim(0.5, 1.05)
ax.set_xlabel("integration time after step / us"); ax.set_ylabel("(Q(t) - offset x t) / Q_inf")
ax.set_title("Fraction of the transient collected, offset removed")
ax.legend(loc="lower right")
save(fig, "long-fraction-collected.png")

# ----------------------------------------------------------------------------- 4 raw traces, late
fig, ax = plt.subplots(figsize=(7.5, 4.5))
for i, (c, n) in enumerate((("C3", "light"), ("k", "dark"))):
    y = (r["L"] if n == "light" else r["D"])[1]
    k = 200
    ax.plot(r["t"] * 1e6, np.convolve(y, np.ones(k) / k, mode="same") * 1e3, color=c, lw=1, label=f"{n} (200 ns mean)")
ax.axhline(0, color="0.4", lw=0.6)
ax.axvline(r["w"][0] * 1e6, color="0.4", ls="--", lw=0.8)
ax.set_ylim(-0.3, 0.3)
ax.set_xlabel("record time / us"); ax.set_ylabel("current / mA")
ax.set_title("Raw traces at Voc, zoomed: both overshoot past zero after ~8 us")
ax.legend(loc="lower right")
save(fig, "long-traces-late.png")

# ----------------------------------------------------------------------------- 5 both runs
fig, ax = plt.subplots(figsize=(7.5, 4.5))
for i in range(3):
    ax.plot(r["ts"] * 1e6, r["q"][i] * 1e9, color=COL[i], lw=1.2, label=f"20 us pulse, {r['lbl'][i]}")
    ax.plot(s["ts"] * 1e6, s["q"][i] * 1e9, color=COL[i], lw=1, ls="--", label=f"5 us pulse, {s['lbl'][i]}")
ax.set_xlim(0, 6)
ax.set_xlabel("integration time after step / us"); ax.set_ylabel("-Q(t) / nC")
ax.set_title("Two runs 45 min apart, same cell: spread between points ~ +/-0.02 nC")
ax.legend(loc="lower right", ncol=2, fontsize=7)
save(fig, "long-vs-short.png")

with open(os.path.join(OUT, "numbers-long.txt"), "w", encoding="utf-8") as fh:
    fh.write(f"run {os.path.basename(RUN)}\nVoc {r['voc']:.4f} V, window {r['w'][0] * 1e6:.3f}-{r['w'][1] * 1e6:.3f} us, dt {r['dt'] * 1e9:.2f} ns\n\n")
    fh.write("offset fit 8-17 us: offset current / uA, Q_inf / nC\n")
    for i in range(3):
        fh.write(f"{r['lbl'][i]}  {fits[i][0] * 1e6:.2f}  {fits[i][1] * 1e9:.4f}\n")
    T = (1e-6, 1.5e-6, 2e-6, 3e-6, 4.3e-6, 5e-6, 6e-6, 8e-6, 10e-6, 15e-6, 17.4e-6)
    fh.write("\n-Q(t) / nC at " + " ".join(f"{x * 1e6:g}us" for x in T) + "\n")
    for i in range(3):
        fh.write(r["lbl"][i] + "  " + " ".join(f"{r['q'][i][np.searchsorted(ts, x) - 1] * 1e9:.4f}" for x in T) + "\n")
    fh.write("\n(Q(t) - offset x t) / Q_inf at the same times\n")
    for i in range(3):
        sl, ic = fits[i]
        fh.write(r["lbl"][i] + "  " + " ".join(
            f"{(r['q'][i][np.searchsorted(ts, x) - 1] - sl * x) / ic:.3f}" for x in T) + "\n")
print(open(os.path.join(OUT, "numbers-long.txt"), encoding="utf-8").read())
