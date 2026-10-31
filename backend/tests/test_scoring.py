"""Deterministic indicators and scoring."""

from dataclasses import replace

import pytest
from sqlalchemy import select

from app.intelligence import indicators, scoring
from app.intelligence.data import load_customer_snapshot
from app.models.ews import Customer
from app.services.policy import DEFAULT_POLICY, ScoringPolicy
from evaluation.scenarios import SCENARIOS, build_records


@pytest.fixture
def snapshot_of(client, load_scenarios):
    def get(key, **kwargs):
        load_scenarios(key)
        with client.app.state.session_factory() as db:
            customer = db.scalar(select(Customer).where(Customer.customer_id == key))
            return load_customer_snapshot(db, customer.id, **kwargs)

    return get


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.key)
def test_benchmark_classification(snapshot_of, scenario):
    evaluation = scoring.evaluate_snapshot(snapshot_of(scenario.key), DEFAULT_POLICY)
    assert evaluation.assessment.classification in scenario.expected


def test_calculations_are_reproducible(snapshot_of):
    snap = snapshot_of("accelerating_deterioration")
    first = scoring.evaluate_snapshot(snap, DEFAULT_POLICY)
    second = scoring.evaluate_snapshot(snap, DEFAULT_POLICY)
    for name, calc in first.calculations.items():
        assert calc == second.calculations[name]
        assert len(calc.input_digest) == 64
    assert first.assessment == second.assessment


def test_digest_changes_when_any_input_changes(snapshot_of):
    snap = snapshot_of("normal_repayment")
    changed = replace(snap, repayments=(replace(snap.repayments[0], dpd=9), *snap.repayments[1:]))
    assert (
        indicators.repayment_profile(snap).input_digest
        != indicators.repayment_profile(changed).input_digest
    )


def test_insufficient_evidence_is_never_low_risk():
    a = scoring.assess(
        repayment={},
        cashflow={},
        exposure={},
        pressure={},
        baseline={},
        integrity={"sufficiency": "insufficient"},
        scoring=ScoringPolicy(),
    )
    assert a.classification == "insufficient_evidence"
    assert a.severity == "undetermined" and a.risk_score is None
    assert a.recommendation == "request_additional_data"


def test_correlated_signals_do_not_double_count():
    worst = {
        "periods": 12,
        "recent_late_ratio": 1.0,
        "recent_max_dpd": 400,
        "current_streak": 9,
        "recent_shortfall_ratio": 1.0,
    }
    family = scoring.repayment_family(worst)
    assert family.score == 100  # bounded within the family
    weights = ScoringPolicy().family_weights
    alone = scoring.composite({"repayment": family}, weights)
    assert alone == pytest.approx(100 * weights["repayment"])
    cash = scoring.FamilyScore("cashflow", 50, {})
    both = scoring.composite({"repayment": family, "cashflow": cash}, weights)
    assert alone < both < alone + 50 * weights["cashflow"]  # corroboration, not addition


def test_duplicates_do_not_change_the_score(snapshot_of, client, load_scenarios):
    load_scenarios("normal_repayment", prefix="clean-")
    with client.app.state.session_factory() as db:
        customer = db.scalar(
            select(Customer).where(Customer.customer_id == "clean-normal_repayment")
        )
        clean = load_customer_snapshot(db, customer.id)
    duplicated = snapshot_of("duplicate_records")
    a = indicators.repayment_profile(clean).output
    b = indicators.repayment_profile(duplicated).output
    assert len(b["duplicates_excluded"]) == 3
    assert a["periods"] == b["periods"] and a["delinquent_periods"] == b["delinquent_periods"]


def test_contradictory_records_need_human_confirmation_to_count(snapshot_of):
    snap = snapshot_of("contradictory_records")
    profile = indicators.repayment_profile(snap).output
    assert len(profile["contradictory_excluded"]) == 4
    assert profile["delinquent_periods"] == 0
    confirmed = replace(snap, confirmed_record_ids=frozenset(profile["contradictory_excluded"]))
    assert indicators.repayment_profile(confirmed).output["delinquent_periods"] == 4


def test_seasonal_dip_is_mitigated(snapshot_of):
    cash = indicators.cashflow_profile(snapshot_of("seasonal_behaviour")).output
    assert cash["change_vs_baseline"] < -0.2
    assert cash["seasonality"]["explains_decline"] is True
    family = scoring.cashflow_family(cash)
    assert family.components["seasonal_mitigation_factor"] == 0.4


def test_scenarios_build_upload_shaped_rows():
    records = build_records(SCENARIOS[0], "X")
    assert set(records) == {"customers", "loans", "repayments", "cashflow", "exposure"}
    assert records["repayments"][-1]["due_date"] == "2026-09-01"
