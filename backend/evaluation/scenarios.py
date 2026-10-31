"""Independent, labelled credit-deterioration scenarios.

Each scenario is a synthetic customer whose repayment, cash-flow and exposure history
was written to exhibit one behaviour. The expected labels are set by scenario design
(what a credit analyst would conclude), not by running the scoring code, so the
benchmark measures the system rather than restating it.

All histories end on AS_OF so staleness is measured consistently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

AS_OF = date(2026, 9, 1)
DETERIORATING = {"emerging_deterioration", "sustained_deterioration", "accelerating_deterioration"}


def months_back(n: int, end: date = AS_OF) -> list[date]:
    out = []
    year, month = end.year, end.month
    for _ in range(n):
        out.append(date(year, month, 1))
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return list(reversed(out))


@dataclass
class Scenario:
    key: str
    description: str
    # One entry per month: (status, dpd, paid_fraction)
    repayments: list[tuple[str, int, float]]
    inflows: list[float]
    outflows: list[float | None] | None = None
    # One entry per exposure snapshot (oldest first): (utilisation, credit_limit)
    exposure: list[tuple[float, float]] = field(default_factory=list)
    instalment: float = 20_000.0
    segment: str = "retail"
    end: date = AS_OF
    duplicate_months: tuple[int, ...] = ()
    contradictions: tuple[tuple[int, str], ...] = ()
    # Expected labels
    expected: set[str] = field(default_factory=set)
    expected_signals: dict[str, bool] = field(default_factory=dict)
    must_select: set[str] = field(default_factory=set)
    must_not_select: set[str] = field(default_factory=set)
    expect_data_issue: bool = False
    expect_human_review: bool | None = None

    @property
    def deteriorating(self) -> bool:
        return bool(self.expected & DETERIORATING)


def _on_time(n: int) -> list[tuple[str, int, float]]:
    return [("on_time", 0, 1.0)] * n


def _flat(n: int, level: float = 100_000, wobble: float = 0.02) -> list[float]:
    return [level * (1 + wobble * ((i % 3) - 1)) for i in range(n)]


STABLE_EXPOSURE = [(0.40, 1_000_000)] * 4

SCENARIOS: list[Scenario] = [
    Scenario(
        "normal_repayment",
        "Twelve on-time payments, stable inflows and utilisation",
        _on_time(12),
        _flat(12),
        exposure=STABLE_EXPOSURE,
        expected={"low_risk"},
        expected_signals={"repayment": False, "cashflow": False, "exposure": False},
        must_select={"repayment_specialist"},
        must_not_select={"baseline_specialist", "integrity_specialist"},
        expect_human_review=False,
    ),
    Scenario(
        "isolated_late_payment",
        "One six-day delay eight months ago, otherwise clean",
        _on_time(4) + [("late", 6, 1.0)] + _on_time(7),
        _flat(12),
        exposure=STABLE_EXPOSURE,
        expected={"low_risk", "recovery"},
        expected_signals={"repayment": False, "cashflow": False, "exposure": False},
        must_select={"repayment_specialist"},
        expect_human_review=False,
    ),
    Scenario(
        "repeated_delays",
        "Four consecutive late payments after eight clean months",
        _on_time(8) + [("late", 12, 1.0), ("late", 15, 1.0), ("late", 14, 1.0), ("late", 16, 1.0)],
        _flat(12),
        exposure=STABLE_EXPOSURE,
        expected={"sustained_deterioration", "accelerating_deterioration"},
        expected_signals={"repayment": True, "cashflow": False},
        must_select={"repayment_specialist", "baseline_specialist"},
        expect_human_review=True,
    ),
    Scenario(
        "partial_repayments",
        "Three of the last four instalments only partly paid",
        _on_time(8)
        + [("partial", 0, 0.5), ("on_time", 0, 1.0), ("partial", 3, 0.6), ("partial", 5, 0.5)],
        _flat(12),
        exposure=STABLE_EXPOSURE,
        expected={"emerging_deterioration", "sustained_deterioration"},
        expected_signals={"repayment": True},
        must_select={"repayment_specialist"},
        expect_human_review=True,
    ),
    Scenario(
        "declining_inflows",
        "Payments on time but inflows down about 30% for four months",
        _on_time(12),
        _flat(8) + [82_000, 76_000, 72_000, 70_000],
        exposure=STABLE_EXPOSURE,
        expected=DETERIORATING,
        expected_signals={"repayment": False, "cashflow": True},
        must_select={"cashflow_specialist"},
        expect_human_review=True,
    ),
    Scenario(
        "increasing_utilisation",
        "Utilisation rising from 50% to 93% with stable payments and inflows",
        _on_time(12),
        _flat(12),
        exposure=[(0.50, 1_000_000), (0.66, 1_000_000), (0.81, 1_000_000), (0.93, 1_000_000)],
        instalment=45_000,
        expected={"emerging_deterioration"},
        expected_signals={"repayment": False, "cashflow": False, "exposure": True},
        must_select={"exposure_specialist"},
        expect_human_review=True,
    ),
    Scenario(
        "combined_deterioration",
        "Late payments together with falling inflows",
        _on_time(9) + [("late", 10, 1.0), ("late", 14, 1.0), ("missed", 31, 0.0)],
        _flat(9) + [84_000, 78_000, 74_000],
        exposure=STABLE_EXPOSURE,
        expected={"sustained_deterioration", "accelerating_deterioration"},
        expected_signals={"repayment": True, "cashflow": True},
        must_select={"repayment_specialist", "cashflow_specialist"},
        expect_human_review=True,
    ),
    Scenario(
        "temporary_disruption_recovered",
        "Three late months mid-year, then five clean months and recovered inflows",
        _on_time(4) + [("late", 10, 1.0), ("late", 18, 1.0), ("late", 12, 1.0)] + _on_time(5),
        _flat(4) + [80_000, 78_000, 85_000] + _flat(5),
        exposure=STABLE_EXPOSURE,
        expected={"recovery", "low_risk"},
        expected_signals={"repayment": False, "cashflow": False},
        must_select={"repayment_specialist"},
    ),
    Scenario(
        "sustained_deterioration",
        "Six months of consistent 15-day delays and inflows 18% below baseline",
        _on_time(6) + [("late", 15, 1.0)] * 6,
        _flat(6) + [82_000] * 6,
        exposure=STABLE_EXPOSURE,
        expected={"sustained_deterioration"},
        expected_signals={"repayment": True, "cashflow": True},
        must_select={"repayment_specialist", "cashflow_specialist", "baseline_specialist"},
        expect_human_review=True,
    ),
    Scenario(
        "accelerating_deterioration",
        "Delays widening 5 -> 15 -> 30 -> 55 days while inflows fall faster each month",
        _on_time(8) + [("late", 5, 1.0), ("late", 15, 1.0), ("late", 30, 1.0), ("missed", 55, 0.0)],
        _flat(8) + [96_000, 88_000, 76_000, 60_000],
        exposure=[(0.55, 1_000_000), (0.62, 1_000_000), (0.74, 1_000_000), (0.86, 1_000_000)],
        expected={"accelerating_deterioration"},
        expected_signals={"repayment": True, "cashflow": True},
        must_select={"repayment_specialist", "cashflow_specialist"},
        expect_human_review=True,
    ),
    Scenario(
        "missing_history",
        "Established facility with a single repayment record and no cash-flow data",
        [("on_time", 0, 1.0)],
        [],
        exposure=[(0.45, 1_000_000)],
        expected={"insufficient_evidence"},
        must_select={"integrity_specialist"},
        must_not_select={"baseline_specialist"},
        expect_data_issue=True,
        expect_human_review=True,
    ),
    Scenario(
        "contradictory_records",
        "Records marked on-time but carrying 20+ DPD, and 'missed' but fully paid",
        _on_time(12),
        _flat(12),
        exposure=STABLE_EXPOSURE,
        contradictions=(
            (7, "on_time_dpd"),
            (8, "missed_paid"),
            (9, "on_time_dpd"),
            (10, "missed_paid"),
        ),
        expected={"low_risk", "emerging_deterioration"},
        must_select={"integrity_specialist"},
        expect_data_issue=True,
    ),
    Scenario(
        "duplicate_records",
        "Clean history where three months were loaded twice",
        _on_time(12),
        _flat(12),
        exposure=STABLE_EXPOSURE,
        duplicate_months=(3, 6, 9),
        expected={"low_risk"},
        expected_signals={"repayment": False},
        must_select={"integrity_specialist"},
        expect_data_issue=True,
    ),
    Scenario(
        "seasonal_behaviour",
        "Twenty-four months with the same autumn inflow dip each year; payments on time",
        _on_time(24),
        [100_000 * (0.72 if (i % 12) in (9, 10, 11) else 1.0) for i in range(24)],
        exposure=STABLE_EXPOSURE,
        expected={"low_risk"},
        expected_signals={"repayment": False, "cashflow": False},
        must_select={"cashflow_specialist"},
        expect_human_review=False,
    ),
    Scenario(
        "new_customer",
        "New customer with two months of history",
        _on_time(2),
        _flat(2),
        exposure=[(0.20, 500_000)],
        expected={"insufficient_evidence"},
        must_select={"integrity_specialist"},
        must_not_select={"repayment_specialist", "cashflow_specialist"},
        expect_data_issue=True,
        expect_human_review=True,
    ),
    Scenario(
        "stale_data",
        "Clean twelve-month history that stopped seven months before the as-of date",
        _on_time(12),
        _flat(12),
        exposure=STABLE_EXPOSURE,
        end=date(2026, 2, 1),
        expected={"low_risk"},
        must_select={"integrity_specialist"},
        expect_data_issue=True,
    ),
]

# Held-out variants, labelled by design before the system was first run on them and never
# used for tuning. They vary magnitude, history length, noise and signal mix.
HELD_OUT: list[Scenario] = [
    Scenario(
        "ho_volatile_but_healthy",
        "On-time payments with noisy (+/-8%) but flat inflows",
        _on_time(12),
        _flat(12, 90_000, 0.08),
        exposure=STABLE_EXPOSURE,
        expected={"low_risk"},
        expected_signals={"repayment": False, "cashflow": False},
    ),
    Scenario(
        "ho_two_isolated_delays",
        "Two short delays seven months apart, both followed by clean payments",
        _on_time(2) + [("late", 4, 1.0)] + _on_time(6) + [("late", 3, 1.0)] + _on_time(2),
        _flat(12),
        exposure=STABLE_EXPOSURE,
        expected={"low_risk", "recovery"},
        expected_signals={"repayment": False},
    ),
    Scenario(
        "ho_slow_inflow_erosion",
        "Inflows eroding about 3% a month for a year while payments stay current",
        _on_time(12),
        [100_000 * (0.97**i) for i in range(12)],
        exposure=STABLE_EXPOSURE,
        expected=DETERIORATING,
        expected_signals={"repayment": False, "cashflow": True},
    ),
    Scenario(
        "ho_creeping_utilisation",
        "Utilisation creeping from 70% to 88% with heavier obligations",
        _on_time(12),
        _flat(12),
        exposure=[(0.70, 2_000_000), (0.76, 2_000_000), (0.82, 2_000_000), (0.88, 2_000_000)],
        instalment=55_000,
        expected={"emerging_deterioration"},
        expected_signals={"repayment": False, "cashflow": False, "exposure": True},
    ),
    Scenario(
        "ho_moderate_sustained_delays",
        "Five consecutive 8-day delays with stable inflows",
        _on_time(7) + [("late", 8, 1.0)] * 5,
        _flat(12),
        exposure=STABLE_EXPOSURE,
        expected={"sustained_deterioration"},
        expected_signals={"repayment": True, "cashflow": False},
    ),
    Scenario(
        "ho_repayment_only_acceleration",
        "Delays widening 7 -> 20 -> 45 days; inflows unchanged",
        _on_time(9) + [("late", 7, 1.0), ("late", 20, 1.0), ("late", 45, 1.0)],
        _flat(12),
        exposure=STABLE_EXPOSURE,
        expected={"accelerating_deterioration"},
        expected_signals={"repayment": True, "cashflow": False},
    ),
    Scenario(
        "ho_recovered_cashflow_shock",
        "Inflow shock months 5-7 that fully recovered; payments always on time",
        _on_time(12),
        _flat(4) + [70_000, 65_000, 72_000] + _flat(5),
        exposure=STABLE_EXPOSURE,
        expected={"recovery", "low_risk"},
        expected_signals={"repayment": False, "cashflow": False},
    ),
    Scenario(
        "ho_short_history_with_misses",
        "Four months of history containing two missed payments",
        [("on_time", 0, 1.0), ("on_time", 0, 1.0), ("missed", 30, 0.0), ("missed", 60, 0.0)],
        _flat(4),
        exposure=[(0.5, 500_000)],
        expected={
            "emerging_deterioration",
            "sustained_deterioration",
            "accelerating_deterioration",
            "insufficient_evidence",
        },
        expected_signals={"repayment": True},
    ),
    Scenario(
        "ho_long_seasonal_business",
        "Thirty months with a recurring spring dip, latest quarter in the dip",
        _on_time(30),
        [100_000 * (0.75 if (i % 12) in (3, 4, 5) else 1.0) for i in range(30)],
        exposure=STABLE_EXPOSURE,
        end=date(2026, 9, 1),
        expected={"low_risk"},
        expected_signals={"repayment": False, "cashflow": False},
    ),
    Scenario(
        "ho_stale_deteriorating",
        "Deteriorating history whose last record is eight months old",
        _on_time(6) + [("late", 15, 1.0)] * 6,
        _flat(6) + [80_000] * 6,
        exposure=STABLE_EXPOSURE,
        end=date(2026, 1, 1),
        expected={"sustained_deterioration"},
        expected_signals={"repayment": True, "cashflow": True},
        expect_data_issue=True,
    ),
]

# The acceptance-scenario customer: deterioration with a material unresolved question.
ACCEPTANCE = Scenario(
    "acceptance_customer",
    "Repayment delays rising and inflows falling, with a first sign of improvement",
    _on_time(9) + [("late", 12, 1.0), ("partial", 18, 0.5), ("late", 25, 1.0)],
    [
        120_000,
        118_000,
        122_000,
        119_000,
        121_000,
        120_000,
        118_000,
        122_000,
        120_000,
        95_000,
        88_000,
        96_000,
    ],
    outflows=[90_000] * 9 + [92_000, 91_000, 90_000],
    exposure=[(0.60, 4_000_000), (0.61, 4_000_000), (0.63, 4_000_000), (0.64, 4_000_000)],
    instalment=35_000,
    segment="sme_finance",
    expected={"sustained_deterioration", "accelerating_deterioration"},
    expected_signals={"repayment": True, "cashflow": True},
    must_select={
        "repayment_specialist",
        "cashflow_specialist",
        "exposure_specialist",
        "baseline_specialist",
    },
    expect_human_review=True,
)


def build_records(s: Scenario, customer_ref: str) -> dict[str, list[dict]]:
    """Upload-shaped rows (same columns the /ews/upload endpoint accepts)."""
    loan_ref = f"{customer_ref}-L1"
    due_dates = months_back(len(s.repayments), s.end)
    exposure_dates = months_back(len(s.exposure), s.end) if s.exposure else []
    limit = s.exposure[-1][1] if s.exposure else 1_000_000
    outstanding = s.exposure[-1][0] * limit if s.exposure else 0.5 * limit
    repayments = []
    contradictions = dict(s.contradictions)
    for i, ((status, dpd, paid), due) in enumerate(zip(s.repayments, due_dates, strict=True)):
        amount_paid = round(s.instalment * paid, 2)
        payment_date = None if status == "missed" else due.replace(day=min(1 + dpd, 28))
        if i in contradictions:
            kind = contradictions[i]
            if kind == "on_time_dpd":
                status, dpd, amount_paid, payment_date = "on_time", 22, s.instalment, due
            else:
                status, dpd, amount_paid, payment_date = "missed", 0, s.instalment, due
        row = {
            "customer_id": customer_ref,
            "loan_id": loan_ref,
            "due_date": due.isoformat(),
            "payment_date": payment_date.isoformat() if payment_date else None,
            "amount_due": s.instalment,
            "amount_paid": amount_paid,
            "dpd": dpd,
            "repayment_status": status,
        }
        repayments.append(row)
        if i in s.duplicate_months:
            repayments.append(dict(row))
    outflows = s.outflows or [None] * len(s.inflows)
    cash_dates = months_back(len(s.inflows), s.end)
    cashflow = [
        {
            "customer_id": customer_ref,
            "month": i + 1,
            "monthly_inflow": round(v, 2),
            "monthly_outflow": outflows[i],
            "record_date": d.isoformat(),
        }
        for i, (v, d) in enumerate(zip(s.inflows, cash_dates, strict=True))
    ]
    exposure = [
        {
            "customer_id": customer_ref,
            "facility_type": "working_capital",
            "sanctioned_amount": lim,
            "credit_limit": lim,
            "current_utilisation": util,
            "outstanding_balance": round(util * lim, 2),
            "instalment_amount": s.instalment,
            "disbursement_date": "2024-01-15",
            "snapshot_date": d.isoformat(),
        }
        for (util, lim), d in zip(s.exposure, exposure_dates, strict=True)
    ]
    return {
        "customers": [
            {
                "customer_id": customer_ref,
                "customer_type": "sme" if s.segment == "sme_finance" else "individual",
                "employment_category": "business" if s.segment == "sme_finance" else "salaried",
                "portfolio_segment": s.segment,
            }
        ],
        "loans": [
            {
                "customer_id": customer_ref,
                "loan_id": loan_ref,
                "product_type": "working_capital",
                "sanctioned_amount": limit,
                "outstanding_balance": round(outstanding, 2),
                "tenure_months": 36,
                "instalment_amount": s.instalment,
                "disbursement_date": "2024-01-15",
                "status": "active",
            }
        ],
        "repayments": repayments,
        "cashflow": cashflow,
        "exposure": exposure,
    }
