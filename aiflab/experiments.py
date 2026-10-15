"""Run every question through every configuration, check answers, collect metrics."""
from __future__ import annotations

import statistics
from collections import Counter

from . import codemode, tasks
from .conversation import Conversation
from .data import World
from .native import ACTIVE, AgentNativeAPI, ClaimReadModel
from .rest import DerivedREST, LegacyREST, Meter, TunedREST
from .tooldefs import as_text, code_mode_tools, derived_tools, legacy_tools, native_tools

CONFIGS = ["A_legacy_rest", "B_tuned_rest", "C_code_mode", "C2_code_mode_tuned", "B2_rest_derived", "D_agent_native"]
SENSITIVITY = ["A_lean_rest"]
LABEL = {"A_legacy_rest": "A · legacy REST", "A_lean_rest": "A-lean · legacy REST, lean payloads",
         "B_tuned_rest": "B · tuned REST", "C_code_mode": "C · code mode over A", "C2_code_mode_tuned": "C′ · code mode over B",
         "B2_rest_derived": "B+ · REST + derived fields", "D_agent_native": "D · agent-native tools"}

QUESTION = {"u1": tasks.q1, "u2": tasks.q2, "u3": tasks.q3, "u4": tasks.q4}
TRUTH = {"u1": tasks.truth_u1, "u2": tasks.truth_u2, "u3": None, "u4": tasks.truth_u4}
DERIVED_PLAN = {"u1": tasks.u1_derived, "u2": tasks.u2_derived, "u3": tasks.u3_derived, "u4": tasks.u4_derived}
REST_PLAN = {"u1": tasks.u1_rest, "u2": tasks.u2_rest, "u3": tasks.u3_rest, "u4": tasks.u4_rest}
NATIVE_PLAN = {"u1": tasks.u1_native, "u2": tasks.u2_native, "u3": tasks.u3_native, "u4": tasks.u4_native}


def tool_text(cfg):
    return as_text({"A_legacy_rest": legacy_tools(20), "A_lean_rest": legacy_tools(20), "B_tuned_rest": legacy_tools(100, tuned=True),
                    "C_code_mode": code_mode_tools(), "C2_code_mode_tuned": code_mode_tools(tuned=True),
                    "B2_rest_derived": derived_tools(), "D_agent_native": native_tools()}[cfg])


def run_one(w: World, rm: ClaimReadModel, cfg: str, task: str, arg, truth):
    m = Meter()
    conv = Conversation(tool_text(cfg), QUESTION[task](arg))
    if cfg in ("A_legacy_rest", "A_lean_rest"):
        ans = REST_PLAN[task](LegacyREST(w, m, lean=cfg == "A_lean_rest"), conv, arg, tuned=False)
    elif cfg == "B_tuned_rest":
        ans = REST_PLAN[task](TunedREST(w, m), conv, arg, tuned=True)
    elif cfg == "C_code_mode":
        ans = codemode.run(task, arg, LegacyREST(w, m), conv)
    elif cfg == "C2_code_mode_tuned":
        ans = codemode.run(task, arg, TunedREST(w, m), conv, tuned=True)
    elif cfg == "B2_rest_derived":
        ans = DERIVED_PLAN[task](DerivedREST(w, m, rm), conv, arg)
    else:
        ans = NATIVE_PLAN[task](AgentNativeAPI(w, m, rm), conv, arg)
    assert ans == truth, (cfg, task, arg, ans, truth)
    return {**conv.summary(), "backend_requests": m.requests, "bytes": m.bytes, "sor_rows": m.sor_rows, "rm_rows": m.rm_rows}


def pick_params(w: World):
    active = [c for c in w.claims if c["status"] in ACTIVE]
    u1 = sorted((c for c in active if tasks.truth_u1(w, c["claim_number"])["missing"]), key=lambda c: c["claim_number"])[0]["claim_number"]
    names = Counter(f"{p['first_name']} {p['last_name']}" for p in w.policyholders)
    per_ph = Counter(c["policyholder_id"] for c in w.claims)
    u3 = sorted((p for p in w.policyholders if names[f"{p['first_name']} {p['last_name']}"] == 1 and per_ph[p["id"]] >= 2),
                key=lambda p: p["customer_number"])[0]
    loads = {a["display_name"]: sum(1 for c in active if c["adjuster_id"] == a["id"]) for a in w.adjusters}
    med = statistics.median_low(loads.values())
    u2 = sorted(n for n, k in loads.items() if k == med)[0]
    team_load = {t["code"]: sum(1 for c in active if c["team_id"] == t["id"]) for t in w.teams}
    u4 = sorted(team_load, key=lambda k: (team_load[k], k))[len(team_load) // 2]
    return {"u1": u1, "u2": u2, "u3": u3, "u4": u4, "adjuster_loads": loads, "team_loads": team_load}


def run_all(w: World):
    rm = ClaimReadModel(w)
    p = pick_params(w)
    headline = {}
    for task in ("u1", "u2", "u3", "u4"):
        if task == "u3":
            ph = p["u3"]
            arg, truth = f"{ph['first_name']} {ph['last_name']}", tasks.truth_u3(w, ph["id"])
        else:
            arg = p[task]
            truth = TRUTH[task](w, arg)
        headline[task] = {"arg": arg, "question": QUESTION[task](arg),
                          "results": {cfg: run_one(w, rm, cfg, task, arg, truth) for cfg in CONFIGS + SENSITIVITY}}
    # Scale sweep: U2 for every adjuster (portfolio sizes vary by design)
    sweep = []
    for a in w.adjusters:
        name = a["display_name"]
        truth = tasks.truth_u2(w, name)
        sweep.append({"adjuster": name, "active_claims": p["adjuster_loads"][name],
                      "results": {cfg: run_one(w, rm, cfg, "u2", name, truth) for cfg in CONFIGS + SENSITIVITY}})
    sweep.sort(key=lambda r: r["active_claims"])
    teams = []
    for code, n in sorted(p["team_loads"].items()):
        truth = tasks.truth_u4(w, code)
        teams.append({"team": code, "active_claims": n, "answer_size": len(truth),
                      "results": {cfg: run_one(w, rm, cfg, "u4", code, truth) for cfg in CONFIGS + SENSITIVITY}})
    return {"params": {k: (v if k != "u3" else f"{v['first_name']} {v['last_name']}") for k, v in p.items()},
            "headline": headline, "sweep_u2": sweep, "teams_u4": teams,
            "tool_def_tokens": {cfg: headline["u1"]["results"][cfg]["tool_def_tokens"] for cfg in CONFIGS + SENSITIVITY}}
