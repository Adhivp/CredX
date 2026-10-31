"""Deterministic risk scoring and deterioration classification.

Correlated indicators are first combined *within* a signal family (repayment, cash
flow, exposure) into one bounded family score, so e.g. late ratio, DPD and streak are
not counted three times. Independent families are then combined with a noisy-OR,
which rewards corroboration without letting scores simply add up.

Insufficient evidence is its own outcome with severity "undetermined"; it is never
mapped to low risk.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from app.intelligence import indicators
from app.intelligence.data import CustomerSnapshot
from app.services.policy import PolicyConfig, ScoringPolicy

SCORING_VERSION = "1.0"
CLASSIFICATIONS = (
    "low_risk",
    "emerging_deterioration",
    "sustained_deterioration",
    "accelerating_deterioration",
    "recovery",
    "insufficient_evidence",
)
SEVERITIES = ("low", "moderate", "high", "severe", "undetermined")


@dataclass(frozen=True)
class FamilyScore:
    family: str
    score: float
    components: dict[str, float]


@dataclass(frozen=True)
class Assessment:
    classification: str
    severity: str
    risk_score: float | None
    confidence: float
    family_scores: dict[str, float]
    elevated_families: list[str]
    persistence_periods: int
    accelerating: bool
    recovering: bool
    recommendation: str
    interventions: list[str]
    rationale: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def _clip(value: float) -> float:
    return max(0.0, min(1.0, value))


def repayment_family(rep: dict) -> FamilyScore | None:
    if not rep or not rep.get("periods"):
        return None
    components = {
        "recent_late_ratio": (rep.get("recent_late_ratio") or 0) * 40,
        "recent_max_dpd": _clip((rep.get("recent_max_dpd") or 0) / 90) * 30,
        "current_streak": _clip((rep.get("current_streak") or 0) / 3) * 20,
        "recent_shortfall": _clip((rep.get("recent_shortfall_ratio") or 0) / 0.5) * 10,
    }
    return FamilyScore("repayment", round(min(sum(components.values()), 100), 2), components)


def cashflow_family(cf: dict) -> FamilyScore | None:
    if not cf or (cf.get("months") or 0) < 4:
        return None
    decline = max(-(cf.get("change_vs_baseline") or 0), 0)
    components = {
        "decline_vs_baseline": _clip(decline / 0.4) * 50,
        "months_below_baseline": _clip((cf.get("months_below_baseline_streak") or 0) / 3) * 25,
        "volatility_increase": _clip(
            max((cf.get("recent_cv") or 0) - (cf.get("baseline_cv") or 0), 0) / 0.2
        )
        * 10,
        "net_negative_months": _clip((cf.get("net_negative_recent") or 0) / 3) * 15,
    }
    score = sum(components.values())
    seasonal = cf.get("seasonality") or {}
    if seasonal.get("explains_decline"):
        components["seasonal_mitigation_factor"] = 0.4
        score *= 0.4
    return FamilyScore("cashflow", round(min(score, 100), 2), components)


def exposure_family(exp: dict, pressure: dict | None) -> FamilyScore | None:
    if not exp or exp.get("latest_utilisation") is None:
        return None
    util = exp["latest_utilisation"]
    level = 0 if util <= 0.5 else 15 if util <= 0.7 else 30 if util <= 0.85 else 45
    if util > 0.95:
        level = 55
    ratio = (pressure or {}).get("recent_ratio")
    obligation = 0
    if ratio is not None:
        obligation = 0 if ratio <= 0.3 else 10 if ratio <= 0.5 else 20 if ratio <= 0.7 else 30
    components = {
        "utilisation_level": float(level),
        "utilisation_increase": _clip(max(exp.get("utilisation_change") or 0, 0) / 0.3) * 15,
        "obligation_pressure": float(obligation),
    }
    return FamilyScore("exposure", round(min(sum(components.values()), 100), 2), components)


def composite(families: dict[str, FamilyScore], weights: dict[str, float]) -> float:
    survival = 1.0
    for name, family in families.items():
        survival *= 1 - weights.get(name, 0.5) * family.score / 100
    return round(100 * (1 - survival), 2)


def severity_for(score: float, thresholds: dict[str, float]) -> str:
    if score >= thresholds["severe"]:
        return "severe"
    if score >= thresholds["high"]:
        return "high"
    if score >= thresholds["moderate"]:
        return "moderate"
    return "low"


def _recommend(classification: str, severity: str, exp: dict) -> list[str]:
    rank = SEVERITIES.index(severity) if severity != "undetermined" else -1
    if classification == "insufficient_evidence":
        return ["request_additional_data"]
    if classification == "low_risk":
        return ["continue_monitoring"]
    if classification == "recovery":
        return ["continue_monitoring"] + (["increase_monitoring_frequency"] if rank >= 1 else [])
    if classification == "emerging_deterioration":
        return ["increase_monitoring_frequency"] + (
            ["relationship_contact_review"] if rank >= 1 else []
        )
    actions = ["relationship_contact_review"]
    if classification == "accelerating_deterioration" and (
        (exp.get("revolving_utilisation") or 0) > 0.7 or (exp.get("latest_utilisation") or 0) > 0.7
    ):
        actions.append("credit_limit_review")
    if rank >= 2:
        actions.append("restructuring_assessment")
    if classification == "accelerating_deterioration" and severity == "severe":
        actions.append("collections_referral_review")
    return actions


def assess(
    *,
    repayment: dict | None,
    cashflow: dict | None,
    exposure: dict | None,
    pressure: dict | None,
    baseline: dict | None,
    integrity: dict,
    scoring: ScoringPolicy,
    excluded_families: frozenset[str] = frozenset(),
    open_material_challenges: int = 0,
) -> Assessment:
    rep, cf, exp, base = repayment or {}, cashflow or {}, exposure or {}, baseline or {}
    candidates = {
        "repayment": repayment_family(rep),
        "cashflow": cashflow_family(cf),
        "exposure": exposure_family(exp, pressure),
    }
    families = {
        name: score
        for name, score in candidates.items()
        if score is not None and name not in excluded_families
    }
    rationale = [f"{name} family excluded by human review" for name in sorted(excluded_families)]
    sufficiency = integrity.get("sufficiency", "insufficient")

    if sufficiency == "insufficient" or not families:
        rationale.append(
            "Insufficient evidence: "
            f"{integrity.get('repayment_periods', 0)} repayment periods and "
            f"{integrity.get('cashflow_months', 0)} cash-flow months available"
        )
        return Assessment(
            classification="insufficient_evidence",
            severity="undetermined",
            risk_score=None,
            confidence=0.3,
            family_scores={k: v.score for k, v in families.items()},
            elevated_families=[],
            persistence_periods=0,
            accelerating=False,
            recovering=False,
            recommendation="request_additional_data",
            interventions=["request_additional_data"],
            rationale=rationale,
        )

    score = composite(families, scoring.family_weights)

    def is_elevated(name: str, family: FamilyScore) -> bool:
        if family.score < scoring.elevated_family_score:
            return False
        # A high but static utilisation level is a vulnerability, not a deterioration:
        # exposure counts only when it is rising or creating repayment pressure.
        if name == "exposure":
            c = family.components
            return c["utilisation_increase"] > 0 or c["obligation_pressure"] >= 20
        return True

    elevated = [n for n, f in families.items() if is_elevated(n, f)]
    rep_persistence = 0
    if "repayment" in families:
        streak = rep.get("current_streak") or 0
        last4 = rep.get("delinquent_in_last_4") or 0
        rep_persistence = max(streak, last4 if last4 >= 3 else 0)
    cash_persistence = (
        (cf.get("months_below_baseline_streak") or 0) if "cashflow" in families else 0
    )
    persistence = max(rep_persistence, cash_persistence)
    accelerating = bool(
        ("repayment" in families and base.get("repayment_accelerating"))
        or ("cashflow" in families and base.get("cashflow_accelerating"))
        or ("repayment" in families and (rep.get("dpd_slope_recent") or 0) > 5)
    )
    rep_recovering = bool(
        rep.get("prior_had_delinquency")
        and (rep.get("clean_periods_at_end") or 0) >= scoring.recovery_clean_periods
    )
    cash_recovering = bool(base.get("cashflow_recovering") or cf.get("recovered_after_dip"))
    cash_declining = cash_persistence > 0
    recovering = (rep_recovering and not cash_declining) or (
        cash_recovering and (rep.get("current_streak") or 0) == 0
    )

    if not elevated:
        classification = "recovery" if recovering else "low_risk"
    elif recovering and not accelerating and "exposure" not in elevated:
        classification = "recovery"
    elif accelerating and persistence >= 2:
        classification = "accelerating_deterioration"
    elif persistence >= scoring.sustained_min_periods:
        classification = "sustained_deterioration"
    else:
        classification = "emerging_deterioration"

    severity = severity_for(score, scoring.severity_thresholds)
    for name, family in sorted(families.items()):
        rationale.append(f"{name} family score {family.score:.1f}/100")
    rationale.append(f"Composite (noisy-OR across independent families) {score:.1f}/100")
    rationale.append(f"Persistence {persistence} period(s); accelerating={accelerating}")
    if recovering:
        rationale.append("Recovery evidence present in the most recent periods")
    if (cf.get("seasonality") or {}).get("explains_decline"):
        rationale.append("Year-on-year comparison indicates the inflow change is seasonal")

    confidence = {"sufficient": 0.85, "limited": 0.65}.get(sufficiency, 0.35)
    if len(families) == 1:
        confidence -= 0.10
        rationale.append("Only one signal family available; corroboration not possible")
    if len(elevated) >= 2:
        confidence += 0.05
    if (integrity.get("contradiction_share") or 0) > 0.05:
        confidence -= 0.10
    if not base.get("sufficient_history", True):
        confidence -= 0.05
    confidence -= min(0.10 * open_material_challenges, 0.30)
    confidence = round(max(0.2, min(0.95, confidence)), 2)

    actions = _recommend(classification, severity, exp)
    return Assessment(
        classification=classification,
        severity=severity,
        risk_score=score,
        confidence=confidence,
        family_scores={k: v.score for k, v in families.items()},
        elevated_families=elevated,
        persistence_periods=persistence,
        accelerating=accelerating,
        recovering=recovering,
        recommendation=actions[0],
        interventions=actions,
        rationale=rationale,
    )


@dataclass(frozen=True)
class SnapshotEvaluation:
    assessment: Assessment
    calculations: dict[str, indicators.Calculation]


def evaluate_snapshot(snapshot: CustomerSnapshot, policy: PolicyConfig) -> SnapshotEvaluation:
    """Full deterministic evaluation of one customer, used by assessments and monitoring."""
    s, d = policy.scoring, policy.data
    rep = indicators.repayment_profile(snapshot)
    cf = indicators.cashflow_profile(snapshot, s.cashflow_decline_threshold)
    exp = indicators.exposure_profile(snapshot)
    pressure = indicators.obligation_pressure(exp, cf)
    base = indicators.behavioural_baseline(
        snapshot, rep, cf, d.min_repayment_records, d.min_cashflow_months, s.recovery_clean_periods
    )
    integrity = indicators.data_integrity(
        snapshot, d.min_repayment_records, d.min_cashflow_months, d.stale_after_days
    )
    result = assess(
        repayment=rep.output,
        cashflow=cf.output,
        exposure=exp.output,
        pressure=pressure.output,
        baseline=base.output,
        integrity=integrity.output,
        scoring=s,
    )
    calcs = {c.name: c for c in (rep, cf, exp, pressure, base, integrity)}
    return SnapshotEvaluation(result, calcs)
