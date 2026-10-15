"""Two REST APIs over the same world.

* ``LegacyREST``  (config A) — the API the claims UI already uses. Resource-shaped,
  full objects, 20-row pages, filters the screens need (by adjuster, team,
  policyholder, status, claim number), and a reference endpoint.
* ``TunedREST``   (config B) — the same API after REST best practice: sparse
  fieldsets (``fields``), compound documents (``include``), 100-row pages.
  This is the "just fix your API" baseline; it is deliberately strong.

Every call is metered: one backend request, bytes on the wire, and the JSON text
that would be handed to the model as the tool result.
"""
from __future__ import annotations

import json
import math

from .data import World


def dumps(obj) -> str:
    # The text an agent framework passes to the model: standard JSON, no indentation.
    return json.dumps(obj, ensure_ascii=False)


def _clean(rec: dict) -> dict:
    return {k: v for k, v in rec.items() if k != "_weight"}  # generator-internal field


def _pick(rec: dict, fields):
    if not fields:
        return rec
    return {k: rec[k] for k in fields if k in rec}


class Meter:
    """Per-conversation counters.

    requests   calls made to the interface at question time
    bytes      size of the response bodies the interface returned (after any field projection)
    sor_rows   records read from the system of record to build those responses
               (each claim, note, document, payment, customer, adjuster, team, policy, reference row)
    rm_rows    read-model rows examined (configs that use the read model)
    """

    def __init__(self):
        self.requests = 0
        self.bytes = 0
        self.sor_rows = 0
        self.rm_rows = 0
        self.log = []

    def record(self, path: str, body: str, sor_rows: int = 0, rm_rows: int = 0):
        self.requests += 1
        self.bytes += len(body.encode())
        self.sor_rows += sor_rows
        self.rm_rows += rm_rows
        self.log.append(path)


def _count_records(payload) -> int:
    """Source records behind a REST response: each top-level item plus any embedded related records."""
    data = payload.get("data") if isinstance(payload, dict) else None
    items = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
    n = 0
    for it in items:
        n += 1
        for rel in ("documents", "notes", "payments"):
            if isinstance(it.get(rel), list):
                n += len(it[rel])
    return n


class LegacyREST:
    MAX_PAGE = 20
    name = "A_legacy_rest"

    LEAN_DROP = {"checksum_sha256", "storage_uri", "_links", "geo"}

    def __init__(self, world: World, meter: Meter | None = None, lean: bool = False):
        self.w = world
        self.m = meter or Meter()
        self.lean = lean  # sensitivity variant: drop integration-only fields a UI API often omits

    def _strip(self, x):
        if isinstance(x, dict):
            return {k: self._strip(v) for k, v in x.items() if k not in self.LEAN_DROP}
        if isinstance(x, list):
            return [self._strip(v) for v in x]
        return x

    # --- plumbing -------------------------------------------------------------
    def _respond(self, path: str, payload) -> str:
        if self.lean:
            payload = self._strip(payload)
        body = dumps(payload)
        self.m.record(path, body, sor_rows=_count_records(payload))
        return body

    def _page(self, path, rows, page, page_size, extra_meta=None):
        page_size = min(page_size or self.MAX_PAGE, self.MAX_PAGE)
        total = len(rows)
        pages = max(1, math.ceil(total / page_size))
        chunk = rows[(page - 1) * page_size: page * page_size]
        payload = {"data": chunk,
                   "meta": {"page": page, "page_size": page_size, "total": total, "total_pages": pages, **(extra_meta or {})},
                   "_links": {"self": f"{path}?page={page}", "next": f"{path}?page={page + 1}" if page < pages else None,
                              "prev": f"{path}?page={page - 1}" if page > 1 else None}}
        return self._respond(f"{path}?page={page}", payload)

    # --- endpoints ------------------------------------------------------------
    def get_reference_loss_types(self, **kw):
        return self._respond("/reference/loss-types", {"data": list(self.w.loss_types.values())})

    def search_adjusters(self, q, page=1, page_size=None, **kw):
        rows = [_clean(a) for a in self.w.adjusters if q.lower() in a["display_name"].lower()]
        return self._page("/adjusters", self._shape("adjusters", rows, kw), page, page_size)

    def search_teams(self, q, page=1, page_size=None, **kw):
        rows = [t for t in self.w.teams if q.lower() in (t["name"] + " " + t["code"]).lower()]
        return self._page("/teams", self._shape("teams", rows, kw), page, page_size)

    def search_policyholders(self, q, page=1, page_size=None, **kw):
        rows = [p for p in self.w.policyholders if q.lower() in f"{p['first_name']} {p['last_name']}".lower()]
        return self._page("/policyholders", self._shape("policyholders", rows, kw), page, page_size)

    def list_claims(self, page=1, page_size=None, adjuster_id=None, team_id=None, policyholder_id=None,
                    status=None, claim_number=None, **kw):
        sts = set(status.split(",")) if status else None
        rows = [c for c in self._claim_pool(kw)
                if (adjuster_id is None or c["adjuster_id"] == adjuster_id)
                and (team_id is None or c["team_id"] == team_id)
                and (policyholder_id is None or c["policyholder_id"] == policyholder_id)
                and (sts is None or c["status"] in sts)
                and (claim_number is None or c["claim_number"] == claim_number)]
        return self._page("/claims", [self._claim_view(c, kw) for c in rows], page, page_size)

    def get_claim(self, claim_id, **kw):
        return self._respond(f"/claims/{claim_id}", {"data": self._claim_view(self.w.by_id[claim_id], kw)})

    def list_claim_documents(self, claim_id, page=1, page_size=None, **kw):
        return self._page(f"/claims/{claim_id}/documents", self._shape("documents", self.w.documents[claim_id], kw), page, page_size)

    def list_claim_notes(self, claim_id, page=1, page_size=None, **kw):
        notes = sorted(self.w.notes[claim_id], key=lambda n: n["created_at"], reverse=True)  # newest first, as the UI shows
        return self._page(f"/claims/{claim_id}/notes", self._shape("notes", notes, kw), page, page_size)

    def list_claim_payments(self, claim_id, page=1, page_size=None, **kw):
        return self._page(f"/claims/{claim_id}/payments", self._shape("payments", self.w.payments[claim_id], kw), page, page_size)

    def get_policy(self, policy_id, **kw):
        return self._respond(f"/policies/{policy_id}", {"data": self._shape("policies", [self.w.by_id[policy_id]], kw)[0]})

    def _claim_pool(self, kw):
        return self.w.claims

    # Legacy API ignores fields/include.
    def _shape(self, kind, rows, kw):
        return rows

    def _claim_view(self, claim, kw):
        return claim


class TunedREST(LegacyREST):
    """Same resources, plus sparse fieldsets, compound documents and larger pages."""
    MAX_PAGE = 100
    name = "B_tuned_rest"

    def _shape(self, kind, rows, kw):
        fields = (kw.get("fields") or {}).get(kind)
        return [_pick(r, fields) for r in rows]

    def _claim_view(self, claim, kw):
        f = kw.get("fields") or {}
        out = dict(_pick(claim, f.get("claims")))  # copy: never mutate the world
        inc = kw.get("include") or []
        for rel, src in (("documents", self.w.documents), ("notes", self.w.notes), ("payments", self.w.payments)):
            if rel in inc:
                out[rel] = [_pick(r, f.get(rel)) for r in src[claim["id"]]]
        return out


class DerivedREST(TunedREST):
    """Config B+ — the tuned REST API plus derived fields and filters served from the same read model as D.

    Claims gain ``missing_documents``, ``last_activity``, ``days_since_activity`` and ``paid_total``,
    and ``list_claims`` gains ``has_missing_documents`` and ``stale`` filters. This isolates the
    effect of *prepared data* from the effect of the *protocol / tool shape*.
    """
    name = "B2_rest_derived"
    DERIVED = ("missing_documents", "last_activity", "days_since_activity", "paid_total")

    def __init__(self, world, meter=None, read_model=None):
        super().__init__(world, meter)
        from .native import ClaimReadModel
        self.rm = read_model or ClaimReadModel(world)

    def _claim_view(self, claim, kw):
        r = self.rm.rows[claim["claim_number"]]
        self.m.rm_rows += 1  # derived fields come from one read-model row per returned claim
        full = {**claim, "missing_documents": r["missing_documents"], "last_activity": r["last_activity"],
                "days_since_activity": r["days_since_activity"], "paid_total": r["paid"]}
        return super()._claim_view(full, kw)

    def _claim_pool(self, kw):
        hm, st = kw.get("has_missing_documents"), kw.get("stale")
        rows = self.rm.rows
        return [c for c in self.w.claims
                if (hm is None or bool(rows[c["claim_number"]]["missing_documents"]) == hm)
                and (st is None or (rows[c["claim_number"]]["days_since_activity"] > rows[c["claim_number"]]["sla_days_no_activity"]) == st)]
