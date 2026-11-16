"""Dependency readiness logic used by the bounded run supervisor."""
from __future__ import annotations

from .models import ExecutionPlan, PlanNode


def ready_nodes(plan: ExecutionPlan, states: dict[str, str]) -> list[PlanNode]:
    ready = []
    for node in plan.nodes:
        if node.id in states:
            continue
        if all(states.get(dep) == "passed" for dep in node.deps):
            ready.append(node)
    return ready
