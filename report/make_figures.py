"""Build the four report/poster figures from the results files ONLY.

    pip install -r requirements-report.txt
    python experiments/run_offline.py     # produces the results files
    python report/make_figures.py         # -> report/figures/*.png + captions.json

(a) architecture  (b) dead-zone illustration (worked examples)  (c) E3  (d) E4
No number in these figures is typed in: (b)-(d) read experiments/results/.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "experiments" / "results"
OUT = ROOT / "report" / "figures"

# Colour-blind-safe categorical slots (validated: first three pass all pairs).
BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
INK, INK2, GRID = "#0b0b0b", "#52514e", "#e1e0d9"
GOOD, CRITICAL, NEUTRAL = "#0ca30c", "#d03b3b", "#898781"

plt.rcParams.update({
    "font.size": 12, "axes.titlesize": 13, "axes.labelsize": 12, "legend.fontsize": 11,
    "xtick.labelsize": 11, "ytick.labelsize": 11, "font.family": "DejaVu Sans",
    "axes.edgecolor": "#c3c2b7", "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8,
    "axes.spines.top": False, "axes.spines.right": False, "savefig.dpi": 200,
})


def need(name: str) -> dict:
    path = RESULTS / name
    if not path.exists():
        sys.exit(f"missing {path}: run `python experiments/run_offline.py` first")
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
def fig_architecture(path: Path) -> str:
    fig, ax = plt.subplots(figsize=(10, 5.2))
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 56)
    ax.axis("off")

    def box(x, y, w, h, title, lines, colour):
        ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.6,rounding_size=1.5",
                                    linewidth=1.6, edgecolor=colour, facecolor="white"))
        ax.text(x + w / 2, y + h - 3, title, ha="center", va="top", fontsize=13, weight="bold", color=INK)
        for i, line in enumerate(lines):
            ax.text(x + w / 2, y + h - 8.5 - i * 4.2, line, ha="center", va="top", fontsize=11, color=INK2)

    def arrow(x1, y1, x2, y2, label, dy=1.6):
        ax.add_patch(FancyArrowPatch((x1, y1), (x2, y2), arrowstyle="-|>", mutation_scale=16, linewidth=1.5,
                                     color=INK2))
        ax.text((x1 + x2) / 2, (y1 + y2) / 2 + dy, label, ha="center", fontsize=10, color=INK)

    box(1, 24, 18, 28, "Client", ["submit job", "{type, params,", "max_delay_h}", "GET /me"], BLUE)
    box(33, 6, 27, 46, "Server", ["choose_slot (plan once)", "queue + tick", "(decay, leases)",
                                    "settle_job ->", "compute_settlement", "append-only ledger",
                                    "simulated clock + grid", "dashboard"], ORANGE)
    box(77, 24, 21, 28, "Worker (laptop)", ["claims jobs (pull)", "runs catalog job", "Phase 1 meter",
                                              "posts result"], AQUA)
    box(77, 1, 21, 17, "LHM", ["LibreHardwareMonitor", "CPU Package W"], NEUTRAL)
    arrow(21, 44, 32, 44, "POST /jobs")
    arrow(32, 34, 21, 34, "plan, balance")
    arrow(76, 46, 61, 46, "claim (pull)")
    arrow(61, 38, 76, 38, "job {type, params}")
    arrow(76, 30, 61, 30, "result + telemetry")
    arrow(87.5, 18.8, 87.5, 23, "", dy=0)
    ax.text(89, 20, "data.json", fontsize=10.5, color=INK)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return ("Architecture: the worker on the laptop pulls jobs; the server plans with choose_slot and settles "
            "measured energy into an append-only ledger.")


def fig_deadzone(path: Path) -> str:
    data = need("worked_examples.json")
    k = data["params"]["k"]
    fig, ax = plt.subplots(figsize=(9, 4.2))
    colours = {"saving": GOOD, "dead_zone": NEUTRAL, "backfire": CRITICAL}
    labels = {"saving": "saving", "dead_zone": "dead zone", "backfire": "backfire"}
    cases = data["cases"]
    label_x = max(c["saved_g"] + k * c["saved_unc_g"] for c in cases) + 0.015  # one label column, clear of the bands
    for i, c in enumerate(cases):
        y = len(cases) - i
        lo, hi = c["saved_g"] - k * c["saved_unc_g"], c["saved_g"] + k * c["saved_unc_g"]
        colour = colours[c["zone"]]
        ax.plot([lo, hi], [y, y], color=colour, linewidth=6, alpha=0.35, solid_capstyle="butt")
        ax.plot([c["saved_g"]], [y], "o", color=colour, markersize=9, markeredgecolor="white", markeredgewidth=2)
        ax.text(label_x, y, f"{c['name'].split()[0]}: {labels[c['zone']]} | rebate {c['rebate']:.2f}, "
                f"net {c['net']:.2f} credits", va="center", fontsize=10.5, color=INK)
    ax.axvline(0, color=INK, linewidth=1.2)
    ax.text(0.002, len(cases) + 0.55, "zero", fontsize=10.5, color=INK)
    ax.set_yticks([])
    ax.set_ylim(0.3, len(cases) + 0.9)
    lo_all = min(c["saved_g"] - k * c["saved_unc_g"] for c in cases)
    hi_all = max(c["saved_g"] + k * c["saved_unc_g"] for c in cases)
    ax.set_xlim(lo_all - 0.02, hi_all + 0.19)
    ax.set_xlabel(f"saved emissions (g CO2e); band = saved ± k × uncertainty, k = {k:g}")
    ax.grid(axis="y", visible=False)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return ("Dead zone: a rebate is paid only if the whole band lies above zero, and a backfire is declared only if "
            "it lies below zero (worked examples A, C, D, computed by compute_settlement).")


def fig_e3(path: Path) -> str:
    data = need("e3.json")
    rows = data["rows"]
    z = 1.0
    fig, ax = plt.subplots(figsize=(8, 4.6))
    for err, colour, marker in ((0.0, BLUE, "o"), (0.10, ORANGE, "s"), (0.20, AQUA, "^")):
        pts = sorted((r["delay_h"], r["change_pct"]) for r in rows if r["forecast_error"] == err and r["defer_z"] == z)
        ax.plot([p[0] for p in pts], [p[1] for p in pts], marker=marker, color=colour, linewidth=2, markersize=8,
                label=f"forecast error {err:.0%} (simulated)")
        ax.annotate(f"{pts[-1][1]:+.1f} %", (pts[-1][0], pts[-1][1]), textcoords="offset points", xytext=(6, -4),
                    fontsize=10.5, color=INK)
    ax.axhline(0, color=INK2, linewidth=1)
    ax.set_xlabel("delay tolerance (hours)")
    ax.set_ylabel("change in gCO2e per job vs run immediately (%)")
    ax.set_xticks(sorted({r["delay_h"] for r in rows}))
    ax.legend(loc="lower left", frameon=False)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return (f"E3 (simulated grid and forecast): emissions per job relative to running immediately, choose_slot "
            f"with defer_z = {z:g}, {rows[0]['jobs']} jobs over {data['days']} days.")


def fig_e4(path: Path) -> str:
    data = need("e4.json")
    rows = data["rows"]
    fig, ax = plt.subplots(figsize=(8, 5.0))
    for gain, colour, marker, label in ((0.0, CRITICAL, "o", "true gain 0 % (false rewards)"),
                                        (0.20, ORANGE, "s", "true gain 20 %"),
                                        (0.40, BLUE, "^", "true gain 40 %")):
        pts = sorted((r["k"], 100 * r["reward_rate"]) for r in rows if r["true_gain"] == gain)
        ax.plot([p[0] for p in pts], [p[1] for p in pts], marker=marker, color=colour, linewidth=2, markersize=7,
                label=label)
    ax.set_xlabel("dead-zone width k (standard uncertainties)")
    ax.set_ylabel("jobs that received a rebate (%)")
    ax.set_ylim(-3, 103)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.2), ncol=3, frameon=False, fontsize=10.5)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    jobs = rows[0]["jobs"]
    return (f"E4 (simulated 10 % measurement noise, {jobs} jobs per point): a wider dead zone removes false rewards "
            f"faster than deserved ones.")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    captions = {
        "architecture": fig_architecture(OUT / "fig_a_architecture.png"),
        "deadzone": fig_deadzone(OUT / "fig_b_deadzone.png"),
        "e3": fig_e3(OUT / "fig_c_e3.png"),
        "e4": fig_e4(OUT / "fig_d_e4.png"),
    }
    (OUT / "captions.json").write_text(json.dumps(captions, indent=2), encoding="utf-8")
    for name, cap in captions.items():
        print(f"{name}: {cap}")
    print(f"Wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
