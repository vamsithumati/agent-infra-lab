# agent-infra-lab v0.3 — self-building, self-healing data preparation

A simulated day: 2,000 agent questions mixed with 6,000 writes. Every answer is checked against the truth at the moment it
was asked. Cost = records read from the system of record (question time + build/upkeep + verification/repair).
Change events apply with no propagation lag (see README: assumptions). Gap replay from the event log is baseline
maintenance in every prepared variant.

## Question types (clean day, unprepared)

| Type | Questions | Share of unprepared reads |
|---|---:|---:|
| T1 | 602 | 1.0% |
| T2 | 288 | 7.4% |
| T3 | 499 | 0.4% |
| T4 | 319 | 90.2% |
| T5 | 135 | 0.1% |
| T6 | 157 | 0.9% |

## Clean day, single seed

| Strategy | Records read, total | at question time | build + upkeep | verification + repair | Stale answers | Served prepared |
|---|---:|---:|---:|---:|---:|---:|
| S0 · no cache | 1,286,099 | 1,286,099 | 0 | 0 | 0 (0.0%) | 0 |
| S1 · TTL cache (400 ticks) | 272,411 | 272,411 | 0 | 0 | 200 (10.0%) | 0 |
| S2 · write-invalidated cache | 1,189,977 | 1,189,977 | 0 | 0 | 0 (0.0%) | 0 |
| D · static, designer's guess (T1–T3) | 1,212,213 | 1,176,042 | 36,171 | 0 | 0 (0.0%) | 1,389 |
| D · static, everything (T1–T6) | 36,171 | 0 | 36,171 | 0 | 0 (0.0%) | 2,000 |
| S3 · self-building | 90,692 | 54,535 | 36,157 | 0 | 0 (0.0%) | 1,666 |
| S4 · self-building + self-healing | 153,796 | 74,140 | 36,157 | 43,499 | 0 (0.0%) | 1,636 |

S4 lifecycle (question number, action, type): [(50, 'promote (shadow)', 'T4_team_stale_claims'), (63, 'live', 'T4_team_stale_claims'), (100, 'promote (shadow)', 'T2_adjuster_missing_docs'), (100, 'promote (shadow)', 'T6_adjuster_loss_mix'), (113, 'live', 'T2_adjuster_missing_docs'), (138, 'live', 'T6_adjuster_loss_mix'), (150, 'promote (shadow)', 'T1_claim_blockers'), (168, 'live', 'T1_claim_blockers'), (1200, 'promote (shadow)', 'T3_customer_financials'), (1207, 'live', 'T3_customer_financials'), (1250, 'promote (shadow)', 'T5_claim_paid_total'), (1299, 'live', 'T5_claim_paid_total')]

S4 healing counters: {'checks': 5475, 'mismatches': 0, 'repairs': 0, 'rebuilds': 0, 'gaps': 0, 'replayed': 0}

S4 incidents (question number): []

## Faulty day, single seed (1% of change events lost in transit; a 150-document backfill written straight to the database at 30%; a required-documents rule change at 70%)

| Strategy | Records read, total | at question time | build + upkeep | verification + repair | Stale answers | Served prepared |
|---|---:|---:|---:|---:|---:|---:|
| S0 · no cache | 1,294,126 | 1,294,126 | 0 | 0 | 0 (0.0%) | 0 |
| S1 · TTL cache (400 ticks) | 282,066 | 282,066 | 0 | 0 | 207 (10.3%) | 0 |
| S2 · write-invalidated cache | 1,193,175 | 1,193,175 | 0 | 0 | 7 (0.4%) | 0 |
| D · static, designer's guess (T1–T3) | 1,219,079 | 1,182,842 | 36,237 | 0 | 105 (5.2%) | 1,389 |
| D · static, everything (T1–T6) | 36,237 | 0 | 36,237 | 0 | 198 (9.9%) | 2,000 |
| S3 · self-building | 90,758 | 54,535 | 36,223 | 0 | 198 (9.9%) | 1,666 |
| S4 · self-building + self-healing | 190,680 | 74,140 | 72,822 | 43,718 | 42 (2.1%) | 1,636 |

S4 lifecycle (question number, action, type): [(50, 'promote (shadow)', 'T4_team_stale_claims'), (63, 'live', 'T4_team_stale_claims'), (100, 'promote (shadow)', 'T2_adjuster_missing_docs'), (100, 'promote (shadow)', 'T6_adjuster_loss_mix'), (113, 'live', 'T2_adjuster_missing_docs'), (138, 'live', 'T6_adjuster_loss_mix'), (150, 'promote (shadow)', 'T1_claim_blockers'), (168, 'live', 'T1_claim_blockers'), (1200, 'promote (shadow)', 'T3_customer_financials'), (1207, 'live', 'T3_customer_financials'), (1250, 'promote (shadow)', 'T5_claim_paid_total'), (1299, 'live', 'T5_claim_paid_total')]

S4 healing counters: {'checks': 5475, 'mismatches': 3, 'repairs': 3, 'rebuilds': 3, 'gaps': 65, 'replayed': 66}

S4 incidents (question number): [(650, 'reconcile: 2/40 drifted', 'missing'), (650, 'drift rebuild', 'missing'), (700, 'reconcile: 1/40 drifted', 'activity'), (700, 'drift rebuild', 'activity'), (1450, 'definition-change rebuild', 'missing')]

## Faulty day, mean of 5 seeds (min–max)

| Strategy | Records read | Stale answers |
|---|---:|---:|
| no preparation | 1,185,210 (1,107,991–1,322,811) | 0.0% (0.0%–0.0%) |
| self-building, no checks | 89,474 (71,773–105,752) | 9.7% (6.7%–12.3%) |
| self-healing, verify every 200 | 210,217 (168,865–236,178) | 4.7% (2.8%–7.6%) |
| self-healing, verify every 100 | 215,738 (177,844–238,250) | 2.1% (0.5%–3.2%) |
| self-healing, verify every 50 | 213,825 (175,622–287,943) | 1.3% (0.7%–1.8%) |
| self-healing, verify every 25 | 243,007 (221,131–273,234) | 0.8% (0.4%–1.6%) |
| timer cache, TTL 25 ticks | 881,447 (842,725–956,400) | 0.5% (0.4%–0.7%) |
| timer cache, TTL 100 ticks | 541,957 (511,987–554,722) | 3.4% (2.9%–4.0%) |
| timer cache, TTL 400 ticks | 257,316 (244,414–278,770) | 9.5% (8.6%–11.2%) |
| timer cache, TTL 800 ticks | 165,743 (157,845–170,957) | 13.6% (12.1%–15.2%) |
| write-invalidated cache | 1,047,661 (977,087–1,162,294) | 0.9% (0.2%–1.4%) |

## Every three-view static guess (clean day)

| Views prepared up front | Records read |
|---|---:|
| T1+T2+T4 | 50,262 |
| T2+T4+T6 | 53,252 |
| T2+T3+T4 | 60,579 |
| T2+T4+T5 | 64,215 |
| T1+T4+T6 | 132,275 |
| T3+T4+T6 | 134,090 |
| T4+T5+T6 | 137,726 |
| T1+T3+T4 | 139,602 |
| T3+T4+T5 | 143,203 |
| T1+T4+T5 | 143,238 |
| T2+T3+T6 | 1,191,413 |
| T2+T5+T6 | 1,195,049 |
| T2+T3+T5 | 1,200,526 |
| T1+T2+T6 | 1,204,886 |
| T1+T2+T3 | 1,212,213 |
| T1+T2+T5 | 1,215,849 |
| T3+T5+T6 | 1,274,037 |
| T1+T3+T6 | 1,294,226 |
| T1+T5+T6 | 1,297,862 |
| T1+T3+T5 | 1,303,339 |

## One fault at a time (5 seeds): stale answers, mean (min–max)

| Fault | No checks | Self-healing |
|---|---:|---:|
| lost events only | 0.0% (0.0%–0.0%) | 0.0% (0.0%–0.0%) |
| backfill only | 8.2% (5.7%–10.3%) | 1.1% (0.6%–1.5%) |
| rule change only | 2.3% (1.1%–3.2%) | 0.2% (0.1%–0.3%) |

## Backfill size: detection by the verifier (5 seeds)

| Documents written without events | Detected | Median questions to first detection | Stale answers, mean |
|---:|---:|---:|---:|
| 5 | 3/5 runs | 100 | 0.1% |
| 20 | 5/5 runs | 100 | 0.1% |
| 150 | 5/5 runs | 50 | 1.1% |

## Sensitivity (faulty day, single seed)

### write-heavy (10 writes per question)

| Strategy | Records read | Stale answers |
|---|---:|---:|
| S0 · no cache | 1,395,341 | 0 (0.0%) |
| S3 · self-building, no checks | 110,197 | 98 (4.9%) |
| S4 · self-building + self-healing | 202,719 | 6 (0.3%) |

### no skew over who is asked about

| Strategy | Records read | Stale answers |
|---|---:|---:|
| S0 · no cache | 1,120,957 | 0 (0.0%) |
| S3 · self-building, no checks | 109,658 | 191 (9.6%) |
| S4 · self-building + self-healing | 215,163 | 17 (0.9%) |

### flat mix of question types

| Strategy | Records read | Stale answers |
|---|---:|---:|
| S0 · no cache | 1,518,408 | 0 (0.0%) |
| S3 · self-building, no checks | 81,240 | 224 (11.2%) |
| S4 · self-building + self-healing | 183,653 | 87 (4.3%) |

### no demand shift

| Strategy | Records read | Stale answers |
|---|---:|---:|
| S0 · no cache | 2,033,001 | 0 (0.0%) |
| S3 · self-building, no checks | 89,043 | 261 (13.1%) |
| S4 · self-building + self-healing | 203,471 | 49 (2.5%) |

