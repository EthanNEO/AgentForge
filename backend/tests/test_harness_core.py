import tempfile
import time
import unittest
from pathlib import Path
import scenarios

from app.models import ExecutionPlan, PlanNode, WorkerSession
from app.planner import PlanError, create_plan, validate_plan
from app.policy import ToolPolicy
from app.watchdog import Watchdog
from app.selfloop import feedback_patch
from app import validator


class PlannerTests(unittest.TestCase):
    def test_rejects_cycle_before_dispatch(self):
        plan = ExecutionPlan("p", "t", "mock", [
            PlanNode("a", "A", "worker", ("b",), (), "generic", 1),
            PlanNode("b", "B", "worker", ("a",), (), "generic", 1),
        ])
        with self.assertRaises(PlanError):
            validate_plan(plan)

    def test_scenario_becomes_versioned_plan(self):
        scenario = {"id": "demo", "nodes": [{
            "id": "a", "name": "A", "role": "worker", "deps": [],
            "outputs": ["a.txt"], "validator": "json", "max_attempts": 2,
        }]}
        plan = create_plan("deliver", scenario, "mock")
        self.assertEqual(plan.scenario_id, "demo")
        self.assertEqual(plan.nodes[0].outputs, ("a.txt",))
        self.assertEqual(plan.provenance, "replay")

    def test_model_smoke_scenario_is_registered_and_requires_real_model(self):
        module = scenarios.get("model_smoke")

        self.assertEqual(module.SCENARIO["id"], "model_smoke")
        self.assertEqual(module.SCENARIO["nodes"][0]["required"], ["status", "summary"])
        with self.assertRaisesRegex(RuntimeError, "live"):
            module.produce({}, {}, 1)


class PolicyTests(unittest.TestCase):
    def test_role_policy_separates_read_from_execute(self):
        reviewer = ToolPolicy.for_role("reviewer")
        self.assertTrue(reviewer.authorize("read", write=False))
        self.assertFalse(reviewer.authorize("run_script", write=True))

    def test_artifact_path_cannot_escape_workspace(self):
        with tempfile.TemporaryDirectory() as td:
            policy = ToolPolicy.for_role("coder")
            with self.assertRaises(PermissionError):
                policy.resolve_path(Path(td), "../outside.py")


class WatchdogTests(unittest.TestCase):
    def test_overdue_worker_becomes_timeout_action(self):
        session = WorkerSession(
            id="w1", run_id="r1", node_id="n1", attempt=1, role="coder",
            agent_id="a1", workspace="x", status="running", started_at=10.0,
            deadline=20.0, provenance="replay",
        )
        actions = Watchdog().check(21.0, [session])
        self.assertEqual(actions[0].kind, "timeout")
        self.assertEqual(actions[0].session_id, "w1")


class FeedbackPatchTests(unittest.TestCase):
    def test_retry_patch_contains_concrete_verifier_evidence(self):
        attribution = {
            "patch": "请按失败用例修复。",
        }
        verdict = {
            "reasons": ["存在失败断言"],
            "detail": {
                "l1": {
                    "failures": ["test_cli @ test_main.TestCli.test_cli"],
                    "stderr": "TypeError: run_cli() got an unexpected keyword argument 'out'",
                },
                "l3": {"gaps": ["AC-005"]},
            },
        }

        patch = feedback_patch(attribution, verdict)

        self.assertIn("test_cli @ test_main.TestCli.test_cli", patch)
        self.assertIn("TypeError: run_cli() got an unexpected keyword argument 'out'", patch)
        self.assertIn("AC-005", patch)
        self.assertLessEqual(len(patch), 3500)


class ValidatorSafetyTests(unittest.TestCase):
    def test_test_design_rejects_recursive_subprocess_execution(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            (work / "test_main.py").write_text(
                "import unittest\nimport subprocess\n"
                "subprocess.run(['python', __file__])\n",
                encoding="utf-8",
            )
            node = {"validator": "test", "outputs": ["test_main.py"], "levels": ["l1"]}

            verdict = validator.validate(node, work, {})

        self.assertFalse(verdict["passed"])
        self.assertIn("递归", verdict["l1"]["detail"])


if __name__ == "__main__":
    unittest.main()
