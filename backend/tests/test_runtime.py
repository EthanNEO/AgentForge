import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import db, validator
from app.context import ContextAssembler
from app.engine import Engine
from app.events import EventStore
from app.llm import GatewayError
from app.models import ExecutionPlan, PlanNode
from app.optimizer import Optimizer
from app.reviewer import VerificationReport
from scenarios.software_delivery import SCENARIO


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.reset_connection(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        db.reset_connection()
        self.tmp.cleanup()

    def test_coding_validation_distinguishes_broken_test_from_bad_implementation(self):
        work = Path(self.tmp.name)
        node = {"validator": "coding", "levels": ["l1"]}
        (work / "main.py").write_text("def overlap(): return 60", encoding="utf-8")
        test = work / "test_main.py"
        prefix = "import unittest\nimport main\nclass Check(unittest.TestCase):\n"
        test.write_text(prefix + "    def test_overlap(self):\n"
                        "        bookings = [{}, {}]\n"
                        "        self.assertEqual({bookings[0], bookings[1]}, set())\n", encoding="utf-8")
        broken = validator.validate(node, work, {})
        self.assertEqual(broken["l1"]["responsibility"]["stage"], "test")
        test.write_text(prefix + "    def test_overlap(self):\n"
                        "        self.assertEqual(main.overlap(), 30)\n", encoding="utf-8")
        wrong = validator.validate(node, work, {})
        self.assertEqual(wrong["l1"]["responsibility"]["stage"], "code")

    def test_spec_rejects_incorrect_meeting_overlap_example(self):
        work = Path(self.tmp.name)
        spec = work / "spec.json"
        example = "同一会议室 A 的预约 09:00-10:00 与 09:30-10:30，重叠时长为 {} 分钟"
        def validate(minutes):
            spec.write_text(json.dumps({"acceptance_criteria": [
                {"id": "AC-001", "verification": example.format(minutes)}, {}, {}
            ]}), encoding="utf-8")
            return validator.validate({"validator": "spec", "levels": ["l1"]}, work,
                                      {"task": "检查会议室冲突"})
        wrong = validate(60)
        self.assertEqual(wrong["l1"]["responsibility"]["stage"], "spec")
        self.assertIn("30", wrong["l1"]["detail"])
        self.assertTrue(validate(30)["passed"])

    def test_aa_contract_checks_each_expense_participant_list(self):
        given = "付款人=小李，金额=300，参与人=[小李, 小王, 小张]；付款人=小王，金额=120，参与人=[小王, 小张]"
        wrong = {"id": "AC-002", "given": given, "then": "小李净额 = +160"}
        issue = validator._split_bill_contract_issue("出游 AA 算账", [wrong])
        self.assertIn("200.00", issue)
        correct = {**wrong, "then": "小李净额 = +200，小王净额 = -40，小张净额 = -160"}
        self.assertIsNone(validator._split_bill_contract_issue("出游 AA 算账", [correct]))

    def test_coding_retry_when_candidate_contains_only_test_function(self):
        work = Path(self.tmp.name)
        (work / "main.py").write_text("def test_expense(): pass", encoding="utf-8")
        (work / "test_main.py").write_text("from main import add_expense", encoding="utf-8")
        result = validator.validate({"validator": "coding", "levels": ["l1"]}, work, {})
        self.assertEqual(result["l1"]["responsibility"]["stage"], "code")

    def test_custom_aa_plan_replaces_wrong_model_expectation(self):
        from app.planner import plan_custom

        def expense(payer, amount, people):
            return [
                {"kind": "fill", "target": "payer-input", "value": payer},
                {"kind": "fill", "target": "amount-input", "value": amount},
                {"kind": "fill", "target": "participants-input", "value": people},
                {"kind": "click", "target": "add-expense-button"},
            ]

        actions = expense("小李", "300", "小李,小王,小张")
        actions += expense("小王", "120", "小李,小王")
        actions.append({"kind": "expect_text", "target": "result-output", "value": "错误金额"})
        row = {"id": "build", "name": "制作", "desc": "制作 AA 工具", "role": "coder",
               "deps": [], "outputs": ["index.html"], "acceptance": ["错误金额"],
               "browser_cases": [{"criterion": 1, "actions": actions}]}
        response = SimpleNamespace(text=json.dumps([row]), model="actual-model", provider="provider")
        with patch("app.planner.llm.complete", return_value=response):
            scenario = plan_custom("出游 AA 算账", [])
        from app.planner import create_plan
        self.assertEqual(create_plan("AA", scenario, "live").to_dict()["planning_model"],
                         {"model": "actual-model", "provider": "provider"})
        node = scenario["nodes"][0]
        expected = "小王转给小李 40 元；小张转给小李 100 元"
        self.assertEqual(node["browser_cases"][0]["actions"][-1]["value"], expected)
        self.assertIn(expected, node["acceptance"][0])
        self.assertNotIn("错误金额", node["acceptance"][0])

    def test_aa_browser_expectation_distributes_remainder_in_input_order(self):
        from app.planner import _split_bill_expected

        actions = [
            {"kind": "fill", "target": "payer-input", "value": "小李"},
            {"kind": "fill", "target": "amount-input", "value": "100"},
            {"kind": "fill", "target": "participants-input", "value": "小王,小李,小张"},
            {"kind": "click", "target": "add-expense-button"},
            {"kind": "expect_text", "target": "result-output", "value": "待核算"},
        ]
        self.assertEqual(_split_bill_expected("AA 算账", actions),
                         "小王转给小李 33.34 元；小张转给小李 33.33 元")
        self.assertIsNone(_split_bill_expected("AA 算账，余分由付款人承担", actions))

    def test_confirmed_browser_cases_cannot_pass_without_browser(self):
        work = Path(self.tmp.name)
        (work / "index.html").write_text("<body>Report</body>", encoding="utf-8")
        node = {"validator": "deliverable", "outputs": ["index.html"], "levels": ["l1"]}
        unavailable = {"status": "unavailable", "detail": "浏览器未安装"}
        with patch("app.validator.check_page", return_value=unavailable):
            self.assertTrue(validator.validate(node, work, {})["passed"])
            node["browser_cases"] = [{"criterion": 1, "actions": [
                {"kind": "expect_text", "target": "report", "value": "Report"}
            ]}]
            verdict = validator.validate(node, work, {})
        self.assertFalse(verdict["passed"])
        self.assertEqual(verdict["l1"]["responsibility"]["stage"], "environment")

    def test_resume_reuses_only_passed_and_revalidated_dependencies(self):
        root = Path(self.tmp.name)
        original = root / "old" / "artifacts"
        original.mkdir(parents=True)
        (original / "rules.md").write_text("Accepted rules", encoding="utf-8")
        (original / "damaged.md").write_bytes(bytes([255]))
        (original / "index.html").write_text("<main>Rejected page</main>", encoding="utf-8")
        scenario = {"id": "custom", "nodes": [
            {"id": "rules", "name": "Rules", "role": "analyst", "deps": [],
             "outputs": ["rules.md"], "validator": "deliverable", "levels": ["l1"]},
            {"id": "damaged", "name": "Damaged", "role": "analyst", "deps": [],
             "outputs": ["damaged.md"], "validator": "deliverable", "levels": ["l1"]},
            {"id": "build", "name": "Build", "role": "coder", "deps": ["rules"],
             "outputs": ["index.html"], "validator": "deliverable"},
        ]}
        for nid, status in (("rules", "passed"), ("damaged", "passed"), ("build", "failed")):
            db.execute("INSERT INTO steps(run_id,node_id,status,attempt,verdict) VALUES(?,?,?,?,?)",
                       ("old", nid, status, 1, json.dumps({"passed": status == "passed"})))
        with patch("app.config.RUNS_DIR", root):
            engine = Engine("new", "task", scenario, "mock")
            engine.emit = lambda *args, **kwargs: None
            states, done = {}, {}
            engine._reuse_passed_steps("old", states, done)
        self.assertEqual(states, {"rules": "passed"})
        self.assertEqual(engine.ctx["artifacts"], {"rules.md": "Accepted rules"})
        self.assertFalse((engine.work / "index.html").exists())
        self.assertFalse((engine.work / "damaged.md").exists())

    def test_resume_feedback_preserves_multiple_browser_failures(self):
        engine = object.__new__(Engine)
        engine.scenario = {"reuse_from": "old"}
        verdict = {"passed": False, "detail": {"l1": {"browser": {"case_results": [
            {"status": "failed", "detail": "Expected 100, saw 90"},
            {"status": "failed", "detail": "Second payer missing"},
        ]}}}}
        db.execute("INSERT INTO steps(run_id,node_id,status,attempt,verdict) VALUES(?,?,?,?,?)",
                   ("old", "build", "failed", 1, json.dumps(verdict)))
        feedback = engine._resume_feedback("build")
        self.assertIn("Expected 100, saw 90", feedback)
        self.assertIn("Second payer missing", feedback)
        self.assertEqual(engine._resume_feedback("review"), "")

    def test_deliverable_rejects_invalid_structured_files(self):
        work = Path(self.tmp.name)
        cases = [
            ("report.json", '{"total":NaN}', False),
            ("report.json", '{"total":', False),
            ("report.json", '{"total":0}', True),
            ("report.csv", 'city,total\n杭州,10,20\n', False),
            ("report.csv", 'city,total\n"Hangzhou, China",10\n', True),
        ]
        for name, content, expected in cases:
            with self.subTest(name=name, content=content):
                (work / name).write_text(content, encoding="utf-8")
                node = {"validator": "deliverable", "outputs": [name], "levels": ["l1"]}
                self.assertEqual(validator.validate(node, work, {})["passed"], expected)

    def test_explicit_file_delivery_can_omit_html(self):
        from app.planner import plan_custom
        row = {"id": "build", "name": "Build", "desc": "Produce JSON", "role": "coder",
               "deps": [], "outputs": ["result.json"], "acceptance": ["Valid JSON"]}
        response = SimpleNamespace(text=json.dumps([row]))
        with patch("app.planner.llm.complete", return_value=response):
            plan = plan_custom('读取 orders.csv 并输出 "result.json"', [])
            self.assertEqual(plan["nodes"][0]["outputs"], ["result.json"])
            self.assertEqual(plan_custom("读取 orders.csv 并制作可操作报告", [])["nodes"][0]["outputs"], ["index.html"])

    def test_format_only_delivery_requires_format_and_keeps_interactive_entry(self):
        from app.planner import plan_custom
        row = {"id": "build", "name": "Build", "desc": "Produce CSV", "role": "coder",
               "deps": [], "outputs": ["report.csv"], "acceptance": ["Valid CSV"]}
        with patch("app.planner.llm.complete", return_value=SimpleNamespace(text=json.dumps([row]))):
            plan = plan_custom("汇总附件并导出为 CSV。", [])
            self.assertEqual(plan["nodes"][0]["outputs"], ["report.csv"])
            self.assertEqual(plan_custom("做个可操作工具，导出为 CSV。", [])["nodes"][0]["outputs"], ["result.csv", "index.html"])
            self.assertEqual(plan_custom("汇总附件并输出 JSON 格式。", [])["nodes"][0]["outputs"], ["result.json"])

    def test_planner_accepts_empty_fill_for_missing_phone_case(self):
        from app.planner import plan_custom
        row = {"id": "build", "name": "Build", "desc": "Check registration", "role": "coder",
               "deps": [], "outputs": ["index.html"], "acceptance": ["Show missing phone"],
               "browser_cases": [{"criterion": 1, "actions": [
                   {"kind": "fill", "target": "phone", "value": ""},
                   {"kind": "expect_text", "target": "result", "value": "Missing phone"},
               ]}]}
        with patch("app.planner.llm.complete", return_value=SimpleNamespace(text=json.dumps([row]))):
            plan = plan_custom("制作报名工具", [])
        self.assertEqual(plan["nodes"][0]["browser_cases"][0]["actions"][0]["value"], "")

    def test_invalid_first_plan_hands_feedback_to_backup_provider(self):
        from app import llm as llm_module
        from app.planner import plan_custom
        row = {"id": "build", "name": "Build", "desc": "d", "role": "coder",
               "deps": [], "outputs": ["index.html"], "acceptance": ["check"]}
        calls = []

        def complete(system, user, **kwargs):
            calls.append((user, kwargs))
            if len(calls) == 1:
                return SimpleNamespace(text="[]")
            return SimpleNamespace(text=json.dumps([row]))

        with patch("app.planner.llm.complete", side_effect=complete), \
                patch.object(llm_module.llm, "available", return_value=True), \
                patch.object(llm_module.llm, "default_provider", "deepseek"):
            scenario = plan_custom("制作可操作页面", [])
        self.assertEqual(len(calls), 2)
        self.assertIsNone(calls[0][1].get("provider"))
        self.assertEqual(calls[1][1].get("provider"), "deepseek")
        self.assertIn("上一次计划被拒", calls[1][0])
        self.assertEqual(scenario["nodes"][0]["id"], "build")

    def test_invalid_plans_fall_back_to_manual_acceptance_without_losing_task(self):
        from app.planner import create_plan, plan_custom

        task = "制作活动报名名单工具，保留最后一次记录，列出手机号缺失者"
        with patch("app.planner.llm.complete", return_value=SimpleNamespace(text="[]")) as model:
            scenario = plan_custom(task, [])
        plan = create_plan(task, scenario, "live").to_dict()
        self.assertEqual(model.call_count, 2)
        self.assertEqual(plan["task"], task)
        self.assertEqual(plan["provenance"], "deterministic")
        self.assertEqual(plan["nodes"][0]["outputs"], ("index.html",))
        self.assertFalse(plan["nodes"][0]["config"].get("browser_cases"))

    def test_event_stream_is_ordered_and_has_provenance(self):
        db.execute("INSERT INTO runs(id,task,scenario,status,mode,created_at) VALUES(?,?,?,?,?,?)",
                   ("r", "t", "s", "running", "mock", db.now()))
        store = EventStore()
        first = store.append("r", "plan.created", "planner", {"v": 1}, provenance="replay")
        second = store.append("r", "worker.started", "worker", {}, session_id="w", provenance="replay")
        rows = store.list("r", after_id=first.id)
        self.assertEqual([row.id for row in rows], [second.id])
        self.assertEqual(rows[0].provenance, "replay")

    def test_run_schema_persists_generated_plan(self):
        columns = {row["name"] for row in db.query("PRAGMA table_info(runs)")}
        self.assertIn("plan", columns)
        self.assertIn("plan_version", columns)

    def test_concurrent_engines_keep_scheduler_session_state_isolated(self):
        first = Engine("run-a", "task a", SCENARIO, "mock")
        second = Engine("run-b", "task b", SCENARIO, "mock")

        first.scheduler.session["coder-fast"] = {"ok": 2, "fail": 0, "consecutive": 0}
        first.scheduler.inflight["coder-fast"] = 1
        second.scheduler.reset_session()

        self.assertEqual(first.scheduler.session["coder-fast"]["ok"], 2)
        self.assertEqual(first.scheduler.inflight["coder-fast"], 1)
        self.assertIsNot(first.scheduler, second.scheduler)

    def test_emit_rolls_back_trace_when_event_insert_fails(self):
        db.execute("INSERT INTO runs(id,task,scenario,status,mode,created_at) VALUES(?,?,?,?,?,?)",
                   ("r", "t", "software_delivery", "running", "mock", db.now()))
        db.execute("CREATE TRIGGER reject_event BEFORE INSERT ON events "
                   "BEGIN SELECT RAISE(FAIL, 'event rejected'); END")
        engine = object.__new__(Engine)
        engine.run_id = "r"

        with self.assertRaises(sqlite3.IntegrityError):
            engine.emit("spec", "state", "ready")

        trace_count = db.one("SELECT COUNT(*) AS total FROM traces WHERE run_id=?", ("r",))
        event_count = db.one("SELECT COUNT(*) AS total FROM events WHERE run_id=?", ("r",))
        self.assertEqual(trace_count["total"], 0)
        self.assertEqual(event_count["total"], 0)

    def test_context_uses_prior_lesson_and_only_declared_dependency(self):
        db.execute("INSERT INTO lessons(run_id,signature,strategy,patch,hits,created_at) VALUES(?,?,?,?,?,?)",
                   ("old", "code:LOGIC_DEFECT", "tests", "fix boundary", 3, db.now()))
        plan = ExecutionPlan("s", "task", "mock", [
            PlanNode("spec", "Spec", "analyst", (), ("spec.json",), "json", 1),
            PlanNode("code", "Code", "coder", ("spec",), ("main.py",), "coding", 2),
        ])
        snapshot = ContextAssembler().build(
            plan.nodes[1], plan, {"spec": {"spec.json": "ok"}, "other": {"secret": "no"}},
            "", scenario_id="s",
        )
        self.assertIn("spec.json", snapshot.upstream)
        self.assertNotIn("secret", snapshot.upstream)
        self.assertEqual(snapshot.lesson_ids, (1,))

    def test_optimizer_switches_agent_after_failed_verification(self):
        report = VerificationReport(
            passed=False, reviewer_id="verifier-1", producer_session_id="w1",
            evidence={"failures": ["edge"]}, reasons=("edge failed",),
            provenance="deterministic", score=20,
        )
        decision = Optimizer().decide(report, attempt=1, max_attempts=3,
                                      current_agent="fast", ranked_agents=["fast", "pro"])
        self.assertEqual(decision.action, "switch_agent")
        self.assertEqual(decision.next_agent, "pro")

    def test_live_generation_failure_never_falls_back_to_replay_artifacts(self):
        class FailingGateway:
            @staticmethod
            def available():
                return True

            @staticmethod
            def complete(*args, **kwargs):
                raise GatewayError("provider unavailable")

        class ReplayProducer:
            @staticmethod
            def produce(*args, **kwargs):
                raise AssertionError("live mode must not call scenario replay producer")

        engine = object.__new__(Engine)
        engine.mode = "live"
        engine.mod = ReplayProducer()
        engine.ctx = {}
        engine.emit = lambda *args, **kwargs: None
        node = {"id": "code", "outputs": ["main.py"]}
        agent = {"model": "deepseek-flash"}

        with patch("app.engine.llm", FailingGateway()):
            with self.assertRaisesRegex(GatewayError, "provider unavailable"):
                engine._generate(node, agent, "system", "prompt", 1)

    def test_json_prompt_declares_required_root_fields(self):
        node = {
            "id": "risk", "name": "风险审查", "role": "reviewer", "deps": [],
            "outputs": ["risk.json"], "validator": "json", "max_attempts": 1,
            "required": ["risks", "mitigations"], "desc": "识别风险",
        }
        engine = object.__new__(Engine)
        engine.mode = "live"
        engine.mod = SimpleNamespace(SYSTEM_PROMPTS={})
        engine.task = "review"
        engine.ctx = {"kb": {}, "artifacts": {}}
        engine.scenario = {"id": "demo", "nodes": [node]}
        engine.plan = ExecutionPlan("demo", "review", "live", [
            PlanNode("risk", "风险审查", "reviewer", (), ("risk.json",), "json", 1)
        ])
        engine.context_assembler = ContextAssembler()
        engine.emit = lambda *args, **kwargs: None

        prompt, _ = engine._build_prompt(node, "", 1)

        self.assertIn('根对象必须直接包含字段："risks", "mitigations"', prompt)
        self.assertIn("不要添加外层包装对象", prompt)

    def test_delivery_review_receives_every_required_upstream_artifact_and_can_retry(self):
        review = next(node for node in SCENARIO["nodes"] if node["id"] == "review")

        self.assertEqual(
            review["deps"], ["spec", "test", "risk", "code", "hardening"]
        )
        self.assertEqual(review["max_attempts"], 2)

    def test_worker_receives_run_inputs_and_prompt_lists_them_without_binary_data(self):
        node = {
            "id": "spec", "name": "需求解析", "role": "analyst", "deps": [],
            "outputs": ["spec.json"], "validator": "spec", "max_attempts": 2,
            "desc": "分析附件",
        }
        engine = object.__new__(Engine)
        engine.mode = "live"
        engine.mod = SimpleNamespace(SYSTEM_PROMPTS={})
        engine.task = "analyze"
        engine.run_dir = Path(self.tmp.name) / "run"
        inputs = engine.run_dir / "inputs"
        inputs.mkdir(parents=True)
        (inputs / "brief.txt").write_text("requirements", encoding="utf-8")
        (inputs / "screen.png").write_bytes(b"\x89PNG\x00binary")
        engine.inputs = inputs
        engine.ctx = {"kb": {}, "artifacts": {}, "attachments": [
            {"name": "brief.txt", "kind": "text", "mime": "text/plain", "size": 12},
            {"name": "screen.png", "kind": "image", "mime": "image/png", "size": 11},
        ]}
        engine.scenario = {"id": "demo", "nodes": [node]}
        engine.plan = ExecutionPlan("demo", "analyze", "live", [
            PlanNode("spec", "需求解析", "analyst", (), ("spec.json",), "spec", 2)
        ])
        engine.context_assembler = ContextAssembler()
        engine.emit = lambda *args, **kwargs: None
        worker = Path(self.tmp.name) / "worker"
        worker.mkdir()

        engine._prepare_worker_workspace(worker, "analyst", node)
        prompt, _ = engine._build_prompt(node, "", 1)

        self.assertEqual((worker / "inputs" / "brief.txt").read_text(), "requirements")
        self.assertEqual((worker / "inputs" / "screen.png").read_bytes(), b"\x89PNG\x00binary")
        self.assertIn("inputs/brief.txt", prompt)
        self.assertIn("inputs/screen.png", prompt)
        self.assertNotIn("binary", prompt)

    def test_review_prompt_requires_exact_english_verdict_enum(self):
        node = {
            "id": "review", "name": "交付评审", "role": "reviewer", "deps": [],
            "outputs": ["review.json"], "validator": "review", "max_attempts": 2,
            "desc": "独立复核并出交付结论",
        }
        engine = object.__new__(Engine)
        engine.mode = "live"
        engine.mod = SimpleNamespace(SYSTEM_PROMPTS={})
        engine.task = "review"
        engine.ctx = {"kb": {}, "artifacts": {}}
        engine.scenario = {"id": "demo", "nodes": [node]}
        engine.plan = ExecutionPlan("demo", "review", "live", [
            PlanNode("review", "交付评审", "reviewer", (), ("review.json",), "review", 2)
        ])
        engine.context_assembler = ContextAssembler()
        engine.emit = lambda *args, **kwargs: None

        prompt, _ = engine._build_prompt(node, "", 1)

        self.assertIn('"verdict" 的值必须是精确字符串 "pass" 或 "fail"', prompt)

    def test_test_design_prompt_requires_acceptance_ids_and_external_implementation(self):
        node = {
            "id": "test", "name": "测试设计", "role": "tester", "deps": [],
            "outputs": ["test_main.py"], "validator": "test", "max_attempts": 2,
            "desc": "每条验收标准一个用例",
        }
        engine = object.__new__(Engine)
        engine.mode = "live"
        engine.mod = SimpleNamespace(SYSTEM_PROMPTS={})
        engine.task = "test"
        engine.ctx = {"kb": {}, "artifacts": {}}
        engine.scenario = {"id": "demo", "nodes": [node]}
        engine.plan = ExecutionPlan("demo", "test", "live", [
            PlanNode("test", "测试设计", "tester", (), ("test_main.py",), "test", 2)
        ])
        engine.context_assembler = ContextAssembler()
        engine.emit = lambda *args, **kwargs: None

        prompt, _ = engine._build_prompt(node, "", 1)

        self.assertIn("每条验收标准的精确 ID", prompt)
        self.assertIn("从 main.py 导入被测对象", prompt)
        self.assertIn("不得在测试文件内实现被测逻辑", prompt)
        self.assertIn("禁止使用 subprocess", prompt)

    def test_spec_prompt_caps_acceptance_criteria_for_downstream_execution(self):
        node = {
            "id": "spec", "name": "需求解析", "role": "analyst", "deps": [],
            "outputs": ["spec.json"], "validator": "spec", "max_attempts": 2,
            "desc": "拆出可执行的验收标准",
        }
        engine = object.__new__(Engine)
        engine.mode = "live"
        engine.mod = SimpleNamespace(SYSTEM_PROMPTS={})
        engine.task = "spec"
        engine.ctx = {"kb": {}, "artifacts": {}}
        engine.scenario = {"id": "demo", "nodes": [node]}
        engine.plan = ExecutionPlan("demo", "spec", "live", [
            PlanNode("spec", "需求解析", "analyst", (), ("spec.json",), "spec", 2)
        ])
        engine.context_assembler = ContextAssembler()
        engine.emit = lambda *args, **kwargs: None

        prompt, _ = engine._build_prompt(node, "", 1)

        self.assertIn("acceptance_criteria 必须为 4 到 6 条", prompt)

    def test_test_design_cannot_write_implementation_or_execute_tests(self):
        def complete(*args, **kwargs):
            execute = kwargs["tool_executor"]
            names = {entry["function"]["name"] for entry in kwargs["tool_definitions"]}
            self.assertNotIn("run_tests", names)
            self.assertFalse(execute("run_tests", {})["ok"])
            self.assertFalse(execute("write_file", {"path": "main.py", "content": ""})["ok"])
            self.assertTrue(execute("write_file", {
                "path": "test_main.py", "content": "import unittest",
            })["ok"])
            return SimpleNamespace(text="done", tokens=1, latency=0, model="mimo", provider="mimo")

        engine = object.__new__(Engine)
        engine.mode = "live"
        engine.ctx = {}
        engine.emit = lambda *args, **kwargs: None
        node = {"id": "test", "role": "tester", "validator": "test", "outputs": ["test_main.py"]}
        with tempfile.TemporaryDirectory() as td, patch("app.engine.llm", SimpleNamespace(complete=complete)):
            artifacts, _ = engine._generate(node, {"model": "mimo"}, "system", "task", 1, Path(td))
            self.assertFalse((Path(td) / "main.py").exists())
        self.assertEqual(artifacts, {"test_main.py": "import unittest"})

    def test_coding_round_limit_submits_written_candidate_for_validation(self):
        from app.llm import ToolRoundLimitError

        def complete(*args, **kwargs):
            kwargs["tool_executor"]("write_file", {"path": "main.py", "content": "VALUE = 42\n"})
            raise ToolRoundLimitError(6, 19, "mimo", "mimo")

        engine = object.__new__(Engine)
        engine.mode = "live"
        engine.ctx = {}
        engine.emit = lambda *args, **kwargs: None
        node = {"id": "code", "role": "coder", "validator": "coding", "outputs": ["main.py"]}
        with tempfile.TemporaryDirectory() as td, patch("app.engine.llm", SimpleNamespace(complete=complete)):
            artifacts, metadata = engine._generate(node, {"model": "mimo"}, "system", "task", 1, Path(td))
        self.assertEqual(artifacts, {"main.py": "VALUE = 42\n"})
        self.assertEqual(metadata["tokens"], 19)

    def test_budget_exhaustion_submits_only_complete_written_candidates(self):
        from app.llm import ModelBudgetExceeded

        engine = object.__new__(Engine)
        engine.mode = "live"
        engine.ctx = {}
        engine.emit = lambda *args, **kwargs: None
        node = {"id": "build", "role": "coder", "validator": "deliverable",
                "outputs": ["index.html", "notes.md"]}

        def complete(*args, **kwargs):
            write = kwargs["tool_executor"]
            write("write_file", {"path": "index.html", "content": "<body>Report</body>"})
            if write_notes:
                write("write_file", {"path": "notes.md", "content": "Notes"})
            error = ModelBudgetExceeded("budget")
            error.tokens = 29
            raise error

        with tempfile.TemporaryDirectory() as td, patch("app.engine.llm", SimpleNamespace(complete=complete)):
            write_notes = False
            with self.assertRaises(ModelBudgetExceeded):
                engine._generate(node, {"model": "mimo"}, "system", "task", 1, Path(td))
            write_notes = True
            artifacts, metadata = engine._generate(node, {"model": "mimo"}, "system", "task", 1, Path(td))
        self.assertEqual(set(artifacts), {"index.html", "notes.md"})
        self.assertEqual(metadata["tokens"], 29)

    def test_live_generation_uses_workspace_tools_and_promotes_declared_output(self):
        class ToolUsingGateway:
            @staticmethod
            def complete(*args, **kwargs):
                executor = kwargs["tool_executor"]
                upstream = executor("read_file", {"path": "spec.json"})
                self.assertEqual(upstream["content"], '{"goal":"demo"}')
                written = executor(
                    "write_file", {"path": "main.py", "content": "VALUE = 42\n"}
                )
                self.assertTrue(written["ok"])
                return SimpleNamespace(
                    text="产物已写入",
                    tokens=20,
                    latency=0.1,
                    model="deepseek-flash",
                    provider="deepseek",
                    tool_calls=2,
                    tool_trace=(
                        {"name": "read_file", "round": 1, "ok": True, "path": "spec.json"},
                        {"name": "write_file", "round": 1, "ok": True, "path": "main.py"},
                    ),
                )

        engine = object.__new__(Engine)
        engine.mode = "live"
        engine.ctx = {"artifacts": {"spec.json": '{"goal":"demo"}'}}
        engine.scenario = {"nodes": [
            {"id": "spec", "outputs": ["spec.json"]},
            {"id": "code", "deps": ["spec"], "outputs": ["main.py"]},
        ]}
        node = {
            "id": "code", "role": "coder", "deps": ["spec"], "outputs": ["main.py"]
        }
        agent = {"model": "deepseek-flash"}

        with tempfile.TemporaryDirectory() as td:
            worker = Path(td)
            engine._prepare_worker_workspace(worker, "coder", node)
            with patch("app.engine.llm", ToolUsingGateway()):
                artifacts, metadata = engine._generate(
                    node, agent, "system", "prompt", 1, worker_dir=worker
                )

        self.assertEqual(artifacts, {"main.py": "VALUE = 42\n"})
        self.assertEqual(metadata["tool_calls"], 2)
        self.assertEqual(metadata["tool_trace"][1]["path"], "main.py")

    def test_live_generation_exposes_read_only_github_mcp_tools(self):
        class RemoteTools:
            def definitions(self):
                return [{"type": "function", "function": {
                    "name": "github__get_file_contents",
                    "parameters": {"type": "object"},
                }}]

            def execute(self, name, arguments):
                return {"ok": True, "server": "github", "repository": "acme/widget"}

        class InspectingGateway:
            @staticmethod
            def complete(*args, **kwargs):
                names = {item["function"]["name"] for item in kwargs["tool_definitions"]}
                self.assertIn("write_file", names)
                self.assertIn("github__get_file_contents", names)
                github = kwargs["tool_executor"](
                    "github__get_file_contents", {"owner": "acme", "repo": "widget"}
                )
                self.assertEqual(github["repository"], "acme/widget")
                kwargs["tool_observer"]({
                    "name": "github__get_file_contents", "round": 1, "ok": True,
                    "source": "mcp", "server": "github", "repository": "acme/widget",
                })
                written = kwargs["tool_executor"](
                    "write_file", {"path": "main.py", "content": "VALUE = 42\n"}
                )
                self.assertTrue(written["ok"])
                return SimpleNamespace(
                    text="done", tokens=5, latency=0.1, model="deepseek-flash",
                    provider="deepseek", tool_calls=2, tool_trace=(),
                )

        engine = object.__new__(Engine)
        engine.mode = "live"
        engine.github_mcp = RemoteTools()
        observed_events = []
        engine.emit = lambda *args, **kwargs: observed_events.append((args, kwargs))
        engine.ctx = {"artifacts": {}}
        engine.scenario = {"nodes": [{"id": "code", "deps": [], "outputs": ["main.py"]}]}
        node = {"id": "code", "role": "coder", "deps": [], "outputs": ["main.py"]}

        with tempfile.TemporaryDirectory() as td:
            with patch("app.engine.llm", InspectingGateway()):
                artifacts, _ = engine._generate(
                    node, {"model": "deepseek-flash"}, "system", "prompt", 1,
                    worker_dir=Path(td),
                )

        self.assertEqual(artifacts, {"main.py": "VALUE = 42\n"})
        kinds = [args[1] for args, _ in observed_events]
        self.assertIn("mcp.tools.discovered", kinds)
        self.assertIn("mcp.call.completed", kinds)

    def test_live_prompt_tells_worker_to_write_declared_outputs_with_tools(self):
        node = {
            "id": "code", "name": "编码实现", "role": "coder", "deps": [],
            "outputs": ["main.py"], "validator": "coding", "max_attempts": 1,
            "desc": "实现功能",
        }
        engine = object.__new__(Engine)
        engine.mode = "live"
        engine.mod = SimpleNamespace(SYSTEM_PROMPTS={})
        engine.task = "code"
        engine.ctx = {"kb": {}, "artifacts": {}}
        engine.scenario = {"id": "demo", "nodes": [node]}
        engine.plan = ExecutionPlan("demo", "code", "live", [
            PlanNode("code", "编码实现", "coder", (), ("main.py",), "coding", 1)
        ])
        engine.context_assembler = ContextAssembler()
        engine.emit = lambda *args, **kwargs: None

        prompt, _ = engine._build_prompt(node, "", 1)

        self.assertIn("调用 write_file", prompt)
        self.assertIn("main.py", prompt)
        self.assertIn("写入 main.py 的内容", prompt)
        self.assertNotIn("只输出一个完整 Python 代码块", prompt)

    def test_worker_workspace_only_receives_declared_dependencies(self):
        engine = object.__new__(Engine)
        engine.ctx = {
            "artifacts": {"spec.json": "allowed", "sibling-secret.json": "blocked"}
        }
        engine.scenario = {"nodes": [
            {"id": "spec", "outputs": ["spec.json"]},
            {"id": "sibling", "outputs": ["sibling-secret.json"]},
            {"id": "code", "deps": ["spec"], "outputs": ["main.py"]},
        ]}
        node = engine.scenario["nodes"][2]

        with tempfile.TemporaryDirectory() as td:
            worker = Path(td)
            engine._prepare_worker_workspace(worker, "coder", node)
            self.assertTrue((worker / "spec.json").is_file())
            self.assertFalse((worker / "sibling-secret.json").exists())


if __name__ == "__main__":
    unittest.main()
