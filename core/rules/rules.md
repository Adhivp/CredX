# Early Warning System — Rule Definitions & Threshold Justification

**Dataset:** Home Credit Default Risk (Kaggle)
**Population:** 307,511 customers | 13,605,401 instalment records
**Baseline default rate:** 8.07% (24,825 defaults)

All thresholds in this document are derived from statistical analysis of the dataset above. Each severity label is justified by observed default rates relative to the population baseline. A threshold is meaningful only when the observed default rate for that segment materially deviates from 8.07%.

---

## Rule 1: Repeated Repayment Delays

**Purpose:** Identify customers with recurring late or missed instalments. Distinguish occasional lateness from deteriorating repayment behaviour.

### Data Findings

**Late payment prevalence:** 8.43% of all instalments are late (1,146,669 / 13,605,401).

| Metric | P25 | P50 | P75 | P90 | P95 | P99 |
|---|---|---|---|---|---|---|
| Days late (when late) | 2 | 4 | 9 | 20 | 27 | 107 |
| Customer late ratio | 0.0% | 1.8% | 10.9% | 23.4% | 32.1% | 50.0% |

**Default rate by late payment ratio:**

| Late Ratio | Customers | Default Rate | vs Baseline |
|---|---|---|---|
| 0–5% | 205,897 | 6.83% | 0.85x |
| 5–10% | 41,796 | 8.87% | 1.10x |
| 10–20% | 46,842 | 9.76% | 1.21x |
| 20–30% | 24,619 | 10.83% | 1.34x |
| 30–50% | 16,960 | 12.81% | 1.59x |
| 50–100% | 3,473 | 15.82% | 1.96x |

**Default rate by maximum DPD:**

| Max DPD | Customers | Default Rate | vs Baseline |
|---|---|---|---|
| 0 | 18,437 | 8.17% | 1.01x |
| 1–15 | 105,074 | 9.39% | 1.16x |
| 16–30 | 34,918 | 9.30% | 1.15x |
| 31–60 | 11,618 | 10.84% | 1.34x |
| 61–90 | 1,777 | 14.39% | 1.78x |

**Default rate by average days late:**

| Avg Days Late | Customers | Default Rate | vs Baseline |
|---|---|---|---|
| 0–5 days | 101,408 | 8.92% | 1.11x |
| 5–10 days | 38,706 | 10.34% | 1.28x |
| 10–15 days | 17,081 | 9.92% | 1.23x |
| 15–30 days | 15,701 | 9.93% | 1.23x |
| 30–60 days | 2,639 | 11.41% | 1.41x |

### Severity Thresholds

| Severity | Score Range | Trigger Conditions | Observed Default | vs Baseline | Rationale |
|---|---|---|---|---|---|
| **NONE** | 0–10 | Late ratio < 5% | 6.83% | 0.85x | Below baseline. Occasional lateness in a long history is noise, not signal. These customers are statistically safer than average. |
| **LOW** | 10–30 | Late ratio 5–10% | 8.87% | 1.10x | At baseline. Early signal visible but not actionable alone. Warrants passive tracking. |
| **MEDIUM** | 30–55 | Late ratio 10–20% OR max DPD 31–60 | 9.76–10.84% | 1.21–1.34x | Meaningful deviation from norm. One in ten customers in this band defaults. Warrants active monitoring. |
| **HIGH** | 55–80 | Late ratio 20–30% OR max DPD 61–90 | 10.83–14.39% | 1.34–1.78x | Clear escalation. Max DPD 61–90 shows the sharpest jump in the entire DPD distribution (+4.5pp from the previous band). Requires investigation. |
| **CRITICAL** | 80–100 | Late ratio > 30% | 12.81–15.82% | 1.59–1.96x | Nearly double baseline default rate. Urgent intervention needed. |

### Business Rule

A customer is flagged when their repayment behaviour deviates significantly from the population norm. The 5% late ratio threshold for NONE was chosen because below that level, default rates are actually **below** baseline — occasional lateness in a long history is noise, not signal. The sharpest risk inflection occurs between 10–20% late ratio (where default jumps from 8.87% to 9.76%) and at max DPD 61–90 (where default spikes from 10.84% to 14.39%).

---

## Rule 2: Temporary vs Sustained Misses

**Purpose:** Distinguish isolated payment delays (bank holidays, processing lags, temporary salary delays) from sustained repayment deterioration. Prevent false positives from one-off events.

### Data Findings

**Default rate by maximum consecutive late streak:**

| Max Streak | Customers | Default Rate | vs Baseline |
|---|---|---|---|
| 0 | 12,051 | 6.70% | 0.83x |
| 1 | 7,194 | 9.34% | 1.16x |
| 2 | 3,899 | 10.23% | 1.27x |
| 3 | 1,276 | 10.58% | 1.31x |
| 4–5 | 834 | 10.43% | 1.29x |
| 6–10 | 415 | 10.12% | 1.25x |

**Default rate by recovery rate** (% of misses followed by on-time payment):

| Recovery Rate | Customers | Default Rate | vs Baseline |
|---|---|---|---|
| 80–100% | 20,693 | 7.84% | **0.97x** |
| 60–80% | 3,036 | 9.88% | 1.22x |
| 40–60% | 1,366 | 10.47% | 1.30x |
| 20–40% | 479 | 12.73% | 1.58x |
| 0–20% | 127 | 9.45% | 1.17x |

### Classification Thresholds

| Classification | Trigger Conditions | Observed Default | vs Baseline | Rationale |
|---|---|---|---|---|
| **TEMPORARY** | Streak ≤ 1 AND recovery ≥ 80% | 7.84–9.34% | 0.97–1.16x | At or below baseline. Recovery rate ≥ 80% actually predicts **lower** default than the population average (7.84% vs 8.07%). The customer resumed payments immediately. |
| **INCONCLUSIVE** | Streak = 2 OR recovery 40–80% | 9.88–10.47% | 1.22–1.30x | Ambiguous signal. Could resolve or worsen. Warrants continued monitoring before escalation. |
| **SUSTAINED** | Streak ≥ 3 OR recovery < 40% | 10.58–12.73% | 1.31–1.58x | Structural difficulty. Recovery rate below 40% shows the highest default (12.73%) of any recovery band. Pattern is unlikely to self-correct. |

### Business Rule

Not every missed payment is a risk signal. The data proves that customers who miss once and recover immediately (recovery ≥ 80%) are **safer than average** — 7.84% default vs 8.07% baseline. This makes sense: a customer who has the financial capacity and intent to recover immediately is demonstrating resilience, not fragility.

The system must avoid false positives from bank holidays, processing delays, or temporary salary delays. Only persistent patterns (streak ≥ 3, where default stabilises at ~10.5%) or poor recovery (< 40%, where default spikes to 12.7%) warrant escalation.

The confidence score is lower for INCONCLUSIVE classifications (max 0.7) because the data shows streak-2 and recovery-40-80% segments sit in a zone where outcomes could go either way.

---

## Rule 3: Increasing Financial Exposure

**Purpose:** Detect rising financial obligations, credit utilisation, and over-indebtedness. Assess whether exposure is growing faster than repayment capacity.

### Data Findings

**Active loan count:**

| Metric | P25 | P50 | P75 | P90 | P95 | P99 |
|---|---|---|---|---|---|---|
| Active loans | 1 | 2 | 3 | 4 | 5 | 8 |

**Default rate by active loan count:**

| Active Loans | Customers | Default Rate | vs Baseline |
|---|---|---|---|
| 0–1 | 85,488 | 7.10% | 0.88x |
| 2 | 67,509 | 7.44% | 0.92x |
| 3 | 44,111 | 8.26% | 1.02x |
| 4 | 25,828 | 9.23% | 1.14x |
| 5 | 14,144 | 10.66% | 1.32x |
| 6+ | 14,735 | 13.38% | 1.66x |

**Default rate by bureau credit utilisation** (total debt / total credit limit):

| Utilisation | Customers | Default Rate | vs Baseline |
|---|---|---|---|
| 0–30% | 89,405 | 6.40% | 0.79x |
| 30–50% | 54,200 | 8.19% | 1.01x |
| 50–70% | 38,152 | 10.31% | 1.28x |
| 70–85% | 18,336 | 12.17% | 1.51x |
| 85–95% | 9,172 | 13.01% | 1.61x |
| 95–100% | 4,775 | 14.52% | 1.80x |
| 100%+ | 1,415 | 19.47% | 2.41x |

**Default rate by credit card utilisation:**

| CC Utilisation | Customers | Default Rate | vs Baseline |
|---|---|---|---|
| 0–30% | 4,848 | 6.23% | 0.77x |
| 30–50% | 2,714 | 7.49% | 0.93x |
| 50–70% | 3,216 | 7.89% | 0.98x |
| 70–85% | 3,503 | 9.17% | 1.14x |
| 85–95% | 6,115 | 12.66% | 1.57x |
| 95–100% | 7,039 | 15.54% | 1.93x |
| 100%+ | 8,533 | 20.55% | 2.55x |

**Default rate by debt-to-annual-income:**

| DTI | Customers | Default Rate | vs Baseline |
|---|---|---|---|
| 0–20% | 87,264 | 7.98% | 0.99x |
| 20–40% | 38,569 | 8.93% | 1.11x |
| 40–60% | 21,790 | 9.24% | 1.14x |
| 60–80% | 12,836 | 9.45% | 1.17x |
| 80–100% | 7,614 | 9.93% | 1.23x |
| 100%+ | 17,073 | 9.01% | 1.12x |

**Payment-to-income ratio:**

| Metric | P25 | P50 | P75 | P90 | P95 |
|---|---|---|---|---|---|
| PTI | 11.5% | 16.3% | 22.9% | 30.2% | 35.5% |

### Severity Thresholds

| Severity | Score Range | Trigger Conditions | Observed Default | vs Baseline | Rationale |
|---|---|---|---|---|---|
| **NONE** | 0–10 | Utilisation < 30% AND active ≤ 2 | 6.40–7.44% | 0.79–0.92x | Below baseline. Low utilisation and few active loans are protective factors. |
| **LOW** | 10–30 | Utilisation 30–50% OR active = 3 | 8.19–8.26% | 1.01–1.02x | At baseline. Exposure is within normal range but warrants tracking. |
| **MEDIUM** | 30–55 | Utilisation 50–70% OR active = 4 | 9.23–10.31% | 1.14–1.28x | Meaningful elevation. Utilisation crosses the 50% threshold where default breaks above 10% for the first time. |
| **HIGH** | 55–80 | Utilisation 70–85% OR active = 5 OR CC util > 85% | 10.66–13.01% | 1.32–1.61x | Serious exposure. Credit card utilisation > 85% shows a sharp inflection — default jumps from 9.17% to 12.66% (+3.5pp in one band). |
| **CRITICAL** | 80–100 | Utilisation > 85% OR active ≥ 6 OR CC util > 95% | 13.38–20.55% | 1.66–2.55x | Extreme risk. CC utilisation > 95% defaults at 15.5–20.6%, the highest observed rates in any indicator. Urgent intervention. |

### Business Rule

Credit utilisation is the strongest single exposure indicator in the dataset. The risk curve is non-linear: default rate is nearly flat from 0–30% utilisation (6.4%), then accelerates sharply above 70% (12.2%), reaching 19.5% at 100%+. This is a 3x increase.

Credit card overutilisation is the most dangerous signal: at > 95% CC utilisation, one in five customers defaults (20.6%). This exceeds even the riskiest repayment delay segment.

Active loan count provides a compounding signal: each additional active loan beyond 3 adds approximately 1 percentage point to default probability (8.26% at 3 loans → 9.23% at 4 → 10.66% at 5 → 13.38% at 6+).

Debt-to-income ratio is a weaker predictor than utilisation in this dataset (max 9.93% at 80–100% DTI vs 19.47% at 100%+ utilisation), but still contributes as a secondary signal for affordability stress.

---

## Rule 4: Missing or Incomplete Customer History

**Purpose:** Identify customers with insufficient data for risk assessment. Distinguish between genuinely new customers and data gaps. **Missing data must never be treated as high risk.**

### Data Findings

**Data availability across tables:**

| Data Source | Coverage | Missing |
|---|---|---|
| Instalment history | 94.8% (291,643) | 5.2% (15,868) |
| Bureau history | 85.7% (263,491) | 14.3% (44,020) |
| Credit card data | 28.3% (86,905) | 71.7% (220,606) |
| POS cash data | 94.1% (289,444) | 5.9% (18,067) |

**Default rate by data availability:**

| Data Present | Customers | Default Rate | vs Baseline |
|---|---|---|---|
| No instalment history | 15,868 | **5.98%** | **0.74x** |
| No bureau history | 44,020 | 10.12% | 1.25x |
| Both inst + bureau | 250,003 | 7.84% | 0.97x |
| Neither | 2,380 | 7.61% | 0.94x |

**Default rate by instalment record count:**

| Records | Customers | Default Rate | vs Baseline |
|---|---|---|---|
| 1–3 | 3,792 | 8.25% | 1.02x |
| 4–6 | 23,049 | 8.27% | 1.02x |
| 7–12 | 51,079 | 9.15% | 1.13x |
| 13–24 | 66,422 | 8.61% | 1.07x |
| 24+ | 147,301 | 7.65% | 0.95x |

**Key column missing rates:**

| Column | Missing Rate | Impact |
|---|---|---|
| AMT_INCOME_TOTAL | 0.00% | Always available |
| AMT_CREDIT | 0.00% | Always available |
| AMT_ANNUITY | 0.00% | Always available |
| EXT_SOURCE_1 | 56.38% | Frequently missing — do not rely on |
| EXT_SOURCE_3 | 19.83% | Sometimes missing |
| OCCUPATION_TYPE | 31.35% | Often missing |
| OWN_CAR_AGE | 65.99% | Frequently missing — do not rely on |

### Completeness Thresholds

| Completeness | Score Range | Trigger Conditions | Can Assess Risk? | Rationale |
|---|---|---|---|---|
| **COMPLETE** | ≥ 75 | Has instalment + bureau + ≥ 12 records + income data | Yes | Full history for robust assessment. Both primary data sources present with sufficient depth. |
| **ADEQUATE** | 50–74 | Has instalment + ≥ 6 records | Yes | Enough records for pattern detection (6 months of monthly instalments). May lack bureau depth. |
| **PARTIAL** | 25–49 | < 6 records OR missing bureau data | Limited | Assessments may be unreliable. Bureau-absent customers default at 10.12% (1.25x baseline), but this may reflect the customer segment rather than data quality. Flag for data collection. |
| **INSUFFICIENT** | < 25 | < 3 records or no instalment history | No | Cannot make a risk judgement. System must output "insufficient data" rather than a risk score. |

### Critical Business Rule

**Missing data does not equal high risk.** The data conclusively demonstrates this:

- Customers **without** instalment history default at **5.98%** — 26% **below** baseline
- Customers with **both** instalment and bureau data default at **7.84%** — 3% below baseline
- Customers with **24+ records** default at **7.65%** — the lowest of any record-count band

Customers without instalment history are likely **new customers** who have not yet had time to default. Flagging them as high risk would be a systematic false positive affecting 15,868 customers.

The correct response to missing data is:
1. Classify as "insufficient data" (not "high risk")
2. Recommend data collection (request bureau report, wait for instalment history to accumulate)
3. Apply conservative exposure limits until sufficient data is available
4. Never invent risk indicators from absent data

The one exception: customers **without bureau history** default at 10.12% (1.25x baseline). This is a genuine signal — bureau-absent customers may have limited formal credit history, which is itself a risk factor. However, this should be captured by the exposure rule (Rule 3), not the missing data rule.

---

## Threshold Summary

| Rule | Indicator | NONE | LOW | MEDIUM | HIGH | CRITICAL |
|---|---|---|---|---|---|---|
| 1 — Delays | Late ratio | < 5% | 5–10% | 10–20% | 20–30% | > 30% |
| 1 — Delays | Max DPD | 0 | 1–15 | 16–60 | 61–90 | > 90 |
| 2 — Misses | Streak + Recovery | streak 0 | streak 1, rec ≥ 80% | streak 2 | streak ≥ 3 | streak ≥ 3, rec < 40% |
| 3 — Exposure | Utilisation | < 30% | 30–50% | 50–70% | 70–85% | > 85% |
| 3 — Exposure | Active loans | ≤ 2 | 3 | 4 | 5 | ≥ 6 |
| 3 — Exposure | CC utilisation | < 30% | 30–50% | 50–85% | 85–95% | > 95% |
| 4 — Data | Completeness | ≥ 75 | 50–74 | 25–49 | — | < 25 (insufficient) |

---

## Methodology Notes

1. **Baseline comparison:** All thresholds are anchored to the population default rate of 8.07%. A severity level is assigned based on how far the observed default rate for that segment deviates from baseline (expressed as a multiplier: 1.0x = same as baseline, 2.0x = double).

2. **Multiplier bands used:**
   - < 1.0x baseline → NONE (protective factor)
   - 1.0–1.15x → LOW (noise range)
   - 1.15–1.35x → MEDIUM (meaningful signal)
   - 1.35–1.65x → HIGH (clear escalation)
   - \> 1.65x → CRITICAL (urgent)

3. **Inflection points:** Thresholds are placed at points where the default rate curve shows the steepest increase between adjacent bands, not at arbitrary round numbers. For example, the HIGH threshold for CC utilisation is set at 85% because that is where default jumps from 9.17% to 12.66% (+3.5pp) — the sharpest single-band increase in the credit card data.

4. **Score composition:** The 0–100 warning score for each rule is a weighted composite of multiple sub-indicators within that rule. The weights are calibrated so that the score thresholds (10, 30, 55, 80) correspond to the default-rate multiplier bands above.

5. **Dataset limitations:** The Home Credit dataset captures loan applications and outcomes, not real-time monitoring. The analysis assumes that patterns observable at application time (late ratio, utilisation, etc.) are representative of patterns that would be observable in an ongoing monitoring system. Cash flow trend data is not present in this dataset and is handled by a separate ML model (Model 1).
