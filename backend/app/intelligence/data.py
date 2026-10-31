"""Read-only, single-customer evidence snapshot.

The snapshot is the only data a customer-level investigation can see: it is loaded for
one internal customer id, so specialists cannot reach another customer's records.
Every observation keeps its source table and record id for evidence references.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.ews import CashFlowRecord, Customer, CustomerExposure, LoanFacility, RepaymentRecord


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        pass
    try:
        return datetime.strptime(text[:10], "%d/%m/%Y").date()
    except ValueError:
        return None


@dataclass(frozen=True)
class Repayment:
    id: str
    loan_ref: str
    due_date: date | None
    payment_date: date | None
    amount_due: float
    amount_paid: float
    dpd: int
    status: str

    @property
    def is_delinquent(self) -> bool:
        return self.status in {"late", "missed", "partial"} or self.dpd > 0


@dataclass(frozen=True)
class CashFlow:
    id: str
    month: int
    inflow: float
    outflow: float | None
    record_date: date | None


@dataclass(frozen=True)
class Exposure:
    id: str
    facility_type: str
    sanctioned: float
    credit_limit: float
    utilisation: float
    outstanding: float
    instalment: float
    snapshot_date: date | None


@dataclass(frozen=True)
class Loan:
    id: str
    loan_ref: str
    product_type: str
    sanctioned: float
    outstanding: float
    instalment: float
    status: str


@dataclass(frozen=True)
class CustomerSnapshot:
    customer_pk: str
    customer_ref: str
    customer_type: str
    segment: str
    employment_category: str
    loans: tuple[Loan, ...]
    repayments: tuple[Repayment, ...]
    cashflows: tuple[CashFlow, ...]
    exposures: tuple[Exposure, ...]
    # Latest repayment due date across the institution's data: a reproducible
    # "as of" date for staleness, independent of the wall clock.
    data_as_of: date | None
    excluded_record_ids: frozenset[str] = field(default_factory=frozenset)
    # Records a human confirmed as correct despite failing an integrity check.
    confirmed_record_ids: frozenset[str] = field(default_factory=frozenset)

    def record_ids(self) -> set[str]:
        return (
            {r.id for r in self.repayments}
            | {c.id for c in self.cashflows}
            | {e.id for e in self.exposures}
            | {loan.id for loan in self.loans}
        )

    def source_table(self, record_id: str) -> str | None:
        for table, rows in (
            ("repayment_records", self.repayments),
            ("cashflow_records", self.cashflows),
            ("customer_exposures", self.exposures),
            ("loan_facilities", self.loans),
        ):
            if any(row.id == record_id for row in rows):
                return table
        return None


def load_customer_snapshot(
    db: Session,
    customer_pk: str,
    excluded_record_ids: frozenset[str] = frozenset(),
    confirmed_record_ids: frozenset[str] = frozenset(),
) -> CustomerSnapshot:
    customer = db.get(Customer, customer_pk)
    if customer is None:
        raise LookupError("Customer not found")
    loans = db.scalars(select(LoanFacility).where(LoanFacility.customer_id == customer_pk)).all()
    loan_refs = {loan.id: loan.loan_id for loan in loans}
    repayments = (
        db.scalars(select(RepaymentRecord).where(RepaymentRecord.loan_id.in_(loan_refs))).all()
        if loan_refs
        else []
    )
    cashflows = db.scalars(
        select(CashFlowRecord).where(CashFlowRecord.customer_id == customer_pk)
    ).all()
    exposures = db.scalars(
        select(CustomerExposure).where(CustomerExposure.customer_id == customer_pk)
    ).all()
    as_of = parse_date(db.scalar(select(func.max(RepaymentRecord.due_date))))

    reps = tuple(
        sorted(
            (
                Repayment(
                    id=r.id,
                    loan_ref=loan_refs.get(r.loan_id, ""),
                    due_date=parse_date(r.due_date),
                    payment_date=parse_date(r.payment_date),
                    amount_due=float(r.amount_due or 0),
                    amount_paid=float(r.amount_paid or 0),
                    dpd=int(r.dpd or 0),
                    status=(r.repayment_status or "").strip().lower(),
                )
                for r in repayments
                if r.id not in excluded_record_ids
            ),
            key=lambda r: (r.due_date or date.min, r.loan_ref, r.id),
        )
    )
    flows = tuple(
        sorted(
            (
                CashFlow(
                    id=c.id,
                    month=int(c.month),
                    inflow=float(c.monthly_inflow or 0),
                    outflow=None if c.monthly_outflow is None else float(c.monthly_outflow),
                    record_date=parse_date(c.record_date),
                )
                for c in cashflows
                if c.id not in excluded_record_ids
            ),
            key=lambda c: (c.month, c.id),
        )
    )
    exps = tuple(
        sorted(
            (
                Exposure(
                    id=e.id,
                    facility_type=e.facility_type or "",
                    sanctioned=float(e.sanctioned_amount or 0),
                    credit_limit=float(e.credit_limit or 0),
                    utilisation=float(e.current_utilisation or 0),
                    outstanding=float(e.outstanding_balance or 0),
                    instalment=float(e.instalment_amount or 0),
                    snapshot_date=parse_date(e.snapshot_date),
                )
                for e in exposures
                if e.id not in excluded_record_ids
            ),
            key=lambda e: (e.snapshot_date or date.min, e.facility_type, e.id),
        )
    )
    return CustomerSnapshot(
        customer_pk=customer.id,
        customer_ref=customer.customer_id,
        customer_type=customer.customer_type or "",
        segment=customer.portfolio_segment or "",
        employment_category=customer.employment_category or "",
        loans=tuple(
            Loan(
                id=loan.id,
                loan_ref=loan.loan_id,
                product_type=loan.product_type or "",
                sanctioned=float(loan.sanctioned_amount or 0),
                outstanding=float(loan.outstanding_balance or 0),
                instalment=float(loan.instalment_amount or 0),
                status=loan.status or "",
            )
            for loan in sorted(loans, key=lambda item: item.loan_id)
        ),
        repayments=reps,
        cashflows=flows,
        exposures=exps,
        data_as_of=as_of,
        excluded_record_ids=excluded_record_ids,
        confirmed_record_ids=confirmed_record_ids,
    )
