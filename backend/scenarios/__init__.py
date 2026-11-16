"""场景注册表：平台与场景解耦，新增业务只加一个模块。"""
from __future__ import annotations

from importlib import import_module

SCENARIO_IDS = ["software_delivery", "aiops", "model_smoke"]


def get(scenario_id: str):
    sid = scenario_id if scenario_id in SCENARIO_IDS else SCENARIO_IDS[0]
    return import_module(f"scenarios.{sid}")


def all_scenarios() -> list[dict]:
    items = []
    for sid in SCENARIO_IDS:
        mod = import_module(f"scenarios.{sid}")
        sc = mod.SCENARIO
        items.append({
            "id": sc["id"],
            "name": sc["name"],
            "desc": sc["desc"],
            "task_default": sc["task_default"],
            "node_count": len(sc["nodes"]),
            "nodes": sc["nodes"],
        })
    return items
