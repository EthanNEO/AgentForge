"""动态调度器：运行时按能力匹配、历史成功率、负载、成本四维打分路由。

与静态流程编排的区别：DAG 节点只声明「需要什么角色」，
具体由哪个 Agent 执行在运行时决定，并支持熔断降级与 fallback 链。
"""
from __future__ import annotations

from . import config, db
from .db import json_load

PRICE_CEILING = 0.02


class Scheduler:
    """同一进程内维护「本次运行」的调度视图。

    跨运行的累计统计只用于 Agent 面板展示，
    调度打分使用会话内实时数据 + 能力先验，保证每次演示行为一致且可解释。
    """

    def __init__(self):
        self.inflight: dict[str, int] = {}
        self.session: dict[str, dict] = {}

    def reset_session(self):
        self.inflight.clear()
        self.session.clear()

    def _load(self, agent_id: str, capacity: int = 3) -> float:
        return min(1.0, self.inflight.get(agent_id, 0) / max(1, capacity))

    def _session(self, agent_id: str) -> dict:
        return self.session.setdefault(agent_id, {"ok": 0, "fail": 0, "consecutive": 0})

    def rank(self, role: str, node_name: str = "", need_tools: int = 0) -> list[dict]:
        rows = db.query("SELECT * FROM agents WHERE role=? AND status!='offline'", (role,))
        w = config.SCHEDULER_WEIGHTS
        ranked = []
        for r in rows:
            skills = json_load(r["skills"], {})
            capability = float(skills.get(role, 0.5))
            s = self._session(r["id"])
            success_rate = (s["ok"] + 1) / (s["ok"] + s["fail"] + 2)
            load = self._load(r["id"])
            cost_norm = min(1.0, (r["cost_per_call"] or 0) / PRICE_CEILING)

            penalty, breaker = 0.0, "closed"
            if s["consecutive"] >= 2:
                penalty, breaker = config.CIRCUIT_PENALTY, "circuit_open"
            elif s["consecutive"] == 1:
                penalty, breaker = config.DEGRADE_PENALTY, "degraded"

            score = (
                w["capability"] * capability
                + w["success"] * success_rate
                + w["load"] * (1 - load)
                + w["cost"] * (1 - cost_norm)
                - penalty
            )
            reason = (
                f"能力 {capability:.2f}×{w['capability']} + 成功率 {success_rate:.2f}×{w['success']}"
                f" + 空闲 {1 - load:.2f}×{w['load']} + 成本优势 {1 - cost_norm:.2f}×{w['cost']}"
            )
            if penalty:
                reason += f" − 降级罚分 {penalty}"
            ranked.append({
                "agent_id": r["id"],
                "agent_name": r["name"],
                "model": r["model"],
                "score": round(score, 4),
                "breaker": breaker,
                "reason": reason,
                "factors": {
                    "capability": round(capability, 3),
                    "success_rate": round(success_rate, 3),
                    "load": round(load, 3),
                    "cost_per_1k": r["cost_per_call"],
                    "penalty": penalty,
                },
            })
        ranked.sort(key=lambda x: -x["score"])
        return ranked

    def pick(self, role: str, node_name: str = "") -> tuple[dict | None, list[dict]]:
        ranked = self.rank(role, node_name)
        if not ranked:
            return None, []
        best = ranked[0]
        self.inflight[best["agent_id"]] = self.inflight.get(best["agent_id"], 0) + 1
        return best, ranked

    def release(self, agent_id: str):
        if agent_id in self.inflight and self.inflight[agent_id] > 0:
            self.inflight[agent_id] -= 1

    def report(self, agent_id: str, ok: bool, tokens: int = 0, cost: float = 0.0):
        s = self._session(agent_id)
        if ok:
            s["ok"] += 1
            s["consecutive"] = 0
        else:
            s["fail"] += 1
            s["consecutive"] += 1

        row = db.one("SELECT * FROM agents WHERE id=?", (agent_id,))
        if not row:
            return
        tokens, cost = int(tokens or 0), round(float(cost or 0.0), 6)
        if ok:
            db.execute(
                "UPDATE agents SET success=success+1, consecutive_failure=0, status='ready', "
                "total_tokens=total_tokens+?, total_cost=total_cost+? WHERE id=?",
                (tokens, cost, agent_id),
            )
        else:
            db.execute(
                "UPDATE agents SET failure=failure+1, consecutive_failure=consecutive_failure+1, "
                "total_tokens=total_tokens+?, total_cost=total_cost+? WHERE id=?",
                (tokens, cost, agent_id),
            )
            after = db.one("SELECT consecutive_failure FROM agents WHERE id=?", (agent_id,))
            if after and after["consecutive_failure"] >= 2:
                db.execute("UPDATE agents SET status='degraded' WHERE id=?", (agent_id,))

    def summary(self) -> list[dict]:
        rows = db.query("SELECT * FROM agents ORDER BY role, id")
        for r in rows:
            total = (r["success"] or 0) + (r["failure"] or 0)
            r["success_rate"] = round((r["success"] + 1) / (total + 2), 3)
            r["skills"] = json_load(r["skills"], {})
            r["tools"] = json_load(r["tools"], [])
        return rows


def seed_agents():
    agents = [
        ("analyst-01", "需求分析师·析言", "analyst", "把自然语言需求拆成可验收的条目",
         "deepseek-flash", {"analyst": 0.88}, 0.004, 1.2),
        ("tester-01", "测试工程师·断言", "tester", "依据验收标准设计可执行测试",
         "mimo-v2.6-pro", {"tester": 0.9}, 0.003, 1.5),
        ("coder-fast", "编码助手·疾风", "coder", "低成本快速出码，适合常规实现",
         "mimo-v2.6-pro", {"coder": 0.78}, 0.002, 2.4),
        ("coder-pro", "编码专家·磐石", "coder", "强推理模型，擅长修复复杂逻辑缺陷",
         "deepseek-v4-pro", {"coder": 0.95}, 0.02, 6.1),
        ("triage-01", "值班分诊·哨兵", "triage", "告警定级与影响面圈定",
         "deepseek-flash", {"triage": 0.88}, 0.003, 1.1),
        ("diag-01", "故障诊断·溯源", "diagnostician", "证据链分析与根因定位",
         "deepseek-v4-pro", {"diagnostician": 0.91}, 0.018, 4.6),
        ("ops-fast", "运维执行·快修", "operator", "低成本处置脚本，适合常规变更",
         "mimo-v2.6-pro", {"operator": 0.75}, 0.002, 2.6),
        ("ops-pro", "运维专家·稳手", "operator", "强推理模型，擅长处理复杂根因",
         "deepseek-v4-pro", {"operator": 0.94}, 0.02, 5.8),
        ("verify-01", "独立验证·核验", "verifier", "执行健康检查并留存证据",
         "mimo-v2.6-pro", {"verifier": 0.92}, 0.003, 1.4),
        ("reviewer-01", "交付评审·明鉴", "reviewer", "独立复核产出与验收标准的一致性",
         "deepseek-v4-pro", {"reviewer": 0.86}, 0.02, 2.8),
    ]
    for aid, name, role, desc, model, skills, cost, latency in agents:
        # 只补缺，不覆盖：用户在配置面板对种子 Agent 的修改跨重启保留。
        db.execute(
            """INSERT INTO agents(id,name,role,description,model,skills,tools,
               cost_per_call,avg_latency,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO NOTHING""",
            (aid, name, role, desc, model,
             __import__("json").dumps(skills, ensure_ascii=False),
             '["write_file","run_tests","kb_search","run_script"]', cost, latency, db.now()),
        )


scheduler = Scheduler()
