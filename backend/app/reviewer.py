"""Independent verification result contracts."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VerificationReport:
    passed: bool
    reviewer_id: str
    producer_session_id: str
    evidence: dict
    reasons: tuple[str, ...]
    provenance: str
    score: float | None = None

    def __post_init__(self):
        if self.reviewer_id == self.producer_session_id:
            raise ValueError("产出者不能验证自己的结果")
