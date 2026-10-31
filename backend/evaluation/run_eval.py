"""Independent evaluation harness.

    cd backend
    python -m evaluation.run_eval                 # deterministic (LLM disabled)
    python -m evaluation.run_eval --llm           # also use the configured Nebius model

Each labelled scenario is loaded into a fresh, isolated database and investigated
through the real HTTP API by a credit analyst. When the system pauses for analyst
judgment, a scripted analyst completes the review so the final adjudication can be
scored; the pause itself is scored as a routing outcome. The security, human-in-the-
loop and agent-loop test suites are executed and their results included.

Outputs (overwritten): evaluation/results/eval_results.json and EVALUATION_REPORT.md.
Every figure is computed from this run; nothing is estimated or carried over.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
RESULTS = BACKEND / "evaluation" / "results"
SUITES = [
    "tests/test_security_rbac.py",
    "tests/test_hitl.py",
    "tests/test_acceptance.py",
    "tests/test_agent_loop.py",
    "tests/test_llm_gateway.py",
    "tests/test_scoring.py",
]
PASSWORD = "Evaluation passphrase 2026!"


def _app(tmp: Path, use_llm: bool):
    os.environ.update(
        {
            "DATABASE_URL": f"sqlite:///{tmp / 'eval.db'}",
            "INVESTIGATIONS_ASYNC": "false",
            "CORS_ORIGINS": "[]",
        }
    )
    os.environ.setdefault("JWT_SECRET", "evaluation-only-secret-" * 3)
    if not use_llm:
        os.environ["LLM_ENABLED"] = "false"
    from app.db.session import Base
    from app.main import create_app

    app = create_app()
    Base.metadata.create_all(app.state.engine)
    return app


def _analyst(client, app):
    from app.models.governance import UserScope
    from app.models.user import User

    client.post(
        "/api/v1/auth/register",
        json={
            "email": "eval@bank.example",
            "full_name": "Evaluation Analyst",
            "password": PASSWORD,
        },
    )
    with app.state.session_factory() as db:
        user = db.query(User).filter_by(email="eval@bank.example").one()
        user.role = "credit_analyst"
        db.add(UserScope(user_id=user.id, scope_type="all", scope_value=""))
        db.commit()
    token = client.post(
        "/api/v1/auth/login", json={"email": "eval@bank.example", "password": PASSWORD}
    ).json()
    return {"Authorization": f"Bearer {token['access_token']}"}


def _evidence_validity(app, ws) -> tuple[int, int]:
    from sqlalchemy import text

    total = valid = 0
    with app.state.session_factory() as db:
        for f in ws["findings"]:
            for e in f["evidence"]:
                total += 1
                if e["source_table"] == "portfolio_aggregates":
                    valid += 1
                    continue
                row = db.execute(
                    text(f"select 1 from {e['source_table']} where id = :i"),
                    {"i": e["record_id"]},
                ).first()
                valid += row is not None
    return valid, total


def run_scenario(client, app, headers, scenario) -> dict:
    started = time.monotonic()
    ws = client.post(
        "/api/v1/investigations", headers=headers, json={"customer_id": scenario.key}
    ).json()
    iid = ws["id"]
    paused_pre_adjudication = ws["status"] == "awaiting_analyst_review" and not ws["adjudications"]
    first_status = ws["status"]
    if paused_pre_adjudication:
        ws = client.post(
            f"/api/v1/investigations/{iid}/complete-review",
            headers=headers,
            json={"reason": "Scripted evaluation analyst: proceed"},
        ).json()
    elapsed = time.monotonic() - started
    adj = ws["adjudications"][-1] if ws["adjudications"] else None
    events = ws["events"]
    selected = {t["agent"] for t in ws["tasks"] if t["status"] == "completed"}
    waves = [e["payload"] for e in events if e["type"] == "wave_started"]
    rejected = [e["payload"]["decision"] for e in events if e["type"] == "delegation_rejected"]
    delegated = [
        e
        for e in events
        if e["type"] == "delegation_authorised"
        and e["payload"]["requested_by"].startswith("agent:")
    ]
    executed = Counter(
        (t["agent"], t["hypothesis_id"])
        for t in ws["tasks"]
        if t["status"] == "completed" and not t["requested_by"].startswith("human:")
    )
    valid, total = _evidence_validity(app, ws)
    elevated = (
        set(adj["calculation"]["output"]["assessment"]["elevated_families"])
        if (adj and adj["calculation"] and "assessment" in adj["calculation"]["output"])
        else set()
    )
    challenges = ws["challenges"]
    reinvestigated = [c for c in challenges if c["followup_task_ids"]]
    data_issue_found = any(
        f["agent"] == "integrity_specialist" and f["direction"] in {"supports", "insufficient"}
        for f in ws["findings"]
    )
    usage = ws["usage"].get("totals", {})
    human_review = first_status in {"awaiting_analyst_review", "awaiting_approval"} or (
        ws["status"] in {"awaiting_analyst_review", "awaiting_approval"}
    )
    return {
        "scenario": scenario.key,
        "description": scenario.description,
        "expected": sorted(scenario.expected),
        "classification": adj["classification"] if adj else None,
        "correct": bool(adj and adj["classification"] in scenario.expected),
        "severity": adj["severity"] if adj else None,
        "pathway": adj["pathway"] if adj else None,
        "confidence": adj["confidence"] if adj else None,
        "final_status": ws["status"],
        "paused_for_analyst_before_adjudication": paused_pre_adjudication,
        "human_review": human_review,
        "expected_human_review": scenario.expect_human_review,
        "expected_signals": scenario.expected_signals,
        "elevated_families": sorted(elevated),
        "selected_specialists": sorted(selected),
        "must_select_missing": sorted(scenario.must_select - selected),
        "must_not_select_violations": sorted(scenario.must_not_select & selected),
        "rounds": ws["round"],
        "tasks": len(ws["tasks"]),
        "parallel_waves": sum(1 for w in waves if w["parallel"]),
        "waves": len(waves),
        "delegations_authorised": len(delegated),
        "delegations_rejected": len(rejected),
        "duplicate_requests_blocked": sum(1 for r in rejected if r.startswith("duplicate_of")),
        "duplicate_tasks_executed": sum(n - 1 for n in executed.values() if n > 1),
        "challenges": len(challenges),
        "material_challenges": sum(c["materiality"] == "material" for c in challenges),
        "reinvestigation_challenges": len(reinvestigated),
        "reinvestigation_addressed": sum(c["status"] == "addressed" for c in reinvestigated),
        "evidence_refs": total,
        "evidence_refs_valid": valid,
        "data_issue_expected": scenario.expect_data_issue,
        "data_issue_found": data_issue_found,
        "missing_evidence_items": sum(len(f["missing_evidence"]) for f in ws["findings"]),
        "contradictions_flagged": any(f["code"] == "contradictory_records" for f in ws["findings"]),
        "terminated_cleanly": ws["run_state"] == "idle",
        "latency_seconds": round(elapsed, 3),
        "llm_calls": usage.get("llm_calls", 0),
        "llm_tokens": usage.get("tokens", 0),
        "llm_cost_usd": usage.get("cost_usd", 0.0),
        "llm_status_counts": {u["status"]: u["calls"] for u in ws["llm_usage"]},
    }


def _ratio(numerator, denominator):
    return round(numerator / denominator, 4) if denominator else None


def summarise(rows: list[dict]) -> dict:
    from evaluation.scenarios import DETERIORATING

    n = len(rows)
    classes = sorted(
        {c for r in rows for c in r["expected"]} | {r["classification"] or "none" for r in rows}
    )
    per_class = {}
    for c in classes:
        tp = sum(r["classification"] == c and c in r["expected"] for r in rows)
        fp = sum(r["classification"] == c and c not in r["expected"] for r in rows)
        fn = sum(
            c in r["expected"] and r["classification"] != c and len(r["expected"]) == 1
            for r in rows
        )
        per_class[c] = {
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "precision": _ratio(tp, tp + fp),
            "recall": _ratio(tp, tp + fn),
        }
    # Binary detection uses only unambiguous labels: scenarios whose acceptable labels mix
    # deterioration and non-deterioration classes are excluded.
    binary = [
        r
        for r in rows
        if set(r["expected"]) <= DETERIORATING or not set(r["expected"]) & DETERIORATING
    ]
    expected_det = [set(r["expected"]) <= DETERIORATING for r in binary]
    predicted_det = [r["classification"] in DETERIORATING for r in binary]
    tp = sum(e and p for e, p in zip(expected_det, predicted_det, strict=True))
    fp = sum(p and not e for e, p in zip(expected_det, predicted_det, strict=True))
    fn = sum(e and not p for e, p in zip(expected_det, predicted_det, strict=True))
    signals = {}
    for family in ("repayment", "cashflow", "exposure"):
        labelled = [r for r in rows if family in r["expected_signals"]]
        stp = sum(
            r["expected_signals"][family] and family in r["elevated_families"] for r in labelled
        )
        sfp = sum(
            not r["expected_signals"][family] and family in r["elevated_families"] for r in labelled
        )
        sfn = sum(
            r["expected_signals"][family] and family not in r["elevated_families"] for r in labelled
        )
        signals[family] = {
            "labelled": len(labelled),
            "precision": _ratio(stp, stp + sfp),
            "recall": _ratio(stp, stp + sfn),
            "false_positives": sfp,
            "missed": sfn,
        }
    review_labelled = [r for r in rows if r["expected_human_review"] is not None]
    must = sum(len(r["must_select_missing"]) == 0 for r in rows)
    must_not = sum(len(r["must_not_select_violations"]) == 0 for r in rows)
    refs = sum(r["evidence_refs"] for r in rows)
    data_rows = [r for r in rows if r["data_issue_expected"]]
    reinv = sum(r["reinvestigation_challenges"] for r in rows)
    return {
        "scenarios": n,
        "classification_accuracy": _ratio(sum(r["correct"] for r in rows), n),
        "per_class": per_class,
        "deterioration_detection": {
            "precision": _ratio(tp, tp + fp),
            "recall": _ratio(tp, tp + fn),
            "false_positives": fp,
            "missed_deterioration": fn,
            "excluded_ambiguous_labels": n - len(binary),
        },
        "signal_detection": signals,
        "human_review_routing_accuracy": _ratio(
            sum(r["human_review"] == r["expected_human_review"] for r in review_labelled),
            len(review_labelled),
        ),
        "specialist_selection": {
            "required_specialists_selected": _ratio(must, n),
            "excluded_specialists_avoided": _ratio(must_not, n),
            "mean_specialists_per_case": round(
                sum(len(r["selected_specialists"]) for r in rows) / n, 2
            ),
        },
        "scenarios_with_parallel_execution": sum(r["parallel_waves"] > 0 for r in rows),
        "delegations_authorised": sum(r["delegations_authorised"] for r in rows),
        "duplicate_requests_blocked": sum(r["duplicate_requests_blocked"] for r in rows),
        "duplicate_tasks_executed": sum(r["duplicate_tasks_executed"] for r in rows),
        "evidence_reference_validity": _ratio(sum(r["evidence_refs_valid"] for r in rows), refs),
        "evidence_references": refs,
        "data_issue_discovery_recall": _ratio(
            sum(r["data_issue_found"] for r in data_rows), len(data_rows)
        ),
        "challenges_raised": sum(r["challenges"] for r in rows),
        "reinvestigation_effectiveness": _ratio(
            sum(r["reinvestigation_addressed"] for r in rows), reinv
        ),
        "terminated_cleanly": _ratio(sum(r["terminated_cleanly"] for r in rows), n),
        "multi_round_low_risk_cases": sum(
            r["rounds"] > 1 and "low_risk" in r["expected"] for r in rows
        ),
        "mean_rounds": round(sum(r["rounds"] for r in rows) / n, 2),
        "mean_latency_seconds": round(sum(r["latency_seconds"] for r in rows) / n, 3),
        "llm_calls": sum(r["llm_calls"] for r in rows),
        "llm_tokens": sum(r["llm_tokens"] for r in rows),
        "llm_cost_usd": round(sum(r["llm_cost_usd"] for r in rows), 6),
    }


def run_suites() -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "junit.xml"
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                f"--junitxml={report}",
                *SUITES,
            ],
            cwd=BACKEND,
            capture_output=True,
            text=True,
            check=False,
        )
        cases = []
        if report.exists():
            for case in ET.parse(report).getroot().iter("testcase"):
                failed = case.find("failure") is not None or case.find("error") is not None
                skipped = case.find("skipped") is not None
                cases.append(
                    {
                        "suite": case.get("classname", "").rsplit(".", 1)[-1],
                        "test": case.get("name"),
                        "outcome": "failed" if failed else "skipped" if skipped else "passed",
                    }
                )
    counts = Counter(c["outcome"] for c in cases)
    return {
        "exit_code": proc.returncode,
        "counts": dict(counts),
        "cases": cases,
        "summary_line": (proc.stdout.strip().splitlines() or [""])[-1],
    }


def _yes(flag) -> str:
    return "yes" if flag else "no"


def report_markdown(result: dict) -> str:
    s, meta = result["summary"], result["meta"]
    lines = [
        "# CredX evaluation report",
        "",
        f"Generated {meta['generated_at']} from commit `{meta['commit']}` "
        f"({meta['platform']}, Python {meta['python']}). LLM mode: **{meta['llm_mode']}**.",
        "",
        "All figures below were computed by `python -m evaluation.run_eval` in this run. The "
        "scenarios are synthetic and labelled by design (see `evaluation/scenarios.py`); they "
        "test behaviour, not predictive accuracy on real portfolios.",
        "",
        "## Financial classification (development scenarios)",
        "",
        "These scenarios were used while developing the scoring and challenge rules, so their "
        "results overstate generalisation. See the held-out section for a fairer measure.",
        "",
        f"- Scenarios: {s['scenarios']}; classification accuracy "
        f"{s['classification_accuracy']:.0%}",
        f"- Deterioration detection: precision {s['deterioration_detection']['precision']}, "
        f"recall {s['deterioration_detection']['recall']}, false positives "
        f"{s['deterioration_detection']['false_positives']}, missed deterioration "
        f"{s['deterioration_detection']['missed_deterioration']} (scenarios with mixed "
        f"labels excluded: {s['deterioration_detection']['excluded_ambiguous_labels']})",
        "",
        "| Signal family | Labelled | Precision | Recall | False positives | Missed |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for family, m in s["signal_detection"].items():
        lines.append(
            f"| {family} | {m['labelled']} | {m['precision']} | {m['recall']} | "
            f"{m['false_positives']} | {m['missed']} |"
        )
    lines += [
        "",
        "| Scenario | Expected | Classified | Severity | Path | Analyst pause | "
        "Specialists | Rounds | Correct |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in result["scenarios"]:
        agents = ", ".join(a.split("_")[0] for a in r["selected_specialists"])
        lines.append(
            f"| {r['scenario']} | {' / '.join(r['expected'])} | {r['classification']} | "
            f"{r['severity']} | {r['pathway']} | "
            f"{_yes(r['paused_for_analyst_before_adjudication'])} | "
            f"{agents} | {r['rounds']} | {'yes' if r['correct'] else '**no**'} |"
        )
    h, sel = result["held_out"]["summary"], s["specialist_selection"]
    lines += [
        "",
        "## Held-out scenarios",
        "",
        "Written and labelled before the system was first run on them, and not used for "
        "tuning afterwards.",
        "",
        f"- Scenarios: {h['scenarios']}; accuracy {h['classification_accuracy']:.0%}",
        f"- Deterioration detection: precision {h['deterioration_detection']['precision']}, "
        f"recall {h['deterioration_detection']['recall']}, false positives "
        f"{h['deterioration_detection']['false_positives']}, missed deterioration "
        f"{h['deterioration_detection']['missed_deterioration']} (scenarios with mixed "
        f"labels excluded: {h['deterioration_detection']['excluded_ambiguous_labels']})",
        "",
        "| Scenario | Expected | Classified | Severity | Path | Specialists | Correct |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in result["held_out"]["scenarios"]:
        agents = ", ".join(a.split("_")[0] for a in r["selected_specialists"])
        lines.append(
            f"| {r['scenario']} | {' / '.join(r['expected'])} | {r['classification']} | "
            f"{r['severity']} | {r['pathway']} | {agents} | "
            f"{'yes' if r['correct'] else '**no**'} |"
        )
    lines += [
        "",
        "## Agent loop (development scenarios)",
        "",
        f"- Required specialists selected: {sel['required_specialists_selected']:.0%}; "
        f"excluded specialists avoided: {sel['excluded_specialists_avoided']:.0%}; "
        f"mean specialists per case {sel['mean_specialists_per_case']}",
        f"- Scenarios with parallel execution: {s['scenarios_with_parallel_execution']}",
        f"- Specialist-initiated delegations authorised: {s['delegations_authorised']}; duplicate "
        f"requests blocked: {s['duplicate_requests_blocked']}; duplicate tasks executed: "
        f"{s['duplicate_tasks_executed']}",
        f"- Challenges raised: {s['challenges_raised']}; re-investigations that addressed their "
        f"challenge: {s['reinvestigation_effectiveness']}",
        f"- Evidence references: {s['evidence_references']}, valid "
        f"{s['evidence_reference_validity']:.0%}; data-issue discovery recall "
        f"{s['data_issue_discovery_recall']:.0%}",
        f"- Clean termination: {s['terminated_cleanly']:.0%}; mean rounds {s['mean_rounds']}; "
        f"low-risk cases needing more than one round: {s['multi_round_low_risk_cases']}",
        f"- Human-review routing matched labels: {s['human_review_routing_accuracy']:.0%}",
        f"- Mean latency per investigation: {s['mean_latency_seconds']} s; LLM calls "
        f"{s['llm_calls']}, tokens {s['llm_tokens']}, cost USD {s['llm_cost_usd']} "
        "(cost is 0 unless LLM_*_COST_PER_MTOK are configured)",
        "",
        "## Security, human-in-the-loop and agent-loop test suites",
        "",
        f"`{result['suites']['summary_line']}`",
        "",
        "| Suite | Passed | Failed |",
        "| --- | --- | --- |",
    ]
    by_suite: dict[str, Counter] = {}
    for c in result["suites"]["cases"]:
        by_suite.setdefault(c["suite"], Counter())[c["outcome"]] += 1
    for suite, counts in sorted(by_suite.items()):
        lines.append(f"| {suite} | {counts['passed']} | {counts['failed']} |")
    failures = [c for c in result["suites"]["cases"] if c["outcome"] == "failed"]
    if failures:
        lines += ["", "Failed tests:", ""] + [f"- {c['suite']}::{c['test']}" for c in failures]
    incorrect = [
        r for r in result["scenarios"] + result["held_out"]["scenarios"] if not r["correct"]
    ]
    lines += [
        "",
        "## Limitations",
        "",
        "- Synthetic scenarios cannot establish predictive validity; no claim is made "
        "about default prediction.",
        "- Pre-adjudication pauses were answered by a scripted analyst that adds no "
        "context; real reviewer input changes outcomes.",
    ]
    if incorrect:
        lines.append("- Misclassified scenarios: " + ", ".join(r["scenario"] for r in incorrect))
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--llm", action="store_true", help="use the configured Nebius model")
    parser.add_argument("--skip-suites", action="store_true")
    args = parser.parse_args()
    sys.path.insert(0, str(BACKEND))
    from fastapi.testclient import TestClient

    from app.services.ingestion import load_dataset
    from evaluation.scenarios import HELD_OUT, SCENARIOS, build_records

    with tempfile.TemporaryDirectory() as tmp:
        app = _app(Path(tmp), args.llm)
        llm_mode = "nebius" if args.llm and app.state.settings.nebius_api_key else "disabled"
        with TestClient(app) as client:
            headers = _analyst(client, app)
            with app.state.session_factory() as db:
                for scenario in [*SCENARIOS, *HELD_OUT]:
                    load_dataset(db, build_records(scenario, scenario.key))
                db.commit()
            rows = [run_scenario(client, app, headers, s) for s in SCENARIOS]
            held_out = [run_scenario(client, app, headers, s) for s in HELD_OUT]
        app.state.engine.dispose()
    commit = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    result = {
        "meta": {
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "commit": commit or "unknown",
            "llm_mode": llm_mode,
            "model": app.state.settings.nebius_model if llm_mode == "nebius" else None,
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
        "summary": summarise(rows),
        "scenarios": rows,
        # Written and labelled before the system was first run on them; never tuned against.
        "held_out": {"summary": summarise(held_out), "scenarios": held_out},
        "suites": {"skipped": True, "cases": [], "summary_line": "not run"}
        if args.skip_suites
        else run_suites(),
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    suffix = "_llm" if llm_mode == "nebius" else ""
    (RESULTS / f"eval_results{suffix}.json").write_text(json.dumps(result, indent=2) + "\n")
    (RESULTS / f"EVALUATION_REPORT{suffix}.md").write_text(report_markdown(result))
    print(json.dumps(result["summary"], indent=2))
    return 1 if result["suites"].get("counts", {}).get("failed", 0) else 0


if __name__ == "__main__":
    raise SystemExit(main())
