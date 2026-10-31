"""Record ingestion shared by the upload API, demo seeding and the evaluation harness.

Rows are validated and inserted through the ORM only. When a ``Scope`` is given, rows
for customers outside it are skipped and reported (new customers must be created in a
permitted segment).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.ews import CashFlowRecord, Customer, CustomerExposure, LoanFacility, RepaymentRecord
from app.services.access import Scope

REQUIRED_COLUMNS: dict[str, set[str]] = {
    "customers": {"customer_id", "customer_type"},
    "loans": {"customer_id", "loan_id", "product_type", "sanctioned_amount", "outstanding_balance"},
    "repayments": {
        "customer_id",
        "loan_id",
        "due_date",
        "amount_due",
        "amount_paid",
        "repayment_status",
    },
    "exposure": {"customer_id", "facility_type", "outstanding_balance"},
    "cashflow": {"customer_id", "month", "monthly_inflow"},
}
MAX_TEXT = 64


@dataclass
class IngestResult:
    loaded: int = 0
    skipped: list[str] = field(default_factory=list)
    customer_ids: set[str] = field(default_factory=set)


def _clean(value) -> object:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def _text(row: dict, key: str, default: str = "") -> str:
    value = _clean(row.get(key))
    return default if value is None else str(value).strip()[:MAX_TEXT]


def _num(row: dict, key: str, default: float = 0.0) -> float:
    value = _clean(row.get(key))
    if value is None or value == "":
        return default
    number = float(value)
    if math.isnan(number) or math.isinf(number):
        raise ValueError(f"{key} is not a finite number")
    return number


def _permitted(scope: Scope | None, customer: Customer | None, segment: str) -> bool:
    if scope is None or scope.all:
        return True
    if customer is not None:
        return (
            customer.portfolio_segment in scope.segments or customer.customer_id in scope.customers
        )
    return segment in scope.segments


def ingest(db: Session, data_type: str, rows: list[dict], scope: Scope | None) -> IngestResult:
    result = IngestResult()
    customers: dict[str, Customer] = {}

    def customer(ref: str) -> Customer | None:
        if ref not in customers:
            customers[ref] = db.scalar(select(Customer).where(Customer.customer_id == ref))
        return customers[ref]

    for index, row in enumerate(rows, start=1):
        try:
            ref = _text(row, "customer_id")
            if not ref:
                raise ValueError("customer_id is required")
            existing = customer(ref)
            segment = _text(row, "portfolio_segment")
            if not _permitted(scope, existing, segment):
                result.skipped.append(f"row {index}: customer outside your data scope")
                continue
            if data_type == "customers":
                if existing is None:
                    existing = Customer(customer_id=ref)
                    db.add(existing)
                    customers[ref] = existing
                existing.customer_type = _text(row, "customer_type", "individual")
                existing.employment_category = _text(row, "employment_category")
                existing.portfolio_segment = segment
            elif existing is None:
                result.skipped.append(f"row {index}: unknown customer {ref}")
                continue
            elif data_type == "loans":
                loan_ref = _text(row, "loan_id")
                if db.scalar(select(LoanFacility).where(LoanFacility.loan_id == loan_ref)):
                    result.skipped.append(f"row {index}: loan {loan_ref} already exists")
                    continue
                db.flush()
                db.add(
                    LoanFacility(
                        customer_id=existing.id,
                        loan_id=loan_ref,
                        product_type=_text(row, "product_type"),
                        sanctioned_amount=_num(row, "sanctioned_amount"),
                        outstanding_balance=_num(row, "outstanding_balance"),
                        tenure_months=int(_num(row, "tenure_months")),
                        instalment_amount=_num(row, "instalment_amount"),
                        disbursement_date=_text(row, "disbursement_date"),
                        status=_text(row, "status", "active"),
                    )
                )
            elif data_type == "repayments":
                loan = db.scalar(
                    select(LoanFacility).where(
                        LoanFacility.loan_id == _text(row, "loan_id"),
                        LoanFacility.customer_id == existing.id,
                    )
                )
                if loan is None:
                    result.skipped.append(f"row {index}: loan not found for this customer")
                    continue
                payment = _clean(row.get("payment_date"))
                db.add(
                    RepaymentRecord(
                        loan_id=loan.id,
                        due_date=_text(row, "due_date"),
                        payment_date=str(payment)[:32] if payment not in (None, "") else None,
                        amount_due=_num(row, "amount_due"),
                        amount_paid=_num(row, "amount_paid"),
                        dpd=int(_num(row, "dpd")),
                        repayment_status=_text(row, "repayment_status", "on_time").lower(),
                    )
                )
            elif data_type == "exposure":
                db.flush()
                db.add(
                    CustomerExposure(
                        customer_id=existing.id,
                        facility_id=_text(row, "facility_id"),
                        facility_type=_text(row, "facility_type"),
                        sanctioned_amount=_num(row, "sanctioned_amount"),
                        credit_limit=_num(row, "credit_limit"),
                        current_utilisation=_num(row, "current_utilisation"),
                        outstanding_balance=_num(row, "outstanding_balance"),
                        instalment_amount=_num(row, "instalment_amount"),
                        disbursement_date=_text(row, "disbursement_date"),
                        snapshot_date=_text(row, "snapshot_date"),
                    )
                )
            elif data_type == "cashflow":
                outflow = _clean(row.get("monthly_outflow"))
                db.flush()
                db.add(
                    CashFlowRecord(
                        customer_id=existing.id,
                        month=int(_num(row, "month")),
                        monthly_inflow=_num(row, "monthly_inflow"),
                        monthly_outflow=None if outflow in (None, "") else float(outflow),
                        record_date=_text(row, "record_date"),
                    )
                )
            else:
                raise ValueError(f"Unknown data_type {data_type}")
            result.loaded += 1
            result.customer_ids.add(ref)
        except (TypeError, ValueError) as exc:
            result.skipped.append(f"row {index}: {exc}"[:200])
    db.flush()
    return result


def load_dataset(db: Session, records: dict[str, list[dict]]) -> None:
    """Load a full dataset (customers first) without scope restriction."""
    for data_type in ("customers", "loans", "repayments", "cashflow", "exposure"):
        ingest(db, data_type, records.get(data_type, []), None)
