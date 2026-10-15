"""python -m aiflab.adaptive_run — lab v0.3 experiments: self-building, self-healing data preparation.

Writes results/adaptive/results.json, results/adaptive/REPORT.md and results/adaptive/charts/*.png.
Labels used in the article: S0 = no preparation, S1 = timer (TTL) cache, S2 = write-invalidated cache,
D = static views, S3 = self-building without checks, S4 = self-building with self-healing.
"""
from __future__ import annotations

import itertools
import json
import os
import statistics

from .adaptive import (FAULTY, SPECS, Fresh, InvalidatingCache, LayerStrategy, TTLCache, Workload, default_strategies,
                       simulate)
from .data import build_world

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "results", "adaptive")
INK, INK2, GRID, SURF = "#0b0b0b", "#52514e", "#e6e5e0", "#fcfcfb"
SEEDS = range(5)
SHORT = {"T1_claim_blockers": "T1", "T2_adjuster_missing_docs": "T2", "T3_customer_financials": "T3",
         "T4_team_stale_claims": "T4", "T5_claim_paid_total": "T5", "T6_adjuster_loss_mix": "T6"}


def _slim(r):
    return {k: {kk: vv for kk, vv in v.items() if kk not in ("timeline", "stale_at")} for k, v in r.items()}


def _agg(rows):
    st = [r["stale_rate"] for r in rows]
    tot = [r["sor_total"] for r in rows]
    return {"sor_total": statistics.mean(tot), "sor_min": min(tot), "sor_max": max(tot),
            "stale_rate": statistics.mean(st), "stale_min": min(st), "stale_max": max(st), "seeds": len(rows)}


def _seeded(make, **wl_kw):
    rows = []
    for seed in SEEDS:
        wl = Workload(seed=100 + seed, **wl_kw)
        r = simulate(build_world, wl, [lambda ix, log: make(ix, log, seed)])
        rows.append(next(iter(_slim(r).values())))
    return rows


def run():
    out = {}
    # 1. headline days (single seed, full detail)
    out["clean"] = simulate(build_world, Workload(), default_strategies(Workload()))
    out["faulty"] = simulate(build_world, Workload(**FAULTY), default_strategies(Workload(**FAULTY)))

    # 2. multi-seed headline: faulty day
    multi = {
        "no preparation": _seeded(lambda ix, log, s: Fresh(ix), **FAULTY),
        "self-building, no checks": _seeded(lambda ix, log, s: LayerStrategy(ix, log, "S3", adaptive=True, healing=False), **FAULTY),
    }
    for every in (200, 100, 50, 25):
        multi[f"self-healing, verify every {every}"] = _seeded(
            lambda ix, log, s, every=every: LayerStrategy(ix, log, "S4", adaptive=True, healing=True, verify_every=every, seed=s), **FAULTY)
    for ttl in (25, 100, 400, 800):
        multi[f"timer cache, TTL {ttl} ticks"] = _seeded(lambda ix, log, s, ttl=ttl: TTLCache(ix, ttl=ttl), **FAULTY)
    multi["write-invalidated cache"] = _seeded(lambda ix, log, s: InvalidatingCache(ix), **FAULTY)
    out["faulty_5_seeds"] = {k: _agg(v) for k, v in multi.items()}

    # 3. every possible three-view static guess (clean day): is "self-building beats a guess" true?
    wl = Workload()
    combos = list(itertools.combinations(list(SPECS), 3))
    r = simulate(build_world, wl, [lambda ix, log, c=c: LayerStrategy(ix, log, "+".join(SHORT[x] for x in c), adaptive=False,
                                                                      healing=False, static_shapes=list(c)) for c in combos])
    out["static_guesses"] = sorted(({"views": k, "sor_total": v["sor_total"]} for k, v in r.items()), key=lambda x: x["sor_total"])


    # 4. one fault at a time (5 seeds): what each mechanism is for
    single = {}
    for label, kw in (("lost events only", dict(event_loss=0.01)), ("backfill only", dict(backfill_at=0.3)),
                      ("rule change only", dict(rule_change_at=0.7))):
        single[label] = {
            "no checks": _agg(_seeded(lambda ix, log, s: LayerStrategy(ix, log, "S3", adaptive=True, healing=False), **kw)),
            "self-healing": _agg(_seeded(lambda ix, log, s: LayerStrategy(ix, log, "S4", adaptive=True, healing=True, seed=s), **kw))}
    out["single_faults"] = single

    # 5. backfill size: how small a silent change can the verifier find, and how fast?
    bf = {}
    for size in (5, 20, 150):
        rows, lags = [], []
        for seed in SEEDS:
            wl = Workload(seed=100 + seed, backfill_at=0.3, backfill_size=size)
            res = simulate(build_world, wl, [lambda ix, log, s=seed: LayerStrategy(ix, log, "S4", adaptive=True, healing=True, seed=s)])
            v = res["S4"]
            rows.append(_slim(res)["S4"])
            at = v["fault_at_q"].get("backfill")
            hits = [q for q, act, _ in v["incidents"] if q >= at and ("drift" in act or "mismatch" in act or "reconcile" in act)]
            lags.append(hits[0] - at if hits else None)
        found = [x for x in lags if x is not None]
        bf[size] = {**_agg(rows), "detected_in": f"{len(found)}/{len(lags)} runs",
                    "questions_to_detect_median": statistics.median(found) if found else None}
    out["backfill_size"] = bf

    # 6. sensitivity (faulty day, single seed)
    sens = {}
    flat = {k: 1 / 6 for k in SPECS}
    for label, wl in (("write-heavy (10 writes per question)", Workload(writes_per_question=10.0, **FAULTY)),
                      ("no skew over who is asked about", Workload(param_zipf=0.0, **FAULTY)),
                      ("flat mix of question types", Workload(template_weights=flat, shifted_weights=flat, **FAULTY)),
                      ("no demand shift", Workload(shift_at=None, **FAULTY))):
        sens[label] = _slim(simulate(build_world, wl, [
            lambda ix, log: Fresh(ix),
            lambda ix, log: LayerStrategy(ix, log, "S3 · self-building, no checks", adaptive=True, healing=False),
            lambda ix, log: LayerStrategy(ix, log, "S4 · self-building + self-healing", adaptive=True, healing=True)]))
    out["sensitivity"] = sens
    return out


def _shares(no_prep):
    return None  # filled from a dedicated pass below (needs per-type costs)


def _type_shares():
    from .adaptive import Index, fresh_cost, generate_events
    w = build_world()
    ev = generate_events(w, Workload())
    ix = Index(w)
    n, cost = {}, {}
    for e in ev:
        if e[0] == "q":
            n[e[1]] = n.get(e[1], 0) + 1
            cost[e[1]] = cost.get(e[1], 0) + fresh_cost(ix, e[1], e[2])
    tot = sum(cost.values())
    return {SHORT[k]: {"questions": n[k], "share_of_reads": cost[k] / tot} for k in sorted(n)}


def report(o):
    pct = lambda x: f"{x:.1%}"  # noqa: E731
    L = ["# agent-infra-lab v0.3 — self-building, self-healing data preparation", "",
         "A simulated day: 2,000 agent questions mixed with 6,000 writes. Every answer is checked against the truth at the moment it",
         "was asked. Cost = records read from the system of record (question time + build/upkeep + verification/repair).",
         "Change events apply with no propagation lag (see README: assumptions). Gap replay from the event log is baseline",
         "maintenance in every prepared variant.", ""]
    L += ["## Question types (clean day, unprepared)", "", "| Type | Questions | Share of unprepared reads |", "|---|---:|---:|"]
    L += [f"| {k} | {v['questions']} | {pct(v['share_of_reads'])} |" for k, v in o["type_shares"].items()]
    for label in ("clean", "faulty"):
        L += ["", f"## {label.capitalize()} day, single seed" + (" (1% of change events lost in transit; a 150-document backfill written"
              " straight to the database at 30%; a required-documents rule change at 70%)" if label == "faulty" else ""), "",
              "| Strategy | Records read, total | at question time | build + upkeep | verification + repair | Stale answers | Served prepared |",
              "|---|---:|---:|---:|---:|---:|---:|"]
        for name, v in o[label].items():
            L.append(f"| {name} | {v['sor_total']:,} | {v['sor_question']:,} | {v['sor_upkeep']:,} | {v['sor_verify']:,} | "
                     f"{v['stale']} ({pct(v['stale_rate'])}) | {v['served_prepared']:,} |")
        s4 = o[label]["S4 · self-building + self-healing"]
        L += ["", f"S4 lifecycle (question number, action, type): {s4['log']}", "", f"S4 healing counters: {s4['verifier']}", "",
              f"S4 incidents (question number): {s4['incidents']}"]
    L += ["", "## Faulty day, mean of 5 seeds (min–max)", "", "| Strategy | Records read | Stale answers |", "|---|---:|---:|"]
    for k, v in o["faulty_5_seeds"].items():
        L.append(f"| {k} | {v['sor_total']:,.0f} ({v['sor_min']:,.0f}–{v['sor_max']:,.0f}) | {pct(v['stale_rate'])} ({pct(v['stale_min'])}–{pct(v['stale_max'])}) |")
    L += ["", "## Every three-view static guess (clean day)", "", "| Views prepared up front | Records read |", "|---|---:|"]
    L += [f"| {g['views']} | {g['sor_total']:,} |" for g in o["static_guesses"]]
    L += ["", "## One fault at a time (5 seeds): stale answers, mean (min–max)", "", "| Fault | No checks | Self-healing |", "|---|---:|---:|"]
    for k, v in o["single_faults"].items():
        a, b = v["no checks"], v["self-healing"]
        L.append(f"| {k} | {pct(a['stale_rate'])} ({pct(a['stale_min'])}–{pct(a['stale_max'])}) | {pct(b['stale_rate'])} ({pct(b['stale_min'])}–{pct(b['stale_max'])}) |")
    L += ["", "## Backfill size: detection by the verifier (5 seeds)", "",
          "| Documents written without events | Detected | Median questions to first detection | Stale answers, mean |", "|---:|---:|---:|---:|"]
    for k, v in o["backfill_size"].items():
        L.append(f"| {k} | {v['detected_in']} | {v['questions_to_detect_median']} | {pct(v['stale_rate'])} |")
    L += ["", "## Sensitivity (faulty day, single seed)", ""]
    for label, r in o["sensitivity"].items():
        L += [f"### {label}", "", "| Strategy | Records read | Stale answers |", "|---|---:|---:|"]
        L += [f"| {n} | {v['sor_total']:,} | {v['stale']} ({pct(v['stale_rate'])}) |" for n, v in r.items()]
        L.append("")
    return "\n".join(L) + "\n"


def charts(o):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.family": "DejaVu Sans", "axes.edgecolor": GRID, "axes.labelcolor": INK2,
                         "xtick.color": INK2, "ytick.color": INK2, "figure.facecolor": SURF, "axes.facecolor": SURF})
    os.makedirs(os.path.join(OUT, "charts"), exist_ok=True)
    pick = [("S0 · no cache", "#2a78d6", "No preparation"), ("S2 · write-invalidated cache", "#eb6834", "Write-invalidated cache"),
            ("S3 · self-building", "#1baf7a", "Self-building, no checks"),
            ("S4 · self-building + self-healing", "#eda100", "Self-building + self-healing"),
            ("D · static, everything (T1–T6)", "#e87ba4", "All six types prepared up front (no checks)")]
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.9), dpi=160, sharey=True)
    for ax, label in zip(axes, ("clean", "faulty")):
        for name, col, short in pick:
            tl = o[label][name]["timeline"]
            cum, s = [], 0
            for x in tl:
                s += x
                cum.append(max(s, 1))
            ax.plot(range(1, len(cum) + 1), cum, color=col, lw=2, label=f"{short}  ({o[label][name]['stale_rate']:.1%} stale)")
        ax.set_yscale("log")
        ax.set_title(("Clean day" if label == "clean" else "Faulty day: lost events, backfill, rule change"), loc="left", fontsize=10.5, color=INK)
        ax.set_xlabel("Agent questions answered")
        ax.grid(color=GRID)
        ax.spines[["top", "right"]].set_visible(False)
        ax.legend(frameon=False, fontsize=7.3, loc="lower right")
    axes[0].set_ylabel("Records read from the claims system, cumulative (log)")
    fig.suptitle("Self-building, self-healing preparation vs caching and up-front views (single seed)", x=0.01, ha="left", fontsize=12, color=INK)
    fig.text(0.01, 0.01, "Simulated day: 2,000 questions, 6,000 writes, skewed demand with a mid-day shift. Stale share in legend. "
             "github.com/vamsithumati/agent-infra-lab", fontsize=7, color=INK2)
    fig.tight_layout(rect=(0, 0.03, 1, 0.94))
    fig.savefig(os.path.join(OUT, "charts", "01_cumulative_records.png"))
    plt.close(fig)

    # 2. the frontier: reads vs stale answers, faulty day, 5-seed means
    m = o["faulty_5_seeds"]
    fig, ax = plt.subplots(figsize=(8.6, 5.0), dpi=160)
    series = [([f"timer cache, TTL {t} ticks" for t in (25, 100, 400, 800)], "#eb6834", "Timer cache (TTL 25 → 800)"),
              ([f"self-healing, verify every {e}" for e in (200, 100, 50, 25)], "#eda100", "Self-building + self-healing (verify every 200 → 25)")]
    for keys, col, lab in series:
        xs = [m[k]["sor_total"] for k in keys]
        ys = [m[k]["stale_rate"] * 100 for k in keys]
        ax.plot(xs, ys, color=col, lw=2, marker="o", ms=5, label=lab)
    for k, col, lab in (("self-building, no checks", "#1baf7a", "Self-building, no checks"),
                        ("write-invalidated cache", "#2a78d6", "Write-invalidated cache"),
                        ("no preparation", INK2, "No preparation")):
        ax.plot([m[k]["sor_total"]], [m[k]["stale_rate"] * 100], marker="s", ms=7, color=col, ls="none", label=lab)
    ax.set_xscale("log")
    ax.set_xlabel("Records read from the claims system over the day (log)")
    ax.set_ylabel("Stale answers (%)")
    ax.set_title("Freshness vs load on a faulty day (mean of 5 seeds)\nLower-left is better", loc="left", fontsize=10.5, color=INK)
    ax.grid(color=GRID)
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(frameon=False, fontsize=7.8, loc="upper right")
    fig.tight_layout()
    fig.savefig(os.path.join(OUT, "charts", "02_freshness_vs_load.png"))
    plt.close(fig)
    old = os.path.join(OUT, "charts", "02_price_of_trust.png")
    if os.path.exists(old):
        os.remove(old)


def main():
    o = run()
    o["type_shares"] = _type_shares()
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "results.json"), "w") as f:
        json.dump({k: (_slim(v) if k in ("clean", "faulty") else v) for k, v in o.items()}, f, indent=1, default=str)
    with open(os.path.join(OUT, "REPORT.md"), "w") as f:
        f.write(report(o))
    charts(o)
    print(report(o))


if __name__ == "__main__":
    main()
