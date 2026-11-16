"""竞赛缺口整改回归：P0-1 多节点协作规划、P0-2 Agent CRUD、P1-3 自愈演示。"""
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.request import Request, urlopen

from app import config, db
from app.engine import Engine
from app.planner import (
    _complexity_cues, _is_complex_task, _is_platform_simple_task,
    ground_aa_demo_cases, plan_custom,
)
from app.router import (
    agents_create, agents_delete, agents_rank, agents_update, demo_selfheal,
)
from app.scheduler import scheduler, seed_agents
from app.server import Handler

COMPLEX_TASK = ("构建一个团队活动经费报销工具（可操作网页）：先做需求分析，再实现报销录入与审批页面，"
                "然后编写测试报告，最后输出评审结论和风险清单。")
SIMPLE_TASK = "制作一个可操作的倒计时页面"


def single_build_node(task_outputs=None, cases=None):
    return {"id": "build", "name": "制作交付物", "desc": "按任务要求制作", "role": "coder",
            "deps": [], "outputs": task_outputs or ["index.html"],
            "acceptance": ["页面可操作并给出正确结果"],
            "browser_cases": cases or []}


class ComplexityPlanningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.reset_connection(Path(self.tmp.name) / "plan.db")

    def tearDown(self):
        db.reset_connection()
        self.tmp.cleanup()

    def test_complexity_classification_separates_platform_simple_tasks(self):
        self.assertTrue(_is_complex_task(COMPLEX_TASK))
        self.assertTrue(_is_complex_task("构建一个订单统计工具，并生成测试和风险报告"))
        self.assertFalse(_is_complex_task(SIMPLE_TASK))
        self.assertFalse(_is_complex_task("做个出游 AA 算账工具，帮我测试一下平摊"))
        self.assertFalse(_is_complex_task("做活动报名名单工具，需要手机号，整理名单并测试"))
        self.assertFalse(_is_complex_task("会议室冲突检查工具，先分析再测试"))
        self.assertFalse(_is_complex_task("按城市汇总附件，排除负数和无法解析的金额，保留两位小数，并输出测试报告"))
        self.assertTrue(_is_platform_simple_task("做个出游 AA 算账工具"))
        self.assertIn("测试", _complexity_cues(COMPLEX_TASK))
        self.assertIn("评审", _complexity_cues(COMPLEX_TASK))

    def test_prompt_asks_for_shortest_sufficient_plan_by_complexity(self):
        captured = {}

        def complete(prompt, task="", **kwargs):
            captured["prompt"] = prompt
            return SimpleNamespace(text=json.dumps([single_build_node()]),
                                   provider="deepseek", model="deepseek-flash")

        with patch("app.planner.llm.complete", side_effect=complete):
            plan_custom(COMPLEX_TASK, [])
        self.assertIn("按复杂度选择最短充分计划", captured["prompt"])
        self.assertIn("必须拆成多节点 DAG 协作", captured["prompt"])
        self.assertIn("analyst", captured["prompt"])
        with patch("app.planner.llm.complete", side_effect=complete):
            plan_custom(SIMPLE_TASK, [])
        self.assertIn("保持最短计划", captured["prompt"])

    def test_complex_single_node_plan_expands_to_four_role_dag(self):
        cases = [{"criterion": 1, "actions": [
            {"kind": "expect_text", "target": "result", "value": "ok"}]}]
        with patch("app.planner.llm.complete",
                   return_value=SimpleNamespace(text=json.dumps([single_build_node(cases=cases)]),
                                                provider="deepseek", model="deepseek-flash")):
            scenario = plan_custom(COMPLEX_TASK, [])
        nodes = scenario["nodes"]
        self.assertEqual([n["role"] for n in nodes], ["analyst", "coder", "tester", "reviewer"])
        self.assertEqual(nodes[0]["deps"], [])
        self.assertEqual(nodes[1]["deps"], [nodes[0]["id"]])
        self.assertEqual(nodes[2]["deps"], [nodes[1]["id"]])
        self.assertEqual(set(nodes[3]["deps"]), {nodes[1]["id"], nodes[2]["id"]})
        outputs = [name for n in nodes for name in n["outputs"]]
        self.assertEqual(len(outputs), len(set(outputs)))
        self.assertIn("index.html", nodes[1]["outputs"])
        self.assertEqual(nodes[1]["browser_cases"], cases)
        self.assertTrue(scenario["complexity"]["complex"])
        self.assertTrue(scenario["complexity"]["expanded"])
        for node in nodes:
            self.assertTrue(node["acceptance"])

    def test_complex_multi_node_model_plan_is_kept_without_expansion(self):
        model_nodes = [
            {"id": "analyze", "name": "分析", "role": "analyst", "deps": [],
             "outputs": ["analysis.md"], "acceptance": ["分析覆盖目标"], "desc": "分析"},
            {"id": "build", "name": "实现", "role": "coder", "deps": ["analyze"],
             "outputs": ["index.html"], "acceptance": ["页面可操作"], "desc": "实现"},
            {"id": "verify", "name": "测试", "role": "tester", "deps": ["build"],
             "outputs": ["test_report.md"], "acceptance": ["报告给出结论"], "desc": "测试"},
        ]
        with patch("app.planner.llm.complete",
                   return_value=SimpleNamespace(text=json.dumps(model_nodes),
                                                provider="deepseek", model="deepseek-flash")):
            scenario = plan_custom(COMPLEX_TASK, [])
        self.assertEqual(len(scenario["nodes"]), 3)
        self.assertFalse(scenario["complexity"]["expanded"])

    def test_simple_tasks_stay_single_node_with_grounding_intact(self):
        signup = ("做个活动报名名单整理工具。报名名单里有人重复填，也有人忘了留手机号。"
                  "把重复的人合并，只保留最后一次填写的信息。没有手机号的人单独列出来。")
        with patch("app.planner.llm.complete",
                   return_value=SimpleNamespace(text=json.dumps([single_build_node()]),
                                                provider="deepseek", model="deepseek-flash")):
            scenario = plan_custom(signup, [])
        self.assertEqual(len(scenario["nodes"]), 1)
        self.assertFalse(scenario["complexity"]["complex"])
        self.assertTrue(scenario["nodes"][0]["demo_roster"])
        self.assertTrue(scenario["nodes"][0]["browser_cases"])

    def test_expansion_avoids_id_and_file_collisions(self):
        node = single_build_node(task_outputs=["analysis.md", "index.html"])
        node["id"] = "test"
        with patch("app.planner.llm.complete",
                   return_value=SimpleNamespace(text=json.dumps([node]),
                                                provider="deepseek", model="deepseek-flash")):
            scenario = plan_custom(COMPLEX_TASK, [])
        ids = [n["id"] for n in scenario["nodes"]]
        files = [f for n in scenario["nodes"] for f in n["outputs"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(files), len(set(files)))
        self.assertIn("analysis.md", scenario["nodes"][1]["outputs"])


class MultiNodeExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        db.reset_connection(self.root / "exec.db")
        self.original_runs_dir = config.RUNS_DIR
        config.RUNS_DIR = self.root / "runs"
        seed_agents()
        db.execute("INSERT INTO runs(id,task,scenario,status,mode,created_at) VALUES(?,?,?,?,?,?)",
                   ("r", "task", "custom", "queued", "mock", db.now()))

    def tearDown(self):
        config.RUNS_DIR = self.original_runs_dir
        db.reset_connection()
        self.tmp.cleanup()

    def _engine(self, scenario):
        engine = Engine("r", "task", scenario, "mock")

        def produce(node, ctx, attempt):
            artifacts = {}
            for name in node["outputs"]:
                if name.endswith(".html"):
                    artifacts[name] = ("<html><body><main>" + node["name"]
                                       + " 交付页面</main></body></html>")
                else:
                    artifacts[name] = f"# {node['name']} 产出 {name}\n内容基于任务与上游产物。"
            return artifacts

        engine.mod = SimpleNamespace(SYSTEM_PROMPTS={}, KB_QUERIES={}, produce=produce)
        return engine

    @staticmethod
    def _browser_passed():
        return {"status": "passed", "verified_criteria": [], "case_results": [],
                "detail": "隔离浏览器已加载页面"}

    def test_expanded_complex_plan_runs_four_roles_with_artifact_handoffs(self):
        with patch("app.planner.llm.complete",
                   return_value=SimpleNamespace(text=json.dumps([single_build_node()]),
                                                provider="deepseek", model="deepseek-flash")):
            scenario = plan_custom(COMPLEX_TASK, [])
        engine = self._engine(scenario)
        with patch("app.validator.check_page", return_value=self._browser_passed()):
            engine.run()
        run = db.one("SELECT * FROM runs WHERE id='r'")
        self.assertEqual(run["status"], "succeeded")
        steps = db.query("SELECT * FROM steps WHERE run_id='r' ORDER BY id")
        self.assertEqual({s["role"] for s in steps}, {"analyst", "coder", "tester", "reviewer"})
        handoffs = db.query("SELECT * FROM events WHERE run_id='r' AND kind='artifact.handoff' ORDER BY id")
        self.assertGreaterEqual(len(handoffs), 3)
        files = [item["file"] for row in handoffs for item in json.loads(row["payload"])["files"]]
        self.assertIn("analysis.md", files)
        self.assertIn("index.html", files)
        self.assertIn("test_report.md", files)
        for name in ("analysis.md", "index.html", "test_report.md", "review.md"):
            self.assertTrue((engine.work / name).exists())

    def test_length_limited_generation_submits_already_written_candidate(self):
        from app.llm import GatewayError
        db.execute("UPDATE runs SET mode='live' WHERE id='r'")
        scenario = {"id": "custom", "nodes": [
            {"id": "build", "name": "实现", "role": "coder", "deps": [],
             "outputs": ["index.html"], "validator": "deliverable", "levels": ["l1"],
             "max_attempts": 1, "desc": "实现"},
        ]}
        engine = Engine("r", "task", scenario, "live")

        def fake_complete(system, prompt, **kwargs):
            executor = kwargs.get("tool_executor")
            self.assertIsNotNone(executor)
            executor("write_file", {"path": "index.html",
                                    "content": "<html><body>分块写出的完整页面</body></html>"})
            error = GatewayError("模型输出达到长度上限，候选响应不完整")
            error.category = "length_limit"
            raise error

        with patch("app.engine.llm.complete", side_effect=fake_complete), \
                patch("app.validator.check_page", return_value=self._browser_passed()):
            engine.run()
        run = db.one("SELECT * FROM runs WHERE id='r'")
        self.assertEqual(run["status"], "succeeded")
        kinds = [e["kind"] for e in db.query("SELECT kind FROM events WHERE run_id='r'")]
        self.assertIn("llm.candidate", kinds)
        self.assertTrue((engine.work / "index.html").exists())

    def test_downstream_prompt_receives_upstream_artifact_content(self):
        scenario = {"id": "custom", "nodes": [
            {"id": "rules", "name": "规则", "role": "analyst", "deps": [],
             "outputs": ["rules.md"], "validator": "deliverable", "levels": ["l1"],
             "max_attempts": 1, "desc": "规则"},
            {"id": "build", "name": "实现", "role": "coder", "deps": ["rules"],
             "outputs": ["index.html"], "validator": "deliverable", "levels": ["l1"],
             "max_attempts": 1, "desc": "实现"},
        ]}
        engine = self._engine(scenario)
        prompts = []
        original = engine._build_prompt

        def spy(node, patch_text, attempt):
            prompt, system = original(node, patch_text, attempt)
            prompts.append((node["id"], prompt))
            return prompt, system

        engine._build_prompt = spy
        with patch("app.validator.check_page", return_value=self._browser_passed()):
            engine.run()
        build_prompt = next(p for nid, p in prompts if nid == "build")
        self.assertIn("rules.md", build_prompt)
        self.assertIn("规则 产出 rules.md", build_prompt)


class AgentCrudTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.reset_connection(Path(self.tmp.name) / "agents.db")
        seed_agents()

    def tearDown(self):
        db.reset_connection()
        self.tmp.cleanup()

    def test_create_agent_then_scheduler_prefers_it_and_stats_accumulate(self):
        created = agents_create({"name": "编码新星·自定义", "role": "coder",
                                 "model": "deepseek-flash", "skills": {"coder": 0.99},
                                 "cost_per_call": 0.001, "description": "面板创建"})
        agent = created["agent"]
        self.assertEqual(agent["status"], "ready")
        self.assertEqual(agent["skills"], {"coder": 0.99})
        best, ranked = scheduler.pick("coder")
        self.assertEqual(best["agent_id"], agent["id"])
        self.assertIn("能力", best["reason"])
        self.assertEqual(ranked[0]["agent_id"], agent["id"])
        scheduler.report(agent["id"], True, tokens=1200, cost=0.0012)
        scheduler.release(agent["id"])
        row = db.one("SELECT * FROM agents WHERE id=?", (agent["id"],))
        self.assertEqual(row["success"], 1)
        self.assertEqual(row["total_tokens"], 1200)
        self.assertAlmostEqual(row["total_cost"], 0.0012)
        summary = next(r for r in scheduler.summary() if r["id"] == agent["id"])
        self.assertGreater(summary["success_rate"], 0.5)

    def test_create_validation_rejects_bad_payloads(self):
        for body in ({"name": "", "role": "coder", "model": "m"},
                     {"name": "x", "role": "hacker", "model": "m"},
                     {"name": "x", "role": "coder", "model": ""},
                     {"name": "x", "role": "coder", "model": "m", "skills": {"coder": 1.5}},
                     {"name": "x", "role": "coder", "model": "m", "skills": {"unknown": 0.5}},
                     {"name": "x", "role": "coder", "model": "m", "tools": ["rm_rf"]},
                     {"name": "x", "role": "coder", "model": "m", "cost_per_call": -1},
                     {"name": "x", "role": "coder", "model": "m", "status": "degraded"}):
            with self.subTest(body=body):
                with self.assertRaises(ValueError):
                    agents_create(body)
        agents_create({"id": "dup-agent", "name": "甲", "role": "coder", "model": "m"})
        with self.assertRaises(ValueError):
            agents_create({"id": "dup-agent", "name": "乙", "role": "coder", "model": "m"})

    def test_update_partial_fields_and_delete(self):
        agent = agents_create({"name": "临时", "role": "tester", "model": "mimo-v2.6-pro"})["agent"]
        updated = agents_update(agent["id"], {"model": "deepseek-flash", "status": "offline"})["agent"]
        self.assertEqual(updated["model"], "deepseek-flash")
        self.assertEqual(updated["status"], "offline")
        self.assertEqual(updated["name"], "临时")
        self.assertNotIn(agent["id"], [i["agent_id"] for i in scheduler.rank("tester")])
        agents_update(agent["id"], {"status": "ready"})
        self.assertIn(agent["id"], [i["agent_id"] for i in scheduler.rank("tester")])
        with self.assertRaises(KeyError):
            agents_update("missing", {"name": "x"})
        self.assertEqual(agents_delete(agent["id"]), {"ok": True, "id": agent["id"]})
        with self.assertRaises(KeyError):
            agents_delete(agent["id"])

    def test_delete_refuses_busy_agent(self):
        agent = agents_create({"name": "忙碌", "role": "coder", "model": "m"})["agent"]
        db.execute("INSERT INTO worker_sessions(id,run_id,node_id,attempt,role,agent_id,workspace,"
                   "status,provenance,started_at,deadline) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   ("w", "r", "build", 1, "coder", agent["id"], "x", "running", "model", 1.0, 2.0))
        with self.assertRaises(ValueError):
            agents_delete(agent["id"])

    def test_rank_exposes_reason_and_degradation(self):
        payload = agents_rank({"role": ["coder"]})
        self.assertEqual(payload["weights"], config.SCHEDULER_WEIGHTS)
        self.assertTrue(payload["ranked"])
        for item in payload["ranked"]:
            self.assertIn("reason", item)
            self.assertIn("breaker", item)
        fast = next(i for i in payload["ranked"] if i["agent_id"] == "coder-fast")
        scheduler.report("coder-fast", False)
        scheduler.report("coder-fast", False)
        degraded = next(i for i in agents_rank({"role": ["coder"]})["ranked"]
                        if i["agent_id"] == "coder-fast")
        self.assertEqual(degraded["breaker"], "circuit_open")
        self.assertIn("降级罚分", degraded["reason"])
        self.assertLess(degraded["score"], fast["score"])
        with self.assertRaises(ValueError):
            agents_rank({"role": ["nope"]})

    def test_seed_does_not_overwrite_user_edits(self):
        db.execute("UPDATE agents SET model='custom-model' WHERE id='coder-pro'")
        seed_agents()
        self.assertEqual(db.one("SELECT model FROM agents WHERE id='coder-pro'")["model"],
                         "custom-model")


class AgentHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.reset_connection(Path(self.tmp.name) / "http.db")

    def tearDown(self):
        db.reset_connection()
        self.tmp.cleanup()

    def request(self, method, path, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = Request(self.base + path, data=data, method=method,
                      headers={"Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            if hasattr(exc, "code"):
                return exc.code, json.loads(exc.read().decode("utf-8"))
            raise

    def test_full_crud_cycle_over_http(self):
        status, created = self.request("POST", "/api/agents",
                                       {"name": "HTTP 测试员", "role": "reviewer",
                                        "model": "deepseek-v4-pro", "skills": {"reviewer": 0.9}})
        self.assertEqual(status, 200)
        agent_id = created["agent"]["id"]
        status, listed = self.request("GET", "/api/agents")
        self.assertEqual(status, 200)
        self.assertIn(agent_id, [a["id"] for a in listed])
        status, detail = self.request("GET", f"/api/agents/{agent_id}")
        self.assertEqual(status, 200)
        self.assertEqual(detail["name"], "HTTP 测试员")
        status, updated = self.request("PUT", f"/api/agents/{agent_id}",
                                       {"description": "已更新", "cost_per_call": 0.01})
        self.assertEqual(status, 200)
        self.assertEqual(updated["agent"]["description"], "已更新")
        status, ranked = self.request("GET", "/api/agents/rank?role=reviewer")
        self.assertEqual(status, 200)
        self.assertEqual(ranked["ranked"][0]["agent_id"], agent_id)
        status, deleted = self.request("DELETE", f"/api/agents/{agent_id}")
        self.assertEqual(status, 200)
        status, missing = self.request("GET", f"/api/agents/{agent_id}")
        self.assertEqual(status, 404)
        status, bad = self.request("POST", "/api/agents", {"name": "x", "role": "bad", "model": "m"})
        self.assertEqual(status, 400)


class SelfHealDemoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        db.reset_connection(self.root / "demo.db")
        self.original_runs_dir = config.RUNS_DIR
        config.RUNS_DIR = self.root / "runs"
        seed_agents()

    def tearDown(self):
        config.RUNS_DIR = self.original_runs_dir
        db.reset_connection()
        self.tmp.cleanup()

    def test_ground_aa_demo_cases_computes_expected_transfers_independently(self):
        node = single_build_node()
        ground_aa_demo_cases("做个出游 AA 算账工具", node)
        cases = node["browser_cases"]
        self.assertEqual(len(cases), 2)
        self.assertTrue(all(c["grounded"] for c in cases))
        self.assertEqual(cases[0]["actions"][-1]["value"], "小王转给小李 100 元；小张转给小李 100 元")
        self.assertEqual(cases[1]["actions"][-1]["value"], "小王转给小李 40 元；小张转给小李 100 元")
        self.assertEqual([c["criterion"] for c in cases], [2, 3])
        ground_aa_demo_cases("做个出游 AA 算账工具", node)
        self.assertEqual(len(node["browser_cases"]), 2)

    def test_demo_endpoint_requires_live_mode_and_guarantees_grounded_cases(self):
        with patch("app.router.config.mode_label", return_value="mock"):
            with self.assertRaises(ValueError):
                demo_selfheal({})
        scenario = {"id": "custom", "name": "自由任务", "nodes": [single_build_node()]}
        with patch("app.router.config.mode_label", return_value="live"), \
                patch("app.router.plan_custom", return_value=scenario):
            result = demo_selfheal({})
        self.assertEqual(result["demo"]["kind"], "aa_selfheal")
        node = result["plan"]["nodes"][0]
        self.assertEqual(node["config"]["demo_inject"]["attempt"], 1)
        self.assertEqual(node["config"]["demo_inject"]["fixture"], "aa_faulty_index.html")
        grounded = [c for c in node["config"]["browser_cases"] if c.get("grounded")]
        self.assertGreaterEqual(len(grounded), 2)
        draft = db.one("SELECT * FROM plan_drafts WHERE id=?", (result["draft_id"],))
        task, saved_scenario, upload_ids, run_id, *source = json.loads(draft["payload"])
        self.assertEqual(saved_scenario["demo_inject"]["node"], node["id"])
        self.assertEqual(upload_ids, [])
        self.assertTrue(run_id)

    def test_demo_draft_can_be_confirmed_through_start_run(self):
        from app.router import start_run
        scenario = {"id": "custom", "name": "自由任务", "nodes": [single_build_node()]}
        with patch("app.router.config.mode_label", return_value="live"), \
                patch("app.router.plan_custom", return_value=scenario), \
                patch("app.router.check_browser_environment",
                      return_value={"status": "passed", "detail": "ok"}), \
                patch("app.router.Engine") as engine_cls:
            result = demo_selfheal({})
            started = start_run({"scenario": "custom", "draft_id": result["draft_id"]})
        self.assertIn("run_id", started)
        engine_cls.assert_called_once()
        run = db.one("SELECT * FROM runs WHERE id=?", (started["run_id"],))
        self.assertEqual(run["status"], "queued")
        self.assertIsNone(db.one("SELECT * FROM plan_drafts WHERE id=?", (result["draft_id"],)))

    def test_injected_first_attempt_is_rejected_then_real_retry_passes(self):
        db.execute("INSERT INTO runs(id,task,scenario,status,mode,created_at) VALUES(?,?,?,?,?,?)",
                   ("r", "AA 演示", "custom", "queued", "mock", db.now()))
        cases = [{"criterion": 1, "actions": [
            {"kind": "expect_transfers", "target": "result-output",
             "value": "小王转给小李 100 元；小张转给小李 100 元"}], "grounded": True}]
        node = single_build_node(cases=cases)
        node.update({"validator": "deliverable", "levels": ["l1"], "max_attempts": 2})
        scenario = {"id": "custom", "nodes": [node],
                    "demo_inject": {"node": "build", "attempt": 1, "file": "index.html",
                                    "fixture": "aa_faulty_index.html",
                                    "source_run": "20261116-012851-5870d071", "note": "演示"}}
        engine = Engine("r", "AA 演示", scenario, "mock")
        engine.mod = SimpleNamespace(
            SYSTEM_PROMPTS={}, KB_QUERIES={},
            produce=lambda n, ctx, attempt: {"index.html": "<html><body>修复后的正确页面</body></html>"},
        )
        prompts = []
        original = engine._build_prompt

        def spy(n, patch_text, attempt):
            prompt, system = original(n, patch_text, attempt)
            prompts.append(prompt)
            return prompt, system

        engine._build_prompt = spy
        failure = {"status": "failed", "verified_criteria": [],
                   "case_results": [{"criterion": 1, "status": "failed",
                                     "detail": "验收第 1 条未通过：预期转账 小王转给小李 100 元；小张转给小李 100 元，"
                                               "实际 小王转给小李 140 元；小张转给小李 200 元"}],
                   "detail": "验收第 1 条未通过"}
        success = {"status": "passed", "verified_criteria": [1],
                   "case_results": [{"criterion": 1, "status": "passed"}], "detail": "ok"}
        with patch("app.validator.check_page", side_effect=[failure, success]):
            engine.run()
        run = db.one("SELECT * FROM runs WHERE id='r'")
        self.assertEqual(run["status"], "succeeded")
        events = db.query("SELECT * FROM events WHERE run_id='r' ORDER BY id")
        kinds = [e["kind"] for e in events]
        self.assertIn("demo.fault_injected", kinds)
        self.assertIn("selfloop", kinds)
        injected = next(e for e in events if e["kind"] == "demo.fault_injected")
        self.assertEqual(injected["provenance"], "demo")
        attribution = next(e for e in events if e["kind"] == "selfloop")
        self.assertEqual(json.loads(attribution["payload"])["attribution"], "LOGIC_DEFECT")
        steps = db.query("SELECT * FROM steps WHERE run_id='r' ORDER BY id")
        self.assertEqual([s["attempt"] for s in steps], [1, 2])
        self.assertEqual([s["status"] for s in steps], ["retrying", "passed"])
        sessions = db.query("SELECT * FROM worker_sessions WHERE run_id='r' ORDER BY attempt")
        self.assertEqual(sessions[0]["provenance"], "demo")
        self.assertEqual(sessions[1]["provenance"], "replay")
        self.assertIn("140", prompts[1])
        self.assertIn("失败用例", prompts[1])
        coder = db.one("SELECT * FROM agents WHERE id='coder-fast'")
        self.assertEqual(coder["failure"], 0)
        self.assertEqual(coder["success"], 1)
        self.assertEqual(coder["status"], "ready")
        fixture = Path(__file__).resolve().parents[1] / "app" / "demo_assets" / "aa_faulty_index.html"
        self.assertTrue(fixture.exists())
        self.assertIn("loadDemo();", fixture.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
