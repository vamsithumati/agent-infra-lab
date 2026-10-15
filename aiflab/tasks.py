"""Four adjuster questions, their ground truth, and a best-case agent plan per configuration.

Each plan is what a *perfect* agent would do with that interface: it knows the API,
never retries, never makes a wrong call, and issues independent calls in parallel.
Real agents do worse. So every number here is a floor on that interface's cost,
not an estimate of a real agent's cost.

Every plan computes its answer only from the tool results it received, and the
harness asserts that all configurations return the ground truth.
"""
from __future__ import annotations

import json
from datetime import date

from .conversation import Conversation
from .data import TODAY, World
from .native import ACTIVE

ACTIVE_STATUS = "open,pending_info"


def _d(ts):
    return date.fromisoformat(ts[:10])


# ----------------------------------------------------------------------------- questions
def q1(claim_number):
    return f"What is blocking claim {claim_number}? List missing required documents and days since last activity."


def q2(adjuster):
    return f"Which of {adjuster}'s active claims are missing required documents, and which documents?"


def q3(policyholder):
    return f"For policyholder {policyholder}, what is the total reserved and total paid across all their claims?"


def q4(team_code):
    return f"Which active claims in team {team_code} have had no activity for longer than their SLA?"


# ----------------------------------------------------------------------------- ground truth (from the world, not the APIs)
def _activity(w: World, c):
    acts = [_d(n["created_at"]) for n in w.notes[c["id"]]] + [_d(d["uploaded_at"]) for d in w.documents[c["id"]]]
    return max(acts)


def _missing(w: World, c):
    have = {d["doc_type_code"] for d in w.documents[c["id"]]}
    return [d for d in w.loss_types[c["loss_type_code"]]["required_documents"] if d not in have]


def truth_u1(w, claim_number):
    c = next(c for c in w.claims if c["claim_number"] == claim_number)
    return {"missing": _missing(w, c), "days_since_activity": (TODAY - _activity(w, c)).days}


def truth_u2(w, adjuster_name):
    aid = next(a["id"] for a in w.adjusters if a["display_name"] == adjuster_name)
    rows = [(c["claim_number"], _missing(w, c)) for c in w.claims if c["adjuster_id"] == aid and c["status"] in ACTIVE]
    return sorted([[n, m] for n, m in rows if m])


def truth_u3(w, ph_id):
    cs = [c for c in w.claims if c["policyholder_id"] == ph_id]
    return {"reserved": round(sum(c["reserve"]["amount"] for c in cs), 2),
            "paid": round(sum(p["amount"] for c in cs for p in w.payments[c["id"]] if p["status"] == "paid"), 2)}


def truth_u4(w, team_code):
    tid = next(t["id"] for t in w.teams if t["code"] == team_code)
    out = []
    for c in w.claims:
        if c["team_id"] == tid and c["status"] in ACTIVE:
            if (TODAY - _activity(w, c)).days > w.loss_types[c["loss_type_code"]]["sla_days_no_activity"]:
                out.append(c["claim_number"])
    return sorted(out)


# ----------------------------------------------------------------------------- helpers for REST plans
def _rows(body):
    return json.loads(body)["data"]


def _pages(body):
    return json.loads(body)["meta"]["total_pages"]


def _list_all(conv, api, method, args, first_turn_extra=()):
    """Page 1 (with any independent extra calls in the same turn), then all remaining pages in one parallel turn."""
    fn = getattr(api, method)
    a1 = {**args, "page": 1, "page_size": api.MAX_PAGE}
    r1 = fn(**a1)
    extra_res = [getattr(api, m)(**ea) for m, ea in first_turn_extra]
    conv.turn([(method, a1)] + list(first_turn_extra), [r1] + extra_res)
    rows = _rows(r1)
    n = _pages(r1)
    if n > 1:
        calls = [(method, {**args, "page": p, "page_size": api.MAX_PAGE}) for p in range(2, n + 1)]
        res = [fn(**a) for _, a in calls]
        conv.turn(calls, res)
        for r in res:
            rows += _rows(r)
    return rows, extra_res


def _fan_out(conv, api, method, ids):
    """One parallel turn: the same sub-resource call for every id (follow-up pages if any, in another turn)."""
    calls = [(method, {"claim_id": i, "page": 1, "page_size": api.MAX_PAGE}) for i in ids]
    res = [getattr(api, method)(**a) for _, a in calls]
    if calls:
        conv.turn(calls, res)
    out = {i: _rows(r) for i, r in zip(ids, res)}
    more = [(i, p) for i, r in zip(ids, res) for p in range(2, _pages(r) + 1)]
    if more:
        calls2 = [(method, {"claim_id": i, "page": p, "page_size": api.MAX_PAGE}) for i, p in more]
        res2 = [getattr(api, method)(**a) for _, a in calls2]
        conv.turn(calls2, res2)
        for (i, _), r in zip(more, res2):
            out[i] += _rows(r)
    return out


def _required(ref_body):
    return {r["code"]: r for r in _rows(ref_body)}


def _finish(conv, answer):
    conv.answer(json.dumps(answer))
    return answer


# ----------------------------------------------------------------------------- U1: one claim, several resources
def u1_rest(api, conv, claim_number, tuned):
    if tuned:
        args = {"claim_number": claim_number, "include": ["documents", "notes"],
                "fields": {"claims": ["claim_number", "loss_type_code"], "documents": ["doc_type_code", "uploaded_at"], "notes": ["created_at"]}}
        rows, (ref,) = _list_all(conv, api, "list_claims", args, [("get_reference_loss_types", {})])
        c = rows[0]
        docs, notes = c["documents"], c["notes"]
    else:
        rows, (ref,) = _list_all(conv, api, "list_claims", {"claim_number": claim_number}, [("get_reference_loss_types", {})])
        c = rows[0]
        calls = [("list_claim_documents", {"claim_id": c["id"], "page": 1, "page_size": api.MAX_PAGE}),
                 ("list_claim_notes", {"claim_id": c["id"], "page": 1, "page_size": api.MAX_PAGE})]
        res = [getattr(api, m)(**a) for m, a in calls]
        conv.turn(calls, res)
        docs, notes = _rows(res[0]), _rows(res[1])  # notes newest-first: page 1 holds the latest
        assert _pages(res[0]) == 1
    req = _required(ref)[c["loss_type_code"]]["required_documents"]
    have = {d["doc_type_code"] for d in docs}
    last = max([_d(n["created_at"]) for n in notes] + [_d(d["uploaded_at"]) for d in docs])
    return _finish(conv, {"missing": [d for d in req if d not in have], "days_since_activity": (TODAY - last).days})


def u1_native(api, conv, claim_number):
    args = {"claim_number": claim_number}
    r = api.claim_brief(**args)
    conv.turn([("claim_brief", args)], [r])
    b = json.loads(r)
    return _finish(conv, {"missing": b["missing_documents"], "days_since_activity": b["days_since_activity"]})


# ----------------------------------------------------------------------------- U2: one adjuster's portfolio, fan-out
def u2_rest(api, conv, adjuster, tuned):
    ref = api.get_reference_loss_types()
    sargs = {"q": adjuster, "page": 1, "page_size": api.MAX_PAGE}
    if tuned:
        sargs["fields"] = {"adjusters": ["id", "display_name"]}
    s = api.search_adjusters(**sargs)
    conv.turn([("get_reference_loss_types", {}), ("search_adjusters", sargs)], [ref, s])
    aid = next(a["id"] for a in _rows(s) if a["display_name"] == adjuster)
    req = _required(ref)
    if tuned:
        args = {"adjuster_id": aid, "status": ACTIVE_STATUS, "include": ["documents"],
                "fields": {"claims": ["claim_number", "loss_type_code"], "documents": ["doc_type_code"]}}
        claims, _ = _list_all(conv, api, "list_claims", args)
        docs = {c["claim_number"]: c["documents"] for c in claims}
    else:
        claims, _ = _list_all(conv, api, "list_claims", {"adjuster_id": aid, "status": ACTIVE_STATUS})
        by_id = _fan_out(conv, api, "list_claim_documents", [c["id"] for c in claims])
        docs = {c["claim_number"]: by_id[c["id"]] for c in claims}
    out = []
    for c in claims:
        have = {d["doc_type_code"] for d in docs[c["claim_number"]]}
        miss = [d for d in req[c["loss_type_code"]]["required_documents"] if d not in have]
        if miss:
            out.append([c["claim_number"], miss])
    return _finish(conv, sorted(out))


def u2_native(api, conv, adjuster):
    args = {"adjuster": adjuster}
    r = api.claims_missing_documents(**args)
    conv.turn([("claims_missing_documents", args)], [r])
    return _finish(conv, sorted([c["claim"], c["missing"]] for c in json.loads(r)["claims"]))


# ----------------------------------------------------------------------------- U3: aggregation across a customer's claims
def u3_rest(api, conv, ph_name, tuned):
    sargs = {"q": ph_name, "page": 1, "page_size": api.MAX_PAGE}
    if tuned:
        sargs["fields"] = {"policyholders": ["id", "first_name", "last_name"]}
    s = api.search_policyholders(**sargs)
    conv.turn([("search_policyholders", sargs)], [s])
    pid = next(p["id"] for p in _rows(s) if f"{p['first_name']} {p['last_name']}" == ph_name)
    if tuned:
        args = {"policyholder_id": pid, "include": ["payments"],
                "fields": {"claims": ["claim_number", "reserve"], "payments": ["amount", "status"]}}
        claims, _ = _list_all(conv, api, "list_claims", args)
        pays = [p for c in claims for p in c["payments"]]
    else:
        claims, _ = _list_all(conv, api, "list_claims", {"policyholder_id": pid})
        by_id = _fan_out(conv, api, "list_claim_payments", [c["id"] for c in claims])
        pays = [p for c in claims for p in by_id[c["id"]]]
    return _finish(conv, {"reserved": round(sum(c["reserve"]["amount"] for c in claims), 2),
                          "paid": round(sum(p["amount"] for p in pays if p["status"] == "paid"), 2)})


def u3_native(api, conv, ph_name):
    args = {"name": ph_name}
    r = api.policyholder_financials(**args)
    conv.turn([("policyholder_financials", args)], [r])
    b = json.loads(r)
    return _finish(conv, {"reserved": b["reserved_total"], "paid": b["paid_total"]})


# ----------------------------------------------------------------------------- U4: team-wide scan needing activity history
def u4_rest(api, conv, team_code, tuned):
    ref = api.get_reference_loss_types()
    sargs = {"q": team_code, "page": 1, "page_size": api.MAX_PAGE}
    if tuned:
        sargs["fields"] = {"teams": ["id", "code"]}
    s = api.search_teams(**sargs)
    conv.turn([("get_reference_loss_types", {}), ("search_teams", sargs)], [ref, s])
    tid = next(t["id"] for t in _rows(s) if t["code"] == team_code)
    req = _required(ref)
    if tuned:
        args = {"team_id": tid, "status": ACTIVE_STATUS, "include": ["notes", "documents"],
                "fields": {"claims": ["claim_number", "loss_type_code"], "notes": ["created_at"], "documents": ["uploaded_at"]}}
        claims, _ = _list_all(conv, api, "list_claims", args)
        acts = {c["claim_number"]: [n["created_at"] for n in c["notes"]] + [d["uploaded_at"] for d in c["documents"]] for c in claims}
    else:
        claims, _ = _list_all(conv, api, "list_claims", {"team_id": tid, "status": ACTIVE_STATUS})
        ids = [c["id"] for c in claims]
        # notes (newest first, page 1 is enough) and documents for every claim, all in one parallel turn
        calls = [(m, {"claim_id": i, "page": 1, "page_size": api.MAX_PAGE}) for i in ids for m in ("list_claim_notes", "list_claim_documents")]
        res = [getattr(api, m)(**a) for m, a in calls]
        conv.turn(calls, res)
        acts = {}
        for (m, a), r in zip(calls, res):
            num = next(c["claim_number"] for c in claims if c["id"] == a["claim_id"])
            key = "created_at" if m == "list_claim_notes" else "uploaded_at"
            if m == "list_claim_documents":
                assert _pages(r) == 1
            acts.setdefault(num, []).extend(x[key] for x in _rows(r))
    out = [c["claim_number"] for c in claims
           if (TODAY - max(_d(t) for t in acts[c["claim_number"]])).days > req[c["loss_type_code"]]["sla_days_no_activity"]]
    return _finish(conv, sorted(out))


def u4_native(api, conv, team_code):
    args = {"team": team_code}
    r = api.stale_claims(**args)
    conv.turn([("stale_claims", args)], [r])
    return _finish(conv, sorted(c["claim"] for c in json.loads(r)["claims"]))


# ----------------------------------------------------------------------------- B+: tuned REST with derived fields/filters
def u1_derived(api, conv, claim_number):
    args = {"claim_number": claim_number, "fields": {"claims": ["claim_number", "missing_documents", "days_since_activity"]}}
    rows, _ = _list_all(conv, api, "list_claims", args)
    return _finish(conv, {"missing": rows[0]["missing_documents"], "days_since_activity": rows[0]["days_since_activity"]})


def u2_derived(api, conv, adjuster):
    sargs = {"q": adjuster, "page": 1, "page_size": api.MAX_PAGE, "fields": {"adjusters": ["id", "display_name"]}}
    s = api.search_adjusters(**sargs)
    conv.turn([("search_adjusters", sargs)], [s])
    aid = next(a["id"] for a in _rows(s) if a["display_name"] == adjuster)
    rows, _ = _list_all(conv, api, "list_claims", {"adjuster_id": aid, "status": ACTIVE_STATUS, "has_missing_documents": True,
                                                   "fields": {"claims": ["claim_number", "missing_documents"]}})
    return _finish(conv, sorted([c["claim_number"], c["missing_documents"]] for c in rows))


def u3_derived(api, conv, ph_name):
    sargs = {"q": ph_name, "page": 1, "page_size": api.MAX_PAGE, "fields": {"policyholders": ["id", "first_name", "last_name"]}}
    s = api.search_policyholders(**sargs)
    conv.turn([("search_policyholders", sargs)], [s])
    pid = next(p["id"] for p in _rows(s) if f"{p['first_name']} {p['last_name']}" == ph_name)
    rows, _ = _list_all(conv, api, "list_claims", {"policyholder_id": pid, "fields": {"claims": ["claim_number", "reserve", "paid_total"]}})
    return _finish(conv, {"reserved": round(sum(c["reserve"]["amount"] for c in rows), 2),
                          "paid": round(sum(c["paid_total"] for c in rows), 2)})


def u4_derived(api, conv, team_code):
    sargs = {"q": team_code, "page": 1, "page_size": api.MAX_PAGE, "fields": {"teams": ["id", "code"]}}
    s = api.search_teams(**sargs)
    conv.turn([("search_teams", sargs)], [s])
    tid = next(t["id"] for t in _rows(s) if t["code"] == team_code)
    rows, _ = _list_all(conv, api, "list_claims", {"team_id": tid, "status": ACTIVE_STATUS, "stale": True,
                                                   "fields": {"claims": ["claim_number"]}})
    return _finish(conv, sorted(c["claim_number"] for c in rows))
