"""Config D — an agent-native interface: task-shaped MCP-style tools over read models.

The read models (``ClaimReadModel``) are projections maintained by the system of
record (in production: updated from domain events / CDC). They precompute exactly
what the agent tasks need — missing documents, last activity, totals — and resolve
IDs to names. Tools return compact JSON by default, with ``response_format="detailed"``
available when the agent needs more.

This is the "Backend-for-Agents" idea: the systems of record are unchanged; a thin,
task-shaped layer is built for the agent, the same way a Backend-for-Frontend is built
for a screen.
"""
from __future__ import annotations

from datetime import date

from .data import TODAY, World
from .rest import Meter, dumps

ACTIVE = {"open", "pending_info"}


def _d(ts: str) -> date:
    return date.fromisoformat(ts[:10])


class ClaimReadModel:
    """One row per claim with everything the agent tasks ask about, names resolved."""

    def __init__(self, w: World):
        self.w = w
        adj = {a["id"]: a["display_name"] for a in w.adjusters}
        team = {t["id"]: t for t in w.teams}
        ph = {p["id"]: f"{p['first_name']} {p['last_name']}" for p in w.policyholders}
        self.rows = {}
        for c in w.claims:
            lt = w.loss_types[c["loss_type_code"]]
            have = {d["doc_type_code"] for d in w.documents[c["id"]]}
            acts = [_d(n["created_at"]) for n in w.notes[c["id"]]] + [_d(d["uploaded_at"]) for d in w.documents[c["id"]]]
            last = max(acts) if acts else _d(c["audit"]["created_at"])
            paid = round(sum(p["amount"] for p in w.payments[c["id"]] if p["status"] == "paid"), 2)
            self.rows[c["claim_number"]] = {
                "claim": c["claim_number"], "status": c["status"], "active": c["status"] in ACTIVE,
                "loss_type": lt["label"], "policyholder": ph[c["policyholder_id"]],
                "adjuster": adj[c["adjuster_id"]], "team": team[c["team_id"]]["name"], "team_code": team[c["team_id"]]["code"],
                "missing_documents": [d for d in lt["required_documents"] if d not in have],
                "last_activity": str(last), "days_since_activity": (TODAY - last).days,
                "sla_days_no_activity": lt["sla_days_no_activity"],
                "reserved": c["reserve"]["amount"], "paid": paid,
            }


class AgentNativeAPI:
    name = "D_agent_native"

    def __init__(self, w: World, meter: Meter | None = None, read_model: ClaimReadModel | None = None):
        self.w = w
        self.m = meter or Meter()
        self.rm = read_model or ClaimReadModel(w)

    def _respond(self, tool, payload, rm_rows=0):
        body = dumps(payload)
        self.m.record(tool, body, rm_rows=rm_rows)  # nothing is read from the system of record at question time
        return body

    # Name resolution with an agent-usable error instead of an empty list.
    def _one(self, kind, query, candidates):
        hits = sorted({c for c in candidates if query.lower() in c.lower()})
        exact = [c for c in hits if c.lower() == query.lower()]
        if len(exact) == 1:
            return exact[0], None
        if len(hits) == 1:
            return hits[0], None
        if not hits:
            return None, {"error": f"No {kind} matches '{query}'.", "hint": f"Check spelling or use a fuller {kind} name."}
        return None, {"error": f"'{query}' matches {len(hits)} {kind}s.", "candidates": hits[:10],
                      "hint": "Call again with one of the candidates."}

    def claim_brief(self, claim_number, response_format="concise"):
        r = self.rm.rows.get(claim_number)
        if r is None:
            return self._respond("claim_brief", {"error": f"No claim {claim_number}.", "hint": "Claim numbers look like CLM-100123."})
        keys = ["claim", "status", "loss_type", "missing_documents", "last_activity", "days_since_activity", "sla_days_no_activity"]
        if response_format == "detailed":
            keys = list(r)
        return self._respond("claim_brief", {k: r[k] for k in keys}, rm_rows=1)

    def claims_missing_documents(self, adjuster=None, team=None, active_only=True):
        rows = list(self.rm.rows.values())
        if adjuster:
            name, err = self._one("adjuster", adjuster, {r["adjuster"] for r in rows})
            if err:
                return self._respond("claims_missing_documents", err)
            rows = [r for r in rows if r["adjuster"] == name]
        if team:
            rows = [r for r in rows if team.lower() in (r["team"] + " " + r["team_code"]).lower()]
        out = [{"claim": r["claim"], "missing": r["missing_documents"]} for r in rows
               if r["missing_documents"] and (r["active"] or not active_only)]
        return self._respond("claims_missing_documents", {"count": len(out), "claims": sorted(out, key=lambda x: x["claim"])}, rm_rows=len(rows))

    def policyholder_financials(self, name):
        rows = list(self.rm.rows.values())
        who, err = self._one("policyholder", name, {r["policyholder"] for r in rows})
        if err:
            return self._respond("policyholder_financials", err)
        mine = [r for r in rows if r["policyholder"] == who]
        return self._respond("policyholder_financials", {
            "policyholder": who, "claims": len(mine),
            "reserved_total": round(sum(r["reserved"] for r in mine), 2), "paid_total": round(sum(r["paid"] for r in mine), 2),
            "by_claim": [{"claim": r["claim"], "reserved": r["reserved"], "paid": r["paid"]} for r in sorted(mine, key=lambda x: x["claim"])]},
            rm_rows=len(mine))

    def stale_claims(self, team, days=None, response_format="concise"):
        rows = [r for r in self.rm.rows.values() if r["active"] and team.lower() in (r["team"] + " " + r["team_code"]).lower()]
        keys = ["claim", "days_since_activity"] + (["adjuster", "sla_days_no_activity"] if response_format == "detailed" else [])
        out = [{k: r[k] for k in keys}
               for r in rows if r["days_since_activity"] > (days if days is not None else r["sla_days_no_activity"])]
        return self._respond("stale_claims", {"count": len(out), "claims": sorted(out, key=lambda x: x["claim"])}, rm_rows=len(rows))

    def find_claims(self, policyholder=None, adjuster=None, team=None, active_only=True, limit=25):
        rows = [r for r in self.rm.rows.values()
                if (not policyholder or policyholder.lower() in r["policyholder"].lower())
                and (not adjuster or adjuster.lower() in r["adjuster"].lower())
                and (not team or team.lower() in (r["team"] + " " + r["team_code"]).lower())
                and (r["active"] or not active_only)]
        return self._respond("find_claims", {"count": len(rows), "claims": [
            {"claim": r["claim"], "status": r["status"], "loss_type": r["loss_type"], "policyholder": r["policyholder"]}
            for r in sorted(rows, key=lambda x: x["claim"])[:limit]], "truncated": len(rows) > limit})
