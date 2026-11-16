import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import db
from app.browser_check import _conflict_facts
from app.context import ContextAssembler
from app.engine import Engine
from app.planner import (MEETING_SCENARIOS, _ground_meeting_checks, _is_meeting_task,
                         _meeting_expected, _meeting_user_example, create_plan, plan_custom)

MEETING_TASK = ("做一个会议室预约冲突检查工具。输入两条预约的房间和起止时间，判断是否冲突。"
                "浏览器验收：A房间09:00到10:00与09:30到10:30重叠30分钟，首尾相接不算冲突，"
                "不同房间同时段不算冲突。")


class MeetingExpectationTests(unittest.TestCase):
    def test_overlap_computed_independently(self):
        self.assertEqual(_meeting_expected(MEETING_SCENARIOS[0]), "重叠 30 分钟")
        self.assertEqual(_meeting_expected(MEETING_SCENARIOS[1]), "无冲突")
        self.assertEqual(_meeting_expected(MEETING_SCENARIOS[2]), "无冲突")
        self.assertEqual(_meeting_expected(MEETING_SCENARIOS[3]), "重叠 30 分钟")

    def test_adjacent_and_containment_edges(self):
        self.assertEqual(_meeting_expected({"room1": "A", "start1": "09:00", "end1": "10:00",
                                            "room2": "A", "start2": "10:00", "end2": "11:00"}), "无冲突")
        self.assertEqual(_meeting_expected({"room1": "A", "start1": "09:00", "end1": "11:00",
                                            "room2": "A", "start2": "09:30", "end2": "10:00"}), "重叠 30 分钟")
        self.assertEqual(_meeting_expected({"room1": "A", "start1": "09:00", "end1": "10:00",
                                            "room2": "A", "start2": "09:00", "end2": "10:00"}), "重叠 60 分钟")

    def test_recognizes_meeting_tasks_only(self):
        self.assertTrue(_is_meeting_task(MEETING_TASK))
        self.assertFalse(_is_meeting_task("会议室预约工具"))
        self.assertFalse(_is_meeting_task("出游 AA 算账"))

    def test_user_example_parsed_from_task(self):
        example = _meeting_user_example(MEETING_TASK)
        self.assertEqual(example, {"name": "用户示例", "room1": "A", "start1": "09:00",
                                   "end1": "10:00", "room2": "A", "start2": "09:30", "end2": "10:30"})
        self.assertIsNone(_meeting_user_example("做个会议室冲突检查工具"))

    def test_user_example_with_different_times_is_added(self):
        task = "会议室冲突检查。B房间14:00到15:00与14:15到15:30应该冲突。"
        node = {"outputs": ["index.html"], "acceptance": ["模型预期"], "desc": "d"}
        self.assertTrue(_ground_meeting_checks(task, node))
        self.assertEqual(len(node["browser_cases"]), 5)
        last = node["browser_cases"][-1]
        self.assertEqual(last["actions"][-1]["value"], "重叠 45 分钟")
        self.assertEqual([a["value"] for a in last["actions"] if a["kind"] == "fill"],
                         ["B", "14:00", "15:00", "B", "14:15", "15:30"])

    def test_duplicate_user_example_not_added_twice(self):
        node = {"outputs": ["index.html"], "acceptance": ["模型预期"], "desc": "d"}
        self.assertTrue(_ground_meeting_checks(MEETING_TASK, node))
        self.assertEqual(len(node["browser_cases"]), 4)


class MeetingPlanGroundingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.reset_connection(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        db.reset_connection()
        self.tmp.cleanup()

    def _model_row(self):
        return {"id": "build", "name": "制作会议室工具", "desc": "制作冲突检查工具",
                "role": "coder", "deps": [], "outputs": ["index.html"],
                "acceptance": ["页面标题显示会议室冲突检查"],
                "browser_cases": [{"criterion": 1, "actions": [
                    {"kind": "expect_text", "target": "app-title", "value": "会议室冲突检查"},
                ]}]}

    def test_platform_replaces_model_cases_with_computed_scenarios(self):
        response = SimpleNamespace(text=json.dumps([self._model_row()]), model="m", provider="p")
        with patch("app.planner.llm.complete", return_value=response):
            scenario = plan_custom(MEETING_TASK, [])
        node = scenario["nodes"][0]
        self.assertEqual(len(node["acceptance"]), 5)
        self.assertEqual(len(node["browser_cases"]), 4)
        self.assertTrue(all(case.get("grounded") for case in node["browser_cases"]))
        self.assertEqual(node["meeting_scenarios"], MEETING_SCENARIOS)
        self.assertIn("首尾相接不算冲突", node["acceptance"][0])
        self.assertIn("首尾相接不算冲突", node["desc"])
        values = [case["actions"][-1]["value"] for case in node["browser_cases"]]
        self.assertEqual(values, ["重叠 30 分钟", "无冲突", "无冲突", "重叠 30 分钟"])
        for case in node["browser_cases"]:
            self.assertEqual(case["actions"][-1]["kind"], "expect_conflict")
            self.assertEqual(case["actions"][-1]["target"], "result")
            self.assertEqual(len(case["actions"]), 8)
        plan = create_plan(MEETING_TASK, scenario, "live").to_dict()
        self.assertEqual(plan["nodes"][0]["config"]["meeting_scenarios"], MEETING_SCENARIOS)

    def test_grounding_requires_index_html_output(self):
        node = {"outputs": ["result.json"], "acceptance": ["x"], "desc": "d"}
        self.assertFalse(_ground_meeting_checks(MEETING_TASK, node))
        self.assertEqual(node["acceptance"], ["x"])

    def test_worker_prompt_contains_scenarios_and_contract(self):
        response = SimpleNamespace(text=json.dumps([self._model_row()]))
        with patch("app.planner.llm.complete", return_value=response):
            scenario = plan_custom(MEETING_TASK, [])
        node = scenario["nodes"][0]
        engine = object.__new__(Engine)
        engine.mode = "live"
        engine.mod = SimpleNamespace(SYSTEM_PROMPTS={})
        engine.task = MEETING_TASK
        engine.ctx = {"kb": {}, "artifacts": {}}
        engine.scenario = {"id": "custom", "nodes": [node]}
        engine.plan = create_plan(MEETING_TASK, scenario, "live")
        engine.context_assembler = ContextAssembler()
        engine.emit = lambda *args, **kwargs: None
        prompt, _ = engine._build_prompt(node, "", 1)
        self.assertIn("平台验收场景", prompt)
        self.assertIn("check-btn", prompt)
        self.assertIn("room1", prompt)
        self.assertIn("重叠 N 分钟", prompt)


class ConflictComparisonTests(unittest.TestCase):
    def test_wording_tolerant_but_fact_strict(self):
        self.assertEqual(_conflict_facts("冲突（房间 A）：两条预约重叠 30 分钟"), ("conflict", 30))
        self.assertEqual(_conflict_facts("重叠 30 分钟"), ("conflict", 30))
        self.assertEqual(_conflict_facts("无冲突（房间 A）：两条预约时间不重叠"), ("none", 0))
        self.assertEqual(_conflict_facts("无冲突"), ("none", 0))

    def test_wrong_minutes_or_missing_verdict_rejected(self):
        self.assertNotEqual(_conflict_facts("冲突：重叠 60 分钟"), _conflict_facts("重叠 30 分钟"))
        self.assertNotEqual(_conflict_facts("无冲突"), _conflict_facts("重叠 30 分钟"))
        self.assertIsNone(_conflict_facts("检查完成"))
        self.assertIsNone(_conflict_facts("冲突"))


if __name__ == "__main__":
    unittest.main()
