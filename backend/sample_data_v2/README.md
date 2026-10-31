# Sample data v2: 8 test scenarios

Made-up test data, different from `sample_data/`. Customer IDs `T2-001` to `T2-008`,
12 months of history (Sep 2025 to Aug 2026).

Upload on the **Upload** page in this order: `customers.csv`, `loans.csv`,
`repayments.csv`, `exposure.csv`, `cashflow.csv`. Then click **Run Assessment** on the Dashboard.
Re-uploading updates the same records; it does not duplicate them.

| File | Rows |
|---|---|
| customers.csv | 8 |
| loans.csv | 9 |
| repayments.csv | 98 |
| exposure.csv | 14 |
| cashflow.csv | 86 |

| Customer | Scenario | What it tests | Result when checked |
|---|---|---|---|
| T2-001 | Steady salaried, home loan, always on time, low card use | Clean customer | Low (9.5) |
| T2-002 | One payment 4 days late in December, all others on time | One-off delay is not escalated | Low (10.3) |
| T2-003 | On time for 6 months, then later each month (5 to 62 days), partial payments, income falling 47% | Deterioration on every signal | Critical (98.0) |
| T2-004 | Never late, but 2 new loans in 3 months, card 98% and overdraft 97% used | Exposure risk without missed payments | High (68.9) |
| T2-005 | Small delays (6 to 10 days) in 8 of 12 months, always pays in full | Chronic but mild lateness | Medium (29.1) |
| T2-006 | SME misses 3 instalments in a row, then 8 on time | Sustained miss followed by recovery | Medium (40.5) |
| T2-007 | New customer, 2 payments, 2 months of cash flow | Thin file | Insufficient Data |
| T2-008 | SME pays on time, but income falls 50% over 12 months | Cash-flow warning before any missed payment | Medium (32.9), cash flow 94.8 |

The results come from a check against the current models. Scores move if the models are retrained.
