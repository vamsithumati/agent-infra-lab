"""python -m aiflab  — run all experiments, write results/results.json, results/REPORT.md and charts."""
from __future__ import annotations

import json
import os

from .data import build_world
from .experiments import CONFIGS, LABEL, SENSITIVITY, run_all

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "results")
COLORS = {"A_legacy_rest": "#2a78d6", "B_tuned_rest": "#eb6834", "C2_code_mode_tuned": "#1baf7a",
          "B2_rest_derived": "#eda100", "D_agent_native": "#e87ba4"}
CHART_CFGS = ["A_legacy_rest", "B_tuned_rest", "C2_code_mode_tuned", "B2_rest_derived", "D_agent_native"]
INK, INK2, GRID, SURF = "#0b0b0b", "#52514e", "#e6e5e0", "#fcfcfb"
TASK_NAME = {"u1": "U1 · one claim:\nwhat's blocking it?", "u2": "U2 · one adjuster:\nclaims missing documents",
             "u3": "U3 · one customer:\nreserved vs paid", "u4": "U4 · one team:\nclaims past SLA"}


def fmt(n):
    return f"{n / 1e6:.2f}M" if n >= 1e6 else (f"{n / 1e3:.1f}K" if n >= 1e3 else str(n))


def charts(r):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.family": "DejaVu Sans", "axes.edgecolor": GRID, "axes.labelcolor": INK2,
                         "xtick.color": INK2, "ytick.color": INK2, "figure.facecolor": SURF, "axes.facecolor": SURF})
    os.makedirs(os.path.join(OUT, "charts"), exist_ok=True)

    # 1. Context tokens per question, per configuration (log scale)
    fig, ax = plt.subplots(figsize=(11, 6.2), dpi=160)
    tasks = ["u1", "u2", "u3", "u4"]
    w = 0.155
    for i, cfg in enumerate(CHART_CFGS):
        vals = [r["headline"][t]["results"][cfg]["context_tokens"] for t in tasks]
        xs = [k + (i - 2) * (w + 0.015) for k in range(len(tasks))]
        ax.bar(xs, vals, width=w, color=COLORS[cfg], label=LABEL[cfg], zorder=3)
        for x, v in zip(xs, vals):
            ax.text(x, v * 1.12, fmt(v), ha="center", va="bottom", fontsize=6.8, color=INK2)
    ax.set_yscale("log")
    ax.set_ylim(300, 4e6)
    for lim, lab in ((200_000, "200K-token context window"), (1_000_000, "1M-token context window")):
        ax.axhline(lim, color=INK2, lw=1, ls=(0, (4, 3)), zorder=2)
        ax.text(-0.45, lim * 1.12, lab, ha="left", fontsize=7.5, color=INK2)
    ax.set_xticks(range(len(tasks)), [TASK_NAME[t] for t in tasks], fontsize=9, color=INK)
    ax.set_ylabel("Tokens in the model's context when it answers (log scale)")
    ax.grid(axis="y", color=GRID, zorder=0)
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(frameon=False, fontsize=8.5, ncol=5, loc="upper center", bbox_to_anchor=(0.5, -0.13))
    ax.set_title("Same questions, same data, same answers — five of the interfaces", loc="left", fontsize=12.5, color=INK, pad=12)
    fig.text(0.01, 0.01, "Best-case agent: no retries, independent calls in parallel. Tokenizer: o200k_base. Synthetic claims data. "
             "github.com/vamsithumati/agent-infra-lab", fontsize=7, color=INK2)
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    fig.savefig(os.path.join(OUT, "charts", "01_context_tokens_by_question.png"))
    plt.close(fig)

    # 2. Scale: context tokens and data pulled from the system of record vs portfolio size (two panels, no dual axis)
    sw = r["sweep_u2"]
    xs = [x["active_claims"] for x in sw]
    short = {"A_legacy_rest": "A · legacy REST", "B_tuned_rest": "B · tuned REST", "C2_code_mode_tuned": "C′ · code mode over B",
             "B2_rest_derived": "B+ · REST + derived", "D_agent_native": "D · agent-native"}
    fig, axes = plt.subplots(1, 2, figsize=(11.5, 4.8), dpi=160)
    panels = ((axes[0], "context_tokens", 1, "Tokens in the model's context (log scale)"),
              (axes[1], "sor_rows", 1, "Records read from the claims system at question time (log scale)"))
    for ax, metric, scale, title in panels:
        ends = []
        for cfg in CHART_CFGS:
            if metric == "sor_rows" and cfg == "D_agent_native":
                continue  # zero at question time: D reads only its read model (plotted as a note)
            ys = [x["results"][cfg][metric] * scale for x in sw]
            same = metric == "sor_rows" and cfg == "C2_code_mode_tuned"
            ax.plot(xs, ys, color=COLORS[cfg], lw=2, marker="o", ms=3.5, zorder=4 if same else 3, ls=(0, (3, 2)) if same else "-")
            lab = "A, B and C′ (identical)" if same else short[cfg]
            if metric == "sor_rows" and cfg in ("A_legacy_rest", "B_tuned_rest"):
                continue
            ends.append([ys[-1], lab])
        if metric == "context_tokens":
            ax.legend([plt.Line2D([], [], color=COLORS[c], lw=2) for c in CHART_CFGS], [short[c] for c in CHART_CFGS],
                      frameon=False, fontsize=7.5, loc="upper left", bbox_to_anchor=(1.0, 1.0))
            ends = []
        # spread end labels so they don't collide (log space)
        import math
        ends.sort(key=lambda e: e[0])
        pos = []
        for v, lab in ends:
            lv = math.log10(v)
            if pos and lv - pos[-1] < 0.13:
                lv = pos[-1] + 0.13
            pos.append(lv)
            ax.text(xs[-1] + 3, 10 ** lv, lab, va="center", fontsize=8, color=INK2)
        ax.set_yscale("log")
        if metric == "sor_rows":
            ax.text(0.98, 0.04, "D · agent-native: 0 (answers from its read model;\nkeeping that model current is paid at write time, not measured)",
                    transform=ax.transAxes, fontsize=7.5, color=INK2, va="bottom", ha="right")
        ax.set_xlabel("Active claims in the adjuster's portfolio")
        ax.set_title(title, loc="left", fontsize=10, color=INK)
        ax.grid(color=GRID, zorder=0)
        ax.spines[["top", "right"]].set_visible(False)
        ax.set_xlim(0, max(xs) * (1.05 if metric == "context_tokens" else 1.45))
    fig.suptitle("U2 for all 24 adjusters: what grows with the portfolio", x=0.01, ha="left", fontsize=12.5, color=INK)
    fig.text(0.01, 0.01, "Best-case agent. Synthetic data. C (code mode over A) reads the same as A. B+ reads claim rows; its derived fields come from the read model. "
             "github.com/vamsithumati/agent-infra-lab", fontsize=7, color=INK2)
    fig.tight_layout(rect=(0, 0.03, 1, 0.95))
    fig.savefig(os.path.join(OUT, "charts", "02_scaling_u2.png"))
    plt.close(fig)


def report(r):
    L = ["# agent-infra-lab — results", "",
         "Generated by `python -m aiflab`. Every configuration returned the ground-truth answer for every question (asserted).",
         "All costs are for a **best-case agent** (no retries, no wrong calls, independent calls in parallel): a floor, not an estimate.", ""]
    L += ["## Tool-definition tokens (paid on every turn)", "", "| Config | Tokens |", "|---|---:|"]
    L += [f"| {LABEL[c]} | {r['tool_def_tokens'][c]:,} |" for c in CONFIGS + SENSITIVITY]
    for t, h in r["headline"].items():
        L += ["", f"## {t.upper()} — {h['question']}", "",
              "| Config | Turns | Tool calls | Requests at question time | Response KB | System-of-record rows read | Read-model rows | Result tokens | Context at answer | Input tokens, all turns (uncached) | Output tokens |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for c in CONFIGS + SENSITIVITY:
            x = h["results"][c]
            L.append(f"| {LABEL[c]} | {x['turns']} | {x['tool_calls']} | {x['backend_requests']} | {x['bytes'] / 1024:,.1f} | {x['sor_rows']:,} | {x['rm_rows']:,} | {x['result_tokens']:,} | "
                     f"{x['context_tokens']:,} | {x['cumulative_input']:,} | {x['output_tokens']:,} |")
    allc = CONFIGS + SENSITIVITY
    L += ["", "## U2 scale sweep (all adjusters): context tokens / requests / system-of-record rows read", "",
          "| Active claims | " + " | ".join(LABEL[c] for c in allc) + " |", "|---:|" + "---:|" * len(allc)]
    for x in r["sweep_u2"]:
        L.append(f"| {x['active_claims']} | " + " | ".join(
            f"{x['results'][c]['context_tokens']:,} / {x['results'][c]['backend_requests']} / {x['results'][c]['sor_rows']:,}" for c in allc) + " |")
    L += ["", "## U4 by team: context tokens / requests / system-of-record rows read", "",
          "| Team | Active claims | Stale (answer size) | " + " | ".join(LABEL[c] for c in allc) + " |", "|---|---:|---:|" + "---:|" * len(allc)]
    for x in r["teams_u4"]:
        L.append(f"| {x['team']} | {x['active_claims']} | {x['answer_size']} | " + " | ".join(
            f"{x['results'][c]['context_tokens']:,} / {x['results'][c]['backend_requests']} / {x['results'][c]['sor_rows']:,}" for c in allc) + " |")
    L += ["", "## Ratios to D (context tokens), U2 across all adjusters", "", "| Config | Median | Min | Max |", "|---|---:|---:|---:|"]
    import statistics
    for c in allc:
        rs = [x["results"][c]["context_tokens"] / x["results"]["D_agent_native"]["context_tokens"] for x in r["sweep_u2"]]
        L.append(f"| {LABEL[c]} | {statistics.median(rs):.1f}× | {min(rs):.1f}× | {max(rs):.1f}× |")
    return "\n".join(L) + "\n"


def main():
    w = build_world()
    r = run_all(w)
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "results.json"), "w") as f:
        json.dump(r, f, indent=1, default=str)
    with open(os.path.join(OUT, "REPORT.md"), "w") as f:
        f.write(report(r))
    charts(r)
    print(report(r))


if __name__ == "__main__":
    main()
