"""Read model of an investigation's current state, used by the Director and Challenger.

Record rows are ordered by SQLite rowid (insertion order); timestamps have 1 s resolution.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models.investigation import (
    Adjudication,
    AgentFinding,
    AgentTask,
    EvidenceChallenge,
    HumanDecision,
    HumanNote,
    Hypothesis,
    Investigation,
)

FINDING_REJECTIONS = {"reject_finding", "flag_unsupported"}


@dataclass
class InvestigationView:
    investigation: Investigation
    hypotheses: list[Hypothesis]
    tasks: list[AgentTask]
    findings: list[AgentFinding]
    challenges: list[EvidenceChallenge]
    decisions: list[HumanDecision]
    adjudications: list[Adjudication]
    notes: list[HumanNote]

    def context_categories(self) -> list[str]:
        return sorted({n.category for n in self.notes if n.kind == "context" and n.category})

    @property
    def rejected_finding_ids(self) -> set[str]:
        latest: dict[str, str] = {}
        for d in self.decisions:
            if d.target_type == "finding":
                latest[d.target_id] = d.action
        return {fid for fid, action in latest.items() if action in FINDING_REJECTIONS}

    def active_findings(self) -> list[AgentFinding]:
        """Most recent finding per (agent, code, hypothesis), excluding human-rejected ones."""
        rejected = self.rejected_finding_ids
        latest: dict[tuple, AgentFinding] = {}
        for f in self.findings:  # insertion order
            latest[(f.agent_id, f.code, f.hypothesis_id)] = f
        return [f for f in latest.values() if f.id not in rejected]

    def hypothesis(self, code: str) -> Hypothesis | None:
        return next((h for h in self.hypotheses if h.code == code), None)

    def findings_for(self, hypothesis: Hypothesis) -> list[AgentFinding]:
        return [f for f in self.active_findings() if f.hypothesis_id == hypothesis.id]

    def tasks_for(self, agent_id: str, hypothesis_id: str | None = None) -> list[AgentTask]:
        return [
            t
            for t in self.tasks
            if t.agent_id == agent_id
            and (hypothesis_id is None or t.hypothesis_id == hypothesis_id)
        ]

    def agent_ran(self, agent_id: str) -> bool:
        return any(t.agent_id == agent_id and t.status == "completed" for t in self.tasks)

    def family_of(self, finding: AgentFinding) -> str | None:
        return json.loads(finding.metrics or "{}").get("family")


def load_view(db: Session, investigation: Investigation) -> InvestigationView:
    iid = investigation.id
    return InvestigationView(
        investigation=investigation,
        hypotheses=list(
            db.scalars(
                select(Hypothesis)
                .where(Hypothesis.investigation_id == iid)
                .order_by(Hypothesis.priority.desc(), Hypothesis.created_at)
            )
        ),
        tasks=list(
            db.scalars(
                select(AgentTask)
                .where(AgentTask.investigation_id == iid)
                .order_by(text("agent_tasks.rowid"))
            )
        ),
        findings=list(
            db.scalars(
                select(AgentFinding)
                .where(AgentFinding.investigation_id == iid)
                .order_by(text("agent_findings.rowid"))
            )
        ),
        challenges=list(
            db.scalars(
                select(EvidenceChallenge)
                .where(EvidenceChallenge.investigation_id == iid)
                .order_by(text("evidence_challenges.rowid"))
            )
        ),
        decisions=list(
            db.scalars(
                select(HumanDecision)
                .where(HumanDecision.investigation_id == iid)
                .order_by(text("human_decisions.rowid"))
            )
        ),
        adjudications=list(
            db.scalars(
                select(Adjudication)
                .where(Adjudication.investigation_id == iid)
                .order_by(text("adjudications.rowid"))
            )
        ),
        notes=list(
            db.scalars(
                select(HumanNote)
                .where(HumanNote.investigation_id == iid)
                .order_by(text("human_notes.rowid"))
            )
        ),
    )
