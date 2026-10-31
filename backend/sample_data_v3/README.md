# Sample data v3: Level 2 test portfolio

Made-up data for Level 2 (risk trends, correlation, prioritisation, interventions).
30 customers `T3-001` to `T3-030`, 12 months (Nov 2025 to Oct 2026), with a **monthly exposure
snapshot per facility** so balances and utilisation can be followed over time.
Regenerate with `python backend/scripts/generate_sample_v3.py` (deterministic).

Upload on the **Upload** page in this order: `customers.csv`, `loans.csv`, `repayments.csv`,
`exposure.csv`, `cashflow.csv`. Then click **Run assessment** on the **Portfolio Monitor**: it scores
everyone and replays the pipeline at each month end to build the risk history.

| File | Rows |
|---|---|
| customers.csv | 30 |
| loans.csv | 33 |
| repayments.csv | 365 |
| exposure.csv | 584 |
| cashflow.csv | 350 |

`scenarios.csv` lists what each customer is meant to test. T3-001 to T3-016 are the featured
scenarios; T3-017 to T3-030 are background customers (mostly clean, a few small delays or income dips).

| Customer | Scenario | Tests |
|---|---|---|
| T3-001 | Stable prime home loan, always on time | Low and stable; large exposure lands in "protect exposure" |
| T3-002 | 30-45 DPD early in the year, on time for the last 7 months | Trend **improving** |
| T3-003 | Income -24%, card use 40% → 82%, three 3-5 day delays | Moderate signals that **correlate**: warning level raised; ranks above T3-004 |
| T3-004 | Same signals as T3-003 on a ₹90k loan | Same probability, **lower priority** because the exposure is small |
| T3-005 | One instalment 6 days late in October | **Temporary missed instalment** → monitor and verify |
| T3-006 | One home-loan instalment recorded 31 days late (auto-debit failure) | Classify as **processing error** in the workspace → score recalculated (39.5 → ~13) |
| T3-007 | SME: rising delays, then 3 unpaid instalments, overdraft maxed, inflow -46% | **Sustained deterioration** → escalate to collections (**needs approval**) + restructuring review |
| T3-008 | 2 new loans in 3 months, card 97%, overdraft 88%, never late | **Increasing exposure** before delinquency → affordability review |
| T3-009 | SME inflow -41%, every instalment on time | **Declining cash flow** before delinquency → financial health review |
| T3-010 | 2 instalments, 2 months of cash flow, no exposure | **Insufficient information** → request evidence |
| T3-011 | Income -48%, balances unchanged, no delays | **No double counting**: the debt-to-income rise is the same event as the income fall |
| T3-012 | Every instalment ~32 days late for 12 months | **Already delinquent** but stable |
| T3-013 | Clean 9 months, job change, then 8 / 20 DPD and an unpaid October instalment | **Emerging delinquency** |
| T3-014 | ₹1.8 Cr SME loan, overdraft use rising, always on time | Low probability, very large exposure |
| T3-015 | 6-9 days late every other month, always pays in full | Chronic mild lateness → contact customer |
| T3-016 | Premier client, card at 90% all year, on time | Exposure elevated but stable |

## Results

Trend, probability, tiers and recommendations come from the deterioration model
(`core/models/deterioration_model.py`, trained on Home Credit), so they reflect what that data
supports, not the scenario labels above. Some scenarios land differently than designed
(e.g. the card build-up in T3-008 gets a low probability: card utilisation change is a weak
signal in Home Credit). Check the live values on the Priority Queue after running an assessment.
