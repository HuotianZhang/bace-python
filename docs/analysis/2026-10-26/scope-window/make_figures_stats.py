"""Baseline and window from per-loop shots: run A (null) against run C (BACE).

A: shutter shut for both traces and the dark trace at the light trace's levels (dark_reference =
"same"), so light - dark should be zero everywhere; what is left is the amplifier's drift between two
acquisitions under the real load. C: ordinary BACE, the same pulse (20 us), record (2 us/div), 5 loops.
Both store every loop's light - dark (offset_correct off). Usage: make_figures_stats.py RUN_A RUN_C
"""
import glob, os, sys
import numpy as np
import h5py
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = r"Q:/Huotian/bace-python/runs"
RUN_A, RUN_C = (os.path.join(ROOT, a) for a in sys.argv[1:3])
OUT = r"D:/BACE/bace-python/docs/analysis/2026-10-26/scope-window"
WINDOWS = np.array([1.0, 1.5, 2, 3, 4, 5, 6, 7, 8, 10, 12, 15]) * 1e-6
TAIL_LEN = 1.0e-6        # the "last 10 %" stand-in: the 1 us after the window, as a record 10 % longer would have
LATE = (8e-6, 17e-6)     # after the step, for the differential drift: mean over this minus the pre-trigger mean

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
        d = dict(t=h["traces/time"][:], shots=h["traces/shots"][:], w0=h["traces/window"][0, 0],
                 vpre=h["axis/vpre"][:], voc=float(h["metadata"].attrs["voc_v"]),
                 L=h["traces/light"][:], D=h["traces/dark"][:])
    d["shots"] = d["shots"][~np.isnan(d["shots"]).any(axis=(1, 2))]       # completed loops only
    d["dt"] = d["t"][1] - d["t"][0]
    d["head"] = d["t"] < d["w0"] - 0.1e-6
    d["ts"] = d["t"] - d["w0"]
    return d

def q(p, d, width, base):
    """-Q over (0, width] after the step, for one shot p, baseline 'head' or 'tail'."""
    ts = d["ts"]
    if base == "head":
        b = p[d["head"]].mean()
    else:
        b = p[(ts >= width) & (ts < width + TAIL_LEN)].mean()
    m = (ts >= 0) & (ts < width)
    return -(p[m] - b).sum() * d["dt"]

def table(d):
    """Q[baseline][loop, step, window] in nC."""
    n_l, n_s, _ = d["shots"].shape
    out = {}
    for base in ("head", "tail"):
        a = np.empty((n_l, n_s, WINDOWS.size))
        for l in range(n_l):
            for s in range(n_s):
                for k, w in enumerate(WINDOWS):
                    a[l, s, k] = q(d["shots"][l, s], d, w, base)
        out[base] = a * 1e9
    return out

def drift(d):
    """Late minus pre-trigger mean of light - dark, per shot, uA."""
    late = (d["ts"] >= LATE[0]) & (d["ts"] < LATE[1])
    return np.array([[(p[late].mean() - p[d["head"]].mean()) * 1e6 for p in loop] for loop in d["shots"]])

A, C = load(RUN_A), load(RUN_C)
QA, QC = table(A), table(C)
dA, dC = drift(A), drift(C)
lbl = [f"Voc {1e3 * (v - C['voc']):+.0f} mV" for v in C["vpre"]]

# ----------------------------------------------------------------------------- 1 null run, per shot
fig, ax = plt.subplots(figsize=(7.5, 4.5))
k = 200
for l, loop in enumerate(A["shots"]):
    for s, p in enumerate(loop):
        y = np.convolve(p - p[A["head"]].mean(), np.ones(k) / k, mode="same")
        ax.plot(A["ts"] * 1e6, y * 1e3, color=COL[s], lw=0.8, alpha=0.8,
                label=f"point {s + 1}" if l == 0 else None)
ax.axhline(0, color="k", lw=0.6)
ax.set_xlim(-1.8, 17.8); ax.set_ylim(-0.03, 0.03)
ax.set_xlabel("time after step / us"); ax.set_ylabel("light - dark, pre-trigger baseline / mA")
ax.set_title(f"Null run A: two identical dark acquisitions, every shot ({A['shots'].shape[0]} loops x 3)")
ax.legend(loc="upper right")
save(fig, "stats-null-shots.png")

# ----------------------------------------------------------------------------- 2 differential drift, A vs C
fig, ax = plt.subplots(figsize=(7.5, 4.0))
for row, (name, dd) in enumerate((("A: null (identical traces)", dA), ("C: BACE", dC))):
    for s in range(3):
        y = dd[:, s]
        ax.plot(y, np.full(y.size, row) + (s - 1) * 0.1, "o", color=COL[s], ms=5, alpha=0.8,
                label=f"point {s + 1}" if row == 0 else None)
    ax.errorbar(dd.mean(), row + 0.3, xerr=dd.std(ddof=1), fmt="ks", ms=5, capsize=3)
ax.axvline(0, color="k", lw=0.6)
ax.set_yticks([0, 1], ["A: null", "C: BACE"]); ax.set_ylim(-0.5, 1.6)
ax.set_xlabel(f"late ({LATE[0] * 1e6:g}-{LATE[1] * 1e6:g} us) minus pre-trigger level of light - dark / uA")
ax.set_title("Differential drift per shot (square: mean +/- sd)")
ax.legend(loc="upper left")
save(fig, "stats-drift-null-vs-bace.png")

# ----------------------------------------------------------------------------- 3 Q vs window, both baselines
fig, ax = plt.subplots(figsize=(7.5, 4.8))
w_us = WINDOWS * 1e6
for base, mk, ls in (("head", "o", "-"), ("tail", "s", "--")):
    for s in range(3):
        a = QC[base][:, s, :]
        ax.errorbar(w_us + (s - 1) * 0.08, a.mean(0), yerr=a.std(0, ddof=1) / np.sqrt(a.shape[0]),
                    marker=mk, ms=4, ls=ls, lw=1, color=COL[s], capsize=2,
                    label=f"{lbl[s]}, {'pre-trigger' if base == 'head' else 'tail (1 us after window)'}")
null = QA["head"].reshape(-1, WINDOWS.size)
ax.fill_between(w_us, null.mean(0) - null.std(0, ddof=1), null.mean(0) + null.std(0, ddof=1),
                color="0.5", alpha=0.25, label="null run A, pre-trigger, mean +/- sd")
ax.axhline(0, color="k", lw=0.6)
ax.set_xlabel("integration window / us"); ax.set_ylabel("-Q / nC (mean +/- sem over loops)")
ax.set_title("Q vs window length, two baselines")
ax.legend(loc="center right", fontsize=6.5, ncol=1)
save(fig, "stats-q-vs-window.png")

# ----------------------------------------------------------------------------- 4 loop-to-loop scatter vs window
fig, ax = plt.subplots(figsize=(7.5, 4.5))
for base, mk, ls in (("head", "o", "-"), ("tail", "s", "--")):
    sd = QC[base].std(0, ddof=1).mean(0)            # sd over loops, averaged over the 3 points
    ax.plot(w_us, sd, marker=mk, ls=ls, color="C3", label=f"C, {base}")
    sdA = QA[base].std(0, ddof=1).mean(0)
    ax.plot(w_us, sdA, marker=mk, ls=ls, color="0.4", label=f"A (null), {base}")
ax.set_xlabel("integration window / us"); ax.set_ylabel("loop-to-loop sd of Q / nC")
ax.set_title("Repeatability of one loop's Q vs window length")
ax.set_ylim(0, None)
ax.legend()
save(fig, "stats-sd-vs-window.png")

# ----------------------------------------------------------------------------- numbers
lines = [f"A {os.path.basename(RUN_A)}  loops {A['shots'].shape[0]}",
         f"C {os.path.basename(RUN_C)}  loops {C['shots'].shape[0]}  Voc {C['voc']:.4f} V", ""]
lines.append(f"differential drift, late {LATE[0] * 1e6:g}-{LATE[1] * 1e6:g} us minus pre-trigger, uA (rows = loops, cols = points)")
for name, dd in (("A", dA), ("C", dC)):
    lines.append(f"{name}: mean {dd.mean():+.2f}  sd {dd.std(ddof=1):.2f}  per point mean " +
                 " ".join(f"{x:+.2f}" for x in dd.mean(0)))
    for row in dd:
        lines.append("   " + " ".join(f"{x:+.2f}" for x in row))
lines.append("")
for name, Q in (("C", QC), ("A", QA)):
    for base in ("head", "tail"):
        lines.append(f"{name} {base}: -Q / nC, mean over loops (sd) per point, by window")
        for k, w in enumerate(WINDOWS):
            a = Q[base][:, :, k]
            lines.append(f"  {w * 1e6:5.1f} us  " + "  ".join(
                f"{a[:, s].mean():+.4f} ({a[:, s].std(ddof=1):.4f})" for s in range(a.shape[1])))
with open(os.path.join(OUT, "numbers-stats.txt"), "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines) + "\n")
print("\n".join(lines))
