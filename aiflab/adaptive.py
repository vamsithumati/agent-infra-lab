"""Lab v0.3: should the agents' own traffic decide what data gets prepared?

A simulated day on the claims system: agent questions arrive mixed with writes
(notes, document uploads, payments, status changes). Several serving strategies
answer the same question stream:

  S0  fresh            every question runs against the tuned API (code mode over B)
  S1  ttl-cache        S0 + answers cached for a fixed time (stale answers possible)
  S2  invalidating     S0 + answers cached and invalidated by writes they depend on
  S3  adaptive         S2 + the loop: observe -> rank by frequency x cost -> promote hot
                       question types to incrementally maintained facts -> demote cold ones
  D   static           hand-built read models for the question types a designer expected

Cost is counted in records read from the system of record (SoR):
  * question time: records the API reads to answer (same accounting as v0.2's sor_rows)
  * write time:    records read to build and maintain prepared facts
and read-model rows examined. Every answer is checked against the truth *at the
moment it was asked*; stale answers are counted, never hidden.

What is NOT simulated: generating projection code (the lab has a projection per
question type and the policy decides which to build and when); real model
behaviour; latency; storage. See README.
"""
from __future__ import annotations

import random
import zlib
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from datetime import timedelta

from . import tasks
from .data import TODAY, World, _ts, _uuid
from .native import ACTIVE

N_REF = 6  # reference loss-type rows every code-mode script reads

# --------------------------------------------------------------------------------------------- question types
# fact names: missing (needs documents), activity (notes + documents), paid (payments), membership (claim status)
TEMPLATES = {
    "T1_claim_blockers": {"facts": {"missing", "activity"}, "scope": "claim"},
    "T2_adjuster_missing_docs": {"facts": {"missing", "membership"}, "scope": "adjuster"},
    "T3_customer_financials": {"facts": {"paid", "membership"}, "scope": "policyholder"},
    "T4_team_stale_claims": {"facts": {"activity", "membership"}, "scope": "team"},
    "T5_claim_paid_total": {"facts": {"paid"}, "scope": "claim"},
    "T6_adjuster_loss_mix": {"facts": {"membership"}, "scope": "adjuster"},
}
FACT_SOURCES = {"missing": ("documents",), "activity": ("notes", "documents"), "paid": ("payments",), "membership": ("claim",)}


class Index:
    """Lookups the simulator needs; rebuilt cheaply after status changes."""

    def __init__(self, w: World):
        self.w = w
        self.by_number = {c["claim_number"]: c for c in w.claims}
        self.adj_name = {a["display_name"]: a["id"] for a in w.adjusters}
        self.team_code = {t["code"]: t["id"] for t in w.teams}
        self.ph_name = {}
        names = Counter(f"{p['first_name']} {p['last_name']}" for p in w.policyholders)
        for p in w.policyholders:
            n = f"{p['first_name']} {p['last_name']}"
            if names[n] == 1:
                self.ph_name[n] = p["id"]

    def claims_in_scope(self, scope, key, active_only=True):
        w = self.w
        if scope == "claim":
            return [self.by_number[key]]
        field_ = {"adjuster": "adjuster_id", "team": "team_id", "policyholder": "policyholder_id"}[scope]
        ident = {"adjuster": self.adj_name, "team": self.team_code, "policyholder": self.ph_name}[scope][key]
        return [c for c in w.claims if c[field_] == ident and (c["status"] in ACTIVE or not active_only)]


# --------------------------------------------------------------------------------------------- truth and fresh cost
def truth(ix: Index, t, key):
    w = ix.w
    if t == "T1_claim_blockers":
        return tasks.truth_u1(w, key)
    if t == "T2_adjuster_missing_docs":
        return tasks.truth_u2(w, key)
    if t == "T3_customer_financials":
        return tasks.truth_u3(w, ix.ph_name[key])
    if t == "T4_team_stale_claims":
        return tasks.truth_u4(w, key)
    if t == "T5_claim_paid_total":
        c = ix.by_number[key]
        return round(sum(p["amount"] for p in w.payments[c["id"]] if p["status"] == "paid"), 2)
    if t == "T6_adjuster_loss_mix":
        return dict(sorted(Counter(c["loss_type_code"] for c in ix.claims_in_scope("adjuster", key)).items()))
    raise KeyError(t)


def fresh_cost(ix: Index, t, key) -> int:
    """SoR records a code-mode script over the tuned API (C') reads for this question.
    Mirrors v0.2 sor_rows accounting; tests check it against the real scripts for T1-T4."""
    w = ix.w
    if t in ("T1_claim_blockers", "T5_claim_paid_total"):
        c = ix.by_number[key]
        rel = ("documents", "notes") if t.startswith("T1") else ("payments",)
        return N_REF + 1 + sum(len(getattr(w, r)[c["id"]]) for r in rel)
    if t == "T2_adjuster_missing_docs":
        cs = ix.claims_in_scope("adjuster", key)
        return N_REF + 1 + len(cs) + sum(len(w.documents[c["id"]]) for c in cs)
    if t == "T3_customer_financials":
        cs = ix.claims_in_scope("policyholder", key, active_only=False)
        return N_REF + 1 + len(cs) + sum(len(w.payments[c["id"]]) for c in cs)
    if t == "T4_team_stale_claims":
        cs = ix.claims_in_scope("team", key)
        return N_REF + 1 + len(cs) + sum(len(w.notes[c["id"]]) + len(w.documents[c["id"]]) for c in cs)
    if t == "T6_adjuster_loss_mix":
        return N_REF + 1 + len(ix.claims_in_scope("adjuster", key))
    raise KeyError(t)


def deps(ix: Index, t, key):
    """What a cached answer depends on: (claim_id, relation) for every relation the question read, plus scope keys."""
    spec = TEMPLATES[t]
    rels = {r for f in spec["facts"] for r in FACT_SOURCES[f]}
    if spec["scope"] == "claim":
        cs = ix.claims_in_scope("claim", key)
    else:
        cs = ix.claims_in_scope(spec["scope"], key, active_only=t != "T3_customer_financials")
    out = {(c["id"], r) for c in cs for r in rels if r != "claim"}
    if spec["scope"] != "claim":
        out.add(("scope", spec["scope"], key))
    else:
        out.add((cs[0]["id"], "claim"))
    return out


def build_cost(w: World, fact) -> int:
    """One-time records read to build a fact for every claim (full scan of its sources)."""
    n = 0
    for r in FACT_SOURCES[fact]:
        n += len(w.claims) if r == "claim" else sum(len(v) for v in getattr(w, r).values())
    return n


# --------------------------------------------------------------------------------------------- workload
@dataclass
class Workload:
    seed: int = 11
    n_questions: int = 2000
    writes_per_question: float = 3.0
    template_weights: dict = field(default_factory=lambda: {
        "T1_claim_blockers": 0.32, "T4_team_stale_claims": 0.24, "T2_adjuster_missing_docs": 0.18,
        "T6_adjuster_loss_mix": 0.12, "T3_customer_financials": 0.08, "T5_claim_paid_total": 0.06})
    param_zipf: float = 1.1          # skew over which claim / adjuster / team / customer is asked about
    write_mix: dict = field(default_factory=lambda: {"note": 0.60, "document": 0.25, "payment": 0.10, "status": 0.05})
    shift_at: float | None = 0.5     # halfway through, demand shifts (tests re-promotion; demotion is not triggered)
    shifted_weights: dict = field(default_factory=lambda: {
        "T1_claim_blockers": 0.30, "T4_team_stale_claims": 0.05, "T2_adjuster_missing_docs": 0.10,
        "T6_adjuster_loss_mix": 0.05, "T3_customer_financials": 0.40, "T5_claim_paid_total": 0.10})
    # --- injected faults (all off -> a clean day)
    event_loss: float = 0.0          # share of change events lost in transit (sequence gap; replayable from the log)
    backfill_at: float | None = None  # a bulk job writes documents straight to the database, emitting no events
    backfill_size: int = 150
    rule_change_at: float | None = None  # the business changes a required-documents rule; no event is emitted


FAULTY = dict(event_loss=0.01, backfill_at=0.3, rule_change_at=0.7)


def _zipf_choice(rng, items, s):
    if s <= 0:
        return rng.choice(items)
    weights = [1 / (i + 1) ** s for i in range(len(items))]
    return rng.choices(items, weights=weights)[0]


def generate_events(w: World, wl: Workload):
    """A deterministic list of ("q", template, key) and ("w", kind, claim_number, ...) events."""
    rng = random.Random(wl.seed)
    ix = Index(w)
    active = sorted(c["claim_number"] for c in w.claims if c["status"] in ACTIVE)
    rng.shuffle(active)
    params = {
        "claim": active,
        "adjuster": sorted(ix.adj_name),
        "team": sorted(ix.team_code),
        "policyholder": sorted(n for n in ix.ph_name if len(ix.claims_in_scope("policyholder", n, False)) >= 1),
    }
    for k in ("adjuster", "team", "policyholder"):
        rng.shuffle(params[k])
    ev = []
    total_writes = int(wl.n_questions * wl.writes_per_question)
    slots = ["q"] * wl.n_questions + ["w"] * total_writes
    rng.shuffle(slots)
    qn = 0
    frng = random.Random(wl.seed + 1)
    done = set()
    for s in slots:
        frac = qn / wl.n_questions
        if wl.backfill_at is not None and frac >= wl.backfill_at and "backfill" not in done:
            done.add("backfill")
            for _ in range(wl.backfill_size):
                ev.append(("w", "document", frng.choice(active), frng.random(), "out_of_band"))
        if wl.rule_change_at is not None and frac >= wl.rule_change_at and "rule" not in done:
            done.add("rule")
            ev.append(("rule", "MV_COLL", "repair_invoice"))
        if s == "q":
            weights = wl.template_weights if (wl.shift_at is None or qn < wl.shift_at * wl.n_questions) else wl.shifted_weights
            t = rng.choices(list(weights), weights=list(weights.values()))[0]
            key = _zipf_choice(rng, params[TEMPLATES[t]["scope"]], wl.param_zipf)
            ev.append(("q", t, key))
            qn += 1
        else:
            kind = rng.choices(list(wl.write_mix), weights=list(wl.write_mix.values()))[0]
            mode = "lost" if frng.random() < wl.event_loss else "delivered"
            ev.append(("w", kind, rng.choice(active), rng.random(), mode))
    return ev


def apply_write(w: World, ix: Index, kind, claim_number, u):
    """Mutate the world; return the dependency keys the write touches."""
    c = ix.by_number[claim_number]
    cid = c["id"]
    rng = random.Random(zlib.crc32(f"{claim_number}|{kind}|{u}".encode()))
    if kind == "note":
        w.notes[cid].append({"id": _uuid(rng), "claim_id": cid, "author_id": c["adjuster_id"], "created_at": _ts(TODAY, rng),
                             "body": "Follow-up recorded.", "visibility": "internal", "tags": [], "_links": {}})
        return {(cid, "notes", len(w.notes[cid]) - 1)}  # events carry the changed record, not "read the newest"
    if kind == "document":
        req = w.loss_types[c["loss_type_code"]]["required_documents"]
        dt = req[int(u * len(req))] if u < 0.7 else "correspondence"
        w.documents[cid].append({"id": _uuid(rng), "claim_id": cid, "filename": f"{dt}.pdf", "mime_type": "application/pdf",
                                 "size_bytes": 100_000, "doc_type_code": dt, "classification_confidence": 0.9,
                                 "uploaded_by": "policyholder_portal", "uploaded_at": _ts(TODAY, rng), "ocr_status": "complete",
                                 "virus_scan": "clean", "checksum_sha256": "0" * 64, "storage_uri": "s3://x", "retention_class": "7Y", "_links": {}})
        return {(cid, "documents", len(w.documents[cid]) - 1)}
    if kind == "payment":
        w.payments[cid].append({"id": _uuid(rng), "claim_id": cid, "amount": round(200 + u * 2000, 2), "currency": "AUD",
                                "payee_type": "repairer", "payee_ref": "x", "method": "EFT", "status": "paid", "approved_by": c["adjuster_id"],
                                "paid_at": _ts(TODAY, rng), "gl_code": "5100-CLM", "_links": {}})
        return {(cid, "payments", len(w.payments[cid]) - 1)}
    if kind == "status":
        # toggle between open and pending_info most of the time; occasionally close (leaves active scopes)
        c["status"] = "closed" if u < 0.3 else ("pending_info" if c["status"] == "open" else "open")
        adj = next(a["display_name"] for a in w.adjusters if a["id"] == c["adjuster_id"])
        team = next(t["code"] for t in w.teams if t["id"] == c["team_id"])
        ph = next(f"{p['first_name']} {p['last_name']}" for p in w.policyholders if p["id"] == c["policyholder_id"])
        return {(cid, "claim"), ("scope", "adjuster", adj), ("scope", "team", team), ("scope", "policyholder", ph)}
    raise KeyError(kind)


def _facts_touched(keys):
    rel = {k[1] for k in keys if k[0] != "scope"}
    return {f for f, srcs in FACT_SOURCES.items() if rel & set(srcs)}


# --------------------------------------------------------------------------------------------- strategies
class Strategy:
    name = "base"

    def __init__(self, ix: Index):
        self.ix = ix
        self.sor_question = 0
        self.sor_upkeep = 0
        self.rm_rows = 0
        self.stale = 0
        self.hits = 0
        self.served_prepared = 0
        self.timeline = []      # per question: SoR records spent (question + upkeep since last question)
        self.stale_ticks = []
        self._upkeep_since = 0

    # hooks
    def on_write(self, keys, tick):
        pass

    def answer(self, t, key, tick):
        raise NotImplementedError

    def record(self, cost_q):
        self.timeline.append(cost_q + self._upkeep_since)
        self._upkeep_since = 0

    def upkeep(self, n):
        self.sor_upkeep += n
        self._upkeep_since += n

    def fresh(self, t, key):
        c = fresh_cost(self.ix, t, key)
        self.sor_question += c
        return truth(self.ix, t, key), c


class Fresh(Strategy):
    name = "S0 · no cache"

    def answer(self, t, key, tick):
        ans, c = self.fresh(t, key)
        self.record(c)
        return ans


class TTLCache(Strategy):
    def __init__(self, ix, ttl):
        super().__init__(ix)
        self.ttl = ttl
        self.cache = {}
        self.name = f"S1 · TTL cache ({ttl} ticks)"

    def answer(self, t, key, tick):
        hit = self.cache.get((t, key))
        if hit and tick - hit[1] < self.ttl:
            self.hits += 1
            self.record(0)
            return hit[0]
        ans, c = self.fresh(t, key)
        self.cache[(t, key)] = (ans, tick)
        self.record(c)
        return ans


class InvalidatingCache(Strategy):
    name = "S2 · write-invalidated cache"

    def __init__(self, ix):
        super().__init__(ix)
        self.cache = {}
        self.rev = defaultdict(set)

    def on_write(self, keys, tick):
        for k in keys:
            k = k if k[0] == "scope" else k[:2]
            for entry in self.rev.pop(k, ()):
                self.cache.pop(entry, None)

    def answer(self, t, key, tick):
        if (t, key) in self.cache:
            self.hits += 1
            self.record(0)
            return self.cache[(t, key)]
        ans, c = self.fresh(t, key)
        self.cache[(t, key)] = ans
        for d in deps(self.ix, t, key):
            self.rev[d].add((t, key))
        self.record(c)
        return ans


from .selfprep import Event, SelfPreparingLayer, ShapeSpec  # noqa: E402

SPECS = {
    "T1_claim_blockers": ShapeSpec("T1_claim_blockers", "claim", frozenset({"missing", "activity"}),
                                   "What a claim needs next: missing required documents and days since activity.", "claim_number"),
    "T2_adjuster_missing_docs": ShapeSpec("T2_adjuster_missing_docs", "adjuster", frozenset({"missing", "membership"}),
                                          "An adjuster's active claims that are missing required documents.", "adjuster"),
    "T3_customer_financials": ShapeSpec("T3_customer_financials", "policyholder", frozenset({"paid", "membership"}),
                                        "Reserved and paid totals across a customer's claims.", "policyholder", active_only=False),
    "T4_team_stale_claims": ShapeSpec("T4_team_stale_claims", "team", frozenset({"activity", "membership"}),
                                      "A team's active claims with no activity beyond their SLA.", "team"),
    "T5_claim_paid_total": ShapeSpec("T5_claim_paid_total", "claim", frozenset({"paid"}), "Total paid on one claim.", "claim_number"),
    "T6_adjuster_loss_mix": ShapeSpec("T6_adjuster_loss_mix", "adjuster", frozenset({"membership"}),
                                      "Count of an adjuster's active claims by loss type.", "adjuster"),
}


class FactStore:
    """Prepared per-claim facts. Derived values (e.g. the list of missing documents) are stored, computed
    with the rules as they were when built or last repaired -- exactly how a real projection behaves."""

    def __init__(self, w: World):
        self.w = w
        self.f = {}
        self.have = {}
        self.rules = {k: list(v["required_documents"]) for k, v in w.loss_types.items()}
        self.claims = {c["id"]: c for c in w.claims}

    def _missing_for(self, cid):
        c = self.claims[cid]
        return [d for d in self.rules[c["loss_type_code"]] if d not in self.have[cid]]

    def build(self, fact, claims=None):
        w = self.w
        claims = claims if claims is not None else w.claims
        if fact == "missing":
            self.rules = {k: list(v["required_documents"]) for k, v in w.loss_types.items()}
            self.f.setdefault(fact, {})
            for c in claims:
                self.have[c["id"]] = {d["doc_type_code"] for d in w.documents[c["id"]]}
                self.f[fact][c["id"]] = self._missing_for(c["id"])
        elif fact == "activity":
            self.f.setdefault(fact, {}).update({c["id"]: max([n["created_at"][:10] for n in w.notes[c["id"]]] +
                                                             [d["uploaded_at"][:10] for d in w.documents[c["id"]]]) for c in claims})
        elif fact == "paid":
            # integer cents: incremental sums must not drift from fresh sums through float rounding
            self.f.setdefault(fact, {}).update({c["id"]: sum(round(p["amount"] * 100) for p in w.payments[c["id"]] if p["status"] == "paid") for c in claims})
        elif fact == "membership":
            self.f.setdefault(fact, {}).update({c["id"]: c["status"] for c in claims})

    def drop(self, fact):
        self.f.pop(fact, None)

    def apply(self, keys):
        """Incremental update from the newest record: one record read per maintained fact touched."""
        w, n = self.w, 0
        for k in keys:
            if k[0] == "scope":
                continue
            cid, rel = k[0], k[1]
            idx = k[2] if len(k) == 3 else -1
            if rel == "documents":
                d = w.documents[cid][idx]
                if "missing" in self.f:
                    self.have[cid].add(d["doc_type_code"])
                    self.f["missing"][cid] = self._missing_for(cid)
                    n += 1
                if "activity" in self.f:
                    self.f["activity"][cid] = max(self.f["activity"][cid], d["uploaded_at"][:10])
                    n += 1
            elif rel == "notes" and "activity" in self.f:
                self.f["activity"][cid] = max(self.f["activity"][cid], w.notes[cid][idx]["created_at"][:10])
                n += 1
            elif rel == "payments" and "paid" in self.f:
                p = w.payments[cid][idx]
                self.f["paid"][cid] += round(p["amount"] * 100) if p["status"] == "paid" else 0
                n += 1
            elif rel == "claim" and "membership" in self.f:
                self.f["membership"][cid] = self.claims[cid]["status"]
                n += 1
        return n


class ClaimsSource:
    """The `Source` adapter selfprep uses: every read reports its cost in system-of-record records."""

    def __init__(self, w: World, ix: Index, log):
        self.w, self.ix, self.log = w, ix, log

    def new_store(self):
        return FactStore(self.w)

    def build_cost(self, fact):
        return build_cost(self.w, fact)

    def build_fact(self, store, fact):
        store.build(fact)
        return build_cost(self.w, fact) + (N_REF if fact == "missing" else 0)

    def apply_incremental(self, store, keys):
        return store.apply(keys)

    def replay(self, lo, hi):
        return [e for e in self.log if lo <= e.seq <= hi]

    def fresh_cost(self, spec, key):
        return fresh_cost(self.ix, spec.name, key)

    def fresh(self, spec, key):
        return truth(self.ix, spec.name, key), fresh_cost(self.ix, spec.name, key)

    def _scope(self, store, spec, key):
        if spec.scope == "claim":
            return [self.ix.by_number[key]]
        field_ = {"adjuster": "adjuster_id", "team": "team_id", "policyholder": "policyholder_id"}[spec.scope]
        ident = {"adjuster": self.ix.adj_name, "team": self.ix.team_code, "policyholder": self.ix.ph_name}[spec.scope][key]
        mem = store.f["membership"] if "membership" in store.f else {c["id"]: c["status"] for c in self.w.claims}
        return [c for c in self.w.claims if c[field_] == ident and (mem[c["id"]] in ACTIVE or not spec.active_only)]

    def read_model_rows(self, store, spec, key):
        return len(self._scope(store, spec, key))

    def prepared_answer(self, store, spec, key):
        from datetime import date
        f = store.f
        cs = self._scope(store, spec, key)
        days = lambda c: (TODAY - date.fromisoformat(f["activity"][c["id"]])).days  # noqa: E731
        t = spec.name
        if t == "T1_claim_blockers":
            return {"missing": f["missing"][cs[0]["id"]], "days_since_activity": days(cs[0])}
        if t == "T2_adjuster_missing_docs":
            return sorted([[c["claim_number"], f["missing"][c["id"]]] for c in cs if f["missing"][c["id"]]])
        if t == "T3_customer_financials":
            return {"reserved": round(sum(c["reserve"]["amount"] for c in cs), 2), "paid": round(sum(f["paid"][c["id"]] for c in cs) / 100, 2)}
        if t == "T4_team_stale_claims":
            return sorted(c["claim_number"] for c in cs if days(c) > self.w.loss_types[c["loss_type_code"]]["sla_days_no_activity"])
        if t == "T5_claim_paid_total":
            return round(f["paid"][cs[0]["id"]] / 100, 2)
        if t == "T6_adjuster_loss_mix":
            return dict(sorted(Counter(c["loss_type_code"] for c in cs).items()))
        raise KeyError(t)

    def definitions_changed(self, store):
        """Read the reference rules (N_REF records) and compare with the rules the facts were built with."""
        current = {k: list(v["required_documents"]) for k, v in self.w.loss_types.items()}
        return ({"missing"} if current != store.rules else set()), N_REF

    def reconcile_slice(self, store, fact, k, rng):
        """Re-derive `fact` for k random claims from the source; repair and count the ones that drifted."""
        if fact not in store.f:
            return 0, 0
        cs = rng.sample(self.w.claims, k)
        before = {c["id"]: store.f[fact].get(c["id"]) for c in cs}
        store.build(fact, claims=cs) if fact != "missing" else self._rebuild_missing_keep_rules(store, cs)
        bad = sum(1 for c in cs if store.f[fact].get(c["id"]) != before[c["id"]])
        cost = sum(len(cs) if r == "claim" else sum(len(getattr(self.w, r)[c["id"]]) for c in cs) for r in FACT_SOURCES[fact])
        return bad, cost

    def _rebuild_missing_keep_rules(self, store, cs):
        # re-derive from the source documents using the store's current rule snapshot (rule drift is
        # the definition watcher's job; reconciliation catches data drift)
        for c in cs:
            store.have[c["id"]] = {d["doc_type_code"] for d in self.w.documents[c["id"]]}
            store.f["missing"][c["id"]] = store._missing_for(c["id"])

    def repair_scope(self, store, spec, key):
        """Targeted repair: re-read this question's scope from the source (and the current rules)."""
        spec_all = SPECS[spec.name]
        cs = [self.ix.by_number[key]] if spec.scope == "claim" else self.ix.claims_in_scope(spec.scope, key, active_only=False)
        n = N_REF
        for fct in spec_all.facts:
            if fct in store.f:
                store.build(fct, claims=cs)
                for r in FACT_SOURCES[fct]:
                    n += len(cs) if r == "claim" else sum(len(getattr(self.w, r)[c["id"]]) for c in cs)
        return n


# --------------------------------------------------------------------------------------------- strategies
class LayerStrategy(Strategy):
    def __init__(self, ix, log, name, **kw):
        super().__init__(ix)
        self.name = name
        self.layer = SelfPreparingLayer(ClaimsSource(ix.w, ix, log), SPECS, **kw)
        self._last_total = 0  # static builds happen before the first question and count toward it

    def on_event(self, ev, keys):
        self.layer.on_event(ev, _facts_touched(keys))

    def answer(self, t, key, tick):
        ans, src = self.layer.answer(t, key, tick)
        if src.startswith("prepared"):
            self.served_prepared += 1
        total = sum(v for k, v in self.layer.costs.items() if k != "read_model_rows")
        self.timeline.append(total - self._last_total)
        self._last_total = total
        return ans

    def finish(self):
        c = self.layer.costs
        self.sor_question = c["question"]
        self.sor_upkeep = c["build"] + c["upkeep"] + c["replay"]
        self.sor_verify = c["verify"] + c["repair"]
        self.rm_rows = c["read_model_rows"]


def simulate(world_factory, wl: Workload, strategies_factory):
    """Run every strategy on its own copy of the world over the same event stream (same faults)."""
    w0 = world_factory()
    events = generate_events(w0, wl)
    results = {}
    for make in strategies_factory:
        w = world_factory()
        ix = Index(w)
        log = []
        s = make(ix, log)
        tick = seq = 0
        for e in events:
            tick += 1
            if e[0] == "w":
                keys = apply_write(w, ix, e[1], e[2], e[3])
                if e[4] == "out_of_band":
                    continue                      # written straight to the database: no event, nothing logged
                seq += 1
                ev = Event(seq, frozenset(keys))
                log.append(ev)
                if e[4] == "lost":
                    if isinstance(s, LayerStrategy):
                        s.layer.on_event(None, _facts_touched(keys))  # demand stats only; the event never arrives
                    continue                      # lost in transit: recoverable from the log if a gap is noticed
                if isinstance(s, LayerStrategy):
                    s.on_event(ev, keys)
                else:
                    s.on_write(keys, tick)
            elif e[0] == "rule":
                lt = w.loss_types[e[1]]
                if e[2] not in lt["required_documents"]:
                    lt["required_documents"].append(e[2])
            else:
                _, t, key = e
                got = s.answer(t, key, tick)
                if got != truth(ix, t, key):
                    s.stale += 1
                    s.stale_ticks.append(len(s.timeline))
        if hasattr(s, "finish"):
            s.finish()
        q_at = []                                  # event tick -> number of questions asked so far
        qn = 0
        for e in events:
            qn += e[0] == "q"
            q_at.append(qn)
        to_q = lambda tick: q_at[min(len(q_at), max(tick, 1)) - 1]  # noqa: E731
        n_q = len(s.timeline)
        lay = getattr(s, "layer", None)
        results[s.name] = {
            "questions": n_q, "sor_question": s.sor_question, "sor_upkeep": s.sor_upkeep,
            "sor_verify": getattr(s, "sor_verify", 0),
            "sor_total": s.sor_question + s.sor_upkeep + getattr(s, "sor_verify", 0),
            "rm_rows": s.rm_rows, "stale": s.stale, "stale_rate": s.stale / n_q, "stale_at": s.stale_ticks,
            "cache_hits": s.hits, "served_prepared": s.served_prepared, "timeline": s.timeline,
            "log": [(to_q(t), act, n) for t, act, n in lay.log] if lay else [],
            "incidents": [(to_q(t), act, n) for t, act, n in lay.verifier.incidents] if lay else [],
            "fault_at_q": {k: to_q(i + 1) for i, e in enumerate(events) for k in (["backfill"] if len(e) == 5 and e[4] == "out_of_band" else
                                                                                   ["rule"] if e[0] == "rule" else [])},
            "verifier": ({"checks": lay.verifier.checks, "mismatches": lay.verifier.mismatches, "repairs": lay.verifier.repairs,
                          "rebuilds": lay.verifier.rebuilds, "gaps": lay.maintainer.gaps, "replayed": lay.maintainer.replayed}
                         if lay else {}),
            "tools": sorted(lay.registry.tools) if lay else [],
        }
    return results


EXPECTED = ["T1_claim_blockers", "T2_adjuster_missing_docs", "T3_customer_financials"]


def default_strategies(wl: Workload):
    return [
        lambda ix, log: Fresh(ix),
        lambda ix, log: TTLCache(ix, ttl=400),
        lambda ix, log: InvalidatingCache(ix),
        lambda ix, log: LayerStrategy(ix, log, "D · static, designer's guess (T1–T3)", adaptive=False, healing=False, static_shapes=EXPECTED),
        lambda ix, log: LayerStrategy(ix, log, "D · static, everything (T1–T6)", adaptive=False, healing=False, static_shapes=list(SPECS)),
        lambda ix, log: LayerStrategy(ix, log, "S3 · self-building", adaptive=True, healing=False),
        lambda ix, log: LayerStrategy(ix, log, "S4 · self-building + self-healing", adaptive=True, healing=True),
    ]
