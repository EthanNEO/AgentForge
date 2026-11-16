from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.function_tools import CompositeToolExecutor, WorkspaceToolExecutor
from app.policy import ToolPolicy


class WorkspaceToolExecutorTests(unittest.TestCase):
    def test_composite_routes_github_tools_without_breaking_local_tools(self):
        class RemoteTools:
            def __init__(self):
                self.calls = []

            def definitions(self):
                return [{"type": "function", "function": {
                    "name": "github__get_file_contents",
                    "parameters": {"type": "object"},
                }}]

            def execute(self, name, arguments):
                self.calls.append((name, arguments))
                return {"ok": True, "server": "github", "repository": "acme/widget"}

        with tempfile.TemporaryDirectory() as td:
            policy = ToolPolicy.for_role("coder")
            local = WorkspaceToolExecutor(Path(td), policy)
            remote = RemoteTools()
            executor = CompositeToolExecutor(local, remote, policy)

            names = {item["function"]["name"] for item in executor.definitions()}
            local_result = executor.execute("list_files", {"path": "."})
            remote_result = executor.execute(
                "github__get_file_contents", {"owner": "acme", "repo": "widget"}
            )

        self.assertIn("list_files", names)
        self.assertIn("github__get_file_contents", names)
        self.assertTrue(local_result["ok"])
        self.assertEqual(remote_result["repository"], "acme/widget")
        self.assertEqual(remote.calls, [(
            "github__get_file_contents", {"owner": "acme", "repo": "widget"}
        )])

    def test_workspace_tools_write_read_list_and_run_real_tests(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            executor = WorkspaceToolExecutor(root, ToolPolicy.for_role("coder"))

            names = {
                item["function"]["name"] for item in executor.definitions()
            }
            self.assertEqual(
                names, {"list_files", "read_file", "write_file", "append_file", "run_tests"}
            )
            written = executor.execute(
                "write_file", {"path": "answer.py", "content": "VALUE = 42\n"}
            )
            read = executor.execute("read_file", {"path": "answer.py"})
            executor.execute(
                "write_file",
                {
                    "path": "test_answer.py",
                    "content": (
                        "import unittest\n"
                        "from answer import VALUE\n"
                        "class AnswerTests(unittest.TestCase):\n"
                        "    def test_value(self): self.assertEqual(VALUE, 42)\n"
                    ),
                },
            )
            listing = executor.execute("list_files", {"path": "."})
            tests = executor.execute("run_tests", {"pattern": "test_*.py"})

        self.assertTrue(written["ok"])
        self.assertEqual(read["content"], "VALUE = 42\n")
        self.assertEqual(listing["files"], ["answer.py", "test_answer.py"])
        self.assertTrue(tests["ok"])
        self.assertEqual(tests["ran"], 1)

    def test_workspace_tools_reject_unknown_and_out_of_root_operations(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            executor = WorkspaceToolExecutor(root, ToolPolicy.for_role("coder"))

            escaped = executor.execute(
                "write_file", {"path": "../escape.txt", "content": "no"}
            )
            unknown = executor.execute("run_shell", {"command": "whoami"})

            self.assertFalse((root.parent / "escape.txt").exists())

        self.assertFalse(escaped["ok"])
        self.assertIn("路径越界", escaped["error"])
        self.assertFalse(unknown["ok"])
        self.assertIn("未知工具", unknown["error"])

    def test_workspace_tools_do_not_allow_overwriting_injected_inputs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "inputs").mkdir()
            (root / "inputs" / "brief.txt").write_text("original", encoding="utf-8")
            executor = WorkspaceToolExecutor(root, ToolPolicy.for_role("coder"))

            result = executor.execute(
                "write_file", {"path": "inputs/brief.txt", "content": "changed"}
            )

            self.assertFalse(result["ok"])
            self.assertEqual((root / "inputs" / "brief.txt").read_text(encoding="utf-8"), "original")

    def test_append_file_extends_declared_output_in_chunks(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            executor = WorkspaceToolExecutor(
                root, ToolPolicy.for_role("coder"), writable_outputs=["index.html", "other.html"]
            )
            first = executor.execute("write_file", {"path": "index.html", "content": "<html>"})
            second = executor.execute("append_file", {"path": "index.html", "content": "<body>ok</body>"})
            third = executor.execute("append_file", {"path": "index.html", "content": "</html>"})
            missing = executor.execute("append_file", {"path": "other.html", "content": "x"})
            undeclared = executor.execute("append_file", {"path": "extra.html", "content": "x"})
            escaped = executor.execute("append_file", {"path": "../escape.txt", "content": "x"})
            content = (root / "index.html").read_text(encoding="utf-8")

        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertTrue(third["ok"])
        self.assertEqual(content, "<html><body>ok</body></html>")
        self.assertEqual(third["chars"], len(content))
        self.assertFalse(missing["ok"])
        self.assertIn("write_file", missing["error"])
        self.assertFalse(undeclared["ok"])
        self.assertFalse(escaped["ok"])


if __name__ == "__main__":
    unittest.main()
