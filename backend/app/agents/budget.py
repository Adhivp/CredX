"""Thread-safe execution budget for one investigation run.

A run starts when an investigation is initiated, resumed by a human or reopened by
monitoring; every run is bounded. Exhausting any limit stops the loop with an
incomplete outcome instead of forcing a conclusion.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from app.services.policy import BudgetPolicy


@dataclass
class Budget:
    limits: BudgetPolicy
    started: float = field(default_factory=time.monotonic)
    rounds: int = 0
    tasks: int = 0
    llm_calls: int = 0
    tokens: int = 0
    cost_usd: float = 0.0
    challenge_cycles: int = 0
    exhausted_reason: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _exhaust(self, reason: str) -> bool:
        if self.exhausted_reason is None:
            self.exhausted_reason = reason
        return False

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def check_wall(self) -> bool:
        if self.elapsed() > self.limits.max_wall_seconds:
            return self._exhaust("max_wall_seconds")
        return True

    def start_round(self) -> bool:
        with self._lock:
            if self.rounds >= self.limits.max_rounds:
                return self._exhaust("max_rounds")
            self.rounds += 1
            return True

    def reserve_task(self) -> bool:
        with self._lock:
            if self.tasks >= self.limits.max_tasks:
                return self._exhaust("max_tasks")
            self.tasks += 1
            return True

    def start_challenge_cycle(self) -> bool:
        with self._lock:
            if self.challenge_cycles >= self.limits.max_challenge_cycles:
                return False
            self.challenge_cycles += 1
            return True

    def reserve_llm_call(self) -> bool:
        """LLM exhaustion disables reasoning but does not end the deterministic investigation."""
        with self._lock:
            if self.llm_calls >= self.limits.max_llm_calls:
                return False
            if self.tokens >= self.limits.max_tokens or self.cost_usd >= self.limits.max_cost_usd:
                return False
            self.llm_calls += 1
            return True

    def add_usage(self, tokens: int, cost: float) -> None:
        with self._lock:
            self.tokens += tokens
            self.cost_usd += cost

    @property
    def exhausted(self) -> bool:
        return self.exhausted_reason is not None

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "rounds": self.rounds,
                "tasks": self.tasks,
                "llm_calls": self.llm_calls,
                "tokens": self.tokens,
                "cost_usd": round(self.cost_usd, 6),
                "challenge_cycles": self.challenge_cycles,
                "elapsed_seconds": round(self.elapsed(), 3),
                "exhausted_reason": self.exhausted_reason,
                "limits": self.limits.model_dump(),
            }
