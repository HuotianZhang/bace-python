"""Settle times and hardware averages against Q and measurement time.

Eight BACE runs at Voc, 6 loops each, 1 us/div, 6 us window, pre-trigger baseline, alternating order;
four conditions run twice, interleaved (slow, fast, fastest, fast50, slow, ...). Reads the run ids from
the service and the folders from the run records; times each shot from the journal's StepDone stamps.
Usage: make_figures_settle.py JOURNAL RUN_ID...
"""
import glob, json, os, sys, urllib.request
import numpy as np
import h5py
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = r"Q:/Huotian/bace-python"
OUT = r"D:/BACE/bace-python/docs/analysis/2026-10-26/scope-window"
JOURNAL, RUN_IDS = sys.argv[1], sys.argv[2:]
ORDER = ["slow", "fast", "fastest", "fast50"]
SETTLE = {"slow": "2 / 2 / 5 s, 200 avg", "fast": "0.5 / 0.5 / 1 s, 200 avg",
          "fastest": "0.1 / 0.1 / 0.2 s, 200 avg", "fast50": "0.5 / 0.5 / 1 s, 50 avg"}

plt.rcParams.update({
    "figure.dpi": 100, "savefig.dpi": 120, "font.size": 10, "axes.titlesize": 11,
    "axes.grid": True, "grid.alpha": 0.3, "legend.fontsize": 8, "legend.framealpha": 0.9,
})

def save(fig, name):
    fig.tight_layout()
    fig.savefig(os.path.join(OUT, name))
    plt.close(fig)

stamps = {}
for line in open(JOURNAL, encoding="utf-8"):
    e = json.loads(line)
    if e.get("type") == "StepDone" and e.get("run_id") in RUN_IDS:
        stamps.setdefault(e["run_id"], []).append(e["ts"])

runs = []
for rid in RUN_IDS:
    rec = json.load(urllib.request.urlopen(f"http://127.0.0.1:8900/runs/{rid}"))
    cond, rnd = rec["name"].split("-")[1], int(rec["name"][-1])
    folder = os.path.join(ROOT, rec["nodes"]["bace"]["folder"])
    with h5py.File(glob.glob(os.path.join(folder, "run*.h5"))[0]) as h:
        q = -h["charge/q"][:, 0] * 1e9                    # nC, positive
        inten = h["intensity/mean"][()] if "intensity" in h else np.nan
    ts = np.array(stamps[rid])
    runs.append(dict(rid=rid, cond=cond, rnd=rnd, q=q, shot_s=np.median(np.diff(ts)),
                     voc=rec["nodes"]["bace"]["voc"], intensity=float(np.ravel(inten)[0])))

# ----------------------------------------------------------------------------- 1 Q per loop by condition
fig, ax = plt.subplots(figsize=(7.5, 4.5))
for k, cond in enumerate(ORDER):
    for r in (r for r in runs if r["cond"] == cond):
        x = k + (r["rnd"] - 1.5) * 0.25
        ax.plot(np.full(r["q"].size, x), r["q"], "o", ms=4, alpha=0.6, color=f"C{k}")
        ax.errorbar(x + 0.06, r["q"].mean(), yerr=r["q"].std(ddof=1) / np.sqrt(r["q"].size),
                    fmt="ks", ms=4, capsize=3)
ax.set_xticks(range(len(ORDER)), [f"{c}\n{SETTLE[c]}" for c in ORDER], fontsize=7.5)
ax.set_ylabel("-Q per loop / nC")
ax.set_title("Q at Voc per loop, two runs per condition (square: mean +/- sem)")
save(fig, "settle-q-by-condition.png")

# ----------------------------------------------------------------------------- 2 cost of precision
fig, ax = plt.subplots(figsize=(7.5, 4.5))
for k, cond in enumerate(ORDER):
    rs = [r for r in runs if r["cond"] == cond]
    q = np.concatenate([r["q"] for r in rs])
    t = np.mean([r["shot_s"] for r in rs])
    ax.plot(t, q.std(ddof=1) * np.sqrt(t), "o", ms=8, color=f"C{k}", label=f"{cond}: {t:.1f} s per shot")
ax.set_xlabel("time per shot / s"); ax.set_ylabel("sd per loop x sqrt(time per shot) / nC s^0.5")
ax.set_title("Uncertainty for a fixed time budget (lower is better)")
ax.set_ylim(0, None)
ax.legend()
save(fig, "settle-cost-of-precision.png")

lines = ["condition, round, run, Voc / V, intensity / uW, s per shot, -Q mean / nC, sd, sem"]
for r in runs:
    q = r["q"]
    lines.append(f"{r['cond']:8s} {r['rnd']} {r['rid']}  {r['voc']:.4f}  {r['intensity'] * 1e6:.3f}  "
                 f"{r['shot_s']:5.2f}  {q.mean():.4f}  {q.std(ddof=1):.4f}  {q.std(ddof=1) / np.sqrt(q.size):.4f}")
lines.append("\npooled per condition: -Q mean / nC, sem, sd per loop, s per shot, "
             "minutes for sem = 1 % of Q at one point")
ref = None
for cond in ORDER:
    rs = [r for r in runs if r["cond"] == cond]
    q = np.concatenate([r["q"] for r in rs])
    t = np.mean([r["shot_s"] for r in rs])
    sd, m = q.std(ddof=1), q.mean()
    need = (sd / (0.01 * m)) ** 2 * t / 60
    sem = sd / np.sqrt(q.size)
    if ref is None:
        ref = (m, sem)
    diff = m - ref[0]
    lines.append(f"{cond:8s} {m:.4f}  {sem:.4f}  {sd:.4f}  {t:5.2f}  {need:6.1f}   "
                 f"vs slow {diff:+.4f} +/- {np.hypot(sem, ref[1]):.4f}")
with open(os.path.join(OUT, "numbers-settle.txt"), "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines) + "\n")
print("\n".join(lines))
