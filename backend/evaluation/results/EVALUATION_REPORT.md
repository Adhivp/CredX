# CredX evaluation report

Generated 2026-10-31T12:36:24+00:00 from commit `1be3d1a` (Windows-11-10.0.26300-SP0, Python 3.14.7). LLM mode: **disabled**.

All figures below were computed by `python -m evaluation.run_eval` in this run. The scenarios are synthetic and labelled by design (see `evaluation/scenarios.py`); they test behaviour, not predictive accuracy on real portfolios.

## Financial classification (development scenarios)

These scenarios were used while developing the scoring and challenge rules, so their results overstate generalisation. See the held-out section for a fairer measure.

- Scenarios: 16; classification accuracy 100%
- Deterioration detection: precision 1.0, recall 1.0, false positives 0, missed deterioration 0 (scenarios with mixed labels excluded: 1)

| Signal family | Labelled | Precision | Recall | False positives | Missed |
| --- | --- | --- | --- | --- | --- |
| repayment | 12 | 1.0 | 1.0 | 0 | 0 |
| cashflow | 10 | 1.0 | 1.0 | 0 | 0 |
| exposure | 3 | 1.0 | 1.0 | 0 | 0 |

| Scenario | Expected | Classified | Severity | Path | Analyst pause | Specialists | Rounds | Correct |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| normal_repayment | low_risk | low_risk | low | A | no | repayment | 1 | yes |
| isolated_late_payment | low_risk / recovery | recovery | low | A | no | repayment | 1 | yes |
| repeated_delays | accelerating_deterioration / sustained_deterioration | sustained_deterioration | high | C | no | baseline, cashflow, exposure, repayment | 2 | yes |
| partial_repayments | emerging_deterioration / sustained_deterioration | sustained_deterioration | moderate | B | no | baseline, cashflow, exposure, repayment | 2 | yes |
| declining_inflows | accelerating_deterioration / emerging_deterioration / sustained_deterioration | sustained_deterioration | moderate | B | no | baseline, cashflow, exposure, repayment | 2 | yes |
| increasing_utilisation | emerging_deterioration | emerging_deterioration | moderate | B | no | exposure, repayment | 2 | yes |
| combined_deterioration | accelerating_deterioration / sustained_deterioration | accelerating_deterioration | severe | C | no | baseline, cashflow, exposure, repayment | 2 | yes |
| temporary_disruption_recovered | low_risk / recovery | recovery | low | A | no | repayment | 1 | yes |
| sustained_deterioration | sustained_deterioration | sustained_deterioration | severe | C | no | baseline, cashflow, exposure, repayment | 2 | yes |
| accelerating_deterioration | accelerating_deterioration | accelerating_deterioration | severe | C | no | baseline, cashflow, exposure, repayment | 2 | yes |
| missing_history | insufficient_evidence | insufficient_evidence | undetermined | B | no | integrity | 1 | yes |
| contradictory_records | emerging_deterioration / low_risk | low_risk | low | B | no | integrity, repayment | 1 | yes |
| duplicate_records | low_risk | low_risk | low | A | no | integrity, repayment | 1 | yes |
| seasonal_behaviour | low_risk | low_risk | low | A | no | cashflow | 1 | yes |
| new_customer | insufficient_evidence | insufficient_evidence | undetermined | B | no | integrity | 1 | yes |
| stale_data | low_risk | low_risk | low | B | no | integrity, repayment | 1 | yes |

## Held-out scenarios

Written and labelled before the system was first run on them, and not used for tuning afterwards.

- Scenarios: 10; accuracy 100%
- Deterioration detection: precision 1.0, recall 1.0, false positives 0, missed deterioration 0 (scenarios with mixed labels excluded: 1)

| Scenario | Expected | Classified | Severity | Path | Specialists | Correct |
| --- | --- | --- | --- | --- | --- | --- |
| ho_volatile_but_healthy | low_risk | low_risk | low | A | repayment | yes |
| ho_two_isolated_delays | low_risk / recovery | recovery | low | A | baseline, exposure, repayment | yes |
| ho_slow_inflow_erosion | accelerating_deterioration / emerging_deterioration / sustained_deterioration | sustained_deterioration | moderate | B | baseline, cashflow, exposure, repayment | yes |
| ho_creeping_utilisation | emerging_deterioration | emerging_deterioration | moderate | C | exposure, repayment | yes |
| ho_moderate_sustained_delays | sustained_deterioration | sustained_deterioration | high | C | baseline, cashflow, exposure, repayment | yes |
| ho_repayment_only_acceleration | accelerating_deterioration | accelerating_deterioration | severe | C | baseline, cashflow, exposure, repayment | yes |
| ho_recovered_cashflow_shock | low_risk / recovery | recovery | low | A | repayment | yes |
| ho_short_history_with_misses | accelerating_deterioration / emerging_deterioration / insufficient_evidence / sustained_deterioration | accelerating_deterioration | high | C | baseline, cashflow, exposure, integrity, repayment | yes |
| ho_long_seasonal_business | low_risk | low_risk | low | A | cashflow | yes |
| ho_stale_deteriorating | sustained_deterioration | sustained_deterioration | severe | C | baseline, cashflow, exposure, integrity, repayment | yes |

## Agent loop (development scenarios)

- Required specialists selected: 100%; excluded specialists avoided: 100%; mean specialists per case 2.38
- Scenarios with parallel execution: 9
- Specialist-initiated delegations authorised: 5; duplicate requests blocked: 4; duplicate tasks executed: 0
- Challenges raised: 14; re-investigations that addressed their challenge: 1.0
- Evidence references: 131, valid 100%; data-issue discovery recall 100%
- Clean termination: 100%; mean rounds 1.44; low-risk cases needing more than one round: 0
- Human-review routing matched labels: 100%
- Mean latency per investigation: 0.192 s; LLM calls 0, tokens 0, cost USD 0.0 (cost is 0 unless LLM_*_COST_PER_MTOK are configured)

## Security, human-in-the-loop and agent-loop test suites

`91 passed in 44.77s`

| Suite | Passed | Failed |
| --- | --- | --- |
| test_acceptance | 2 | 0 |
| test_agent_loop | 12 | 0 |
| test_hitl | 9 | 0 |
| test_llm_gateway | 19 | 0 |
| test_scoring | 24 | 0 |
| test_security_rbac | 25 | 0 |

## Limitations

- Synthetic scenarios cannot establish predictive validity; no claim is made about default prediction.
- Pre-adjudication pauses were answered by a scripted analyst that adds no context; real reviewer input changes outcomes.
