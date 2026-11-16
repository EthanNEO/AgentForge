"""Typed contracts shared by the AgentForge harness."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class PlanNode:
    id: str
    name: str
    role: str
    deps: tuple[str, ...]
    outputs: tuple[str, ...]
    validator: str
    max_attempts: int
    desc: str = ""
    config: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ExecutionPlan:
    scenario_id: str
    task: str
    mode: str
    nodes: list[PlanNode]
    version: int = 1
    provenance: str = "replay"
    planning_model: dict[str, str] = field(default_factory=dict)
    clarification: str = ""
    complexity: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WorkerSession:
    id: str
    run_id: str
    node_id: str
    attempt: int
    role: str
    agent_id: str
    workspace: str
    status: str
    started_at: float
    deadline: float
    provenance: str
    ended_at: float | None = None


@dataclass(frozen=True)
class WatchdogAction:
    kind: str
    session_id: str
    node_id: str
    reason: str
