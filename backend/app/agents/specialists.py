"""Specialist agents.

Each specialist answers one ``TaskSpec`` with deterministic analysis performed through
its allowlisted tools, producing typed findings with evidence references and
calculation references. It may ask another specialist for help via a
``FollowUpRequest``; the Director decides whether that delegation happens. The LLM is
optional and only adds a verified narrative interpretation.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.agents.contracts import AgentId, AgentResult, EvidenceRef, FindingSpec, FollowUpRequest
from app.agents.llm import unverified_numbers
from app.agents.tools import ToolContext
from app.intelligence.scoring import cashflow_family, exposure_family, repayment_family


class Interpretation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interpretation: str = Field(max_length=600)
    caveats: list[str] = Field(default_factory=list, max_length=3)


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.0f}%"


def _ev(table: str, record_id: str, field: str = "", value="") -> EvidenceRef:
    return EvidenceRef(
        source_table=table, record_id=record_id, field=field, observed_value=str(value)[:128]
    )


class Specialist:
    agent_id: AgentId
    purpose = "interpret_findings"

    def run(self, ctx: ToolContext) -> AgentResult:
        findings, followups, calcs, status = self.analyse(ctx)
        llm_status = self.interpret(ctx, findings)
        return AgentResult(
            task_id=ctx.task.task_id,
            agent_id=self.agent_id,
            findings=findings,
            calculation_names=calcs,
            followups=followups,
            completion_status=status,
            llm_status=llm_status,
        )

    def analyse(self, ctx: ToolContext):
        raise NotImplementedError

    def interpret(self, ctx: ToolContext, findings: list[FindingSpec]) -> str:
        primary = [f for f in findings if f.direction in {"supports", "contradicts"}]
        if not primary:
            return "not_used"
        payload = {
            "subject": "SUBJECT",
            "specialist": self.agent_id.value,
            "hypothesis": ctx.task.hypothesis_code,
            "findings": [
                {
                    "code": f.code,
                    "direction": f.direction,
                    "summary": f.summary,
                    "metrics": f.metrics,
                }
                for f in primary
            ],
            "human_context": [c.text for c in ctx.task.context],
        }
        result = ctx.llm.complete_json(
            agent_id=self.agent_id.value,
            purpose=self.purpose,
            task_id=ctx.task.task_id,
            instructions=(
                "Explain in at most three sentences what these deterministic findings mean for "
                "the hypothesis, and list up to three caveats. Do not add new figures."
            ),
            data=payload,
            schema=Interpretation,
        )
        if result is None:
            return ctx.llm.status if ctx.llm.status != "available" else "failed"
        # Figures must come from the deterministic metrics or the finding summaries.
        summaries = " ".join(f.summary for f in primary)
        bad = [
            token
            for token in unverified_numbers(result.interpretation, [f.metrics for f in primary])
            if token not in summaries
        ]
        if bad:
            ctx.llm.flag(type="unverified_numbers", agent=self.agent_id.value, numbers=bad[:5])
            return "rejected_unverified_numbers"
        primary[0].interpretation = result.interpretation
        primary[0].interpretation_source = "llm"
        return "ok"


class RepaymentSpecialist(Specialist):
    agent_id = AgentId.REPAYMENT

    def analyse(self, ctx: ToolContext):
        calc = ctx.calculation("repayment_profile")
        rules = ctx.calculation("core_rules")
        r = calc.output
        if not r.get("periods"):
            finding = FindingSpec(
                code="no_repayment_history",
                hypothesis_code="repayment_deterioration",
                summary="No dated repayment records are available for this customer.",
                direction="insufficient",
                confidence=0.9,
                missing_evidence=["repayment_history"],
                calculation_names=[calc.name],
                family="repayment",
            )
            return [finding], [], [calc.name], "insufficient_data"

        recent, prior = r["recent_late_ratio"] or 0, r["prior_late_ratio"]
        deteriorated = (
            (prior is not None and recent >= prior + 0.15)
            or (prior is None and recent >= 0.34)
            or r["current_streak"] >= 2
            or (r["recent_max_dpd"] > max(r["prior_max_dpd"], 15) and recent >= 0.34)
        )
        family = repayment_family(r)
        evidence = [
            _ev("repayment_records", rid, "repayment_status", "delinquent")
            for rid in r["delinquent_record_ids"][-6:]
        ] or [
            _ev("repayment_records", rid, "repayment_status", "on_time")
            for rid in r["recent_period_ids"][:3]
        ]
        contradictions = []
        if rules.output.get("miss_classification") == "temporary" and deteriorated:
            contradictions.append(
                "Existing rule engine classifies the misses as temporary "
                f"(confidence {rules.output.get('miss_confidence')})"
            )
        summary = (
            f"Delinquent in {_pct(recent)} of the last 3 periods versus {_pct(prior)} earlier; "
            f"recent max DPD {r['recent_max_dpd']} days (earlier {r['prior_max_dpd']}); "
            f"current delinquent streak {r['current_streak']}."
        )
        findings = [
            FindingSpec(
                code="repayment_deterioration" if deteriorated else "repayment_stable",
                hypothesis_code="repayment_deterioration",
                summary=summary,
                direction="supports" if deteriorated else "contradicts",
                confidence=round(min(0.55 + 0.03 * r["periods"], 0.9), 2),
                uncertainty=f"Based on {r['periods']} monthly repayment periods.",
                contradictions=contradictions,
                missing_evidence=(
                    ["at least 6 periods of repayment history"] if r["periods"] < 6 else []
                ),
                evidence=evidence,
                calculation_names=[calc.name, rules.name],
                metrics={
                    k: r[k]
                    for k in (
                        "recent_late_ratio",
                        "prior_late_ratio",
                        "recent_max_dpd",
                        "prior_max_dpd",
                        "current_streak",
                        "delinquent_in_last_4",
                        "recent_shortfall_ratio",
                        "partial_count",
                        "missed_count",
                    )
                }
                | {"family_score": family.score if family else None},
                family="repayment",
            )
        ]
        if r["delinquent_periods"] == 1 and r["current_streak"] <= 1:
            findings.append(
                FindingSpec(
                    code="isolated_delay",
                    hypothesis_code="temporary_disruption",
                    summary="A single delinquent period in the history: an isolated delay.",
                    direction="supports",
                    confidence=0.7,
                    evidence=evidence[:2],
                    calculation_names=[calc.name],
                    metrics={"delinquent_periods": 1},
                    family="repayment",
                )
            )
        if r["partial_count"] >= 2:
            findings.append(
                FindingSpec(
                    code="repeated_partial_payments",
                    hypothesis_code="repayment_deterioration",
                    summary=f"{r['partial_count']} partial payments recorded.",
                    direction="supports",
                    confidence=0.75,
                    evidence=evidence[:3],
                    calculation_names=[calc.name],
                    metrics={"partial_count": r["partial_count"]},
                    family="repayment",
                )
            )
        followups = []
        if deteriorated:
            followups.append(
                FollowUpRequest(
                    target_agent=AgentId.EXPOSURE,
                    hypothesis_code="obligation_pressure",
                    question="Do exposure growth or obligations explain the repayment stress?",
                    reason="Repayment deterioration detected; obligation pressure is untested.",
                    requested_evidence=["utilisation trend", "obligation-to-inflow ratio"],
                )
            )
        return findings, followups, [calc.name, rules.name], "complete"


class CashflowSpecialist(Specialist):
    agent_id = AgentId.CASHFLOW

    def analyse(self, ctx: ToolContext):
        calc = ctx.calculation("cashflow_profile")
        c = calc.output
        threshold = ctx.cache.policy.scoring.cashflow_decline_threshold
        if (c.get("months") or 0) < 4:
            return (
                [
                    FindingSpec(
                        code="insufficient_cashflow_history",
                        hypothesis_code="cashflow_decline",
                        summary=f"Only {c.get('months', 0)} months of cash-flow records.",
                        direction="insufficient",
                        confidence=0.9,
                        missing_evidence=["at least 4 months of cash-flow history"],
                        calculation_names=[calc.name],
                        family="cashflow",
                    )
                ],
                [],
                [calc.name],
                "insufficient_data",
            )
        change = c["change_vs_baseline"]
        seasonal = c.get("seasonality")
        seasonal_dip = bool(seasonal and seasonal.get("explains_decline"))
        declining = (
            change < -threshold and c["months_below_baseline_streak"] >= 1 and not seasonal_dip
        )
        family = cashflow_family(c)
        evidence = [
            _ev("cashflow_records", rid, "monthly_inflow", value)
            for rid, value in zip(
                c["recent_record_ids"],
                c["inflow_series"][-len(c["recent_record_ids"]) :],
                strict=False,
            )
        ]
        missing = []
        if declining and seasonal is None:
            missing.append("15+ months of inflows for a year-on-year seasonality comparison")
        if c.get("outflow_coverage", 0) == 0:
            missing.append("monthly outflows for net cash-flow analysis")
        findings = [
            FindingSpec(
                code="cashflow_decline"
                if declining
                else "cashflow_seasonal_dip"
                if seasonal_dip
                else "cashflow_stable",
                hypothesis_code="cashflow_decline",
                summary=(
                    f"Average inflow over the last 3 months is {_pct(abs(change))} "
                    f"{'below' if change < 0 else 'above'} the customer's earlier baseline; "
                    f"below baseline for {c['months_below_baseline_streak']} consecutive months."
                ),
                direction="supports" if declining else "contradicts",
                confidence=round(min(0.55 + 0.03 * c["months"], 0.9), 2),
                uncertainty=f"Based on {c['months']} months of inflows.",
                missing_evidence=missing,
                evidence=evidence,
                calculation_names=[calc.name],
                metrics={
                    k: c.get(k)
                    for k in (
                        "change_vs_baseline",
                        "months_below_baseline_streak",
                        "decline_streak",
                        "baseline_cv",
                        "recent_cv",
                        "net_negative_recent",
                        "latest_vs_previous",
                    )
                }
                | {"family_score": family.score if family else None},
                family="cashflow",
            )
        ]
        if seasonal and seasonal.get("explains_decline"):
            findings.append(
                FindingSpec(
                    code="seasonal_pattern",
                    hypothesis_code="seasonal_pattern",
                    summary=(
                        "Recent inflows are within "
                        f"{_pct(abs(seasonal['yoy_change']))} of the same months last year."
                    ),
                    direction="supports",
                    confidence=0.75,
                    evidence=evidence,
                    calculation_names=[calc.name],
                    metrics={"yoy_change": seasonal["yoy_change"]},
                    family="cashflow",
                )
            )
        if declining and (c.get("latest_vs_previous") or 0) > 0.05:
            findings.append(
                FindingSpec(
                    code="latest_inflow_improvement",
                    hypothesis_code="temporary_disruption",
                    summary=(
                        f"Latest month's inflow is {_pct(c['latest_vs_previous'])} above the "
                        "previous month."
                    ),
                    direction="supports",
                    confidence=0.5,
                    uncertainty="A single month of improvement is not yet a recovery.",
                    evidence=evidence[-1:],
                    calculation_names=[calc.name],
                    metrics={"latest_vs_previous": c["latest_vs_previous"]},
                    family="cashflow",
                )
            )
        for item in ctx.context_items():
            if item.category not in {"temporary_disruption", "income_change"}:
                continue
            improving = (c.get("latest_vs_previous") or 0) > 0.05
            findings.append(
                FindingSpec(
                    code="human_context_cashflow_check",
                    hypothesis_code="human_reported_context",
                    summary=(
                        f"Reported context ({item.category}): latest inflow changed "
                        f"{_pct(c.get('latest_vs_previous'))} on the previous month, "
                        + ("consistent with" if improving else "not yet consistent with")
                        + " the reported change."
                    ),
                    direction="supports" if improving else "contradicts",
                    confidence=0.55,
                    uncertainty="Human context is not independently verified by the data.",
                    evidence=evidence[-1:],
                    calculation_names=[calc.name],
                    metrics={"latest_vs_previous": c.get("latest_vs_previous")},
                    family="cashflow",
                )
            )
        followups = []
        if declining:
            followups.append(
                FollowUpRequest(
                    target_agent=AgentId.EXPOSURE,
                    hypothesis_code="obligation_pressure",
                    question="Are obligations now high relative to the reduced inflow?",
                    reason="Inflow decline detected; obligation coverage is untested.",
                    requested_evidence=["obligation-to-inflow ratio"],
                )
            )
        return findings, followups, [calc.name], "complete"


class ExposureSpecialist(Specialist):
    agent_id = AgentId.EXPOSURE

    def analyse(self, ctx: ToolContext):
        exp = ctx.calculation("exposure_profile")
        cf = ctx.calculation("cashflow_profile")
        pressure = ctx.calculation("obligation_pressure")
        e, p = exp.output, pressure.output
        names = [exp.name, cf.name, pressure.name]
        if e.get("latest_utilisation") is None:
            return (
                [
                    FindingSpec(
                        code="no_exposure_data",
                        hypothesis_code="obligation_pressure",
                        summary="No exposure snapshots or loan facilities are recorded.",
                        direction="insufficient",
                        confidence=0.9,
                        missing_evidence=["exposure snapshots"],
                        calculation_names=names,
                        family="exposure",
                    )
                ],
                [],
                names,
                "insufficient_data",
            )
        util, change = e["latest_utilisation"], e.get("utilisation_change") or 0
        growth = e.get("exposure_growth") or 0
        ratio, ratio_change = p.get("recent_ratio"), p.get("ratio_change") or 0
        rising = change > 0.05 or growth > 0.10
        pressured = (ratio is not None and ratio > 0.5) or ratio_change > 0.10
        high = util > 0.85 or (e.get("revolving_utilisation") or 0) > 0.85
        supports = rising or pressured or high
        family = exposure_family(e, p)
        snaps = ctx.call("exposure_snapshots")
        loans = ctx.call("loan_facilities")
        latest_date = max((x.snapshot_date for x in snaps if x.snapshot_date), default=None)
        evidence = [
            _ev("customer_exposures", x.id, "outstanding_balance", x.outstanding)
            for x in snaps
            if x.snapshot_date == latest_date
        ][:6] or [_ev("loan_facilities", x.id, "outstanding_balance", x.outstanding) for x in loans]
        missing = [] if ratio is not None else ["cash-flow history for obligation-to-inflow ratio"]
        if e["snapshots"] < 2:
            missing.append("multiple exposure snapshots to measure utilisation change")
        findings = [
            FindingSpec(
                code="obligation_pressure" if supports else "exposure_stable",
                hypothesis_code="obligation_pressure",
                summary=(
                    f"Utilisation {_pct(util)} (change {_pct(change)}); exposure growth "
                    f"{_pct(growth)}; obligations are {_pct(ratio)} of recent inflow "
                    f"(change {_pct(ratio_change)})."
                ),
                direction="supports" if supports else "contradicts",
                confidence=0.75 if e["snapshots"] >= 2 else 0.6,
                missing_evidence=missing,
                evidence=evidence,
                calculation_names=names,
                metrics={
                    "latest_utilisation": util,
                    "utilisation_change": change,
                    "exposure_growth": growth,
                    "obligation_ratio": ratio,
                    "obligation_ratio_change": ratio_change,
                    "revolving_utilisation": e.get("revolving_utilisation"),
                    "exposure_at_risk": e["exposure_at_risk"],
                    "family_score": family.score if family else None,
                },
                family="exposure",
            )
        ]
        followups = []
        if e["snapshots"] < 2 and e["source"] == "loan_facilities":
            followups.append(
                FollowUpRequest(
                    target_agent=AgentId.INTEGRITY,
                    hypothesis_code="data_quality_artefact",
                    question="Is exposure history missing for this customer?",
                    reason="No exposure snapshots; utilisation trend cannot be measured.",
                )
            )
        return findings, followups, names, "complete"


class BaselineSpecialist(Specialist):
    agent_id = AgentId.BASELINE

    def analyse(self, ctx: ToolContext):
        base = ctx.calculation("behavioural_baseline")
        rep = ctx.calculation("repayment_profile")
        cf = ctx.calculation("cashflow_profile")
        b = base.output
        policy = ctx.cache.policy.scoring
        names = [base.name, rep.name, cf.name]
        hyp = ctx.task.hypothesis_code
        evidence = [
            _ev("repayment_records", rid, "repayment_status", "delinquent")
            for rid in rep.output.get("delinquent_record_ids", [])[-3:]
        ] + [
            _ev("cashflow_records", rid, "monthly_inflow", "recent")
            for rid in cf.output.get("recent_record_ids", [])
        ]
        findings: list[FindingSpec] = []
        missing = []
        if not b["sufficient_history"]:
            missing.append(
                f"history of at least {ctx.cache.policy.data.min_repayment_records} repayment "
                f"periods and {ctx.cache.policy.data.min_cashflow_months} cash-flow months"
            )
        persistence = b["persistence_periods"]
        sustained = persistence >= policy.sustained_min_periods
        findings.append(
            FindingSpec(
                code="deterioration_persistence",
                hypothesis_code="temporary_disruption",
                summary=(
                    f"Behaviour has deviated from the customer's own baseline for {persistence} "
                    f"consecutive period(s) (repayment {b['repayment_deviation_periods']}, "
                    f"cash flow {b['cashflow_deviation_months']})."
                ),
                direction="contradicts" if sustained else "supports",
                confidence=0.8 if b["sufficient_history"] else 0.55,
                uncertainty=(
                    "" if b["sufficient_history"] else "Short history limits the baseline."
                ),
                missing_evidence=missing,
                evidence=evidence,
                calculation_names=names,
                metrics={
                    "persistence_periods": persistence,
                    "repayment_deviation_periods": b["repayment_deviation_periods"],
                    "cashflow_deviation_months": b["cashflow_deviation_months"],
                    "recent_dpd_z": b["recent_dpd_z"],
                    "recent_inflow_z": b["recent_inflow_z"],
                    "baseline_delinquency_rate": b["baseline_delinquency_rate"],
                },
            )
        )
        if b["repayment_accelerating"] or b["cashflow_accelerating"]:
            findings.append(
                FindingSpec(
                    code="deterioration_acceleration",
                    hypothesis_code="temporary_disruption",
                    summary=(
                        "Deviation is widening period on period "
                        f"(repayment {b['repayment_accelerating']}, "
                        f"cash flow {b['cashflow_accelerating']})."
                    ),
                    direction="contradicts",
                    confidence=0.75,
                    evidence=evidence,
                    calculation_names=names,
                    metrics={
                        "repayment_accelerating": b["repayment_accelerating"],
                        "cashflow_accelerating": b["cashflow_accelerating"],
                    },
                )
            )
        recovering = b["repayment_recovering"] or b["cashflow_recovering"]
        if recovering or b["partial_recovery_signal"]:
            findings.append(
                FindingSpec(
                    code="recovery_signal" if recovering else "partial_recovery_signal",
                    hypothesis_code="temporary_disruption",
                    summary=(
                        "Recent periods are back within the customer's normal range."
                        if recovering
                        else "The latest month improved, but the recent window still deviates "
                        "from the customer's norm."
                    ),
                    direction="supports",
                    confidence=0.75 if recovering else 0.5,
                    uncertainty="" if recovering else "One improving period is not a recovery.",
                    evidence=evidence[-2:],
                    calculation_names=names,
                    metrics={
                        "repayment_recovering": b["repayment_recovering"],
                        "cashflow_recovering": b["cashflow_recovering"],
                        "partial_recovery_signal": b["partial_recovery_signal"],
                    },
                )
            )
        if hyp == "seasonal_pattern":
            seasonal = cf.output.get("seasonality")
            findings.append(
                FindingSpec(
                    code="seasonality_assessment",
                    hypothesis_code="seasonal_pattern",
                    summary=(
                        "Insufficient history for a year-on-year comparison."
                        if seasonal is None
                        else f"Year-on-year inflow change {_pct(seasonal['yoy_change'])}."
                    ),
                    direction=(
                        "insufficient"
                        if seasonal is None
                        else "supports"
                        if seasonal["explains_decline"]
                        else "contradicts"
                    ),
                    confidence=0.8 if seasonal else 0.9,
                    missing_evidence=[] if seasonal else ["15+ months of inflows"],
                    evidence=evidence[-3:],
                    calculation_names=names,
                    metrics={"yoy_change": seasonal["yoy_change"] if seasonal else None},
                )
            )
        for item in ctx.context_items():
            if item.category not in {"temporary_disruption", "seasonal_business", "other"}:
                continue
            consistent = recovering or b["partial_recovery_signal"]
            findings.append(
                FindingSpec(
                    code="human_context_assessment",
                    hypothesis_code="human_reported_context",
                    summary=(
                        f"Reported context ({item.category}) is "
                        + (
                            "consistent with early recovery in the latest data."
                            if consistent
                            else "not yet reflected in the data: no recovery is visible."
                        )
                    ),
                    direction="supports" if consistent else "contradicts",
                    confidence=0.6,
                    uncertainty="Human context is not independently verified by the data.",
                    evidence=evidence[-2:],
                    calculation_names=names,
                    metrics={
                        "partial_recovery_signal": b["partial_recovery_signal"],
                        "persistence_periods": persistence,
                    },
                )
            )
        status = "complete" if b["sufficient_history"] else "partial"
        return findings, [], names, status


class IntegritySpecialist(Specialist):
    agent_id = AgentId.INTEGRITY

    def analyse(self, ctx: ToolContext):
        calc = ctx.calculation("data_integrity")
        i = calc.output
        findings = []
        if i["duplicate_repayment_ids"] or i["duplicate_cashflow_ids"]:
            ids = i["duplicate_repayment_ids"] + i["duplicate_cashflow_ids"]
            findings.append(
                FindingSpec(
                    code="duplicate_records",
                    hypothesis_code="data_quality_artefact",
                    summary=f"{len(ids)} duplicate record(s) detected and excluded from scoring.",
                    direction="supports",
                    confidence=0.95,
                    evidence=[
                        _ev(
                            "repayment_records"
                            if rid in i["duplicate_repayment_ids"]
                            else "cashflow_records",
                            rid,
                            "duplicate",
                            "true",
                        )
                        for rid in ids[:10]
                    ],
                    calculation_names=[calc.name],
                    metrics={"duplicates": len(ids)},
                    family="integrity",
                )
            )
        if i["contradictions"]:
            findings.append(
                FindingSpec(
                    code="contradictory_records",
                    hypothesis_code="data_quality_artefact",
                    summary=(
                        f"{len(i['contradictions'])} internally contradictory record(s), e.g. "
                        f"{i['contradictions'][0]['issue'].replace('_', ' ')}."
                    ),
                    direction="supports",
                    confidence=0.9,
                    contradictions=sorted({c["issue"] for c in i["contradictions"]}),
                    evidence=[
                        _ev(
                            "cashflow_records"
                            if c["issue"] == "negative_inflow"
                            else "repayment_records",
                            c["record_id"],
                            "issue",
                            c["issue"],
                        )
                        for c in i["contradictions"][:10]
                    ],
                    calculation_names=[calc.name],
                    metrics={"contradiction_share": i["contradiction_share"]},
                    family="integrity",
                )
            )
        if i["stale"]:
            findings.append(
                FindingSpec(
                    code="stale_data",
                    hypothesis_code="data_quality_artefact",
                    summary=f"Latest repayment record is {i['staleness_days']} days old.",
                    direction="supports",
                    confidence=0.9,
                    missing_evidence=["recent repayment records"],
                    calculation_names=[calc.name],
                    metrics={"staleness_days": i["staleness_days"]},
                    family="integrity",
                )
            )
        if i["cashflow_gaps"]:
            findings.append(
                FindingSpec(
                    code="history_gaps",
                    hypothesis_code="data_quality_artefact",
                    summary=f"Cash-flow history has {len(i['cashflow_gaps'])} missing month(s).",
                    direction="supports",
                    confidence=0.85,
                    missing_evidence=[f"cash-flow month {m}" for m in i["cashflow_gaps"][:6]],
                    calculation_names=[calc.name],
                    metrics={"gaps": len(i["cashflow_gaps"])},
                    family="integrity",
                )
            )
        if i["sufficiency"] != "sufficient":
            findings.append(
                FindingSpec(
                    code="insufficient_history"
                    if i["sufficiency"] == "insufficient"
                    else "limited_history",
                    hypothesis_code="data_quality_artefact",
                    summary=(
                        f"Evidence is {i['sufficiency']}: {i['repayment_periods']} repayment "
                        f"periods, {i['cashflow_months']} cash-flow months"
                        + ("; appears to be a new customer." if i["new_customer"] else ".")
                    ),
                    direction="insufficient",
                    confidence=0.9,
                    missing_evidence=i["missing_sources"]
                    or ["longer repayment and cash-flow history"],
                    calculation_names=[calc.name],
                    metrics={
                        "repayment_periods": i["repayment_periods"],
                        "cashflow_months": i["cashflow_months"],
                        "new_customer": i["new_customer"],
                    },
                    family="integrity",
                )
            )
        if not findings:
            findings.append(
                FindingSpec(
                    code="data_quality_adequate",
                    hypothesis_code="data_quality_artefact",
                    summary="No duplicates, contradictions, gaps or staleness detected.",
                    direction="contradicts",
                    confidence=0.85,
                    calculation_names=[calc.name],
                    metrics={"repayment_periods": i["repayment_periods"]},
                    family="integrity",
                )
            )
        status = "insufficient_data" if i["sufficiency"] == "insufficient" else "complete"
        return findings, [], [calc.name], status


class PortfolioSpecialist(Specialist):
    agent_id = AgentId.PORTFOLIO

    def analyse(self, ctx: ToolContext):
        calc = ctx.calculation("portfolio_profile")
        p = calc.output
        ref = _ev("portfolio_aggregates", calc.input_digest[:64], "portfolio_profile", "aggregate")
        findings = []
        if ctx.task.scope == "customer":
            segment = ctx.task.portfolio_segment
            group = next(
                (g for g in p["segments"] if g["group"] == (segment or "unspecified")), None
            )
            if group is None or group.get("suppressed"):
                findings.append(
                    FindingSpec(
                        code="segment_context_unavailable",
                        hypothesis_code="segment_wide_stress",
                        summary="Segment too small to report without identifying customers.",
                        direction="insufficient",
                        confidence=0.9,
                        evidence=[ref],
                        calculation_names=[calc.name],
                        family="portfolio",
                    )
                )
            else:
                findings.append(
                    FindingSpec(
                        code="segment_cluster" if group["cluster"] else "segment_not_clustered",
                        hypothesis_code="segment_wide_stress",
                        summary=(
                            f"{_pct(group['deteriorating_share'])} of the segment's customers are "
                            f"deteriorating versus {_pct(p['portfolio_deteriorating_share'])} "
                            "portfolio-wide."
                        ),
                        direction="supports" if group["cluster"] else "contradicts",
                        confidence=0.7,
                        evidence=[ref],
                        calculation_names=[calc.name],
                        metrics={
                            "segment_share": group["deteriorating_share"],
                            "portfolio_share": p["portfolio_deteriorating_share"],
                        },
                        family="portfolio",
                    )
                )
            return findings, [], [calc.name], "complete"

        for group in p["segments"]:
            if group.get("suppressed") or not group["cluster"]:
                continue
            findings.append(
                FindingSpec(
                    code="deterioration_cluster",
                    hypothesis_code="portfolio_cluster",
                    summary=(
                        f"Segment {group['group']}: {group['deteriorating']} of "
                        f"{group['customers']} customers deteriorating "
                        f"({_pct(group['deteriorating_share'])})."
                    ),
                    direction="supports",
                    confidence=0.75,
                    evidence=[ref],
                    calculation_names=[calc.name],
                    metrics={k: group[k] for k in ("deteriorating_share", "exposure_share")},
                    family="portfolio",
                )
            )
        if not findings:
            findings.append(
                FindingSpec(
                    code="no_deterioration_cluster",
                    hypothesis_code="portfolio_cluster",
                    summary="No segment is materially above the portfolio deterioration rate.",
                    direction="contradicts",
                    confidence=0.7,
                    evidence=[ref],
                    calculation_names=[calc.name],
                    metrics={"portfolio_share": p["portfolio_deteriorating_share"]},
                    family="portfolio",
                )
            )
        hhi = p["segment_exposure_hhi"]
        findings.append(
            FindingSpec(
                code="exposure_concentration",
                hypothesis_code="portfolio_concentration",
                summary=f"Segment exposure concentration index (HHI) {hhi:.2f}.",
                direction="supports" if hhi >= 0.25 else "contradicts",
                confidence=0.7,
                evidence=[ref],
                calculation_names=[calc.name],
                metrics={"segment_exposure_hhi": hhi},
                family="portfolio",
            )
        )
        return findings, [], [calc.name], "complete"


REGISTRY: dict[AgentId, Specialist] = {
    s.agent_id: s
    for s in (
        RepaymentSpecialist(),
        CashflowSpecialist(),
        ExposureSpecialist(),
        BaselineSpecialist(),
        IntegritySpecialist(),
        PortfolioSpecialist(),
    )
}
