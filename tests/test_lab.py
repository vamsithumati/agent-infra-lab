import copy
import json

from aiflab import tasks
from aiflab.data import build_world
from aiflab.experiments import CONFIGS, run_all, run_one
from aiflab.native import ACTIVE, ClaimReadModel
from aiflab.rest import LegacyREST, TunedREST

W = build_world()


def test_world_is_deterministic():
    a, b = build_world(), build_world()
    assert json.dumps(a.claims) == json.dumps(b.claims)
    assert json.dumps(a.notes) == json.dumps(b.notes)


def test_every_claim_has_activity():
    assert all(len(W.notes[c["id"]]) >= 2 for c in W.claims)


def test_read_model_matches_independent_truth_for_every_claim():
    rm = ClaimReadModel(W)
    for c in W.claims:
        t = tasks.truth_u1(W, c["claim_number"])
        r = rm.rows[c["claim_number"]]
        assert r["missing_documents"] == t["missing"]
        assert r["days_since_activity"] == t["days_since_activity"]


def test_all_configs_agree_with_truth_everywhere():
    before = copy.deepcopy(W.claims)
    r = run_all(W)  # run_one asserts answer == truth for every config, task, adjuster, team
    assert W.claims == before, "APIs must not mutate the world"
    assert len(r["sweep_u2"]) == len(W.adjusters)


def test_tuned_rest_include_does_not_leak_into_world():
    api = TunedREST(W)
    c = W.claims[0]
    api.list_claims(claim_number=c["claim_number"], include=["documents", "notes"])
    assert "documents" not in c and "notes" not in c


def test_legacy_pagination_caps_page_size():
    body = json.loads(LegacyREST(W).list_claims(page_size=500))
    assert body["meta"]["page_size"] == 20 and len(body["data"]) == 20


def test_code_mode_hits_backend_as_often_as_legacy():
    rm = ClaimReadModel(W)
    name = W.adjusters[-1]["display_name"]
    truth = tasks.truth_u2(W, name)
    a = run_one(W, rm, "A_legacy_rest", "u2", name, truth)
    c = run_one(W, rm, "C_code_mode", "u2", name, truth)
    d = run_one(W, rm, "D_agent_native", "u2", name, truth)
    assert a["backend_requests"] == c["backend_requests"] > 50
    assert d["backend_requests"] == 1
    assert c["context_tokens"] < a["context_tokens"] / 20


def test_native_name_resolution_returns_actionable_error():
    from aiflab.native import AgentNativeAPI
    api = AgentNativeAPI(W)
    first = W.adjusters[0]["display_name"].split()[0]
    body = json.loads(api.claims_missing_documents(adjuster=first[:2]))
    assert ("error" in body and "hint" in body) or "claims" in body


def test_turn_accounting_is_monotone():
    from aiflab.conversation import Conversation
    conv = Conversation("[]", "q")
    conv.turn([("t", {})], ["x" * 100])
    c1 = conv.cumulative_input
    conv.answer("done")
    assert conv.cumulative_input > c1 and conv.turns == 2
