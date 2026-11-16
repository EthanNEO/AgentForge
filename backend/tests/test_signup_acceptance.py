import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import db
from app.browser_check import _name_set, _roster_rows
from app.context import ContextAssembler
from app.engine import Engine
from app.models import ExecutionPlan, PlanNode
from app.planner import (SIGNUP_ROSTER, _ground_signup_checks, _is_signup_task,
                         _signup_expected, create_plan, plan_custom)

SIGNUP_TASK = ("做个活动报名名单整理工具。报名名单里有人重复填，也有人忘了留手机号。"
               "把重复的人合并，只保留最后一次填写的信息。没有手机号的人单独列出来。"
               "告诉我有多少人可以确认报名。做一份假名单演示整理前后区别。")


class SignupExpectationTests(unittest.TestCase):
    def test_expected_merges_by_name_and_keeps_last_entry(self):
        expected = _signup_expected(SIGNUP_ROSTER)
        self.assertEqual(expected["raw_count"], 7)
        self.assertEqual(expected["merged_count"], 5)
        self.assertEqual(expected["confirmed"],
                         {"张三": "13900000003", "赵六": "13600000004", "孙七": "13600000004"})
        self.assertEqual(expected["missing"], ["李四", "王五"])

    def test_empty_last_phone_never_reuses_the_old_number(self):
        expected = _signup_expected([
            {"name": "甲", "phone": "13800000000"},
            {"name": "甲", "phone": ""},
        ])
        self.assertEqual(expected["confirmed"], {})
        self.assertEqual(expected["missing"], ["甲"])

    def test_same_phone_different_names_are_not_merged(self):
        expected = _signup_expected([
            {"name": "甲", "phone": "13800000000"},
            {"name": "乙", "phone": "13800000000"},
        ])
        self.assertEqual(expected["merged_count"], 2)
        self.assertEqual(len(expected["confirmed"]), 2)

    def test_recognizes_signup_tasks_only(self):
        self.assertTrue(_is_signup_task(SIGNUP_TASK))
        self.assertTrue(_is_signup_task("整理报名名单，重复的合并，缺手机号的列出"))
        self.assertFalse(_is_signup_task("制作报名工具"))
        self.assertFalse(_is_signup_task("出游 AA 算账，平摊费用"))


class SignupPlanGroundingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.reset_connection(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        db.reset_connection()
        self.tmp.cleanup()

    def _model_row(self):
        return {"id": "build", "name": "制作报名工具", "desc": "制作报名名单整理工具",
                "role": "coder", "deps": [], "outputs": ["index.html"],
                "acceptance": ["模型手算的错误预期"],
                "browser_cases": [{"criterion": 1, "actions": [
                    {"kind": "expect_text", "target": "result-output", "value": "张三转给张三 0 元"},
                ]}]}

    def test_platform_replaces_model_expectations_with_computed_cases(self):
        response = SimpleNamespace(text=json.dumps([self._model_row()]),
                                   model="m", provider="p")
        with patch("app.planner.llm.complete", return_value=response):
            scenario = plan_custom(SIGNUP_TASK, [])
        node = scenario["nodes"][0]
        self.assertEqual(node["demo_roster"], SIGNUP_ROSTER)
        self.assertEqual(len(node["acceptance"]), 7)
        self.assertEqual(len(node["browser_cases"]), 6)
        self.assertTrue(all(case.get("grounded") for case in node["browser_cases"]))
        self.assertIn("按姓名识别同一人", node["acceptance"][0])
        self.assertIn("按姓名识别同一人", node["desc"])
        by_criterion = {case["criterion"]: case for case in node["browser_cases"]}
        self.assertEqual(by_criterion[2]["actions"][-1]["value"], "整理前 7 条，整理后 5 人")
        self.assertEqual(by_criterion[3]["actions"][-1]["value"], "可确认报名 3 人")
        self.assertEqual(by_criterion[4]["actions"][-1]["kind"], "expect_names")
        self.assertEqual(by_criterion[4]["actions"][-1]["value"], "李四、王五")
        self.assertEqual(by_criterion[5]["actions"][-1]["kind"], "expect_roster")
        self.assertEqual(by_criterion[5]["actions"][-1]["value"],
                         "张三 13900000003；赵六 13600000004；孙七 13600000004")
        self.assertNotIn("张三转给张三", json.dumps(node, ensure_ascii=False))
        plan = create_plan(SIGNUP_TASK, scenario, "live").to_dict()
        self.assertEqual(plan["nodes"][0]["config"]["demo_roster"], SIGNUP_ROSTER)

    def test_manual_cases_cover_keep_last_and_empty_last_phone(self):
        response = SimpleNamespace(text=json.dumps([self._model_row()]))
        with patch("app.planner.llm.complete", return_value=response):
            scenario = plan_custom(SIGNUP_TASK, [])
        cases = {case["criterion"]: case for case in scenario["nodes"][0]["browser_cases"]}
        keep_last = cases[6]["actions"]
        self.assertEqual([a["value"] for a in keep_last if a["kind"] == "fill"],
                         ["测试甲", "13511110000", "测试甲", "13522220000"])
        self.assertEqual(keep_last[-2]["value"], "测试甲 13522220000")
        self.assertEqual(keep_last[-1]["value"], "可确认报名 1 人")
        empty_last = cases[7]["actions"]
        self.assertEqual([a["value"] for a in empty_last if a["kind"] == "fill"],
                         ["测试乙", "13533330000", "测试乙", ""])
        self.assertEqual(empty_last[-2]["value"], "测试乙")
        self.assertEqual(empty_last[-1]["value"], "可确认报名 0 人")
        for case in cases.values():
            self.assertLessEqual(len(case["actions"]), 12)

    def test_non_signup_tasks_keep_model_cases(self):
        row = {"id": "build", "name": "制作", "desc": "制作 AA 工具", "role": "coder",
               "deps": [], "outputs": ["index.html"], "acceptance": ["示例"],
               "browser_cases": [{"criterion": 1, "actions": [
                   {"kind": "fill", "target": "payer-input", "value": "小李"},
                   {"kind": "fill", "target": "amount-input", "value": "300"},
                   {"kind": "fill", "target": "participants-input", "value": "小李,小王,小张"},
                   {"kind": "click", "target": "add-expense-button"},
                   {"kind": "expect_text", "target": "result-output", "value": "待核算"},
               ]}]}
        with patch("app.planner.llm.complete", return_value=SimpleNamespace(text=json.dumps([row]))):
            scenario = plan_custom("出游 AA 算账", [])
        node = scenario["nodes"][0]
        self.assertNotIn("demo_roster", node)
        self.assertEqual(node["browser_cases"][0]["actions"][-1]["kind"], "expect_transfers")

    def test_fallback_plan_stays_manual_for_signup(self):
        with patch("app.planner.llm.complete", return_value=SimpleNamespace(text="[]")):
            scenario = plan_custom(SIGNUP_TASK, [])
        node = scenario["nodes"][0]
        self.assertEqual(scenario["provenance"], "deterministic")
        self.assertFalse(node.get("browser_cases"))
        self.assertNotIn("demo_roster", node)

    def test_grounding_requires_index_html_output(self):
        node = {"outputs": ["result.json"], "acceptance": ["x"], "desc": "d"}
        self.assertFalse(_ground_signup_checks(SIGNUP_TASK, node))
        self.assertEqual(node["acceptance"], ["x"])

    def test_worker_prompt_contains_roster_and_control_contract(self):
        response = SimpleNamespace(text=json.dumps([self._model_row()]))
        with patch("app.planner.llm.complete", return_value=response):
            scenario = plan_custom(SIGNUP_TASK, [])
        node = scenario["nodes"][0]
        engine = object.__new__(Engine)
        engine.mode = "live"
        engine.mod = SimpleNamespace(SYSTEM_PROMPTS={})
        engine.task = SIGNUP_TASK
        engine.ctx = {"kb": {}, "artifacts": {}}
        engine.scenario = {"id": "custom", "nodes": [node]}
        engine.plan = create_plan(SIGNUP_TASK, scenario, "live")
        engine.context_assembler = ContextAssembler()
        engine.emit = lambda *args, **kwargs: None
        prompt, _ = engine._build_prompt(node, "", 1)
        self.assertIn("平台假名单", prompt)
        self.assertIn("13900000003", prompt)
        self.assertIn("demo-button", prompt)
        self.assertIn("merged-list", prompt)
        self.assertIn("按姓名识别同一人", prompt)


class SignupIdentityClarificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.reset_connection(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        db.reset_connection()
        self.tmp.cleanup()

    def test_non_name_identity_cues_trigger_question(self):
        from app.planner import _signup_identity_question
        for task in ("报名名单整理，按手机号去重，重复的合并",
                     "报名工具，手机号相同算同一人，合并名单",
                     "报名名单，同名不同人要分开，按工号识别",
                     "报名名单按身份证号合并重复记录"):
            self.assertIsNotNone(_signup_identity_question(task), task)

    def test_original_task_and_other_tasks_have_no_question(self):
        from app.planner import _signup_identity_question
        self.assertIsNone(_signup_identity_question(SIGNUP_TASK))
        self.assertIsNone(_signup_identity_question("出游 AA 算账，平摊费用"))
        self.assertIsNone(_signup_identity_question("整理报名名单，没有手机号的单独列出"))

    def test_clarification_keeps_name_grounding_visible(self):
        from app.planner import SIGNUP_CLARIFICATION
        row = {"id": "build", "name": "制作", "desc": "制作报名工具", "role": "coder",
               "deps": [], "outputs": ["index.html"], "acceptance": ["x"]}
        task = "报名名单整理，按手机号去重，重复的合并，缺手机号的列出"
        with patch("app.planner.llm.complete", return_value=SimpleNamespace(text=json.dumps([row]))):
            scenario = plan_custom(task, [])
        self.assertEqual(scenario["clarification"], SIGNUP_CLARIFICATION)
        node = scenario["nodes"][0]
        self.assertEqual(node["demo_roster"], SIGNUP_ROSTER)
        self.assertIn("按姓名识别同一人", node["acceptance"][0])
        plan = create_plan(task, scenario, "live").to_dict()
        self.assertEqual(plan["clarification"], SIGNUP_CLARIFICATION)


class SignupBrowserComparisonTests(unittest.TestCase):
    def test_name_comparison_ignores_order_and_separator(self):
        self.assertEqual(_name_set("李四、王五"), _name_set("王五 李四"))
        self.assertEqual(_name_set("李四、王五"), _name_set("李四\n王五"))
        self.assertNotEqual(_name_set("李四、王五"), _name_set("李四"))
        self.assertNotEqual(_name_set("李四、王五"), _name_set("李四、王五、张三"))

    def test_roster_comparison_ignores_order_but_not_values(self):
        expected = "张三 13900000003；赵六 13600000004"
        self.assertEqual(_roster_rows(expected), _roster_rows("赵六 13600000004\n张三 13900000003"))
        self.assertEqual(_roster_rows(expected), _roster_rows("张三：13900000003；赵六：13600000004"))
        self.assertNotEqual(_roster_rows(expected), _roster_rows("张三 13800000001；赵六 13600000004"))
        self.assertNotEqual(_roster_rows(expected), _roster_rows("张三 13900000003"))
        self.assertIsNone(_roster_rows("张三"))

    def test_validator_runs_platform_cases_in_browser(self):
        from app import validator
        work = Path(tempfile.mkdtemp())
        (work / "index.html").write_text("<body>tool</body>", encoding="utf-8")
        response = SimpleNamespace(text=json.dumps([{
            "id": "build", "name": "制作", "desc": "制作", "role": "coder",
            "deps": [], "outputs": ["index.html"], "acceptance": ["x"],
        }]))
        with patch("app.planner.llm.complete", return_value=response):
            scenario = plan_custom(SIGNUP_TASK, [])
        node = {**scenario["nodes"][0], "validator": "deliverable", "levels": ["l1"]}
        seen = {}

        def fake_check(path, cases=()):
            seen["cases"] = cases
            return {"status": "passed", "verified_criteria": [c["criterion"] for c in cases],
                    "case_results": [{"criterion": c["criterion"], "status": "passed"} for c in cases],
                    "detail": "ok"}

        with patch("app.validator.check_page", side_effect=fake_check):
            verdict = validator.validate(node, work, {})
        self.assertTrue(verdict["passed"])
        self.assertEqual(len(seen["cases"]), 6)
        self.assertEqual(seen["cases"][3]["actions"][-1]["kind"], "expect_roster")


if __name__ == "__main__":
    unittest.main()
