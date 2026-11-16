"""执行引擎：按 DAG 拓扑顺序调度节点，串起调度器、验证器与 Self-Loop。"""
from __future__ import annotations

import json
import os
import re
import base64
import shutil
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import config, db, knowledge, selfloop, tools, validator
from .context import ContextAssembler
from .events import events
from .function_tools import CompositeToolExecutor, WorkspaceToolExecutor
from .github_mcp import GitHubMcpClient, GitHubMcpConfig
from .llm import GatewayError, LLMResult, ModelBudget, ModelBudgetExceeded, ToolRoundLimitError, llm, track_usage
from .models import WorkerSession
from .optimizer import Optimizer
from .planner import create_plan
from .policy import ToolPolicy
from .reviewer import VerificationReport
from .scheduler import Scheduler
from .uploads import describe_file
from .supervisor import ready_nodes
from .watchdog import Watchdog
import scenarios


class RunFailed(Exception):
    pass


class RepairTest(Exception):
    def __init__(self, feedback: str, evidence: dict):
        super().__init__(feedback)
        self.evidence = evidence


def extract_code(text: str, filename: str) -> str:
    blocks = re.findall(r"```(?:python|json|markdown|md)?\s*\n(.*?)```", text, re.S)
    if blocks:
        return max(blocks, key=len).strip()
    return text.strip()


class Engine:
    def __init__(self, run_id: str, task: str, scenario: dict, mode: str):
        self.run_id = run_id
        self.task = task
        self.scenario = scenario
        self.mode = mode
        self.model_budget = ModelBudget(config.EXECUTION_TOKEN_LIMIT) if mode == "live" else None
        self.mod = scenarios.get(scenario["id"])
        self.plan = create_plan(task, scenario, mode)
        self.context_assembler = ContextAssembler()
        self.optimizer = Optimizer()
        self.watchdog = Watchdog()
        self.scheduler = Scheduler()
        self.github_mcp = GitHubMcpClient(GitHubMcpConfig.from_env(os.environ))
        self.run_dir = config.RUNS_DIR / run_id
        self.work = self.run_dir / "artifacts"
        self.inputs = self.run_dir / "inputs"
        self.work.mkdir(parents=True, exist_ok=True)
        attachments = []
        if self.inputs.is_dir() and not self.inputs.is_symlink():
            attachments = [
                describe_file(path) for path in sorted(self.inputs.iterdir())
                if path.is_file() and not path.is_symlink()
            ]
        self.ctx: dict = {
            "task": task, "artifacts": {}, "validations": {}, "kb": {},
            "attachments": attachments,
        }
        self._attempt_numbers: dict[str, int] = {}
        self._test_repair_feedback = ""
        self.stats = {
            "tokens": 0, "cost": 0.0, "attempts": 0, "retries": 0,
            "nodes": 0, "lessons": 0, "switches": 0, "started": time.time(),
        }

    # ---------- 事件与持久化 ----------
    def emit(self, node_id: str, kind: str, message: str, payload: dict | None = None,
             level: str = "info", source: str = "supervisor", session_id: str = "",
             provenance: str = "deterministic"):
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO traces(run_id,node_id,kind,level,message,payload,ts) VALUES(?,?,?,?,?,?,?)",
                (self.run_id, node_id, kind, level, message,
                 json.dumps(payload or {}, ensure_ascii=False), db.now()),
            )
            events.append(
                self.run_id, kind, source,
                {"message": message, "level": level, **(payload or {})},
                node_id=node_id, session_id=session_id, provenance=provenance, conn=conn,
            )

    def _set_status(self, status: str, error: str | None = None):
        db.execute("UPDATE runs SET status=?, error=? WHERE id=?", (status, error, self.run_id))

    # ---------- 主流程 ----------
    def _resume_feedback(self, node_id: str) -> str:
        source = self.scenario.get("reuse_from")
        if not source:
            return ""
        step = db.one("SELECT status,verdict FROM steps WHERE run_id=? AND node_id=? ORDER BY id DESC LIMIT 1",
                      (source, node_id))
        if not step or step["status"] not in ("failed", "retrying"):
            return ""
        verdict = db.json_load(step["verdict"], {})
        if not verdict or verdict.get("passed"):
            return ""
        return selfloop.feedback_patch(
            {"patch": "以下是原运行的失败证据。保持已确认的验收要求，修复导致失败的行为。"},
            verdict,
        )

    def _reuse_passed_steps(self, source: str, states: dict, done: dict) -> None:
        """Reuse intact, validated intermediate files in dependency order."""
        original = config.RUNS_DIR / source / "artifacts"
        if not original.is_dir() or original.is_symlink():
            return
        for node in self._topo_sort():
            nid = node["id"]
            outputs = node["outputs"]
            if not outputs or any(dep not in states for dep in node["deps"]):
                continue
            step = db.one("SELECT * FROM steps WHERE run_id=? AND node_id=? ORDER BY id DESC LIMIT 1",
                          (source, nid))
            if not step or step["status"] != "passed":
                continue
            verdict = db.json_load(step["verdict"], {})
            if not isinstance(verdict, dict) or not verdict.get("passed"):
                continue
            paths = [original / name for name in outputs]
            if any(not path.is_file() or path.is_symlink() for path in paths):
                continue
            try:
                artifacts = {path.name: path.read_text(encoding="utf-8") for path in paths}
            except (OSError, UnicodeError):
                self.emit(nid, "step.reuse_skipped", "原运行产物无法读取，将重新执行该步骤",
                          {"source_run": source}, level="warning")
                continue
            if any(not content.strip() for content in artifacts.values()):
                continue
            for name, content in artifacts.items():
                tools.write_artifact(self.work, name, content)
            verdict = validator.validate(node, self.work, self.ctx)
            if not verdict["passed"]:
                for name in artifacts:
                    (self.work / name).unlink(missing_ok=True)
                continue
            self.ctx["artifacts"].update(artifacts)
            self.ctx["validations"][nid] = verdict
            states[nid] = "passed"
            done[nid] = {"node": node, "verdict": verdict, "attempts": 0, "agent": "复用"}
            self.stats["nodes"] += 1
            db.execute(
                "INSERT INTO steps(run_id,node_id,name,role,status,attempt,verdict,started_at,ended_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (self.run_id, nid, node["name"], node["role"], "passed", 0,
                 json.dumps(verdict, ensure_ascii=False), db.now(), db.now()),
            )
            self.emit(nid, "step.reused", "复用原运行已通过的步骤", {"source_run": source})

    def run(self):
        self._set_status("running")
        self.scheduler.reset_session()
        db.execute("UPDATE runs SET plan=?, plan_version=? WHERE id=?",
                   (json.dumps(self.plan.to_dict(), ensure_ascii=False), self.plan.version, self.run_id))
        self.emit("", "plan.created", "Planner 已生成并验证结构化执行计划",
                  {"plan": self.plan.to_dict()}, source="planner", provenance=self.plan.provenance)
        self.emit("", "state", f"任务受理，进入编排执行（模式：{'离线回放' if self.mode == 'mock' else '在线推理'}）")
        try:
            if hasattr(self.mod, "prepare"):
                self.mod.prepare(self.work, self.ctx)
                self.emit("", "state", "场景基线已就绪（服务配置与验收脚本）")
            done: dict[str, dict] = {}
            states: dict[str, str] = {}
            scenario_nodes = {node["id"]: node for node in self.scenario["nodes"]}
            if self.scenario.get("reuse_from"):
                self._reuse_passed_steps(self.scenario["reuse_from"], states, done)
            test_repaired = False
            while len(states) < len(self.plan.nodes):
                ready = ready_nodes(self.plan, states)
                if not ready:
                    blocked = [node.id for node in self.plan.nodes if node.id not in states]
                    raise RunFailed(f"没有可调度节点，依赖被阻塞：{blocked}")
                self.emit("", "supervisor.batch", f"Supervisor 派发 {len(ready)} 个就绪节点",
                          {"nodes": [node.id for node in ready], "parallel": len(ready) > 1},
                          source="supervisor")
                with ThreadPoolExecutor(max_workers=min(3, len(ready)),
                                        thread_name_prefix=f"af-{self.run_id}") as pool:
                    futures = {pool.submit(self._run_node, scenario_nodes[node.id], done): node
                               for node in ready}
                    for future in as_completed(futures):
                        node = futures[future]
                        try:
                            done[node.id] = future.result()
                        except RepairTest as exc:
                            if test_repaired:
                                raise RunFailed("测试设计回退次数已用完，请检查验证记录") from exc
                            test_repaired = True
                            invalid = {"test"}
                            for dependent in self._topo_sort():
                                if invalid.intersection(dependent["deps"]):
                                    invalid.add(dependent["id"])
                            for nid in sorted(invalid):
                                if states.pop(nid, None) == "passed":
                                    self.stats["nodes"] -= 1
                                done.pop(nid, None)
                                self.ctx["validations"].pop(nid, None)
                                for name in self._outputs_of(nid):
                                    (self.work / name).unlink(missing_ok=True)
                                    self.ctx["artifacts"].pop(name, None)
                            self.ctx.pop("baseline_tests", None)
                            self.ctx.pop("test_files", None)
                            self._test_repair_feedback = str(exc)
                            self.emit("test", "selfloop.rollback", "测试自身异常，回退至测试设计",
                                      {"invalidated": sorted(invalid), **exc.evidence}, level="warning")
                            continue
                        states[node.id] = "passed"
            self._finish("succeeded")
        except RunFailed as exc:
            self.emit("", "state", f"任务终止：{exc}", level="error")
            self._finish("failed", str(exc))
        except Exception as exc:  # noqa: BLE001
            self.emit("", "state", f"引擎异常：{exc}",
                      {"trace": traceback.format_exc()[-1500:]}, level="error")
            self._finish("failed", str(exc))

    def _finish(self, status: str, error: str | None = None):
        self.stats["duration"] = round(time.time() - self.stats["started"], 2)
        self.stats["tokens"] = int(self.stats["tokens"])
        self.stats["cost"] = round(self.stats["cost"], 4)
        db.execute(
            "UPDATE runs SET status=?, finished_at=?, stats=?, error=? WHERE id=?",
            (status, db.now(), json.dumps(self.stats, ensure_ascii=False), error, self.run_id),
        )
        self.emit("", "summary",
                  f"执行完成：{status}，共 {self.stats['attempts']} 次执行、{self.stats['retries']} 次自愈，"
                  f"消耗 {self.stats['tokens']} tokens / ¥{self.stats['cost']}",
                  self.stats,
                  level="success" if status == "succeeded" else "error")

    def _topo_sort(self) -> list[dict]:
        nodes = {n["id"]: n for n in self.scenario["nodes"]}
        order, visited = [], set()

        def visit(nid, stack=()):
            if nid in visited:
                return
            if nid in stack:
                raise RunFailed(f"工作流存在环：{nid}")
            for dep in nodes[nid]["deps"]:
                visit(dep, stack + (nid,))
            visited.add(nid)
            order.append(nodes[nid])

        for nid in nodes:
            visit(nid)
        return order

    def _run_node(self, node: dict, done: dict) -> dict:
        self.emit(node["id"], "state", f"节点就绪：{node['name']}")
        kb_hits = knowledge.kb.search(
            f"{node['name']} {node.get('desc','')} {self.scenario['id']}", k=2
        )
        self.ctx["kb"][node["id"]] = kb_hits
        if kb_hits:
            self.emit(node["id"], "kb", f"知识检索命中 {len(kb_hits)} 条",
                      {"hits": [{"title": h["title"], "score": h["score"]} for h in kb_hits]})

        handoff = []
        for dep in node.get("deps", []):
            for name in self._outputs_of(dep):
                content = self.ctx["artifacts"].get(name)
                if content is not None:
                    handoff.append({"file": name, "from_node": dep, "chars": len(content)})
        if handoff:
            self.emit(node["id"], "artifact.handoff",
                      "接收上游产物：" + "、".join(
                          f"{item['file']}（来自 {item['from_node']}，{item['chars']} 字符）"
                          for item in handoff),
                      {"files": handoff})

        attempt = 0
        patch = self._resume_feedback(node["id"])
        if node["id"] == "test" and self._test_repair_feedback:
            patch, self._test_repair_feedback = self._test_repair_feedback, ""
        last_agent = None
        while attempt < node["max_attempts"]:
            attempt += 1
            attempt_number = self._attempt_numbers.get(node["id"], 0) + 1
            self._attempt_numbers[node["id"]] = attempt_number
            self.stats["attempts"] += 1
            best, ranked = self.scheduler.pick(node["role"], node["name"])
            if not best:
                raise RunFailed(f"节点 {node['name']} 无可用 Agent（角色 {node['role']}）")

            if last_agent and best["agent_id"] != last_agent:
                self.stats["switches"] += 1
                self.emit(node["id"], "scheduler",
                          f"熔断改派：{last_agent} → {best['agent_name']}",
                          {"ranked": ranked[:3], "switched": True}, level="warning")
            else:
                self.emit(node["id"], "scheduler",
                          f"调度决策：{best['agent_name']}（{best['model']}）得分 {best['score']}",
                          {"ranked": ranked[:3], "chosen": best})

            step_id = db.execute(
                """INSERT INTO steps(run_id,node_id,name,role,agent_id,agent_name,status,attempt,
                   started_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                (self.run_id, node["id"], node["name"], node["role"], best["agent_id"],
                 best["agent_name"], "running", attempt_number, db.now()),
            )

            session_id = f"ws-{self.run_id}-{node['id']}-{attempt_number}-{uuid.uuid4().hex[:6]}"
            worker_dir = self.run_dir / "workers" / node["id"] / f"attempt-{attempt_number}"
            worker_dir.mkdir(parents=True, exist_ok=True)
            self._prepare_worker_workspace(worker_dir, node["role"], node)
            deadline = time.time() + float(node.get("timeout", 120))
            demo_spec = self.scenario.get("demo_inject") or {}
            demo_attempt = (demo_spec.get("node") == node["id"]
                            and int(demo_spec.get("attempt", 1)) == attempt_number)
            session_provenance = "demo" if demo_attempt else ("model" if self.mode == "live" else "replay")
            db.execute(
                "INSERT INTO worker_sessions(id,run_id,node_id,attempt,role,agent_id,workspace,status,"
                "provenance,started_at,deadline) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (session_id, self.run_id, node["id"], attempt_number, node["role"], best["agent_id"],
                 str(worker_dir), "running", session_provenance,
                 time.time(), deadline),
            )
            self.emit(node["id"], "worker.started",
                      f"独立 Worker 会话启动：{session_id}",
                      {"agent": best["agent_name"], "workspace": str(worker_dir), "deadline": deadline},
                      source="worker", session_id=session_id,
                      provenance=session_provenance)

            prompt, system = self._build_prompt(node, patch, attempt)
            self.ctx["agent_capability"] = best["factors"]["capability"]
            self.ctx["agent_id"] = best["agent_id"]
            started = time.time()
            injected = False
            try:
                injection = self._demo_injection(node, attempt_number)
                if injection is not None:
                    injected = True
                    artifacts, gen = injection
                else:
                    artifacts, gen = self._generate(
                        node, best, system, prompt, attempt, worker_dir=worker_dir,
                        session_id=session_id,
                    )
            except Exception as exc:  # noqa: BLE001
                self.scheduler.release(best["agent_id"])
                budget_stop = isinstance(exc, ModelBudgetExceeded)
                if budget_stop:
                    self.stats["tokens"] += exc.tokens
                    self.emit(node["id"], "budget.exhausted", str(exc), {
                        "used": exc.used, "limit": exc.limit, "attempt": attempt_number,
                    }, level="warning", session_id=session_id)
                else:
                    if not injected:
                        self.scheduler.report(best["agent_id"], False)
                    self.emit(node["id"], "llm", f"模型调用失败：{exc}", level="error")
                db.execute("UPDATE steps SET status='failed', tokens=?, latency=?, ended_at=? WHERE id=?",
                           (exc.tokens if budget_stop else 0, round(time.time() - started, 3),
                            db.now(), step_id))
                db.execute("UPDATE worker_sessions SET status='failed',ended_at=? WHERE id=?",
                           (time.time(), session_id))
                if budget_stop:
                    raise RunFailed(str(exc)) from exc
                patch = "模型调用失败，请重新生成完整产出。"
                last_agent = best["agent_id"]
                continue

            tokens, latency = gen["tokens"], round(time.time() - started, 3)
            cost = round(tokens / 1000 * (best["factors"]["cost_per_1k"] or 0), 6)
            self.stats["tokens"] += tokens
            self.stats["cost"] += cost
            self.ctx["agent_capability"] = best["factors"]["capability"]
            self.ctx["agent_id"] = best["agent_id"]

            overdue = self.watchdog.check(time.time(), [WorkerSession(
                session_id, self.run_id, node["id"], attempt_number, node["role"], best["agent_id"],
                str(worker_dir), "running", started, deadline,
                "model" if self.mode == "live" else "replay",
            )])
            if overdue:
                db.execute("UPDATE worker_sessions SET status='timed_out',ended_at=? WHERE id=?",
                           (time.time(), session_id))
                self.emit(node["id"], "watchdog.timeout", "Watchdog 检测到 Worker 超时",
                          {"session_id": session_id}, level="error", source="watchdog",
                          session_id=session_id, provenance="deterministic")
                self.scheduler.release(best["agent_id"])
                patch = selfloop.ATTRIBUTION_META[selfloop.TOOL_TIMEOUT]["patch"]
                last_agent = best["agent_id"]
                continue

            for name, content in artifacts.items():
                policy = ToolPolicy.for_role(node["role"])
                if not policy.authorize("write", write=True):
                    raise RunFailed(f"角色 {node['role']} 无权写入产物")
                policy.resolve_path(worker_dir, name)
                policy.resolve_path(self.work, name)
                self.emit(node["id"], "tool.authorized", f"工具策略允许写入 {name}",
                          {"tool": "write", "path": name, "role": node["role"]},
                          source="policy", session_id=session_id, provenance="deterministic")
                staged = tools.write_artifact(worker_dir, name, content)
                tools.write_artifact(self.work, name, staged.read_text(encoding="utf-8"))
            self.ctx["artifacts"].update(artifacts)
            db.execute("UPDATE worker_sessions SET status='completed',ended_at=? WHERE id=?",
                       (time.time(), session_id))
            self.emit(node["id"], "worker.completed", "Worker 产出已提交，等待独立验证",
                      {"files": list(artifacts), "model": gen.get("model"), "provider": gen.get("provider"),
                       "tokens": tokens, "attempt": attempt_number},
                      source="worker", session_id=session_id,
                      provenance=gen.get("source", "replay"))
            if artifacts:
                self.emit(node["id"], "artifact",
                          f"产出 {', '.join(artifacts.keys())}",
                          {"files": {k: len(v) for k, v in artifacts.items()},
                           "tokens": tokens, "cost": cost})
            else:
                self.emit(node["id"], "artifact", "本节点无文件产出，执行验证脚本",
                          {"tokens": tokens, "cost": cost})

            verdict = validator.validate(node, self.work, self.ctx)
            report = VerificationReport(
                passed=bool(verdict["passed"]), reviewer_id=f"verifier-{node['id']}-{attempt_number}",
                producer_session_id=session_id,
                evidence={"l1": verdict.get("l1"), "l2": verdict.get("l2"), "l3": verdict.get("l3")},
                reasons=tuple(verdict.get("reasons") or ()), provenance="deterministic",
                score=verdict.get("l2", {}).get("score"),
            )
            self.emit(node["id"], "verification.completed",
                      "独立 Verifier 通过" if report.passed else "独立 Verifier 拒绝产出",
                      {"reviewer_id": report.reviewer_id, "producer_session_id": session_id,
                       "passed": report.passed, "reasons": list(report.reasons),
                       "evidence": report.evidence},
                      level="success" if report.passed else "error", source="verifier",
                      session_id=session_id, provenance=report.provenance)
            l1 = verdict["l1"]
            if l1["ok"] and l1.get("checks"):
                self.emit(node["id"], "validate", f"L1 通过：{l1['checks'][0]}",
                          {"l1": l1.get("detail"), "stdout": l1.get("stdout", "")[-1200:]},
                          level="success")
            elif not l1["ok"]:
                self.emit(node["id"], "validate", f"L1 未通过：{l1.get('detail','')}",
                          {"failures": l1.get("failures", []),
                           "stderr": l1.get("stderr", "")[-1200:]}, level="error")
            if verdict["l2"].get("score") is not None:
                self.emit(node["id"], "validate",
                          f"L2 质量评分 {verdict['l2']['score']}",
                          verdict["l2"], level="success" if verdict["l2"]["score"] >= 60 else "warning")
            if verdict["l3"].get("coverage") is not None:
                self.emit(node["id"], "validate",
                          f"L3 交叉复核覆盖度 {verdict['l3']['coverage']}",
                          verdict["l3"],
                          level="success" if verdict["l3"]["passed"] else "warning")

            if verdict["passed"]:
                self.scheduler.release(best["agent_id"])
                self.scheduler.report(best["agent_id"], True, tokens, cost)
                if node["validator"] == "coding":
                    self.ctx["baseline_tests"] = l1.get("ran", 0)
                db.execute(
                    """UPDATE steps SET status='passed', verdict=?, tokens=?, cost=?, latency=?,
                       ended_at=? WHERE id=?""",
                    (json.dumps(verdict, ensure_ascii=False), tokens, cost, latency, db.now(), step_id),
                )
                self.ctx["validations"][node["id"]] = verdict
                self.stats["nodes"] += 1
                self.emit(node["id"], "state", f"节点通过：{node['name']}", level="success")
                return {"node": node, "verdict": verdict, "attempts": attempt,
                        "agent": best["agent_name"]}

            responsibility = l1.get("responsibility", {})
            owner = responsibility.get("stage")
            if owner == "environment" or (node["validator"] == "coding" and owner in ("test", "unknown")):
                self.scheduler.release(best["agent_id"])
                db.execute(
                    """UPDATE steps SET status='failed', verdict=?, tokens=?, cost=?, latency=?,
                       ended_at=? WHERE id=?""",
                    (json.dumps(verdict, ensure_ascii=False), tokens, cost, latency, db.now(), step_id),
                )
                if owner == "environment":
                    raise RunFailed(l1["detail"])
                if owner == "test" and "test" in node["deps"]:
                    feedback = selfloop.feedback_patch(
                        {"patch": "修复测试自身的异常。保持原有验收条件和断言强度，勿修改实现或契约。"}, verdict)
                    raise RepairTest(feedback, responsibility)
                raise RunFailed("验收失败的责任阶段无法确定，请检查验证记录和测试产物")

            attribution = selfloop.attribute(verdict)
            if owner == "spec":
                attribution = {**attribution, "code": selfloop.SPEC_GAP,
                               "label": "验收样例与用户规则矛盾",
                               "strategy": "重新解析需求并核算验收样例",
                               "patch": "以用户原始规则为准，纠正验收样例的预期并重新核算。"}
            self.scheduler.release(best["agent_id"])
            if not injected:
                self.scheduler.report(best["agent_id"], False, tokens, cost)
            self.stats["retries"] += 1
            patch = selfloop.feedback_patch(attribution, verdict)
            enriched_attribution = {**attribution, "patch": patch}
            lesson = selfloop.record_lesson(self.run_id, node["id"], enriched_attribution)
            self.stats["lessons"] = int(self.stats.get("lessons", 0)) + 1
            ranked_ids = [item["agent_id"] for item in ranked]
            decision = self.optimizer.decide(report, attempt, node["max_attempts"],
                                             best["agent_id"], ranked_ids)
            self.emit(node["id"], "optimizer.decision",
                      f"Optimizer 决策：{decision.action}",
                      {"action": decision.action, "next_agent": decision.next_agent,
                       "reason": decision.reason}, source="optimizer", session_id=session_id,
                      provenance="deterministic")
            self.emit(node["id"], "selfloop",
                      f"失败归因：{attribution['label']} → 策略：{attribution['strategy']}",
                      {"attribution": attribution["code"], "reason": verdict["reasons"],
                       "patch": patch, "lesson_id": lesson.get("id") if lesson else None},
                      level="warning")
            db.execute(
                """UPDATE steps SET status='retrying', verdict=?, tokens=?, cost=?, latency=?,
                   ended_at=? WHERE id=?""",
                (json.dumps(verdict, ensure_ascii=False), tokens, cost, latency, db.now(), step_id),
            )
            last_agent = best["agent_id"]

        raise RunFailed(f"节点 {node['name']} 达到最大重试次数 {node['max_attempts']} 仍未通过")

    # ---------- 演示注错 ----------
    def _demo_injection(self, node: dict, attempt_number: int):
        """演示模式：指定节点的首次尝试使用历史真实失败产物，验收与修复仍真实执行。"""
        spec = self.scenario.get("demo_inject")
        if not spec or spec.get("node") != node["id"] or int(spec.get("attempt", 1)) != attempt_number:
            return None
        fixture = Path(__file__).resolve().parent / "demo_assets" / str(spec.get("fixture", ""))
        try:
            content = fixture.read_text(encoding="utf-8")
        except OSError as exc:
            raise RunFailed(f"演示注错素材缺失：{exc}") from exc
        name = str(spec.get("file", "index.html"))
        self.emit(node["id"], "demo.fault_injected",
                  f"演示注错：第 {attempt_number} 次尝试使用历史真实错账实现"
                  f"（来源运行 {spec.get('source_run', '历史运行')}），平台核算与修复仍真实执行",
                  {"fixture": fixture.name, "source_run": spec.get("source_run"),
                   "file": name, "note": spec.get("note", "")},
                  level="warning", source="demo", provenance="demo")
        return {name: content}, {"tokens": 0, "latency": 0.0, "source": "demo",
                                 "model": "demo-fault-injection", "provider": "platform",
                                 "tool_calls": 0, "tool_trace": ()}

    # ---------- 提示词与生成 ----------
    def _build_prompt(self, node: dict, patch: str, attempt: int) -> tuple[str, str]:
        SYSTEM_PROMPTS = getattr(self.mod, "SYSTEM_PROMPTS", {})

        system = SYSTEM_PROMPTS.get(node["role"], "你是多智能体交付流水线中的执行角色。")
        kb_hits = self.ctx["kb"].get(node["id"], [])
        upstream = []
        for dep in node["deps"]:
            for name, content in self.ctx["artifacts"].items():
                if name in self._outputs_of(dep):
                    upstream.append(f"[{name}]\n{content[:1800]}")
        if node.get("browser_cases"):
            upstream.append("按下列验收用例设置 data-testid，结果必须来自真实操作：\n"
                            + json.dumps(node["browser_cases"], ensure_ascii=False))
        if node.get("demo_roster"):
            from .planner import SIGNUP_CONTROL_CONTRACT, SIGNUP_IDENTITY_RULE
            upstream.append("平台假名单（demo-button 必须按此顺序原样载入并替换当前记录）：\n"
                            + json.dumps(node["demo_roster"], ensure_ascii=False)
                            + "\n" + SIGNUP_IDENTITY_RULE + "\n" + SIGNUP_CONTROL_CONTRACT)
        if node.get("meeting_scenarios"):
            from .planner import MEETING_CONTROL_CONTRACT
            upstream.append("平台验收场景（预期由平台独立核算，结论必须来自真实输入与检查）：\n"
                            + json.dumps(node["meeting_scenarios"], ensure_ascii=False)
                            + "\n" + MEETING_CONTROL_CONTRACT)
        if node.get("acceptance"):
            upstream.append("已确认的验收要求：\n" + "\n".join(
                "- " + check for check in node["acceptance"]
            ))
        if node.get("validator") == "deliverable" and "index.html" in node["outputs"]:
            upstream.append("index.html 是用户交付入口。内联 CSS 和脚本；工具须提供真实输入、结果及可操作样例，报告须直接呈现结论。无需网络，不要引用外部资源。")
        output_contract = (f"只需产出文件：{', '.join(node['outputs'])}"
                           if node.get("outputs") else "本节点无需产出文件，执行验证并给出结论")
        if self.mode == "live" and node.get("outputs"):
            output_contract += (
                f"\n必须调用 write_file 将完整内容写入：{', '.join(node['outputs'])}。"
                "文件内容较长时务必分块：先 write_file 写第一部分，再调用 append_file 逐段追加其余部分，"
                "每次工具调用的内容控制在 4000 字以内，避免单次输出达到长度上限。"
                "可先使用 list_files/read_file 检查上游产物。"
                "工具完成后只需简短确认，不要在最终答复中重复完整文件。"
            )
            if node.get("validator") != "test":
                output_contract += "需要验证代码时可调用 run_tests。"
        if node.get("outputs") and node["outputs"][0].endswith(".json"):
            required = list(node.get("required", []))
            if node.get("validator") == "spec":
                required = ["acceptance_criteria"]
            elif node.get("validator") == "review":
                required = ["verdict", "criteria"]
            if required:
                quoted = ", ".join(f'"{field}"' for field in required)
                output_contract += (
                    f"\n写入 {node['outputs'][0]} 的内容必须是严格 JSON，"
                    f"根对象必须直接包含字段：{quoted}。"
                    "不要添加外层包装对象，不要输出 JSON 之外的解释。"
                )
            if node.get("validator") == "spec":
                output_contract += (
                    "acceptance_criteria 必须为 4 到 6 条；每条使用唯一 AC-xxx ID，"
                    "合并同类边界条件，确保下游能为每条标准编写独立测试。"
                    "写入前逐个按规则手算验收样例，确保相同条件得到相同结果。"
                    "对比场景使用独立输入，或只改变要验证的条件，避免无关记录引入额外结果。"
                    "用户未要求的功能不作为硬性验收条件。"
                )
            elif node.get("validator") == "review":
                output_contract += (
                    '"verdict" 的值必须是精确字符串 "pass" 或 "fail"；'
                    "不得使用中文、布尔值或其他同义词。"
                    'criteria 数组必须覆盖 spec.json 的每个验收编号一次。'
                    '每项字段：id（原始编号）、result（pass 或 fail）、'
                    'file（main.py、test_main.py、test_edge.py 或 risk.json 中的一个）、'
                    'reason（指出文件内具体函数、测试断言或风险记录，解释其如何支持结论）。'
                    '全部条目通过时 verdict 才能为 pass；不要编造用例或测试执行结果。'
                )
        elif node.get("outputs") and node["outputs"][0].endswith(".py"):
            if self.mode == "live":
                output_contract += (
                    f"\n写入 {node['outputs'][0]} 的内容必须是完整 Python 源码，"
                    "不得包含 Markdown 代码围栏；保持实现精简，控制在 250 行以内。"
                )
            else:
                output_contract += (
                    "\n只输出一个完整 Python 代码块，不要解释；保持实现精简，控制在 250 行以内。"
                )
            if node.get("validator") == "test":
                output_contract += (
                    "每个测试方法的注释或文档字符串必须写出它覆盖的每条验收标准的精确 ID；"
                    "从 main.py 导入被测对象，不得在测试文件内实现被测逻辑。"
                    "禁止使用 subprocess、Popen 或再次运行测试文件自身；直接调用 main.py 的函数进行测试。"
                    "本阶段没有正式 main.py，只编写并提交 test_main.py，不生成临时实现。"
                    "系统会在编码节点产出 main.py 后执行这些测试。"
                    "根据用户规则独立推导每组输入的预期结果；需求样例与用户规则冲突时，"
                    "按用户规则设计自洽的用例，使用最少数据隔离每个边界。"
                )
        parts = [
            f"# 任务\n{self.task}",
            f"# 知识库（检索自平台知识库，共 {len(kb_hits)} 条）",
            "\n".join(f"- {h['title']}：{h['content'][:300]}" for h in kb_hits) or "（无）",
            "# 上游产出\n" + ("\n\n".join(upstream) if upstream else "（无）"),
            "# 用户附件\n" + (
                "\n".join(
                    f"- inputs/{item['name']}（{item['kind']}，{item['mime']}，{item['size']} bytes）"
                    for item in self.ctx.get("attachments", [])
                ) if self.ctx.get("attachments") else "（无）"
            ),
            f"# 当前节点\n{node['name']}（第 {attempt} 次尝试）：{node.get('desc','')}",
            f"# 输出要求\n{output_contract}",
        ]
        plan_node = next(item for item in self.plan.nodes if item.id == node["id"])
        if node.get("validator") == "review":
            checks = self.ctx.get("validations", {})
            parts.append("# 系统已执行的测试\n" + "\n".join(
                f"- {stage}: {check}"
                for stage in ("code", "hardening")
                for check in checks.get(stage, {}).get("l1", {}).get("checks", [])
            ))
        completed = {
            dep: {name: self.ctx["artifacts"][name] for name in self._outputs_of(dep)
                  if name in self.ctx["artifacts"]}
            for dep in node["deps"]
        }
        snapshot = self.context_assembler.build(
            plan_node, self.plan, completed, patch, scenario_id=self.scenario["id"]
        )
        if snapshot.lessons:
            parts.append("# 历史经验（来自之前运行）\n" + "\n".join(
                f"- [lesson:{lesson_id}] {lesson}"
                for lesson_id, lesson in zip(snapshot.lesson_ids, snapshot.lessons)
            ))
            self.emit(node["id"], "experience.applied", f"复用 {len(snapshot.lessons)} 条历史经验",
                      {"lesson_ids": list(snapshot.lesson_ids)}, source="memory",
                      provenance="deterministic")
        if patch:
            parts.append(f"# 上一轮失败反馈（必须解决）\n{patch}")
        return "\n\n".join(parts), system

    def _outputs_of(self, node_id: str) -> list[str]:
        for n in self.scenario["nodes"]:
            if n["id"] == node_id:
                return n["outputs"]
        return []

    def _prepare_worker_workspace(self, worker_dir: Path, role: str, node: dict) -> None:
        policy = ToolPolicy.for_role(role)
        allowed = {
            name
            for dependency in node.get("deps", [])
            for name in self._outputs_of(dependency)
        }
        for name, content in self.ctx.get("artifacts", {}).items():
            if name not in allowed:
                continue
            path = policy.resolve_path(worker_dir, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        input_root = getattr(self, "inputs", None)
        if input_root is None and hasattr(self, "run_dir"):
            input_root = self.run_dir / "inputs"
        if input_root is not None and input_root.is_dir() and not input_root.is_symlink():
            worker_inputs = worker_dir / "inputs"
            worker_inputs.mkdir(parents=True, exist_ok=True)
            for source in input_root.iterdir():
                if source.is_file() and not source.is_symlink():
                    shutil.copyfile(source, worker_inputs / source.name)

    def _generate(self, node: dict, agent: dict, system: str, prompt: str,
                  attempt: int, worker_dir: Path | None = None,
                  session_id: str = "") -> tuple[dict, dict]:
        if self.mode == "live" and node.get("outputs"):
            executor = None
            definitions = None
            policy = ToolPolicy.for_role(node.get("role", "worker"))
            if worker_dir is not None:
                test_design = node.get("validator") == "test"
                if test_design:
                    policy = ToolPolicy(policy.allowed - {"run_tests"})
                local_executor = WorkspaceToolExecutor(
                    worker_dir, policy, writable_outputs=node["outputs"] if test_design or node.get("validator") == "deliverable" else None,
                )
                executor = CompositeToolExecutor(
                    local_executor, getattr(self, "github_mcp", None), policy
                )
                definitions = executor.definitions()
                github_tools = [
                    item["function"]["name"] for item in definitions
                    if item.get("function", {}).get("name", "").startswith("github__")
                ]
                github_client = getattr(self, "github_mcp", None)
                github_config = getattr(github_client, "config", None)
                github_enabled = bool(github_tools) or bool(
                    github_config is not None and github_config.enabled
                )
                if github_client is not None and github_enabled:
                    self.emit(
                        node["id"], "mcp.tools.discovered",
                        f"GitHub MCP 发现 {len(github_tools)} 个只读工具",
                        {"server": "github", "tools": github_tools},
                        level="success" if github_tools else "warning",
                        source="mcp", session_id=session_id,
                    )

            def observe_tool(call: dict) -> None:
                is_mcp = call.get("source") == "mcp"
                kind = (
                    "mcp.call.completed" if is_mcp and call.get("ok")
                    else "mcp.call.rejected" if is_mcp
                    else "tool.call"
                )
                self.emit(
                    node["id"], kind,
                    f"Function Calling：{call.get('name', 'unknown')}",
                    dict(call),
                    level="success" if call.get("ok") else "warning",
                    source=call.get("source", "worker"), session_id=session_id,
                    provenance="model",
                )

            image_inputs = []
            supports_vision = getattr(llm, "supports_vision", lambda *args, **kwargs: False)
            if supports_vision(agent["model"]):
                for item in self.ctx.get("attachments", []):
                    if item.get("kind") != "image":
                        continue
                    input_root = getattr(self, "inputs", None)
                    if input_root is None and hasattr(self, "run_dir"):
                        input_root = self.run_dir / "inputs"
                    if input_root is None:
                        continue
                    path = input_root / item["name"]
                    if path.is_file() and not path.is_symlink():
                        image_inputs.append({
                            "mime": item["mime"],
                            "data": base64.b64encode(path.read_bytes()).decode("ascii"),
                        })
            try:
                with track_usage("execution", getattr(self, "run_id", None), node["id"],
                                 budget=getattr(self, "model_budget", None)):
                    res = llm.complete(
                        system, prompt, model=agent["model"],
                        tool_definitions=definitions,
                        tool_executor=executor.execute if executor else None,
                        tool_observer=observe_tool,
                        image_inputs=image_inputs or None,
                        **({"max_output_tokens": 8192} if node.get("validator") == "deliverable" else {}),
                    )
            except (ToolRoundLimitError, ModelBudgetExceeded, GatewayError) as exc:
                if type(exc) is GatewayError and getattr(exc, "category", "") != "length_limit":
                    raise
                if node.get("validator") not in {"coding", "deliverable"} or worker_dir is None:
                    raise
                candidates = [policy.resolve_path(worker_dir, name) for name in node["outputs"]]
                if not candidates or not all(path.is_file() and path.stat().st_size for path in candidates):
                    raise
                self.emit(node["id"], "llm.candidate",
                          f"{exc}。提交已写出的候选产物给独立验证",
                          source="worker", session_id=session_id, provenance="model")
                res = LLMResult("", getattr(exc, "tokens", 0), 0,
                                getattr(exc, "model", ""), getattr(exc, "provider", ""))
            fallback = extract_code(res.text, node["outputs"][0])
            payload = {}
            for name in node["outputs"]:
                path = policy.resolve_path(worker_dir, name) if worker_dir else None
                payload[name] = (
                    path.read_text(encoding="utf-8")
                    if path is not None and path.is_file()
                    else ("" if node.get("validator") == "deliverable" else fallback)
                )
            if node["id"] == "spec" or (node.get("outputs") and node["outputs"][0].endswith(".json")):
                try:
                    json.loads(payload[node["outputs"][0]])
                except ValueError:
                    pass
            return payload, {"tokens": res.tokens, "latency": res.latency,
                             "source": "llm", "model": res.model,
                             "provider": res.provider,
                             "tool_calls": getattr(res, "tool_calls", 0),
                             "tool_trace": getattr(res, "tool_trace", ())}

        artifacts = self.mod.produce(node, self.ctx, attempt)
        est_tokens = max(1, (len(system) + len(prompt)
                             + sum(len(v) for v in artifacts.values())) // 4)
        time.sleep(0.35)
        return artifacts, {"tokens": est_tokens, "latency": 0.35, "source": "mock"}


def create_run(task: str, scenario: dict) -> str:
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + str(int(time.time() * 1000) % 1000)
    db.execute(
        "INSERT INTO runs(id,task,scenario,status,mode,created_at) VALUES(?,?,?,?,?,?)",
        (run_id, task, scenario["id"], "queued", config.mode_label(), db.now()),
    )
    return run_id
