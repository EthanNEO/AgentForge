import json
import tempfile
import unittest
from pathlib import Path

from app.validator import validate


class DeliveryReviewTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.work = Path(temp.name)
        (self.work / "test_main.py").write_text("test_empty", encoding="utf-8")
        self.ctx = {"spec": {"acceptance_criteria": [{"id": "AC-001", "text": "空输入返回零"}]}}
        self.node = {"validator": "review", "levels": ["l1"]}

    def review(self, report):
        (self.work / "review.json").write_text(json.dumps(report), encoding="utf-8")
        return validate(self.node, self.work, self.ctx)

    def test_bare_verdict_is_rejected(self):
        self.assertFalse(self.review({"verdict": "pass"})["passed"])

    def test_report_must_cover_exact_ids_and_reference_existing_evidence(self):
        item = {"id": "AC-001", "result": "pass", "file": "test_main.py", "reason": "test_empty 断言结果为零"}
        for criteria in (
            [],
            [item, item],
            [{**item, "id": "AC-999"}],
            [{**item, "file": "absent.py"}],
            [{**item, "reason": " "}],
            [{**item, "result": "fail"}],
        ):
            with self.subTest(criteria=criteria):
                self.assertFalse(self.review({"verdict": "pass", "criteria": criteria})["passed"])

    def test_complete_report_exposes_requirements_and_evidence(self):
        item = {"id": "AC-001", "result": "pass", "file": "test_main.py", "reason": "test_empty 断言结果为零"}
        verdict = self.review({"verdict": "pass", "criteria": [item]})
        self.assertTrue(verdict["passed"])
        self.assertEqual(verdict["l1"].get("criteria"), [{**item, "text": "空输入返回零"}])

    def test_live_spec_description_is_displayed(self):
        self.ctx["spec"]["acceptance_criteria"] = [
            {"id": "AC-001", "title": "空输入", "description": "空输入返回零"},
        ]
        item = {"id": "AC-001", "result": "pass", "file": "test_main.py", "reason": "空输入测试断言零"}
        verdict = self.review({"verdict": "pass", "criteria": [item]})
        self.assertEqual(verdict["l1"]["criteria"][0]["text"], "空输入返回零")
