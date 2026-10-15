# agent-infra-lab

**What does the *same* question cost an AI agent through different interfaces to the *same* system?**

This is a small, reproducible lab built on a synthetic insurance-claims system. Four adjuster questions run through six interfaces, plus a lean-payload sensitivity variant. Every interface must return the ground-truth answer, and the harness asserts that it does. The lab then measures what each interface costs in the model's context and in records read from the system of record at question time. Write-time costs are not measured.

| Config | What the agent gets |
|---|---|
| **A · legacy REST** | The API the claims UI already uses: resource-shaped, full objects, 20-row pages, IDs rather than names. |
| **A-lean** | A as a sensitivity check, with integration-only fields dropped: checksums, storage URIs, hypermedia links, geo. |
| **B · tuned REST** | The same API after REST best practice: sparse fieldsets (`fields`), compound documents (`include`), 100-row pages. A deliberately strong baseline. |
| **C · code mode over A** | The model writes one Python script against an SDK for API A. The sandbox runs it, and only printed output returns to the context. |
| **C′ · code mode over B** | The same, over the tuned API, with a script that uses `include` and `fields`. |
| **B+ · REST + derived fields** | API B plus derived fields and filters (`missing_documents`, `last_activity`, `stale`, `has_missing_documents`), served from the same read model as D. This config separates the effect of the protocol from the effect of preparing the data. |
| **D · agent-native tools** | Task-shaped MCP-style tools (`claim_brief`, `claims_missing_documents`, …) over read models that precompute what the tasks need and resolve IDs to names. |

The four questions:
- **U1** What is blocking one claim?
- **U2** Which of one adjuster's claims are missing documents? This runs for all 24 adjusters.
- **U3** What is one customer's total reserved vs total paid?
- **U4** Which of one team's claims are past their SLA? This runs for all 4 teams.

```bash
pip install tiktoken matplotlib pytest
python -m aiflab        # writes results/results.json, results/REPORT.md, results/charts/*.png (under 10 s)
python -m pytest -q     # tests
```

## Headline results

See [`results/REPORT.md`](results/REPORT.md) for the full tables. **Records read** means records the claims system reads at question time: each claim, note, document, payment or lookup row behind a response. For D the count is 0, because it answers from its read model; B+ reads claim rows, and its derived fields come from the read model. **Keeping the read model current costs work at write time, and this lab does not measure that.** Counting conventions: system-of-record records = records returned in responses, including embedded ones, assuming indexed access. Read-model rows: for D, the rows a tool examines before filtering (so D's 296 on U4 is conservative); for B+, one row per returned claim.

**U4, team-wide scan (TEAM-03: 296 active claims, 74 stale)**

| Config | Context tokens | Requests | Records read from the claims system |
|---|---:|---:|---:|
| A | 1.07M | 609 | 3,719 |
| B | 75.9K | 5 | 3,719 |
| C | 1.3K | 609 | 3,719 |
| C′ | 1.4K | 5 | 3,719 |
| B+ | 2.7K | 2 | 75 |
| D | 1.9K | 1 | 0 (296 read-model rows) |

**U2, all 24 adjusters: context tokens relative to D**

| Config | Median | Range |
|---|---:|---:|
| A | 89× | 33–169× |
| A-lean | 51× | 20–95× |
| B | 4.9× | |
| B+ | 2.5× | |
| C / C′ | 1.2× | |

**What this shows**
- Code mode fixes the context.
- The API underneath sets the load on the system of record.
- Only prepared data, whether served over REST (B+) or task-shaped tools (D), brings both down.

## How to read these numbers: evidence tags

| Tag | Meaning | Examples |
|---|---|---|
| **BY CONSTRUCTION** (including D's small numbers: its tools return the answer, so what's measured is how far the others sit above that floor) | Follows directly from the interface's shape, so the lab demonstrates it and does not discover it. | Call and request counts. A needs one documents call per claim because the API has no other way to get them. |
| **MEASURED** | Counted on the generated payloads. The ratios between configs are the result; absolute counts depend on the data and on the tokenizer. | Token counts. |
| **NOT MEASURED** | Out of scope for this lab. | Real-model behaviour (retries, wrong calls, accuracy), latency, dollar cost, and the cost of building and running D's read models. |

## Fairness and limitations

- **Best-case agent.** Every plan is the cheapest correct plan for that interface. It never retries or makes a wrong call, and it issues independent calls in parallel, including hundreds at once for A. Real agents do worse, and probably much worse on A, whose results exceed the context window. The numbers are floors.
- **Payloads are realistic, not padded.** Fields are those a claims screen or integration would use: codes, audit metadata, links, nested addresses. Check `aiflab/data.py`. A leaner legacy API would narrow the A↔B gap; B already shows what tuning buys.
- **Tool descriptions are written to the same standard in every config** (`aiflab/tooldefs.py`). Their token cost is charged on every turn.
- **Code mode scripts** (`aiflab/codemode.py`) are what a competent model would write. Their tokens are charged as model output. C′ runs the same idea over the tuned API.
- **D moves work, it doesn't delete it.** Read models must be kept up to date from the system of record (events/CDC), add storage, can be stale, and have to be built and owned. The lab does not measure that cost. D also only answers the questions it was designed for. For the long tail, C or B is the fallback.
- **Tokenizer:** `o200k_base` via tiktoken, a stand-in. Other tokenizers give different absolute counts; the comparisons are between configs on the same tokenizer.
- **Stale share:** about 15–25% of active claims per team are past their SLA. A recent touch is added to most active claims, so the share stays realistic.
- **Synthetic data:** 1,500 policyholders, 1,689 claims, about 7K documents, about 12K notes. Seed 7, deterministic.

MIT licence.

---

## v0.3 — `selfprep`: a self-building, self-healing data-preparation layer

```bash
python -m aiflab.adaptive_run   # simulated day(s); writes results/adaptive/ (about 10 minutes)
```

`aiflab/selfprep.py` is the reference implementation. `aiflab/adaptive.py` is the claims adapter and the simulator.

| Component | What it does |
|---|---|
| **Observer** | Records every agent data access as a query shape, its parameters, and what it would cost unprepared. |
| **Planner** | Promotes a shape when the records it would save over a horizon exceed the cost of building and maintaining it. Demotes the shape when demand fades. |
| **Builder** | Builds the facts a shape needs, then registers a task-shaped tool for it (what an MCP server would list). New shapes go live only after a **shadow phase**: their prepared answers must match the source on live traffic first. |
| **Maintainer** | Applies change events incrementally. The events carry the changed record. A sequence gap triggers a replay from the event log. |
| **Verifier** | Self-healing, in four parts: (1) samples prepared answers against the source; (2) runs **rolling reconciliation** over a slice of claims each cycle; (3) runs a **definition watch**, which catches rule changes cheaply; (4) quarantines and rebuilds on repeated mismatches, or on any drift in a fact that feeds an aggregate answer. |
| **Router** | Serves a question from prepared facts when the shape is promoted, live and healthy. Otherwise it falls back to the source, via code mode over the tuned API. |

**Simulated day.** 2,000 agent questions are mixed with 6,000 writes. Demand is skewed and shifts at question 1,000. Every answer is checked against the truth at the moment it was asked. Cost is counted in records read from the system of record: at question time, for build and upkeep, and for verification and repair. Full tables are in `results/adaptive/REPORT.md`.

- **A good guess beats self-building.** Across all 20 ways to prepare three question types up front (clean day):
  - every guess that includes the team-wide aggregate costs 50K–143K reads;
  - every guess that misses it costs 1.19M–1.30M;
  - self-building costs 91K, or 154K with shadowing and self-healing.

  Self-building is insurance against a wrong guess and against demand shifting. It captures about 96% of the savings of preparing everything up front (about 91% with self-healing).
- **Faulty day, mean of 5 seeds.**
  - Self-building without checks: 89K reads, **9.7%** stale.
  - Self-healing that verifies every 50 questions: 214K reads, **1.3%** stale.
  - A timer cache only reaches 0.5% stale at 881K reads, and 3.4% at 542K.
  - A write-invalidated cache (coarse: any write to any record the answer read) costs 1.05M reads.
- **One fault at a time, with what catches each.**
  - Lost events: gap replay, which is baseline in all variants.
  - Backfill: rolling reconciliation, 8.2% → 1.1%.
  - Rule change: definition watch, 2.3% → 0.2%.
- **Backfill size and detection.**
  - 150 documents: caught in 5/5 runs.
  - 20 documents: caught in 5/5 runs.
  - 5 documents: caught in only 3/5 runs.

**Assumptions to keep in mind:**
- One team-wide question type is 16% of questions but 90% of unprepared reads.
- Events apply with no lag.
- Clean-day correctness is by construction.
- Each fault was chosen to exercise one mechanism. Gap replay assumes a complete, retained log. The definition watch assumes rules are stored as data.
- Demotion and shadow rejection are implemented but were never triggered in these runs.

**What is simulated, not built:**
- Fact *definitions* are declared per shape, not generated. The layer decides which facts to build and when, but does not write new projection code.
- There is a single tenant, with no authorization on prepared facts.
- Incremental upkeep costs 1 record per maintained fact touched, which assumes true incremental maintenance.
- Latency, storage and real model behaviour are not simulated.
- The workload (skew, write mix, faults) is an assumption, so the ratios follow from it. The sensitivity runs are in the report.

**What the tests caught while this was being built:**
- A float-rounding drift in the incremental sums, found by the verifier itself on a clean day.
- A replay bug: events that said "read the newest record" double-applied on replay. Events now carry the changed record.
