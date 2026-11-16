import unittest

from app.models import ExecutionPlan, PlanNode
from app.supervisor import ready_nodes


class SupervisorTests(unittest.TestCase):
    def test_returns_all_parallel_ready_nodes(self):
        plan = ExecutionPlan("s", "t", "mock", [
            PlanNode("spec", "Spec", "analyst", (), (), "json", 1),
            PlanNode("test", "Test", "tester", ("spec",), (), "json", 1),
            PlanNode("risk", "Risk", "reviewer", ("spec",), (), "json", 1),
            PlanNode("code", "Code", "coder", ("test", "risk"), (), "coding", 1),
        ])
        self.assertEqual([n.id for n in ready_nodes(plan, {"spec": "passed"})], ["test", "risk"])

    def test_failed_dependency_blocks_downstream(self):
        plan = ExecutionPlan("s", "t", "mock", [
            PlanNode("a", "A", "analyst", (), (), "json", 1),
            PlanNode("b", "B", "coder", ("a",), (), "json", 1),
        ])
        self.assertEqual(ready_nodes(plan, {"a": "failed"}), [])


if __name__ == "__main__":
    unittest.main()
