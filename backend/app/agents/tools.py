"""Least-privilege tool gateway.

Agents reach data only through named, typed, read-only tools. Each agent has an
explicit allowlist, and each data tool checks that its source table is inside the
task's permitted data scope. There is no SQL, shell, network or write tool; a
customer-level context only holds that one customer's snapshot, so cross-customer
access is structurally impossible.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from app.agents.contracts import AgentId, ContextItem, TaskSpec
from app.agents.llm import LLMGateway
from app.intelligence import indicators
from app.intelligence.data import CustomerSnapshot
from app.intelligence.legacy import core_rule_scores
from app.services.policy import PolicyConfig


class ToolPermissionError(PermissionError):
    pass


_DATA_TOOLS = {
    "repayment_history": "repayment_records",
    "cashflow_history": "cashflow_records",
    "exposure_snapshots": "customer_exposures",
    "loan_facilities": "loan_facilities",
    "customer_profile": "customer_profile",
    "human_context": "human_notes",
    "calc.portfolio_profile": "portfolio_aggregates",
}

TOOL_ALLOWLIST: dict[AgentId, frozenset[str]] = {
    AgentId.REPAYMENT: frozenset(
        {
            "repayment_history",
            "loan_facilities",
            "customer_profile",
            "calc.repayment_profile",
            "calc.core_rules",
            "llm.interpret",
        }
    ),
    AgentId.CASHFLOW: frozenset(
        {
            "cashflow_history",
            "customer_profile",
            "human_context",
            "calc.cashflow_profile",
            "llm.interpret",
        }
    ),
    AgentId.EXPOSURE: frozenset(
        {
            "exposure_snapshots",
            "loan_facilities",
            "cashflow_history",
            "calc.exposure_profile",
            "calc.cashflow_profile",
            "calc.obligation_pressure",
            "llm.interpret",
        }
    ),
    AgentId.BASELINE: frozenset(
        {
            "repayment_history",
            "cashflow_history",
            "human_context",
            "calc.repayment_profile",
            "calc.cashflow_profile",
            "calc.behavioural_baseline",
            "llm.interpret",
        }
    ),
    AgentId.INTEGRITY: frozenset(
        {
            "repayment_history",
            "cashflow_history",
            "exposure_snapshots",
            "loan_facilities",
            "calc.data_integrity",
            "llm.interpret",
        }
    ),
    AgentId.PORTFOLIO: frozenset({"calc.portfolio_profile", "llm.interpret"}),
}


@dataclass
class CalculationCache:
    """Shared by all tasks of one wave so identical calculations are computed once."""

    snapshot: CustomerSnapshot | None
    policy: PolicyConfig
    portfolio_loader: Callable[[], indicators.Calculation] | None = None
    _values: dict[str, indicators.Calculation] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def get(self, name: str) -> indicators.Calculation:
        with self._lock:
            if name not in self._values:
                self._values[name] = self._compute(name)
            return self._values[name]

    def all(self) -> dict[str, indicators.Calculation]:
        with self._lock:
            return dict(self._values)

    def _compute(self, name: str) -> indicators.Calculation:
        s, d = self.policy.scoring, self.policy.data
        if name == "portfolio_profile":
            if self.portfolio_loader is None:
                raise ToolPermissionError("No portfolio scope")
            return self.portfolio_loader()
        snap = self.snapshot
        if snap is None:
            raise ToolPermissionError("No customer scope")
        if name == "repayment_profile":
            return indicators.repayment_profile(snap)
        if name == "cashflow_profile":
            return indicators.cashflow_profile(snap, s.cashflow_decline_threshold)
        if name == "exposure_profile":
            return indicators.exposure_profile(snap)
        if name == "obligation_pressure":
            return indicators.obligation_pressure(
                self.get("exposure_profile"), self.get("cashflow_profile")
            )
        if name == "behavioural_baseline":
            return indicators.behavioural_baseline(
                snap,
                self.get("repayment_profile"),
                self.get("cashflow_profile"),
                d.min_repayment_records,
                d.min_cashflow_months,
                s.recovery_clean_periods,
            )
        if name == "data_integrity":
            return indicators.data_integrity(
                snap, d.min_repayment_records, d.min_cashflow_months, d.stale_after_days
            )
        if name == "core_rules":
            return core_rule_scores(snap)
        raise ToolPermissionError(f"Unknown calculation {name}")


@dataclass
class ToolContext:
    agent_id: AgentId
    task: TaskSpec
    cache: CalculationCache
    llm: LLMGateway
    calls: list[dict] = field(default_factory=list)

    def call(self, tool: str):
        allowed = TOOL_ALLOWLIST.get(self.agent_id, frozenset())
        if tool not in allowed:
            self.calls.append({"tool": tool, "allowed": False})
            raise ToolPermissionError(f"{self.agent_id} may not use {tool}")
        source = _DATA_TOOLS.get(tool)
        if source and source not in self.task.permitted_data:
            self.calls.append({"tool": tool, "allowed": False})
            raise ToolPermissionError(f"{tool} is outside the task data scope")
        started = time.monotonic()
        result = self._dispatch(tool)
        self.calls.append(
            {"tool": tool, "allowed": True, "ms": int((time.monotonic() - started) * 1000)}
        )
        return result

    def calculation(self, name: str) -> indicators.Calculation:
        return self.call(f"calc.{name}")

    def _dispatch(self, tool: str):
        snap = self.cache.snapshot
        if tool.startswith("calc."):
            return self.cache.get(tool.removeprefix("calc."))
        if snap is None:
            raise ToolPermissionError("No customer scope for this task")
        if tool == "repayment_history":
            return snap.repayments
        if tool == "cashflow_history":
            return snap.cashflows
        if tool == "exposure_snapshots":
            return snap.exposures
        if tool == "loan_facilities":
            return snap.loans
        if tool == "customer_profile":
            # Deliberately excludes the customer reference: agents work on a pseudonym.
            return {
                "customer_type": snap.customer_type,
                "segment": snap.segment,
                "employment_category": snap.employment_category,
            }
        if tool == "human_context":
            return list(self.task.context)
        raise ToolPermissionError(f"Unknown tool {tool}")

    def context_items(self) -> list[ContextItem]:
        if "human_context" not in TOOL_ALLOWLIST.get(self.agent_id, frozenset()):
            return []
        return self.call("human_context")
