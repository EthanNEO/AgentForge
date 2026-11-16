"""Structured feedback decisions for failed verification."""
from __future__ import annotations

from dataclasses import dataclass

from .reviewer import VerificationReport


@dataclass(frozen=True)
class RetryDecision:
    action: str
    reason: str
    next_agent: str | None = None


class Optimizer:
    def decide(self, report: VerificationReport, attempt: int, max_attempts: int,
               current_agent: str, ranked_agents: list[str]) -> RetryDecision:
        if report.passed:
            return RetryDecision("accept", "independent verification passed")
        if attempt >= max_attempts:
            return RetryDecision("needs_attention", "attempt budget exhausted")
        alternative = next((agent for agent in ranked_agents if agent != current_agent), None)
        if alternative:
            return RetryDecision("switch_agent", "; ".join(report.reasons), alternative)
        return RetryDecision("revise_context", "; ".join(report.reasons), current_agent)
