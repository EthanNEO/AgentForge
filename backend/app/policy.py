"""Least-privilege tool and workspace policy."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ToolPolicy:
    allowed: frozenset[str]

    @classmethod
    def for_role(cls, role: str) -> "ToolPolicy":
        read_only = {"read", "search", "kb_search", "mcp_read"}
        if role in {"reviewer", "verifier"}:
            return cls(frozenset(read_only | {"write", "run_tests"}))
        return cls(frozenset(read_only | {"write", "run_tests", "run_script"}))

    def authorize(self, tool_name: str, write: bool = False) -> bool:
        if write and tool_name not in {"write", "run_tests", "run_script"}:
            return False
        return tool_name in self.allowed

    def resolve_path(self, root: Path, relative: str) -> Path:
        base = root.resolve()
        candidate = (base / relative).resolve()
        if candidate != base and base not in candidate.parents:
            raise PermissionError(f"路径越界：{relative}")
        return candidate
