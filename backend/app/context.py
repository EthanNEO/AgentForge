"""Role-scoped context assembly with cross-run experience retrieval."""
from __future__ import annotations

from dataclasses import dataclass

from . import db
from .models import ExecutionPlan, PlanNode


@dataclass(frozen=True)
class ContextSnapshot:
    task: str
    node_id: str
    upstream: str
    feedback: str
    lessons: tuple[str, ...]
    lesson_ids: tuple[int, ...]


class ContextAssembler:
    def build(self, node: PlanNode, plan: ExecutionPlan, completed: dict,
              feedback: str, scenario_id: str = "") -> ContextSnapshot:
        upstream_parts = []
        for dep in node.deps:
            for name, content in (completed.get(dep) or {}).items():
                upstream_parts.append(f"[{name}]\n{str(content)[:2400]}")
        lessons = db.query(
            "SELECT * FROM lessons WHERE signature LIKE ? ORDER BY hits DESC,id DESC LIMIT 3",
            (f"{node.id}:%",),
        )
        return ContextSnapshot(
            task=plan.task, node_id=node.id, upstream="\n\n".join(upstream_parts), feedback=feedback,
            lessons=tuple(row["patch"] for row in lessons),
            lesson_ids=tuple(row["id"] for row in lessons),
        )
