"""Access to the repository's shared ``core`` package (existing EWS rules)."""

import sys
from pathlib import Path

from app.intelligence import indicators
from app.intelligence.data import CustomerSnapshot


def ensure_core_importable() -> None:
    # Locally core/ is a sibling of backend/; in the container it is copied to /core.
    for candidate in (Path(__file__).resolve().parents[3], Path("/")):
        if (candidate / "core" / "rules").is_dir():
            if str(candidate) not in sys.path:
                sys.path.insert(0, str(candidate))
            return


def core_rule_scores(snapshot: CustomerSnapshot) -> indicators.Calculation:
    """Run the existing core.rules repayment rules on this customer's records."""
    import pandas as pd

    ensure_core_importable()
    from core.rules.missing_history import detect_missing_history
    from core.rules.repayment_delays import detect_repayment_delays
    from core.rules.temporary_vs_sustained import classify_misses

    rows = [
        {
            "customer_id": "subject",
            "loan_id": r.loan_ref,
            "due_date": str(r.due_date),
            "payment_date": str(r.payment_date) if r.payment_date else None,
            "amount_due": r.amount_due,
            "amount_paid": r.amount_paid,
            "dpd": r.dpd,
            "repayment_status": r.status,
        }
        for r in snapshot.repayments
        if r.due_date is not None
    ]
    output: dict = {"available": bool(rows)}
    if rows:
        frame = pd.DataFrame(rows)
        delay = detect_repayment_delays(frame).get("subject")
        miss = classify_misses(frame).get("subject")
        output.update(
            {
                "delay_score": delay.score if delay else None,
                "delay_severity": delay.severity.value if delay else None,
                "miss_score": miss.score if miss else None,
                "miss_classification": miss.classification.value if miss else None,
                "miss_confidence": miss.confidence if miss else None,
                "delay_indicators": delay.indicators[:3] if delay else [],
            }
        )
    profile = pd.DataFrame([{"customer_id": "subject", "customer_type": snapshot.customer_type}])
    columns = [
        "customer_id",
        "loan_id",
        "due_date",
        "payment_date",
        "amount_due",
        "amount_paid",
        "dpd",
        "repayment_status",
    ]
    history = detect_missing_history(profile, pd.DataFrame(rows, columns=columns)).get("subject")
    output["completeness_score"] = history.completeness_score if history else None
    return indicators.make_calculation(
        "core_rules",
        [tuple(sorted(r.items())) for r in rows],
        [r.id for r in snapshot.repayments],
        output,
    )
