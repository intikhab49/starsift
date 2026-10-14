"""Generate the README chart from docs/validation.json. Regenerate with:  python docs/make_charts.py

Each dot is one repo's rise over its control for one signal, from the validation run. The values are
stored sorted, so no dot can be traced back to a repo. Palette and style follow the Open Jev charts.
"""
import json
import pathlib
import random

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from starsift.judge import SIGNALS  # noqa: E402

SURFACE, INK, INK_2, MUTED, BASELINE = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#c3c2b7"
S1, CRITICAL = "#2a78d6", "#d03b3b"
HERE = pathlib.Path(__file__).parent
ORDER = ("gone", "young", "hollow", "batch")


def style():
    matplotlib.rcParams.update({
        "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
        "text.color": INK, "axes.labelcolor": INK_2, "xtick.color": MUTED, "ytick.color": INK_2,
        "axes.edgecolor": BASELINE, "axes.linewidth": 1.0,
        "axes.spines.top": False, "axes.spines.right": False,
        "axes.grid": True, "grid.color": BASELINE, "grid.alpha": 0.45, "grid.linewidth": 0.8,
        "font.size": 11, "axes.titlesize": 13, "axes.titleweight": "bold",
        "axes.titlelocation": "left", "axes.titlepad": 12, "figure.dpi": 120,
    })


def main():
    result = json.loads((HERE / "validation.json").read_text(encoding="utf-8"))
    sets = result["sets"]
    signals = {s.key: s for s in SIGNALS}
    style()
    fig, ax = plt.subplots(figsize=(12.5, 5.4))
    jitter = random.Random(7)
    for row, key in enumerate(reversed(ORDER)):
        signal = signals[key]
        for label, offset, colour in (("suspected", 0.17, CRITICAL), ("unsuspected", -0.17, S1)):
            values = [100 * v for v in sets[label]["gaps"].get(key, [])]
            ys = [row + offset + jitter.uniform(-0.07, 0.07) for _ in values]
            ax.scatter(values, ys, s=34, color=colour, alpha=0.8, edgecolors="none", zorder=3)
        ax.plot([100 * signal.min_gap] * 2, [row - 0.38, row + 0.38], color=INK_2, lw=1.2, ls=(0, (3, 2)), zorder=2)
    ax.axvline(0, color=BASELINE, lw=1.4, zorder=1)
    ax.set_yticks(range(len(ORDER)), [signals[k].label for k in reversed(ORDER)])
    ax.set_ylim(-0.6, len(ORDER) - 0.4)
    ax.set_xlim(-15, 80)
    ax.set_xlabel("percentage points above the control (dashed tick: where starsift flags the signal)")
    ax.set_title("How far each signal rises above its control")
    ax.grid(axis="y", visible=False)
    for label, colour in ((f"suspected by the study ({sets['suspected']['repos']} repos)", CRITICAL),
                          (f"not suspected ({sets['unsuspected']['repos']} repos)", S1)):
        ax.scatter([], [], s=34, color=colour, label=label)
    ax.legend(loc="lower right", frameon=False, fontsize=10)
    ax.set_axisbelow(True)
    fig.text(0.0, 1.0, "Each dot is one repo: the share of its archived stargazers (fork owners when the archive had too few)\n"
                       "showing the signal, minus the share among random accounts that starred other repos in the same hours.\n"
                       f"50 repos drawn at random from the StarScout data, checked {result['checked']}.",
             ha="left", va="bottom", fontsize=10, color=INK_2, transform=fig.transFigure)
    fig.tight_layout()
    (HERE / "charts").mkdir(exist_ok=True)
    out = HERE / "charts" / "signals.png"
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    print(f"wrote {out}")
    for key in ORDER:
        for label in ("suspected", "unsuspected"):
            values = sets[label]["gaps"].get(key, [])
            print(f"{key:7} {label:12} n={len(values):>2} from {100 * min(values):+.0f} to {100 * max(values):+.0f}")


if __name__ == "__main__":
    main()
