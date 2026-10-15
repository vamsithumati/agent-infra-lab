"""selfprep — a self-building, self-healing data-preparation layer for agents (reference implementation).

Agents ask the same kinds of questions over and over. This layer learns which ones,
prepares the data for them ahead of time, keeps it current from change events, and
continuously checks itself against the source of truth.

    Observer    records every agent data access as (query shape, parameters, cost if unprepared)
    Planner     ranks shapes by frequency x cost against build + upkeep; decides promote / demote
    Builder     builds the facts a shape needs from the system of record and registers a
                task-shaped tool for it (what an MCP server would list)
    Maintainer  applies change events incrementally; detects gaps in the event sequence and
                replays the missing events from the log
    Verifier    samples prepared answers and recomputes them from the source; on a mismatch it
                repairs the affected scope, raises the sampling rate, and after repeated
                mismatches quarantines the shape and rebuilds its facts (self-healing)
    Router      serves a question from prepared facts when the shape is promoted and healthy,
                otherwise from the source (the code-mode / tuned-API path)

The layer talks to the system of record only through a `Source` adapter (see `ClaimsSource`
in adaptive.py), which reports what every read costs in records. The query shapes it can
recognise and the facts behind them are declared in `ShapeSpec`s; generating new fact
definitions automatically is outside this implementation (see README: "What is not built").
"""
from __future__ import annotations

import random
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field


# --------------------------------------------------------------------------------------------- declarations
@dataclass(frozen=True)
class ShapeSpec:
    name: str
    scope: str                    # what the parameter identifies: claim | adjuster | team | policyholder
    facts: frozenset              # facts the prepared answer is computed from
    description: str
    param: str
    active_only: bool = True


@dataclass
class Event:
    seq: int
    keys: frozenset               # dependency keys the change touched, e.g. (claim_id, "notes")


# --------------------------------------------------------------------------------------------- observer
class Observer:
    """Sliding window of observed agent data access."""

    def __init__(self, window: int = 300):
        self.window = deque(maxlen=window)
        self.total = Counter()

    def record(self, shape: str, key, unprepared_cost: int):
        self.window.append((shape, unprepared_cost))
        self.total[shape] += 1

    def stats(self):
        n = len(self.window) or 1
        s = defaultdict(lambda: {"count": 0, "cost": 0})
        for shape, c in self.window:
            s[shape]["count"] += 1
            s[shape]["cost"] += c
        return {k: {"count": v["count"], "rate": v["count"] / n, "avg_cost": v["cost"] / v["count"]} for k, v in s.items()}


# --------------------------------------------------------------------------------------------- planner
class Planner:
    """Promote a shape when the records it would save over `horizon` questions exceed the cost of
    building and maintaining the facts it still needs. Demote when demand falls away (hysteresis)."""

    def __init__(self, horizon: int = 600, min_count: int = 8):
        self.horizon, self.min_count = horizon, min_count

    def plan(self, specs, stats, promoted, maintained, build_cost, upkeep_per_question, quarantined):
        actions = []
        for name, spec in specs.items():
            st = stats.get(name, {"count": 0, "rate": 0.0, "avg_cost": 0.0})
            new = spec.facts - maintained
            cost = sum(build_cost(f) for f in new) + sum(upkeep_per_question(f) for f in new) * self.horizon
            benefit = st["rate"] * st["avg_cost"] * self.horizon
            if name not in promoted and name not in quarantined and st["count"] >= self.min_count and benefit > cost:
                actions.append(("promote", name))
            elif name in promoted and st["count"] < self.min_count / 2:
                actions.append(("demote", name))
        return actions


# --------------------------------------------------------------------------------------------- builder + tool registry
class ToolRegistry:
    """The task-shaped tools the layer currently offers agents (what an MCP server would list)."""

    def __init__(self):
        self.tools = {}

    def register(self, spec: ShapeSpec):
        self.tools[spec.name] = {
            "name": spec.name.split("_", 1)[1], "description": spec.description + " Answers from prepared data; includes as_of.",
            "input_schema": {"type": "object", "properties": {spec.param: {"type": "string"}}, "required": [spec.param]}}

    def unregister(self, name):
        self.tools.pop(name, None)


class Builder:
    def __init__(self, source, store, registry: ToolRegistry, charge):
        self.source, self.store, self.registry, self.charge = source, store, registry, charge

    def build(self, fact):
        self.charge("build", self.source.build_fact(self.store, fact))

    def publish(self, spec: ShapeSpec):
        self.registry.register(spec)

    def retract(self, spec: ShapeSpec):
        self.registry.unregister(spec.name)


# --------------------------------------------------------------------------------------------- maintainer
class Maintainer:
    """Applies change events in sequence order. A gap in the sequence means events were lost in
    transit; the maintainer replays them from the source's event log before continuing."""

    def __init__(self, source, store, charge, replay=True):
        self.source, self.store, self.charge = source, store, charge
        self.replay_enabled = replay
        self.last_seq = 0
        self.gaps = 0
        self.replayed = 0

    def deliver(self, ev: Event):
        if ev.seq > self.last_seq + 1 and self.replay_enabled:
            self.gaps += 1
            missing = self.source.replay(self.last_seq + 1, ev.seq - 1)
            self.replayed += len(missing)
            self.charge("replay", len(missing))
            for m in missing:
                self.charge("upkeep", self.source.apply_incremental(self.store, m.keys))
        self.charge("upkeep", self.source.apply_incremental(self.store, ev.keys))
        self.last_seq = max(self.last_seq, ev.seq)


# --------------------------------------------------------------------------------------------- verifier
class Verifier:
    """Continuous evaluation. Samples prepared answers, recomputes them from the source, compares.

    mismatch -> repair the facts for that question's scope (targeted), raise the shape's sampling rate
    `escalate_after` mismatches within the recent checks -> quarantine the shape (router serves it from
    the source), rebuild its facts, then release it after `release_after` clean checks.
    """

    def __init__(self, source, store, charge, base_rate=0.05, high_rate=0.5, escalate_after=3,
                 recent=10, release_after=5, seed=5):
        self.source, self.store, self.charge = source, store, charge
        self.base_rate, self.high_rate = base_rate, high_rate
        self.escalate_after, self.release_after = escalate_after, release_after
        self.rng = random.Random(seed)
        self.recent = defaultdict(lambda: deque(maxlen=recent))
        self.rate = defaultdict(lambda: base_rate)
        self.quarantined = {}           # shape -> clean checks since rebuild
        self.checks = self.mismatches = self.repairs = self.rebuilds = 0
        self.incidents = []

    # ---- background checks, run once per planning cycle -------------------------------------------
    def watch_definitions(self, tick, maintained, rebuild):
        """Cheap check that the rules behind derived facts haven't changed (e.g. a required-documents list)."""
        changed, cost = self.source.definitions_changed(self.store)
        self.charge("verify", cost)
        for f in changed & maintained:
            self.rebuilds += 1
            self.incidents.append((tick, "definition-change rebuild", f))
            rebuild(f)

    def reconcile(self, tick, maintained, rebuild, sample=40, rebuild_if_over=0.05, aggregate_facts=frozenset()):
        """Rolling reconciliation: re-derive a random slice of claims' facts from the source and compare.
        Repairs what it finds. Rebuilds the whole fact if drift in the slice exceeds `rebuild_if_over` -- or
        if any drift is found in a fact that feeds an aggregate answer (team, portfolio), because an answer over
        hundreds of records is wrong if even a few percent of them have drifted."""
        for f in sorted(maintained):
            bad, cost = self.source.reconcile_slice(self.store, f, sample, self.rng)
            self.charge("verify", cost)
            self.checks += sample
            if bad:
                self.mismatches += bad
                self.repairs += bad
                self.incidents.append((tick, f"reconcile: {bad}/{sample} drifted", f))
                if bad / sample > rebuild_if_over or f in aggregate_facts:
                    self.rebuilds += 1
                    self.incidents.append((tick, "drift rebuild", f))
                    rebuild(f)

    def should_check(self, shape):
        return shape in self.quarantined or self.rng.random() < self.rate[shape]

    def check(self, spec: ShapeSpec, key, prepared_answer, tick, rebuild):
        fresh, cost = self.source.fresh(spec, key)
        self.charge("verify", cost)
        self.checks += 1
        ok = fresh == prepared_answer
        self.recent[spec.name].append(ok)
        if spec.name in self.quarantined:
            if ok:
                self.quarantined[spec.name] += 1
                if self.quarantined[spec.name] >= self.release_after:
                    del self.quarantined[spec.name]
                    self.rate[spec.name] = self.base_rate
                    self.incidents.append((tick, "release", spec.name))
            else:
                self.quarantined[spec.name] = 0
                self.charge("repair", self.source.repair_scope(self.store, spec, key))
            return fresh, ok
        if ok:
            if self.rate[spec.name] > self.base_rate and all(self.recent[spec.name]):
                self.rate[spec.name] = self.base_rate
            return fresh, ok
        self.mismatches += 1
        self.repairs += 1
        self.charge("repair", self.source.repair_scope(self.store, spec, key))
        self.rate[spec.name] = self.high_rate
        self.incidents.append((tick, "mismatch", spec.name))
        if sum(1 for x in self.recent[spec.name] if not x) >= self.escalate_after:
            self.quarantined[spec.name] = 0
            self.rebuilds += 1
            self.incidents.append((tick, "quarantine+rebuild", spec.name))
            for f in spec.facts:
                rebuild(f)
        return fresh, ok


# --------------------------------------------------------------------------------------------- the layer
class SelfPreparingLayer:
    def __init__(self, source, specs: dict, *, adaptive=True, healing=True, static_shapes=(), replan_every=50,
                 window=300, horizon=600, min_count=8, verify_rate=0.02, reconcile_sample=40, verify_every=None, seed=5,
                 shadow_checks=5):
        self.source, self.specs = source, specs
        self.adaptive, self.healing = adaptive, healing
        self.costs = Counter()
        self.store = source.new_store()
        self.registry = ToolRegistry()
        self.observer = Observer(window)
        self.planner = Planner(horizon, min_count)
        self.builder = Builder(source, self.store, self.registry, self._charge)
        self.maintainer = Maintainer(source, self.store, self._charge, replay=True)  # replay on gaps is baseline CDC hygiene, not "healing"
        self.verifier = Verifier(source, self.store, self._charge, base_rate=verify_rate, seed=seed)
        self.reconcile_sample = reconcile_sample
        self.verify_every = verify_every or replan_every
        self.promoted, self.maintained = set(), set()
        self.shadow = {}                 # promoted shapes still in shadow: prepared answers compared, not served
        self.shadow_checks = shadow_checks if healing else 0
        self.writes_by_fact = Counter()
        self.n_questions = 0
        self.replan_every = replan_every
        self.log = []
        self.served = Counter()
        for s in static_shapes:
            self._promote(specs[s], tick=0)

    def _charge(self, kind, n):
        self.costs[kind] += n

    # ---- lifecycle
    def _ensure_fact(self, f):
        if f not in self.maintained:
            self.maintained.add(f)
            self.builder.build(f)

    def _rebuild_fact(self, f):
        self.store.drop(f)
        self.builder.build(f)

    def _promote(self, spec, tick):
        for f in spec.facts:
            self._ensure_fact(f)
        self.promoted.add(spec.name)
        if self.shadow_checks and tick:
            # validate before serving: compare prepared vs source answers on live traffic, then go live
            self.shadow[spec.name] = 0
            self.log.append((tick, "promote (shadow)", spec.name))
        else:
            self.builder.publish(spec)
            self.log.append((tick, "promote", spec.name))

    def _demote(self, spec, tick):
        self.promoted.discard(spec.name)
        self.shadow.pop(spec.name, None)
        self.builder.retract(spec)
        self.log.append((tick, "demote", spec.name))
        needed = {f for n in self.promoted for f in self.specs[n].facts}
        for f in list(self.maintained - needed):
            self.maintained.discard(f)
            self.store.drop(f)
            self.log.append((tick, "drop-fact", f))

    def replan(self, tick):
        q = max(1, self.n_questions)
        actions = self.planner.plan(
            self.specs, self.observer.stats(), self.promoted, self.maintained,
            build_cost=self.source.build_cost, upkeep_per_question=lambda f: self.writes_by_fact[f] / q,
            quarantined=set(self.verifier.quarantined))
        for act, name in actions:
            (self._promote if act == "promote" else self._demote)(self.specs[name], tick)

    # ---- traffic
    def on_event(self, ev: Event | None, touched_facts=()):
        for f in touched_facts:
            self.writes_by_fact[f] += 1
        if ev is not None:
            self.maintainer.deliver(ev)

    def answer(self, shape: str, key, tick):
        spec = self.specs[shape]
        self.n_questions += 1
        self.observer.record(shape, key, self.source.fresh_cost(spec, key))
        healthy = spec.name not in self.verifier.quarantined and spec.name not in self.shadow
        if spec.name in self.shadow:
            ans, cost = self.source.fresh(spec, key)
            self._charge("question", cost)
            prep = self.source.prepared_answer(self.store, spec, key)
            if prep == ans:
                self.shadow[spec.name] += 1
                if self.shadow[spec.name] >= self.shadow_checks:
                    del self.shadow[spec.name]
                    self.builder.publish(spec)
                    self.log.append((tick, "live", spec.name))
            else:
                self.shadow[spec.name] = 0
                self._charge("repair", self.source.repair_scope(self.store, spec, key))
                self.log.append((tick, "shadow mismatch", spec.name))
            self.served["source (shadow)"] += 1
            if self.adaptive and self.n_questions % self.replan_every == 0:
                self.replan(tick)
            return ans, "source"
        if spec.name in self.promoted and healthy:
            ans = self.source.prepared_answer(self.store, spec, key)
            self._charge("read_model_rows", self.source.read_model_rows(self.store, spec, key))
            source = "prepared"
            if self.healing and self.verifier.should_check(shape):
                ans, _ = self.verifier.check(spec, key, ans, tick, self._rebuild_fact)  # serve the verified answer
                source = "prepared+verified"
        else:
            ans, cost = self.source.fresh(spec, key)
            self._charge("question", cost)
            source = "source"
            if spec.name in self.verifier.quarantined and spec.name in self.promoted:
                # keep checking the rebuilt facts in the background so the shape can be released
                prep = self.source.prepared_answer(self.store, spec, key)
                self.verifier.recent[spec.name].append(prep == ans)
                if prep == ans:
                    self.verifier.quarantined[spec.name] += 1
                    if self.verifier.quarantined[spec.name] >= self.verifier.release_after:
                        del self.verifier.quarantined[spec.name]
                        self.verifier.rate[spec.name] = self.verifier.base_rate
                        self.verifier.incidents.append((tick, "release", spec.name))
                else:
                    self.verifier.quarantined[spec.name] = 0
                    self._charge("repair", self.source.repair_scope(self.store, spec, key))
        self.served[source] += 1
        if self.adaptive and self.n_questions % self.replan_every == 0:
            self.replan(tick)
        if self.n_questions % self.verify_every == 0:
            if self.healing and self.maintained:
                self.verifier.watch_definitions(tick, self.maintained, self._rebuild_fact)
                agg = {f for n in self.promoted if self.specs[n].scope != "claim" for f in self.specs[n].facts}
                self.verifier.reconcile(tick, self.maintained, self._rebuild_fact, sample=self.reconcile_sample, aggregate_facts=agg)
        return ans, source
