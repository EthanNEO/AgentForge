import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config, db
from app.events import events
from app.llm import ModelGateway
from app.router import artifact_path, events_for_run, health, input_path, meta, preview_plan, retry_plan, run_detail, start_run


class ApiContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.reset_connection(Path(self.tmp.name) / "api.db")
        self.original_runs_dir = config.RUNS_DIR
        config.RUNS_DIR = Path(self.tmp.name) / "runs"
        plan = {"scenario_id": "software_delivery", "nodes": []}
        db.execute("INSERT INTO runs(id,task,scenario,status,mode,created_at,plan,plan_version) "
                   "VALUES(?,?,?,?,?,?,?,?)",
                   ("r", "task", "software_delivery", "running", "mock", db.now(),
                    json.dumps(plan), 1))
        db.execute("INSERT INTO worker_sessions(id,run_id,node_id,attempt,role,agent_id,workspace,status,"
                   "provenance,started_at,deadline) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   ("w", "r", "spec", 1, "analyst", "a", "x", "running", "replay", 1.0, 2.0))

    def tearDown(self):
        config.RUNS_DIR = self.original_runs_dir
        db.reset_connection()
        self.tmp.cleanup()

    def test_detail_exposes_plan_workers_events_and_provenance(self):
        events.append("r", "worker.started", "worker", {}, session_id="w", provenance="replay")
        detail = run_detail("r")
        self.assertEqual(detail["plan"]["scenario_id"], "software_delivery")
        self.assertEqual(detail["workers"][0]["id"], "w")
        self.assertEqual(detail["events"][0]["provenance"], "replay")

    def test_confirmed_run_includes_usage_from_rejected_planning_response(self):
        node = {"id": "build", "name": "制作", "desc": "生成报告", "role": "coder",
                "deps": [], "outputs": ["index.html"], "acceptance": ["显示报告正文"]}
        payloads = iter(["invalid json", json.dumps([node])])
        gateway = ModelGateway(
            environ={"AF_DEEPSEEK_API_KEY": "test"},
            transport=lambda **_: {"usage": {"total_tokens": 12},
                                   "choices": [{"message": {"content": next(payloads)}}]},
        )
        with patch("app.planner.llm", gateway), patch("threading.Thread"):
            draft = preview_plan({"scenario": "custom", "task": "生成报告"})
            self.assertEqual(db.one("SELECT COUNT(*) AS n FROM runs")["n"], 1)
            run = start_run({"scenario": "custom", "draft_id": draft["draft_id"]})
        usage = run_detail(run["run_id"])["model_usage"]
        self.assertEqual([(row["phase"], row["reported_tokens"]) for row in usage],
                         [("planning", 12), ("planning", 12)])
        self.assertEqual(run_detail("r")["model_usage"], [])

    def test_browser_preflight_keeps_draft_available_for_reconfirmation(self):
        node = {"id": "build", "name": "制作", "desc": "生成报告", "role": "coder",
                "deps": [], "outputs": ["index.html"], "acceptance": ["显示报告"],
                "browser_cases": [{"criterion": 1, "actions": [
                    {"kind": "expect_text", "target": "report", "value": "Report"}
                ]}]}
        with patch("app.router.plan_custom", return_value={"id": "custom", "name": "自由任务", "nodes": [node]}):
            draft = preview_plan({"scenario": "custom", "task": "生成报告"})
        body = {"scenario": "custom", "draft_id": draft["draft_id"]}
        with patch("app.router.check_browser_environment",
                   return_value={"status": "unavailable", "detail": "浏览器无法启动"}):
            with self.assertRaisesRegex(ValueError, "修复后可重新确认"):
                start_run(body)
        self.assertEqual(db.one("SELECT COUNT(*) AS n FROM runs")["n"], 1)
        with patch("app.router.check_browser_environment", return_value={"status": "passed"}), patch("threading.Thread"):
            run = start_run(body)
        self.assertEqual(run_detail(run["run_id"])["run"]["status"], "queued")

    def test_attachment_preparation_failure_keeps_confirmed_draft_retryable(self):
        scenario = {"id": "custom", "name": "自由任务", "nodes": [
            {"id": "build", "name": "制作", "role": "coder", "outputs": ["index.html"]}
        ]}
        with patch("app.router.plan_custom", return_value=scenario):
            draft = preview_plan({"scenario": "custom", "task": "生成报告"})
        body = {"scenario": "custom", "draft_id": draft["draft_id"]}
        with patch("app.router.materialize_uploads", side_effect=OSError("temporary copy failure")):
            with self.assertRaises(OSError):
                start_run(body)
        self.assertEqual(db.one("SELECT COUNT(*) AS n FROM runs")["n"], 1)
        with patch("threading.Thread") as thread:
            run = start_run(body)
            self.assertEqual(start_run(body), run)
        thread.return_value.start.assert_called_once()
        self.assertEqual(run_detail(run["run_id"])["plan"]["nodes"][0]["id"], "build")

    def test_worker_start_failure_is_visible_in_run_history(self):
        with patch("threading.Thread") as thread:
            thread.return_value.start.side_effect = RuntimeError("thread unavailable")
            result = start_run({"scenario": "software_delivery", "task": "生成报告"})
        run = run_detail(result["run_id"])["run"]
        self.assertEqual(run["status"], "failed")
        self.assertIn("无法启动后台执行", run["error"])
        self.assertIsNotNone(run["finished_at"])

    def test_concurrent_confirmation_runs_browser_preflight_once(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event

        scenario = {"id": "custom", "name": "自由任务", "nodes": [
            {"id": "build", "name": "制作", "role": "coder", "outputs": ["index.html"],
             "browser_cases": [{"criterion": 1, "actions": [{"kind": "expect_text", "target": "report", "value": "Report"}]}]}
        ]}
        with patch("app.router.plan_custom", return_value=scenario):
            draft = preview_plan({"scenario": "custom", "task": "生成报告"})
        body = {"scenario": "custom", "draft_id": draft["draft_id"]}
        entered, release = Event(), Event()

        def preflight():
            entered.set()
            if not release.wait(timeout=5):
                raise TimeoutError("confirmation did not release the browser check")
            return {"status": "passed"}

        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(lambda: None).result()
            with patch("app.router.check_browser_environment", side_effect=preflight) as check, patch("threading.Thread") as thread:
                first = pool.submit(start_run, body)
                try:
                    self.assertTrue(entered.wait(timeout=5))
                    with self.assertRaisesRegex(ValueError, "正在确认"):
                        start_run(body)
                finally:
                    release.set()
                run = first.result(timeout=5)
                self.assertEqual(start_run(body), run)
                check.assert_called_once()
                thread.return_value.start.assert_called_once()

    def test_unconfirmed_plan_survives_database_reopen(self):
        scenario = {"id": "custom", "name": "自由任务", "nodes": [
            {"id": "build", "name": "制作", "role": "coder", "outputs": ["index.html"]}
        ]}
        with patch("app.router.plan_custom", return_value=scenario):
            draft = preview_plan({"scenario": "custom", "task": "原始任务"})
        db.reset_connection(Path(self.tmp.name) / "api.db")
        with patch("threading.Thread"):
            run = start_run({"scenario": "custom", "draft_id": draft["draft_id"]})
        self.assertEqual(run_detail(run["run_id"])["run"]["task"], "原始任务")
        self.assertIsNone(db.one("SELECT id FROM plan_drafts WHERE id=?", (draft["draft_id"],)))

    def test_retry_uses_saved_plan_and_copies_original_inputs(self):
        plan = {"nodes": [{"id": "build", "name": "Build", "role": "coder",
                           "deps": [], "outputs": ["index.html"], "config": {"acceptance": ["Show totals"]}}]}
        db.execute("UPDATE runs SET scenario='custom',status='failed',plan=? WHERE id='r'",
                   (json.dumps(plan),))
        original = config.RUNS_DIR / "r" / "inputs"
        original.mkdir(parents=True)
        (original / "orders.csv").write_bytes(b"amount\n10\n")
        with patch("app.router.plan_custom") as planner, patch("threading.Thread"):
            draft = retry_plan("r")
            self.assertEqual([item["name"] for item in draft["attachments"]], ["orders.csv"])
            result = start_run({"scenario": "custom", "draft_id": draft["draft_id"]})
        planner.assert_not_called()
        run_id = result["run_id"]
        self.assertNotEqual(run_id, "r")
        self.assertEqual(run_detail(run_id)["run"]["source_run"], "r")
        self.assertEqual(run_detail("r")["run"]["status"], "failed")
        self.assertEqual(run_detail(run_id)["plan"]["nodes"][0]["config"]["acceptance"], ["Show totals"])
        self.assertEqual((config.RUNS_DIR / run_id / "inputs" / "orders.csv").read_bytes(),
                         (original / "orders.csv").read_bytes())

    def test_clarification_blocks_run_until_acknowledged(self):
        question = "任务似乎要求按手机号识别同一人，请确认按姓名识别或修改任务描述。"
        scenario = {"id": "custom", "name": "自由任务", "clarification": question, "nodes": [
            {"id": "build", "name": "制作", "role": "coder", "outputs": ["index.html"]}
        ]}
        with patch("app.router.plan_custom", return_value=scenario):
            draft = preview_plan({"scenario": "custom", "task": "报名名单按手机号去重"})
        self.assertEqual(draft["clarification"], question)
        body = {"scenario": "custom", "draft_id": draft["draft_id"]}
        with self.assertRaisesRegex(ValueError, "请确认"):
            start_run(body)
        self.assertIsNotNone(db.one("SELECT id FROM plan_drafts WHERE id=?", (draft["draft_id"],)))
        with patch("threading.Thread"):
            run = start_run({**body, "acknowledge_clarification": True})
        self.assertEqual(run_detail(run["run_id"])["run"]["status"], "queued")
        self.assertEqual(run_detail(run["run_id"])["plan"]["clarification"], question)

    def test_retry_carries_clarification_and_still_requires_ack(self):
        question = "请确认识别规则。"
        plan = {"clarification": question, "nodes": [
            {"id": "build", "name": "Build", "role": "coder", "deps": [],
             "outputs": ["index.html"], "config": {"acceptance": ["x"]}}]}
        db.execute("UPDATE runs SET scenario='custom',status='failed',plan=? WHERE id='r'",
                   (json.dumps(plan),))
        draft = retry_plan("r")
        self.assertEqual(draft["clarification"], question)
        with self.assertRaisesRegex(ValueError, "请确认识别规则"):
            start_run({"scenario": "custom", "draft_id": draft["draft_id"]})
        with patch("threading.Thread"):
            run = start_run({"scenario": "custom", "draft_id": draft["draft_id"],
                             "acknowledge_clarification": True})
        self.assertEqual(run_detail(run["run_id"])["run"]["status"], "queued")

    def test_provider_pin_is_validated_and_forwarded(self):
        scenario = {"id": "custom", "name": "自由任务", "nodes": [
            {"id": "build", "name": "制作", "role": "coder", "outputs": ["index.html"]}
        ]}
        with self.assertRaisesRegex(ValueError, "provider"):
            preview_plan({"scenario": "custom", "task": "生成报告", "provider": "unknown"})
        with patch("app.router.plan_custom", return_value=scenario) as planner:
            preview_plan({"scenario": "custom", "task": "生成报告", "provider": "mimo"})
        self.assertEqual(planner.call_args.kwargs.get("provider"), "mimo")

    def test_event_cursor_returns_only_newer_events(self):
        first = events.append("r", "one", "test", {}, provenance="deterministic")
        events.append("r", "two", "test", {}, provenance="deterministic")
        payload = events_for_run("r", {"after": [str(first.id)]})
        self.assertEqual([item["kind"] for item in payload["items"]], ["two"])

    def test_plan_preview_does_not_create_a_run(self):
        before = db.one("SELECT COUNT(*) AS total FROM runs")["total"]

        preview = preview_plan({"scenario": "software_delivery", "task": "构建订单统计工具"})

        after = db.one("SELECT COUNT(*) AS total FROM runs")["total"]
        self.assertEqual(before, after)
        self.assertEqual(preview["task"], "构建订单统计工具")
        self.assertEqual(preview["plan"]["scenario_id"], "software_delivery")
        self.assertGreater(len(preview["plan"]["nodes"]), 0)

    def test_detail_lists_only_final_run_artifacts(self):
        artifacts = config.RUNS_DIR / "r" / "artifacts"
        workers = config.RUNS_DIR / "r" / "workers" / "spec"
        artifacts.mkdir(parents=True)
        workers.mkdir(parents=True)
        (artifacts / "report.md").write_text("# result", encoding="utf-8")
        (workers / "private.txt").write_text("worker scratch", encoding="utf-8")

        detail = run_detail("r")

        self.assertEqual(detail["artifacts"], [{
            "name": "report.md",
            "size": 8,
            "url": "/api/runs/r/artifacts/report.md",
            "previewable": True,
        }])

    def test_detail_lists_run_inputs_separately_and_resolves_safe_download(self):
        inputs = config.RUNS_DIR / "r" / "inputs"
        inputs.mkdir(parents=True)
        (inputs / "screen.png").write_bytes(b"\x89PNG")

        detail = run_detail("r")

        self.assertEqual(detail["inputs"], [{
            "name": "screen.png",
            "size": 4,
            "kind": "image",
            "mime": "image/png",
            "url": "/api/runs/r/inputs/screen.png",
            "previewable": True,
        }])
        self.assertEqual(input_path("r", "screen.png"), inputs / "screen.png")

    def test_run_ids_remain_unique_within_the_same_millisecond(self):
        from app.router import new_run_id

        with patch("app.router.time.time", return_value=1000.123):
            first = new_run_id()
            second = new_run_id()

        self.assertNotEqual(first, second)

    def test_artifact_download_rejects_symbolic_links(self):
        artifacts = config.RUNS_DIR / "r" / "artifacts"
        artifacts.mkdir(parents=True)
        linked = artifacts / "linked.txt"
        linked.write_text("placeholder", encoding="utf-8")

        with patch.object(Path, "is_symlink", return_value=True):
            with self.assertRaises(KeyError):
                artifact_path("r", "linked.txt")

    def test_meta_exposes_model_readiness_without_secrets(self):
        gateway = ModelGateway(environ={
            "AF_DEEPSEEK_API_KEY": "never-render-this-secret",
            "AF_DEEPSEEK_MODEL": "deepseek-flash",
        })

        with patch("app.router.llm", gateway):
            payload = meta()

        self.assertTrue(payload["models"]["providers"]["deepseek"]["configured"])
        self.assertNotIn("never-render-this-secret", repr(payload))

    def test_mode_is_live_when_a_real_provider_key_exists(self):
        with patch.dict("os.environ", {"AF_MIMO_API_KEY": "configured"}, clear=True):
            self.assertEqual(config.mode_label(), "live")

    def test_health_reports_safe_read_only_github_mcp_state(self):
        with patch.dict("os.environ", {}, clear=True):
            payload = health()

        github = payload["mcp"]["github"]
        self.assertEqual(github["state"], "missing_credential")
        self.assertEqual(github["mode"], "read_only")
        self.assertEqual(github["repository_count"], 0)
        self.assertNotIn("credential", github)


if __name__ == "__main__":
    unittest.main()
