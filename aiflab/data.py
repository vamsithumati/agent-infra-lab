"""Synthetic claims world. Deterministic for a given seed.

The record shapes mirror what a typical claims system's UI-backed REST API returns:
nested objects, audit metadata, hypermedia links, codes rather than labels, and
IDs rather than names. Nothing is padded for effect; every field is one a claims
screen or integration would plausibly use. See README "Fairness" section.
"""
from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

TODAY = date(2026, 10, 1)

LOSS_TYPES = {
    "MV_COLL": ("Motor vehicle collision", ["claim_form", "repair_estimate", "photos", "police_report"]),
    "MV_THEFT": ("Motor vehicle theft", ["claim_form", "police_report", "proof_of_ownership"]),
    "HOME_WATER": ("Home - escape of water", ["claim_form", "repair_estimate", "photos", "plumber_report"]),
    "HOME_STORM": ("Home - storm damage", ["claim_form", "repair_estimate", "photos"]),
    "HOME_BURG": ("Home - burglary", ["claim_form", "police_report", "proof_of_ownership", "receipts"]),
    "TRAVEL_MED": ("Travel - medical", ["claim_form", "medical_invoice", "travel_itinerary"]),
}
DOC_TYPES = sorted({d for _, docs in LOSS_TYPES.values() for d in docs} | {"repair_invoice", "correspondence", "other"})
STATUSES = ["open", "open", "open", "pending_info", "closed", "closed"]
FIRST = ["Aarav", "Olivia", "Liam", "Mia", "Noah", "Isla", "Ethan", "Zara", "Lucas", "Ava", "Arjun", "Chloe",
         "Mason", "Priya", "Jack", "Sofia", "Leo", "Grace", "Ravi", "Emily", "Oscar", "Hannah", "Kai", "Ruby"]
LAST = ["Smith", "Patel", "Nguyen", "Brown", "Wilson", "Taylor", "Singh", "Martin", "Lee", "Walker", "Khan",
        "Thompson", "White", "Harris", "Clark", "Lewis", "Young", "King", "Wright", "Scott", "Green", "Baker"]
NOTE_PHRASES = [
    "Called policyholder to confirm loss details.", "Requested additional photos of the damage.",
    "Repairer quote received and reviewed against market rates.", "Left voicemail, awaiting call back.",
    "Coverage confirmed under section 2 of the policy wording.", "Referred to desk assessor for review.",
    "Policyholder advised of excess applicable to this claim.", "Emailed requesting police event number.",
    "Supplier engaged for emergency make-safe works.", "Reserve reviewed following updated estimate.",
]


def _uuid(rng: random.Random) -> str:
    return str(uuid.UUID(int=rng.getrandbits(128), version=4))


def _ts(d: date, rng: random.Random) -> str:
    return datetime(d.year, d.month, d.day, rng.randrange(8, 18), rng.randrange(60), rng.randrange(60)).isoformat() + "Z"


@dataclass
class World:
    teams: list = field(default_factory=list)
    adjusters: list = field(default_factory=list)
    policyholders: list = field(default_factory=list)
    policies: list = field(default_factory=list)
    claims: list = field(default_factory=list)
    documents: dict = field(default_factory=dict)  # claim_id -> [doc]
    notes: dict = field(default_factory=dict)      # claim_id -> [note]
    payments: dict = field(default_factory=dict)   # claim_id -> [payment]
    loss_types: dict = field(default_factory=dict)
    by_id: dict = field(default_factory=dict)


def build_world(seed: int = 7, n_policyholders: int = 1500, n_adjusters: int = 24, n_teams: int = 4) -> World:
    rng = random.Random(seed)
    w = World()
    w.loss_types = {code: {"code": code, "label": label, "required_documents": list(docs),
                           "sla_days_no_activity": 14 if code.startswith("MV") else 21}
                    for code, (label, docs) in LOSS_TYPES.items()}
    for t in range(n_teams):
        w.teams.append({"id": _uuid(rng), "code": f"TEAM-{t + 1:02d}", "name": ["Motor North", "Motor South", "Property", "Travel & Specialty"][t % 4]})
    # adjusters with deliberately varied portfolio weights (used for the scale sweep)
    used = set()
    for a in range(n_adjusters):
        while True:
            name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
            if name not in used:
                used.add(name)
                break
        w.adjusters.append({"id": _uuid(rng), "employee_number": f"E{10000 + a}", "display_name": name,
                            "email": name.lower().replace(" ", ".") + "@insurer.example", "team_id": w.teams[a % n_teams]["id"],
                            "authority_limit": rng.choice([5000, 10000, 25000, 50000]), "active": True,
                            "_weight": 0.3 + 3.0 * (a / max(1, n_adjusters - 1)) ** 2})
    weights = [a["_weight"] for a in w.adjusters]
    for p in range(n_policyholders):
        fn, ln = rng.choice(FIRST), rng.choice(LAST)
        ph = {"id": _uuid(rng), "customer_number": f"C{200000 + p}", "first_name": fn, "last_name": ln,
              "date_of_birth": str(date(1950, 1, 1) + timedelta(days=rng.randrange(18000))),
              "email": f"{fn.lower()}.{ln.lower()}{p}@mail.example", "phone": f"+61 4{rng.randrange(10**7, 10**8)}",
              "address": {"line1": f"{rng.randrange(1, 300)} {rng.choice(['High', 'Station', 'Park', 'Church', 'Mill'])} St",
                          "line2": None, "suburb": rng.choice(["Richmond", "Newtown", "Fitzroy", "Glebe", "Kew"]),
                          "state": rng.choice(["VIC", "NSW", "QLD"]), "postcode": str(rng.randrange(2000, 4999)), "country": "AU"},
              "preferred_contact": rng.choice(["email", "phone", "sms"]), "marketing_opt_in": rng.random() < 0.3,
              "kyc_status": "verified", "created_at": _ts(TODAY - timedelta(days=rng.randrange(400, 4000)), rng)}
        w.policyholders.append(ph)
        for _ in range(rng.choice([1, 1, 2])):
            lt = rng.choice(list(LOSS_TYPES))
            prod = {"MV": "MOTOR_COMP", "HO": "HOME_CONTENTS_BUILDING", "TR": "TRAVEL_INTL"}[lt[:2]]
            start = TODAY - timedelta(days=rng.randrange(30, 700))
            pol = {"id": _uuid(rng), "policy_number": f"P{rng.randrange(10**7, 10**8)}", "product_code": prod,
                   "policyholder_id": ph["id"], "status": "in_force", "inception_date": str(start),
                   "expiry_date": str(start + timedelta(days=365)), "sum_insured": rng.choice([20000, 45000, 350000, 650000]),
                   "excess": rng.choice([500, 750, 1000]), "premium": {"annual": round(rng.uniform(600, 3200), 2), "currency": "AUD",
                                                                         "frequency": rng.choice(["annual", "monthly"])},
                   "coverages": [{"code": c, "limit": rng.choice([5000, 20000, 100000]), "sub_limit": None}
                                 for c in rng.sample(["SEC1", "SEC2", "SEC3", "SEC4", "SEC5"], 3)],
                   "endorsements": [], "underwriting_ref": f"UW-{rng.randrange(10**5, 10**6)}",
                   "_links": {"self": "/policies/" + "{id}", "policyholder": "/policyholders/" + ph["id"]}}
            pol["_links"]["self"] = f"/policies/{pol['id']}"
            w.policies.append(pol)
            if rng.random() < 0.85:
                _make_claim(w, rng, pol, ph, lt, weights)
    for coll in (w.teams, w.adjusters, w.policyholders, w.policies, w.claims):
        for r in coll:
            w.by_id[r["id"]] = r
    return w


def _make_claim(w: World, rng: random.Random, pol, ph, lt, weights):
    adj = rng.choices(w.adjusters, weights=weights)[0]
    loss = TODAY - timedelta(days=rng.randrange(3, 240))
    reported = loss + timedelta(days=rng.randrange(0, 10))
    status = rng.choice(STATUSES)
    cid = _uuid(rng)
    reserve = round(rng.uniform(800, 40000), 2)
    claim = {
        "id": cid, "claim_number": f"CLM-{100000 + len(w.claims)}", "status": status,
        "status_reason_code": None if status == "open" else rng.choice(["AWAIT_DOCS", "SETTLED", "DECLINED", "WITHDRAWN"]),
        "loss_type_code": lt, "loss_date": str(loss), "reported_date": str(reported),
        "loss_description": f"{LOSS_TYPES[lt][0]} reported by policyholder. " + rng.choice(NOTE_PHRASES),
        "loss_location": {"line1": ph["address"]["line1"], "suburb": ph["address"]["suburb"], "state": ph["address"]["state"],
                          "postcode": ph["address"]["postcode"], "geo": {"lat": round(rng.uniform(-38, -27), 5), "lng": round(rng.uniform(144, 153), 5)}},
        "policy_id": pol["id"], "policyholder_id": ph["id"], "adjuster_id": adj["id"], "team_id": adj["team_id"],
        "channel": rng.choice(["web", "phone", "broker", "app"]), "broker_ref": None,
        "reserve": {"amount": reserve, "currency": "AUD", "updated_at": _ts(reported + timedelta(days=2), rng), "updated_by": adj["id"]},
        "excess_applied": pol["excess"], "fraud_score": round(rng.random(), 3), "litigation_flag": rng.random() < 0.03,
        "subrogation_flag": rng.random() < 0.08, "priority": rng.choice(["normal", "normal", "high"]),
        "close_sla": {"target_close_days": 30},
        "audit": {"created_at": _ts(reported, rng), "created_by": "svc-intake", "updated_at": _ts(reported + timedelta(days=rng.randrange(1, 60)), rng),
                  "updated_by": adj["id"], "version": rng.randrange(1, 40)},
        "custom_fields": {"cat_event_code": rng.choice([None, None, "CAT-2026-03"]), "vip": False, "language": "en"},
        "_links": {"self": f"/claims/{cid}", "documents": f"/claims/{cid}/documents", "notes": f"/claims/{cid}/notes",
                   "payments": f"/claims/{cid}/payments", "policy": f"/policies/{pol['id']}", "adjuster": f"/adjusters/{adj['id']}"},
    }
    w.claims.append(claim)
    required = LOSS_TYPES[lt][1]
    have = [d for d in required if rng.random() < 0.8] + [d for d in ("repair_invoice", "correspondence", "other") if rng.random() < 0.35]
    if lt.startswith(("MV_COLL", "HOME")) and rng.random() < 0.5:
        have.append("repair_invoice")
    docs = []
    for dt in have:
        did = _uuid(rng)
        up = reported + timedelta(days=rng.randrange(0, 40))
        docs.append({"id": did, "claim_id": cid, "filename": f"{dt}_{rng.randrange(1000, 9999)}.pdf", "mime_type": "application/pdf",
                     "size_bytes": rng.randrange(40_000, 4_000_000), "doc_type_code": dt, "classification_confidence": round(rng.uniform(0.6, 0.99), 2),
                     "uploaded_by": rng.choice(["policyholder_portal", "broker", adj["id"]]), "uploaded_at": _ts(up, rng),
                     "ocr_status": "complete", "virus_scan": "clean", "checksum_sha256": f"{rng.getrandbits(256):064x}",
                     "storage_uri": f"s3://claims-docs-prod/{cid}/{did}.pdf", "retention_class": "7Y",
                     "_links": {"self": f"/documents/{did}", "content": f"/documents/{did}/content"}})
    w.documents[cid] = docs
    notes = []
    last = reported
    for _ in range(rng.randrange(2, 12)):
        last = last + timedelta(days=rng.randrange(0, 12))
        if last > TODAY:
            last = TODAY
        nid = _uuid(rng)
        notes.append({"id": nid, "claim_id": cid, "author_id": adj["id"], "created_at": _ts(last, rng),
                      "body": " ".join(rng.sample(NOTE_PHRASES, rng.randrange(1, 4))), "visibility": "internal",
                      "tags": rng.sample(["contact", "coverage", "docs", "reserve", "supplier"], rng.randrange(0, 3)),
                      "_links": {"self": f"/notes/{nid}"}})
    # Active claims are usually being worked: most have a recent touch, so a minority are genuinely stale.
    if status in ("open", "pending_info") and rng.random() < 0.75:
        nid = _uuid(rng)
        notes.append({"id": nid, "claim_id": cid, "author_id": adj["id"],
                      "created_at": _ts(TODAY - timedelta(days=rng.randrange(0, 14)), rng),
                      "body": " ".join(rng.sample(NOTE_PHRASES, rng.randrange(1, 4))), "visibility": "internal",
                      "tags": rng.sample(["contact", "coverage", "docs", "reserve", "supplier"], rng.randrange(0, 3)),
                      "_links": {"self": f"/notes/{nid}"}})
    w.notes[cid] = notes
    pays = []
    if status != "open" or rng.random() < 0.4:
        for _ in range(rng.randrange(0, 3)):
            pid = _uuid(rng)
            pays.append({"id": pid, "claim_id": cid, "amount": round(rng.uniform(200, reserve * 0.6), 2), "currency": "AUD",
                         "payee_type": rng.choice(["policyholder", "repairer", "supplier"]), "payee_ref": _uuid(rng),
                         "method": "EFT", "status": rng.choice(["paid", "paid", "scheduled"]), "approved_by": adj["id"],
                         "paid_at": _ts(reported + timedelta(days=rng.randrange(5, 60)), rng),
                         "gl_code": rng.choice(["5100-CLM", "5110-CLM"]), "_links": {"self": f"/payments/{pid}"}})
    w.payments[cid] = pays
