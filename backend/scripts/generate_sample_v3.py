"""Generate backend/sample_data_v3: Level 2 test portfolio (made-up data).

30 customers (T3-001 .. T3-030), 12 months (Nov 2025 - Oct 2026), with a monthly
exposure snapshot per facility so risk trends, exposure build-up and correlation are
observable over time. Deterministic: re-running produces identical files.

    python backend/scripts/generate_sample_v3.py
"""

from __future__ import annotations

import csv
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "sample_data_v3"
MONTHS = 12
FIRST = date(2025, 11, 1)
DATA_END = date(2026, 10, 31)
REVOLVING = {"credit_card", "overdraft"}


def month_start(i: int) -> date:  # i = 0..11
    y, m = FIRST.year + (FIRST.month - 1 + i) // 12, (FIRST.month - 1 + i) % 12 + 1
    return date(y, m, 1)


def month_end(i: int) -> date:
    return month_start(i + 1) - timedelta(days=1) if i < MONTHS - 1 else DATA_END


@dataclass
class Loan:
    loan_id: str
    product: str
    sanctioned: float
    outstanding_end: float  # at the last month
    instalment: float
    tenure: int
    disbursed: date
    # days past due per month (len 12; None = not paid by data end; -1 = no instalment)
    dpd: list = field(default_factory=lambda: [0] * MONTHS)
    paid_share: list = field(default_factory=lambda: [1.0] * MONTHS)
    due_day: int = 5


@dataclass
class Revolving:
    ftype: str
    limit: float
    util: list  # per month
    disbursed: date


@dataclass
class Cust:
    cid: str
    ctype: str
    employment: str
    portfolio: str
    loans: list
    revolving: list
    inflow: list  # per month (len <= 12, aligned to the last months)
    scenario: str
    expect: str


def ramp(start: float, end: float, n: int = MONTHS) -> list:
    return [start + (end - start) * i / (n - 1) for i in range(n)]


def flat_then(base: float, from_month: int, end: float) -> list:
    """Flat at `base`, then a linear move to `end` from month index `from_month`."""
    out = []
    for i in range(MONTHS):
        if i < from_month:
            out.append(base)
        else:
            out.append(base + (end - base) * (i - from_month + 1) / (MONTHS - from_month))
    return out


def d(y, m, dd=1) -> date:
    return date(y, m, dd)


def featured() -> list[Cust]:
    Z = [0] * MONTHS
    cs = [
        Cust("T3-001", "individual", "salaried", "retail",
             [Loan("L3-001", "home_loan", 5_000_000, 4_150_000, 42_000, 240, d(2021, 4))],
             [Revolving("credit_card", 300_000, [0.15] * MONTHS, d(2022, 1))],
             [220_000 + (i % 3) * 1500 for i in range(MONTHS)],
             "Stable prime: home loan, always on time, low card use", "Low, stable; large exposure -> protect exposure"),
        Cust("T3-002", "individual", "salaried", "retail",
             [Loan("L3-002", "personal_loan", 600_000, 380_000, 19_500, 36, d(2024, 9),
                   dpd=[12, 25, 38, 45, 30, 0, 0, 0, 0, 0, 0, 0])],
             [Revolving("credit_card", 150_000, ramp(0.82, 0.38), d(2023, 3))],
             [62_000, 58_000, 55_000, 57_000, 63_000, 70_000, 74_000, 76_000, 76_000, 77_000, 78_000, 78_000],
             "Recovering: 30-45 DPD early in the year, on time for the last 7 months, income back up",
             "Trend improving"),
        Cust("T3-003", "individual", "self_employed", "retail",
             [Loan("L3-003", "personal_loan", 1_200_000, 820_000, 35_000, 48, d(2024, 2),
                   dpd=[0, 0, 0, 0, 0, 0, 0, 0, 3, 0, 4, 5])],
             [Revolving("credit_card", 500_000, flat_then(0.40, 6, 0.82), d(2022, 6))],
             flat_then(300_000, 6, 228_000),
             "Multi-signal, large exposure: income -24%, card use 40% -> 82%, 3 short delays (3-5 days) in 3 months",
             "Each signal moderate; correlated escalation; ranks above T3-004"),
        Cust("T3-004", "individual", "self_employed", "retail",
             [Loan("L3-004", "personal_loan", 90_000, 58_000, 4_200, 24, d(2025, 4),
                   dpd=[0, 0, 0, 0, 0, 0, 0, 0, 3, 0, 4, 5])],
             [Revolving("credit_card", 40_000, flat_then(0.40, 6, 0.82), d(2023, 1))],
             flat_then(42_000, 6, 32_000),
             "Same signals as T3-003 on a small exposure", "Same warning pattern, lower priority (small exposure)"),
        Cust("T3-005", "individual", "salaried", "retail",
             [Loan("L3-005", "personal_loan", 150_000, 96_000, 6_800, 24, d(2025, 3),
                   dpd=[0] * 11 + [6])],
             [],
             [48_000] * MONTHS,
             "Isolated delay: one instalment 6 days late in October, otherwise clean",
             "Temporary missed instalment -> monitor recovery and verify payment status"),
        Cust("T3-006", "individual", "salaried", "retail",
             [Loan("L3-006", "home_loan", 3_800_000, 3_300_000, 32_000, 240, d(2022, 8),
                   dpd=[0] * 9 + [31, 0, 0], paid_share=[1.0] * MONTHS),
              Loan("L3-006B", "auto_loan", 900_000, 520_000, 18_500, 60, d(2023, 11))],
             [Revolving("credit_card", 200_000, [0.22] * MONTHS, d(2021, 5))],
             [185_000] * MONTHS,
             "Processing error: one home-loan instalment recorded 31 days late (auto-debit mandate failure)",
             "Analyst reclassifies the event as a processing error; score is recalculated and drops"),
        Cust("T3-007", "sme", "business", "sme_finance",
             [Loan("L3-007", "business_loan", 8_000_000, 6_900_000, 190_000, 84, d(2023, 1),
                   dpd=[0, 0, 0, 0, 0, 0, 15, 35, 64, None, None, None],
                   paid_share=[1, 1, 1, 1, 1, 1, 1, 0.6, 0.4, 0, 0, 0])],
             [Revolving("overdraft", 2_000_000, flat_then(0.60, 5, 1.0), d(2022, 4))],
             flat_then(2_400_000, 5, 1_300_000),
             "Sustained deterioration: SME misses 3 instalments in a row after rising delays, overdraft maxed, inflow -46%",
             "Critical, 90+ DPD -> escalate to collections (needs approval) + restructuring review"),
        Cust("T3-008", "individual", "salaried", "retail",
             [Loan("L3-008", "personal_loan", 500_000, 310_000, 16_000, 36, d(2024, 6)),
              Loan("L3-008B", "personal_loan", 400_000, 395_000, 13_500, 36, d(2026, 8, 10)),
              Loan("L3-008C", "auto_loan", 700_000, 700_000, 15_500, 60, d(2026, 9, 18))],
             [Revolving("credit_card", 250_000, flat_then(0.55, 6, 0.97), d(2022, 2)),
              Revolving("overdraft", 150_000, flat_then(0.20, 7, 0.88), d(2023, 7))],
             [95_000] * MONTHS,
             "Exposure build-up: 2 new loans in 3 months, card 55% -> 97%, overdraft 20% -> 88%, never late",
             "Pre-delinquent warning -> review affordability"),
        Cust("T3-009", "sme", "trading", "sme_finance",
             [Loan("L3-009", "business_loan", 4_000_000, 3_100_000, 98_000, 60, d(2023, 6))],
             [Revolving("overdraft", 1_000_000, [0.45] * MONTHS, d(2023, 6))],
             flat_then(1_500_000, 3, 880_000),
             "SME cash-flow decline: inflow -41% over 9 months, every instalment on time",
             "Pre-delinquent cash-flow warning -> financial health review"),
        Cust("T3-010", "individual", "salaried", "retail",
             [Loan("L3-010", "personal_loan", 250_000, 240_000, 11_000, 24, d(2026, 8, 12),
                   dpd=[-1] * 10 + [0, 0])],
             [],
             [60_000, 61_000],
             "Thin file: new customer, 2 instalments, 2 months of cash flow, no exposure snapshot",
             "Insufficient Data -> request additional evidence"),
        Cust("T3-011", "individual", "salaried", "retail",
             [Loan("L3-011", "personal_loan", 900_000, 640_000, 30_000, 48, d(2024, 4))],
             [Revolving("credit_card", 200_000, [0.30] * MONTHS, d(2022, 9))],
             flat_then(120_000, 7, 62_000),
             "Income-driven debt-to-income: income -48%, balances unchanged, no delays",
             "DTI rise counted once (cash-flow), not again as exposure"),
        Cust("T3-012", "individual", "self_employed", "retail",
             [Loan("L3-012", "business_loan", 1_500_000, 1_050_000, 48_000, 48, d(2023, 9),
                   dpd=[32] * MONTHS)],
             [],
             [190_000 + (i % 2) * 4000 for i in range(MONTHS)],
             "Chronic but stable: every instalment ~32 days late for 12 months",
             "Already delinquent, trend stable"),
        Cust("T3-013", "individual", "salaried", "retail",
             [Loan("L3-013", "auto_loan", 700_000, 450_000, 16_500, 60, d(2024, 1),
                   dpd=[0] * 9 + [8, 20, None])],
             [Revolving("credit_card", 120_000, flat_then(0.35, 8, 0.75), d(2023, 2))],
             [88_000] * 8 + [62_000, 60_000, 59_000, 58_000],
             "Newly delinquent: clean for 9 months, income -33% after a job change, then 8, 20 DPD and an unpaid October instalment",
             "Emerging delinquency, deteriorating"),
        Cust("T3-014", "sme", "manufacturing", "sme_finance",
             [Loan("L3-014", "business_loan", 18_000_000, 14_800_000, 400_000, 72, d(2022, 11))],
             [Revolving("overdraft", 5_000_000, flat_then(0.55, 7, 0.80), d(2022, 11))],
             [2_500_000 + (i % 3) * 20_000 for i in range(MONTHS)],
             "Large SME, one mild signal: overdraft use 55% -> 80%, always on time",
             "Lower probability, very large exposure -> protect exposure"),
        Cust("T3-015", "individual", "salaried", "retail",
             [Loan("L3-015", "gold_loan", 200_000, 160_000, 9_500, 24, d(2025, 6),
                   dpd=[0, 7, 0, 8, 0, 9, 0, 6, 0, 8, 0, 7])],
             [],
             [41_000] * MONTHS,
             "Chronic mild lateness: 6-9 days late every other month, always pays in full",
             "Medium, stable -> contact customer"),
        Cust("T3-016", "individual", "professional", "premier",
             [Loan("L3-016", "home_loan", 9_000_000, 7_600_000, 78_000, 240, d(2020, 10))],
             [Revolving("credit_card", 1_000_000, [0.90] * MONTHS, d(2019, 4))],
             [650_000] * MONTHS,
             "Premier client, card used at 90% for the whole year, always on time",
             "Exposure elevated but stable"),
    ]
    return cs


PRODUCTS = [
    ("personal_loan", 200_000, 1_000_000, 36),
    ("home_loan", 2_000_000, 7_000_000, 240),
    ("auto_loan", 400_000, 1_200_000, 60),
    ("business_loan", 1_000_000, 6_000_000, 60),
    ("gold_loan", 100_000, 400_000, 24),
]
PROFILES = [
    ("individual", "salaried", "retail"),
    ("individual", "self_employed", "retail"),
    ("individual", "professional", "premier"),
    ("sme", "trading", "sme_finance"),
    ("sme", "services", "sme_finance"),
]


def background(rng: random.Random) -> list[Cust]:
    out = []
    for n in range(17, 31):
        cid = f"T3-{n:03d}"
        ctype, emp, port = PROFILES[(n * 7) % len(PROFILES)]
        prod, lo, hi, tenure = PRODUCTS[(n * 3) % len(PRODUCTS)]
        if ctype == "sme":
            prod, lo, hi, tenure = PRODUCTS[3]
        sanctioned = round(rng.uniform(lo, hi), -4)
        inst = round(sanctioned / tenure * 1.35, -2)
        outstanding = round(sanctioned * rng.uniform(0.45, 0.85), -3)
        roll = rng.random()
        dpd = [0] * MONTHS
        inflow_base = inst * rng.uniform(4.5, 8.0)
        inflow = [round(inflow_base * rng.uniform(0.97, 1.03), -2) for _ in range(MONTHS)]
        label = "Background: clean payer"
        if roll < 0.20:
            for m in rng.sample(range(MONTHS), 2):
                dpd[m] = rng.randint(2, 9)
            label = "Background: two small delays"
        elif roll < 0.30:
            inflow = [round(v * (1 - 0.025 * max(0, i - 5)), -2) for i, v in enumerate(inflow)]
            label = "Background: mild income dip"
        rev = []
        if rng.random() < 0.5:
            rev.append(Revolving("credit_card", round(rng.uniform(50_000, 400_000), -4),
                                 [round(rng.uniform(0.1, 0.5), 2)] * MONTHS, d(2022, rng.randint(1, 12))))
        out.append(Cust(
            cid, ctype, emp, port,
            [Loan(f"L3-{n:03d}", prod, sanctioned, outstanding, inst, tenure,
                  d(rng.randint(2021, 2024), rng.randint(1, 12)), dpd=dpd)],
            rev, inflow, label, "Low / Medium background",
        ))
    return out


def write(customers: list[Cust]) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    rows = {k: [] for k in ("customers", "loans", "repayments", "exposure", "cashflow")}
    for c in customers:
        rows["customers"].append([c.cid, c.ctype, c.employment, c.portfolio])
        for ln in c.loans:
            rows["loans"].append([c.cid, ln.loan_id, ln.product, int(ln.sanctioned), int(ln.outstanding_end),
                                  ln.tenure, int(ln.instalment), ln.disbursed.isoformat(), "active"])
            for i in range(MONTHS):
                due = month_start(i).replace(day=ln.due_day)
                if due <= ln.disbursed or ln.dpd[i] == -1:
                    continue
                dpd = ln.dpd[i]
                share = ln.paid_share[i]
                if dpd is None:  # unpaid at data end
                    rows["repayments"].append([c.cid, ln.loan_id, due.isoformat(), "", int(ln.instalment), 0,
                                               (DATA_END - due).days, "missed"])
                    continue
                paid = due + timedelta(days=dpd)
                if paid > DATA_END:  # not paid yet when the data was extracted
                    rows["repayments"].append([c.cid, ln.loan_id, due.isoformat(), "", int(ln.instalment), 0,
                                               (DATA_END - due).days, "missed"])
                    continue
                amount = round(ln.instalment * share)
                status = "partial" if share < 1 else ("late" if dpd > 0 else "on_time")
                rows["repayments"].append([c.cid, ln.loan_id, due.isoformat(), paid.isoformat(),
                                           int(ln.instalment), amount, dpd, status])
        # Monthly exposure snapshots
        for i in range(MONTHS):
            snap = month_end(i)
            for ln in c.loans:
                if ln.disbursed > snap:
                    continue
                # Balance amortises ~60% of the instalment a month; new loans start at sanction.
                months_left = MONTHS - 1 - i
                bal = min(ln.sanctioned, ln.outstanding_end + 0.6 * ln.instalment * months_left)
                rows["exposure"].append([c.cid, ln.product, int(ln.sanctioned), 0, round(bal / ln.sanctioned, 4),
                                         int(bal), int(ln.instalment), ln.disbursed.isoformat(), snap.isoformat()])
            for rv in c.revolving:
                if rv.disbursed > snap:
                    continue
                u = rv.util[i]
                rows["exposure"].append([c.cid, rv.ftype, int(rv.limit), int(rv.limit), round(u, 4),
                                         int(rv.limit * u), int(rv.limit * u * 0.05), rv.disbursed.isoformat(),
                                         snap.isoformat()])
        offset = MONTHS - len(c.inflow)
        for j, v in enumerate(c.inflow):
            i = offset + j
            rows["cashflow"].append([c.cid, j + 1, int(v), int(v * 0.72), month_end(i).isoformat()])

    headers = {
        "customers": ["customer_id", "customer_type", "employment_category", "portfolio_segment"],
        "loans": ["customer_id", "loan_id", "product_type", "sanctioned_amount", "outstanding_balance",
                  "tenure_months", "instalment_amount", "disbursement_date", "status"],
        "repayments": ["customer_id", "loan_id", "due_date", "payment_date", "amount_due", "amount_paid", "dpd",
                       "repayment_status"],
        "exposure": ["customer_id", "facility_type", "sanctioned_amount", "credit_limit", "current_utilisation",
                     "outstanding_balance", "instalment_amount", "disbursement_date", "snapshot_date"],
        "cashflow": ["customer_id", "month", "monthly_inflow", "monthly_outflow", "record_date"],
    }
    for name, data in rows.items():
        with open(OUT / f"{name}.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(headers[name])
            w.writerows(data)
    return {k: len(v) for k, v in rows.items()}


def main() -> None:
    rng = random.Random(20261031)
    customers = featured() + background(rng)
    counts = write(customers)
    with open(OUT / "scenarios.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["customer_id", "scenario", "expected"])
        for c in customers:
            w.writerow([c.cid, c.scenario, c.expect])
    print(counts)


if __name__ == "__main__":
    main()
