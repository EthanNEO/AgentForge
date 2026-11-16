"""Smallest honest online path: model output, artifact write, deterministic validation."""
from __future__ import annotations


SCENARIO = {
    "id": "model_smoke",
    "name": "真实模型最小验证",
    "desc": "用一个真实模型 Worker 验证网关、调度、文件写入、事件与独立 Schema 校验",
    "task_default": "调用当前可用的真实模型，生成一份 AgentForge 在线链路验收结果",
    "nodes": [
        {
            "id": "model_check",
            "name": "在线模型验收",
            "role": "analyst",
            "deps": [],
            "outputs": ["model_result.json"],
            "validator": "json",
            "required": ["status", "summary"],
            "levels": ["l1"],
            "max_attempts": 2,
            "desc": (
                "生成严格 JSON：status 必须为 ok，summary 简要说明本次输出来自真实在线模型，"
                "可选包含 checks 字符串数组"
            ),
        }
    ],
}


SYSTEM_PROMPTS = {
    "analyst": (
        "你是 AgentForge 在线模型验收 Agent。严格遵守输出 Schema，只生成简短 JSON，"
        "不得使用 Markdown 代码围栏或添加解释。"
    ),
}


def produce(node: dict, ctx: dict, attempt: int) -> dict:
    raise RuntimeError("model_smoke requires live model mode")
