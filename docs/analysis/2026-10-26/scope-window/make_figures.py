"""Figures for the 500 ns/div test run (sample "test", 294 K, LED 1.000 V, Voc +/- 1 mV, Vcoll -4 V).

One BACE run with the scope at 500 ns/div (record 5 us, step at ~0.54 us), the integration window
4.3 us and offset_correct off, so the raw light and dark traces can be baselined several ways here.
Run with the WinPython interpreter (numpy, h5py, matplotlib).
"""
import glob, os
import numpy as np
import h5py
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

RUN = r"Q:/Huotian/bace-python/runs/test_293.99K_1000mVLED_484mVVOC_20261026_125108"
OUT = r"D:/BACE/bace-python/docs/analysis/2026-10-26/scope-window"
os.makedirs(OUT, exist_ok=True)

HEAD_END = 0.45e-6        # pre-trigger baseline: record time 0-0.45 us (trigger at ~0.50 us)
OLD_END = 2.3e-6          # where a 200 ns/div record (trigger -0.2 ... +1.8 us) would have ended
OLD_WIDTH = 1.5e-6        # the old integration window

plt.rcParams.update({
    "figure.dpi": 100, "savefig.dpi": 120, "font.size": 10, "axes.titlesize": 11,
    "axes.grid": True, "grid.alpha": 0.3, "legend.fontsize": 8, "legend.framealpha": 0.9,
})
COL = ["C0", "C1", "C2"]

def save(fig, name):
    fig.tight_layout()
    fig.savefig(os.path.join(OUT, name))
    plt.close(fig)

# ----------------------------------------------------------------------------- load
with h5py.File(glob.glob(os.path.join(RUN, "run*.h5"))[0]) as h:
    t = h["traces/time"][:]
    L = h["traces/light"][:]
    D = h["traces/dark"][:]
    W = h["traces/window"][:]
    vpre = h["axis/vpre"][:]
    voc = float(h["metadata"].attrs["voc_v"])
dt = t[1] - t[0]
w0, w1 = W[0]
head = t < HEAD_END
ph = L - D
lbl = [f"Vpre = Voc {1e3 * (v - voc):+.0f} mV" for v in vpre]

def q_of(p, start, end):
    m = (t >= start) & (t < end)
    return p[m].sum() * dt

def cumq(p):
    m = t >= w0
    return t[m] - w0, np.cumsum(p[m]) * dt

base = {
    "head": [ph[i][head].mean() for i in range(3)],
    "tail": [ph[i][t > t[-1] * 0.9].mean() for i in range(3)],
    "old": [ph[i][(t > OLD_END - 0.2e-6) & (t < OLD_END)].mean() for i in range(3)],
}

# ----------------------------------------------------------------------------- 1 raw traces
fig, ax = plt.subplots(figsize=(7.5, 4.5))
ax.plot(t * 1e6, L[1] * 1e3, color="C3", lw=1, label="light")
ax.plot(t * 1e6, D[1] * 1e3, color="k", lw=1, alpha=0.7, label="dark reference")
ax.axvspan(w0 * 1e6, w1 * 1e6, color="C2", alpha=0.08, label="new window (4.3 us)")
ax.axvline(OLD_END * 1e6, color="C1", ls="--", label="old record end (200 ns/div)")
ax.set_xlabel("record time / us"); ax.set_ylabel("current / mA")
ax.set_title(f"Raw traces, Vpre = Voc ({voc:.3f} V), Vcoll = -4 V, 294 K")
ax.set_ylim(-12, 2)
ax.legend(loc="lower right")
save(fig, "traces-raw.png")

# ----------------------------------------------------------------------------- 2 raw traces, log
fig, ax = plt.subplots(figsize=(7.5, 4.5))
ts = t - w0 + 2e-9
m = ts > 5e-9
ax.loglog(ts[m] * 1e6, np.abs(L[1][m] - L[1][head].mean()) * 1e3, color="C3", lw=0.8, label="|light - head|")
ax.loglog(ts[m] * 1e6, np.abs(D[1][m] - D[1][head].mean()) * 1e3, color="k", lw=0.8, alpha=0.7, label="|dark - head|")
ax.axvline((OLD_END - w0) * 1e6, color="C1", ls="--", label="old record end")
ax.axhline(np.std(ph[1][head]) * 1e3, color="0.5", ls=":", label="noise of light - dark (head std)")
ax.set_xlabel("time after step / us"); ax.set_ylabel("|current| / mA")
ax.set_title("Both traces decay together; neither is flat by the old record end")
ax.legend(loc="lower left")
save(fig, "traces-loglog.png")

# ----------------------------------------------------------------------------- 3 light - dark
fig, ax = plt.subplots(figsize=(7.5, 4.5))
for i in range(3):
    p = ph[i] - base["head"][i]
    k = 25
    ax.plot(t * 1e6, np.convolve(p, np.ones(k) / k, mode="same") * 1e3, color=COL[i], lw=1, label=lbl[i])
ax.axvline(OLD_END * 1e6, color="C1", ls="--", label="old record end")
ax.axvspan(w0 * 1e6, w1 * 1e6, color="C2", alpha=0.06)
ax.axhline(0, color="k", lw=0.6)
ax.set_ylim(-0.6, 0.15)
ax.set_xlabel("record time / us"); ax.set_ylabel("light - dark, pre-trigger baseline / mA")
ax.set_title("Photo-extraction current (20 ns running mean)")
ax.legend(loc="lower right")
save(fig, "photocurrent.png")

# ----------------------------------------------------------------------------- 4 cumulative Q, head baseline
fig, ax = plt.subplots(figsize=(7.5, 4.5))
for i in range(3):
    tt, c = cumq(ph[i] - base["head"][i])
    ax.plot(tt * 1e6, -c * 1e9, color=COL[i], lw=1.2, label=lbl[i])
ax.axvline((OLD_END - w0) * 1e6, color="C1", ls="--", label="old record end")
ax.axvline(OLD_WIDTH * 1e6, color="C1", ls=":", label="old window end (1.5 us)")
ax.set_xlabel("integration time after step / us"); ax.set_ylabel("-Q(t) / nC")
ax.set_title("Charge vs integration time, pre-trigger baseline")
ax.set_ylim(0, None)
ax.legend(loc="lower right")
save(fig, "cumulative-q-head.png")

# ----------------------------------------------------------------------------- 5 cumulative Q, three baselines
fig, ax = plt.subplots(figsize=(7.5, 4.5))
styles = {"none": "-", "head": "-", "tail": "--"}
for i in range(3):
    for name, b, ls, a in (("no offset (the file's Q)", 0.0, ":", 0.9),
                           ("pre-trigger", base["head"][i], "-", 1.0),
                           ("last 10 % (4.5-5 us)", base["tail"][i], "--", 0.9)):
        tt, c = cumq(ph[i] - b)
        ax.plot(tt * 1e6, -c * 1e9, color=COL[i], ls=ls, lw=1, alpha=a,
                label=name if i == 0 else None)
ax.set_xlabel("integration time after step / us"); ax.set_ylabel("-Q(t) / nC")
ax.set_title("The same three cells under three baselines (colour = Vpre as before)")
ax.legend(loc="lower right")
save(fig, "cumulative-q-baselines.png")

# ----------------------------------------------------------------------------- 6 Q vs Vpre
rows = []
for i in range(3):
    rows.append((
        -q_of(ph[i], w0, w1),
        -q_of(ph[i] - base["head"][i], w0, w1),
        -q_of(ph[i] - base["tail"][i], w0, w1),
        -q_of(ph[i] - base["old"][i], w0, w0 + OLD_WIDTH),
        -q_of(ph[i] - base["head"][i], w0, w0 + OLD_WIDTH),
    ))
rows = np.array(rows) * 1e9
names = ["file: no offset, 4.3 us", "pre-trigger, 4.3 us", "last 10 %, 4.3 us",
         "old recipe emulated: 2 us record, last 10 %, 1.5 us", "pre-trigger, 1.5 us"]
fig, ax = plt.subplots(figsize=(7.5, 4.5))
dv = (vpre - voc) * 1e3
for j, (n, mk) in enumerate(zip(names, "osD^v")):
    ax.plot(dv, rows[:, j], marker=mk, lw=1, label=n)
ax.set_xlabel("Vpre - Voc / mV"); ax.set_ylabel("-Q / nC")
ax.set_xticks(dv)
ax.set_title("Q at Voc +/- 1 mV under each baseline and window")
ax.legend(loc="lower left", fontsize=7.5)
save(fig, "q-vs-vpre-baselines.png")

# ----------------------------------------------------------------------------- 7 head offsets
fig, ax = plt.subplots(figsize=(7.5, 4.0))
x = np.arange(3)
ax.plot(x, [L[i][head].mean() * 1e3 for i in range(3)], "o-", color="C3", label="light, pre-trigger mean")
ax.plot(x, [D[i][head].mean() * 1e3 for i in range(3)], "s-", color="k", label="dark, pre-trigger mean")
ax.plot(x, [ph[i][head].mean() * 1e3 + 0.2 for i in range(3)], "^--", color="C2",
        label="light - dark + 0.2 mA")
ax.set_xticks(x, [f"{d:+.0f} mV\n(point {i + 1}, 17 s apart)" for i, d in enumerate(dv)])
ax.set_ylabel("current before the step / mA")
ax.set_title("Pre-trigger level of each trace, point by point")
ax.legend()
save(fig, "head-offsets.png")

with open(os.path.join(OUT, "numbers.txt"), "w", encoding="utf-8") as fh:
    fh.write(f"run {os.path.basename(RUN)}\nVoc {voc:.4f} V, window {w0 * 1e6:.3f}-{w1 * 1e6:.3f} us, dt {dt * 1e9:.2f} ns\n\n")
    fh.write("-Q / nC: " + " | ".join(names) + "\n")
    for i in range(3):
        fh.write(f"{dv[i]:+.0f} mV  " + "  ".join(f"{v:.4f}" for v in rows[i]) + "\n")
    fh.write("\nbaselines / mA (head, tail, old)\n")
    for i in range(3):
        fh.write(f"{dv[i]:+.0f} mV  {base['head'][i] * 1e3:+.4f} {base['tail'][i] * 1e3:+.4f} {base['old'][i] * 1e3:+.4f}\n")
    fh.write("\nfraction of 4.3 us Q (pre-trigger baseline) reached at\n")
    for i in range(3):
        tt, c = cumq(ph[i] - base["head"][i])
        tot = c[np.searchsorted(tt, 4.3e-6) - 1]
        fh.write(f"{dv[i]:+.0f} mV  " + "  ".join(
            f"{T * 1e6:.1f}us {c[np.searchsorted(tt, T)] / tot:.3f}" for T in (0.5e-6, 1e-6, 1.5e-6, 1.77e-6, 2.5e-6, 3e-6, 3.5e-6)) + "\n")
print(open(os.path.join(OUT, "numbers.txt"), encoding="utf-8").read())
