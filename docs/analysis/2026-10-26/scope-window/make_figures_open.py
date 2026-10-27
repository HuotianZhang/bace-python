"""Figures for the open-circuit control: sample disconnected, the same 20 us pulse and 2 us/div record.

Compares run 141941 (no sample) with run 133541 (sample connected, otherwise identical). Vpre is the
typed Voc of 133541 (0.4834 V), so both runs have the same light and dark steps.
"""
import glob, os
import numpy as np
import h5py
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

OPEN = r"Q:/Huotian/bace-python/runs/test_294.01K_1000mVLED_483mVVOC_20261026_141941"
CONN = r"Q:/Huotian/bace-python/runs/test_294K_1000mVLED_483mVVOC_20261026_133541"
OUT = r"D:/BACE/bace-python/docs/analysis/2026-10-26/scope-window"
ZERO_MA = 0.162   # scope zero at the run's range with nothing driven (scope_offset_probe_20261026_141133)

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
                 w=h["traces/window"][0], vpre=h["axis/vpre"][:])
    d["dt"] = d["t"][1] - d["t"][0]
    d["head"] = d["t"] < d["w"][0] - 0.1e-6
    m = d["t"] >= d["w"][0]
    d["ts"] = d["t"][m] - d["w"][0]
    d["q"] = np.array([-np.cumsum(((d["L"][i] - d["D"][i]) - (d["L"][i] - d["D"][i])[d["head"]].mean())[m]) * d["dt"]
                       for i in range(3)])
    return d

def run_mean(y, k=200):
    return np.convolve(y, np.ones(k) / k, mode="same")

o = load(OPEN)
c = load(CONN)
t = o["t"] * 1e6

# ----------------------------------------------------------------------------- 1 raw, late, both runs
fig, ax = plt.subplots(figsize=(7.5, 4.5))
ax.plot(t, run_mean(c["L"][1]) * 1e3, color="C3", lw=1, label="sample connected, light")
ax.plot(t, run_mean(c["D"][1]) * 1e3, color="C3", lw=1, ls="--", label="sample connected, dark")
ax.plot(t, run_mean(o["L"][1]) * 1e3, color="k", lw=1, label="sample disconnected, light")
ax.plot(t, run_mean(o["D"][1]) * 1e3, color="k", lw=1, ls="--", label="sample disconnected, dark")
ax.axhline(ZERO_MA, color="C2", ls=":", label="scope zero, nothing driven (0.162 mA)")
ax.axvline(o["w"][0] * 1e6, color="0.4", lw=0.8)
ax.set_ylim(-0.3, 0.3)
ax.set_xlabel("record time / us"); ax.set_ylabel("current / mA (200 ns mean)")
ax.set_title("After the step the baseline sags by ~0.15 mA, with or without the sample")
ax.legend(loc="lower right", fontsize=7.5)
save(fig, "open-vs-connected-raw.png")

# ----------------------------------------------------------------------------- 2 open circuit, full scale
fig, ax = plt.subplots(figsize=(7.5, 4.5))
ax.plot(t, o["L"][1] * 1e3, color="k", lw=0.6, label="light")
ax.plot(t, o["D"][1] * 1e3, color="C0", lw=0.6, alpha=0.7, label="dark")
ax.set_xlim(1.5, 6)
ax.set_xlabel("record time / us"); ax.set_ylabel("current / mA")
ax.set_title("Sample disconnected: the step itself gives only a -2.4 mA spike")
ax.legend(loc="lower right")
save(fig, "open-raw-spike.png")

# ----------------------------------------------------------------------------- 3 light - dark, both
fig, ax = plt.subplots(figsize=(7.5, 4.5))
for d, ls, name in ((c, "-", "connected"), (o, "--", "disconnected")):
    p = (d["L"][1] - d["D"][1]) - (d["L"][1] - d["D"][1])[d["head"]].mean()
    ax.plot(t, run_mean(p) * 1e3, color="C3" if name == "connected" else "k", ls=ls, lw=1,
            label=f"light - dark, sample {name}")
ax.axhline(0, color="0.4", lw=0.6)
ax.set_ylim(-0.03, 0.015)
ax.set_xlabel("record time / us"); ax.set_ylabel("light - dark, pre-trigger baseline / mA (200 ns mean)")
ax.set_title("Late light - dark: a few uA either way, with or without the sample")
ax.legend(loc="lower right")
save(fig, "open-vs-connected-difference.png")

# ----------------------------------------------------------------------------- 4 Q(t) both
fig, ax = plt.subplots(figsize=(7.5, 4.5))
for i in range(3):
    ax.plot(c["ts"] * 1e6, c["q"][i] * 1e9, color=COL[i], lw=1.2,
            label=f"connected, point {i + 1}")
    ax.plot(o["ts"] * 1e6, o["q"][i] * 1e9, color=COL[i], lw=1, ls="--",
            label=f"disconnected, point {i + 1}")
ax.axhline(0, color="0.4", lw=0.6)
ax.axvline(6, color="C3", ls=":", label="6 us")
ax.set_xlabel("integration time after step / us"); ax.set_ylabel("-Q(t) / nC, pre-trigger baseline")
ax.set_title("Charge with and without the sample")
ax.legend(loc="center right", ncol=2, fontsize=7)
save(fig, "open-vs-connected-q.png")

T = (1.5e-6, 3e-6, 4.3e-6, 6e-6, 8e-6, 12e-6, 17.4e-6)
lines = [f"open {os.path.basename(OPEN)}\nconnected {os.path.basename(CONN)}\n",
         "-Q(t) / nC, pre-trigger baseline, at " + " ".join(f"{x * 1e6:g}us" for x in T)]
for name, d in (("connected", c), ("disconnected", o)):
    for i in range(3):
        lines.append(f"{name:12s} point {i + 1}  " + " ".join(
            f"{d['q'][i][np.searchsorted(d['ts'], x) - 1] * 1e9:+.4f}" for x in T))
lines.append("\nraw level / mA (200 ns mean, point 2): pre-trigger, then 16-20 us")
for name, d in (("connected", c), ("disconnected", o)):
    late = (d["t"] > 16e-6)
    lines.append(f"{name:12s} light {d['L'][1][d['head']].mean() * 1e3:+.4f} {d['L'][1][late].mean() * 1e3:+.4f}"
                 f"  dark {d['D'][1][d['head']].mean() * 1e3:+.4f} {d['D'][1][late].mean() * 1e3:+.4f}")
with open(os.path.join(OUT, "numbers-open.txt"), "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines) + "\n")
print("\n".join(lines))
