import contextlib
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.request import urlopen

from app.server import Handler


class FrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def get(self, path):
        with contextlib.closing(urlopen(self.base_url + path)) as response:
            return response.status, response.headers, response.read().decode("utf-8")

    def test_both_product_pages_load_the_shared_theme(self):
        for path in ("/", "/workbench"):
            with self.subTest(path=path):
                status, _, html = self.get(path)
                self.assertEqual(status, 200)
                self.assertIn('href="/static/theme.css"', html)

        status, headers, css = self.get("/static/theme.css")
        self.assertEqual(status, 200)
        self.assertIn("text/css", headers.get_content_type())
        self.assertGreater(len(css), 1000)

    def test_both_pages_preserve_selected_run_in_navigation_and_location(self):
        _, _, monitor = self.get("/")
        _, _, workbench = self.get("/workbench")

        self.assertIn('id="workbench-link"', monitor)
        self.assertIn("syncRunNavigation(id)", monitor)
        self.assertIn('history.replaceState(null,"","?run="+encodeURIComponent(id))', monitor)
        self.assertIn('id="monitor-nav-link"', workbench)
        self.assertIn("syncRunNavigation(id)", workbench)
        self.assertIn('history.replaceState(null,"","?run="+encodeURIComponent(id))', workbench)

    def test_new_task_clears_the_run_query(self):
        _, _, workbench = self.get("/workbench")

        self.assertIn('history.replaceState(null,"","/workbench")', workbench)

    def test_workbench_exposes_file_upload_preview_and_run_attachment_contract(self):
        _, _, workbench = self.get("/workbench")

        self.assertIn('type="file"', workbench)
        self.assertIn('id="attachment-input"', workbench)
        self.assertIn('accept=".txt,.md,.csv,.json,.yaml,.yml,.py,.js,.ts,.html,.css,.xml,.pdf,.docx,.xlsx,image/png,image/jpeg,image/gif,image/webp"', workbench)
        self.assertIn('api("/api/uploads"', workbench)
        self.assertIn("upload_ids:ATTACHMENTS.map", workbench)
        self.assertIn('id="input-attachments"', workbench)

    def test_monitor_exposes_agent_panel_and_handoff_aware_dag(self):
        _, _, monitor = self.get("/")

        self.assertIn('data-tab="agents"', monitor)
        self.assertIn('id="agents"', monitor)
        self.assertIn("renderAgents", monitor)
        self.assertIn('api("/api/agents/rank?role="', monitor)
        self.assertIn('method: "PUT"', monitor)
        self.assertIn('method: "DELETE"', monitor)
        self.assertIn("artifact.handoff", monitor)
        self.assertIn("dep.outputs", monitor)
        self.assertIn("选择理由", monitor)

    def test_workbench_exposes_selfheal_demo_and_narrative(self):
        _, _, workbench = self.get("/workbench")

        self.assertIn('id="selfheal-demo"', workbench)
        self.assertIn('api("/api/demo/selfheal"', workbench)
        self.assertIn("renderNarrative", workbench)
        self.assertIn("selfheal-narrative", workbench)
        self.assertIn("平台核算拒绝", workbench)
        self.assertIn("携带证据修复", workbench)
        self.assertIn("preview.plan.complexity", workbench)


if __name__ == "__main__":
    unittest.main()
