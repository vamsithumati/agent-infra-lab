import json

from aiflab import codemode
from aiflab.adaptive import (FAULTY, SPECS, ClaimsSource, Index, LayerStrategy, Workload, apply_write, fresh_cost,
                             generate_events, simulate, truth)
from aiflab.conversation import Conversation
from aiflab.data import build_world
from aiflab.rest import Meter, TunedREST
from aiflab.selfprep import Event
from aiflab.tooldefs import as_text, code_mode_tools

SMALL = dict(n_questions=400)


def test_fresh_cost_matches_the_real_code_mode_scripts():
    w = build_world()
    ix = Index(w)
    adj = sorted(ix.adj_name)[3]
    cases = [("u1", "T1_claim_blockers", sorted(ix.by_number)[5]), ("u2", "T2_adjuster_missing_docs", adj),
             ("u3", "T3_customer_financials", sorted(ix.ph_name)[2]), ("u4", "T4_team_stale_claims", "TEAM-02")]
    for task, shape, key in cases:
        m = Meter()
        conv = Conversation(as_text(code_mode_tools(tuned=True)), "q")
        ans = codemode.run(task, key, TunedREST(w, m), conv, tuned=True)
        assert m.sor_rows == fresh_cost(ix, shape, key), (shape, m.sor_rows, fresh_cost(ix, shape, key))
        assert json.loads(json.dumps(ans)) == json.loads(json.dumps(truth(ix, shape, key)))


def test_clean_day_prepared_answers_are_never_stale():
    wl = Workload(**SMALL)
    r = simulate(build_world, wl, [
        lambda ix, log: LayerStrategy(ix, log, "S3", adaptive=True, healing=False),
        lambda ix, log: LayerStrategy(ix, log, "S4", adaptive=True, healing=True),
        lambda ix, log: LayerStrategy(ix, log, "D", adaptive=False, healing=False, static_shapes=list(SPECS))])
    for name, v in r.items():
        assert v["stale"] == 0, name
    assert r["S3"]["served_prepared"] > 0 and r["S3"]["log"], "the planner should promote hot shapes"
    assert not r["S4"]["incidents"], "no false alarms on a clean day"


def test_prepared_tools_are_registered_on_promotion():
    wl = Workload(**SMALL)
    r = simulate(build_world, wl, [lambda ix, log: LayerStrategy(ix, log, "S3", adaptive=True, healing=False)])
    promoted = {name for _, act, name in r["S3"]["log"] if act == "promote"}
    assert set(r["S3"]["tools"]) == promoted


def test_lost_events_are_replayed_from_the_log():
    w = build_world()
    ix = Index(w)
    log = []
    s = LayerStrategy(ix, log, "S4", adaptive=False, healing=True, static_shapes=["T5_claim_paid_total"])
    claim = sorted(ix.by_number)[0]
    keys1 = apply_write(w, ix, "payment", claim, 0.5)
    log.append(Event(1, frozenset(keys1)))          # lost: never delivered
    keys2 = apply_write(w, ix, "payment", claim, 0.6)
    ev2 = Event(2, frozenset(keys2))
    log.append(ev2)
    s.on_event(ev2, keys2)                         # the gap is noticed here
    assert s.layer.maintainer.gaps == 1 and s.layer.maintainer.replayed == 1
    assert s.layer.answer("T5_claim_paid_total", claim, 0)[0] == truth(ix, "T5_claim_paid_total", claim)


def test_rule_change_is_detected_and_rebuilt():
    w = build_world()
    ix = Index(w)
    s = LayerStrategy(ix, [], "S4", adaptive=False, healing=True, static_shapes=["T1_claim_blockers"], verify_every=1, verify_rate=0.0)
    w.loss_types["MV_COLL"]["required_documents"].append("repair_invoice")
    claim = next(c["claim_number"] for c in w.claims if c["loss_type_code"] == "MV_COLL" and c["status"] in ("open", "pending_info")
                 and "repair_invoice" not in {d["doc_type_code"] for d in w.documents[c["id"]]})
    s.layer.answer("T1_claim_blockers", sorted(ix.by_number)[1], 0)   # one question triggers the background checks
    assert any(i[1] == "definition-change rebuild" for i in s.layer.verifier.incidents)
    assert s.layer.answer("T1_claim_blockers", claim, 0)[0] == truth(ix, "T1_claim_blockers", claim)


def test_faulty_day_healing_beats_no_healing():
    wl = Workload(**SMALL, **FAULTY)
    r = simulate(build_world, wl, [lambda ix, log: LayerStrategy(ix, log, "S3", adaptive=True, healing=False),
                                   lambda ix, log: LayerStrategy(ix, log, "S4", adaptive=True, healing=True)])
    assert r["S4"]["stale"] < r["S3"]["stale"]


def test_event_stream_is_deterministic():
    a = generate_events(build_world(), Workload(**SMALL, **FAULTY))
    b = generate_events(build_world(), Workload(**SMALL, **FAULTY))
    assert a == b
