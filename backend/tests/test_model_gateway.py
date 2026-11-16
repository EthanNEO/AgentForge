import unittest
from http.client import IncompleteRead
from unittest.mock import patch

from app.llm import GatewayError, ModelBudget, ModelBudgetExceeded, ModelGateway, TransientTransportError, track_usage


class ModelGatewayTests(unittest.TestCase):
    def test_truncated_http_response_uses_transport_retry(self):
        from io import BytesIO

        broken = BytesIO()
        complete = BytesIO(b'{"choices":[{"message":{"content":"recovered"}}]}')
        with patch.object(broken, "read", side_effect=IncompleteRead(b"", 10)):
            with patch("app.llm.urllib.request.urlopen", side_effect=[broken, complete]):
                gateway = ModelGateway(environ=self.environ, retry_delays=(0,))
                self.assertEqual(gateway.complete("system", "user").text, "recovered")

    def setUp(self):
        self.calls = []
        self.environ = {
            "AF_DEEPSEEK_API_KEY": "deepseek-secret-value",
            "AF_DEEPSEEK_BASE_URL": "https://deepseek.example/v1/",
            "AF_DEEPSEEK_MODEL": "deepseek-flash",
            "AF_MIMO_API_KEY": "mimo-secret-value",
            "AF_MIMO_BASE_URL": "https://mimo.example/v1",
            "AF_MIMO_MODEL": "mimo-v2.5-pro",
            "AF_DEFAULT_PROVIDER": "deepseek",
        }

    def transport(self, *, url, headers, payload, timeout):
        self.calls.append({
            "url": url, "headers": headers, "payload": payload, "timeout": timeout,
        })
        return {
            "model": payload["model"],
            "choices": [{"message": {"content": "真实模型返回"}}],
            "usage": {"total_tokens": 17},
        }

    def test_usage_survives_a_metered_response_without_text(self):
        gateway = ModelGateway(
            environ=self.environ,
            transport=lambda **_: {"model": "returned-model", "usage": {"total_tokens": 23},
                                   "choices": [{"message": {"content": ""}}]},
        )
        with patch("app.db.execute") as write, track_usage("planning"):
            with self.assertRaises(GatewayError):
                gateway.complete("system", "user")
        self.assertEqual(write.call_args.args[1][:9],
                         ("planning", None, None, "deepseek", "returned-model", 23, 1, 1, "failed"))

    def test_execution_budget_stops_next_model_request_and_keeps_usage(self):
        gateway = ModelGateway(environ=self.environ, transport=self.transport)
        budget = ModelBudget(17)
        with patch("app.db.execute") as write, track_usage("execution", "run", "build", budget=budget):
            self.assertEqual(gateway.complete("system", "first").tokens, 17)
            with self.assertRaises(ModelBudgetExceeded):
                gateway.complete("system", "second")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(write.call_args_list[0].args[1][5], 17)

    def test_transport_retry_rechecks_shared_execution_budget(self):
        budget = ModelBudget(17)

        def transport(**kwargs):
            self.calls.append(kwargs)
            budget.record(17)  # Another worker reached the threshold.
            raise TransientTransportError("temporary failure")

        gateway = ModelGateway(environ=self.environ, transport=transport, retry_delays=(0,))
        with patch("app.db.execute"), track_usage("execution", "run", "build", budget=budget):
            with self.assertRaises(ModelBudgetExceeded) as stopped:
                gateway.complete("system", "request")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(stopped.exception.used, 17)
        self.assertEqual(stopped.exception.tokens, 0)

    def test_qwen_expired_key_falls_back_and_is_skipped_afterward(self):
        calls = []

        def transport(**request):
            calls.append(request["url"])
            if "qweneval" in request["url"]:
                self.assertNotIn("thinking", request["payload"])
                raise RuntimeError("HTTP 401: expired")
            return {"choices": [{"message": {"content": "backup"}}]}

        gateway = ModelGateway({**self.environ, "MDATAPLUS_API_KEY": "test-key"},
                               transport=transport, retry_delays=())
        self.assertEqual(gateway.complete("system", "task").provider, "deepseek")
        self.assertEqual(gateway.complete("system", "next").provider, "deepseek")
        self.assertEqual(sum("qweneval" in url for url in calls), 1)
        self.assertFalse(gateway.status()["providers"]["qwen"]["available"])

    def test_model_prefix_routes_to_matching_provider(self):
        gateway = ModelGateway(environ=self.environ, transport=self.transport)

        result = gateway.complete("system", "user", model="mimo-v2.5-pro")

        self.assertEqual(result.provider, "mimo")
        self.assertEqual(result.model, "mimo-v2.5-pro")
        self.assertEqual(result.text, "真实模型返回")
        self.assertEqual(result.tokens, 17)
        self.assertEqual(self.calls[0]["url"], "https://mimo.example/v1/chat/completions")
        self.assertEqual(self.calls[0]["headers"]["Authorization"], "Bearer mimo-secret-value")
        self.assertEqual(self.calls[0]["payload"]["max_completion_tokens"], 4096)
        self.assertEqual(self.calls[0]["payload"]["thinking"], {"type": "disabled"})
        self.assertNotIn("max_tokens", self.calls[0]["payload"])

    def test_deepseek_uses_provider_specific_output_limit(self):
        gateway = ModelGateway(environ=self.environ, transport=self.transport)

        gateway.complete("system", "user", model="deepseek-flash")

        self.assertEqual(self.calls[0]["payload"]["max_tokens"], 4096)
        self.assertEqual(self.calls[0]["payload"]["thinking"], {"type": "disabled"})
        self.assertNotIn("max_completion_tokens", self.calls[0]["payload"])

    def test_vision_model_receives_text_and_base64_image_content_blocks(self):
        gateway = ModelGateway(environ=self.environ, transport=self.transport)

        gateway.complete(
            "system", "inspect",
            model="deepseek-flash",
            image_inputs=[{"mime": "image/png", "data": "iVBORw0KGgo="}],
        )

        content = self.calls[0]["payload"]["messages"][1]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "inspect"})
        self.assertEqual(content[1]["type"], "image_url")
        self.assertEqual(
            content[1]["image_url"]["url"], "data:image/png;base64,iVBORw0KGgo="
        )
        self.assertNotIn("iVBORw0KGgo=", repr(gateway.status()))

    def test_non_vision_model_rejects_image_payload_before_transport(self):
        gateway = ModelGateway(environ=self.environ, transport=self.transport)

        with self.assertRaisesRegex(GatewayError, "不支持图片输入"):
            gateway.complete(
                "system", "inspect",
                model="deepseek-v4-pro",
                image_inputs=[{"mime": "image/png", "data": "iVBORw0KGgo="}],
            )

        self.assertEqual(self.calls, [])

    def test_unknown_model_uses_default_provider_default_model(self):
        gateway = ModelGateway(environ=self.environ, transport=self.transport)

        result = gateway.complete("system", "user", model="legacy-qwen-name")

        self.assertEqual(result.provider, "deepseek")
        self.assertEqual(result.model, "deepseek-flash")
        self.assertEqual(self.calls[0]["payload"]["model"], "deepseek-flash")

    def test_explicit_provider_overrides_model_prefix(self):
        gateway = ModelGateway(environ=self.environ, transport=self.transport)

        result = gateway.complete("system", "user", provider="mimo", model="deepseek-flash")

        self.assertEqual(result.provider, "mimo")
        self.assertEqual(result.model, "mimo-v2.5-pro")

    def test_inferred_unavailable_provider_falls_back_to_configured_provider(self):
        gateway = ModelGateway(environ={
            "AF_MIMO_API_KEY": "mimo-only",
            "AF_DEFAULT_PROVIDER": "deepseek",
        }, transport=self.transport)

        self.assertTrue(gateway.available())
        self.assertTrue(gateway.available("mimo"))
        self.assertFalse(gateway.available("deepseek"))

        result = gateway.complete("system", "user", model="deepseek-flash")

        self.assertEqual(result.provider, "mimo")
        self.assertEqual(result.model, "mimo-v2.6-pro")

    def test_explicit_unavailable_provider_fails(self):
        gateway = ModelGateway(environ={
            "AF_MIMO_API_KEY": "mimo-only",
        }, transport=self.transport)

        with self.assertRaisesRegex(GatewayError, "DeepSeek.*API Key"):
            gateway.complete("system", "user", model="deepseek-flash", provider="deepseek")

    def test_status_never_exposes_api_keys(self):
        gateway = ModelGateway(environ=self.environ, transport=self.transport)

        status = gateway.status()
        rendered = repr(status)

        self.assertEqual(status["default_provider"], "deepseek")
        self.assertTrue(status["providers"]["deepseek"]["configured"])
        self.assertNotIn("deepseek-secret-value", rendered)
        self.assertNotIn("mimo-secret-value", rendered)
        self.assertNotIn("api_key", rendered.lower())

    def test_missing_usage_is_estimated(self):
        def transport_without_usage(**kwargs):
            return {
                "model": kwargs["payload"]["model"],
                "choices": [{"message": {"content": "abcd"}}],
            }

        gateway = ModelGateway(environ=self.environ, transport=transport_without_usage)

        result = gateway.complete("1234", "5678", model="deepseek-flash")

        self.assertEqual(result.tokens, 3)

    def test_transport_error_is_sanitized(self):
        def failing_transport(**kwargs):
            raise RuntimeError(
                "request failed with deepseek-secret-value and mimo-secret-value"
            )

        gateway = ModelGateway(environ=self.environ, transport=failing_transport)

        with self.assertRaises(GatewayError) as caught:
            gateway.complete("system", "user", model="deepseek-flash")

        message = str(caught.exception)
        self.assertIn("DeepSeek", message)
        self.assertNotIn("deepseek-secret-value", message)
        self.assertNotIn("mimo-secret-value", message)

    def test_transient_network_error_is_retried_then_succeeds(self):
        calls = []

        def flaky_transport(**kwargs):
            calls.append(1)
            if len(calls) < 3:
                raise TransientTransportError("网络错误：[WinError 10061] 拒绝连接")
            return {
                "model": kwargs["payload"]["model"],
                "choices": [{"message": {"content": "重试后成功"}}],
                "usage": {"total_tokens": 9},
            }

        gateway = ModelGateway(
            environ=self.environ, transport=flaky_transport, retry_delays=(0.0, 0.0),
        )

        result = gateway.complete("system", "user", model="deepseek-flash")

        self.assertEqual(result.text, "重试后成功")
        self.assertEqual(len(calls), 3)

    def test_transient_network_error_gives_up_after_retry_allowance(self):
        calls = []

        def dead_transport(**kwargs):
            calls.append(1)
            raise TransientTransportError("网络错误：[WinError 10061] 拒绝连接")

        gateway = ModelGateway(
            environ=self.environ, transport=dead_transport, retry_delays=(0.0, 0.0),
        )

        with self.assertRaises(GatewayError):
            gateway.complete("system", "user", model="deepseek-flash")

        self.assertEqual(len(calls), 3)

    def test_non_transient_transport_error_is_not_retried(self):
        calls = []

        def bad_request_transport(**kwargs):
            calls.append(1)
            raise RuntimeError("HTTP 400: invalid model")

        gateway = ModelGateway(
            environ=self.environ, transport=bad_request_transport, retry_delays=(0.0, 0.0),
        )

        with self.assertRaises(GatewayError):
            gateway.complete("system", "user", model="deepseek-flash")

        self.assertEqual(len(calls), 1)

    def test_provider_defaults_use_current_requested_models(self):
        gateway = ModelGateway(environ={})

        status = gateway.status()

        self.assertEqual(status["providers"]["deepseek"]["default_model"], "deepseek-flash")
        self.assertEqual(status["providers"]["mimo"]["default_model"], "mimo-v2.6-pro")

    def test_mainland_token_plan_key_uses_token_plan_endpoint(self):
        gateway = ModelGateway(environ={"AF_MIMO_API_KEY": "tp-mainland-secret"})

        status = gateway.status()

        self.assertEqual(
            status["providers"]["mimo"]["base_url"],
            "https://token-plan-cn.xiaomimimo.com/v1",
        )

    def test_tool_call_result_is_returned_to_model_before_final_answer(self):
        responses = [
            {
                "model": "deepseek-flash",
                "choices": [{"message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_write_1",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": '{"path":"result.txt","content":"hello"}',
                        },
                    }],
                }}],
                "usage": {"total_tokens": 11},
            },
            {
                "model": "deepseek-flash",
                "choices": [{"message": {"role": "assistant", "content": "done"}}],
                "usage": {"total_tokens": 7},
            },
        ]
        calls = []
        executed = []

        def transport(**kwargs):
            calls.append(kwargs["payload"])
            return responses.pop(0)

        def execute(name, arguments):
            executed.append((name, arguments))
            return {"ok": True, "path": arguments["path"]}

        gateway = ModelGateway(environ=self.environ, transport=transport)
        result = gateway.complete(
            "system",
            "user",
            model="deepseek-flash",
            tool_definitions=[{
                "type": "function",
                "function": {
                    "name": "write_file",
                    "description": "write",
                    "parameters": {"type": "object"},
                },
            }],
            tool_executor=execute,
        )

        self.assertEqual(result.text, "done")
        self.assertEqual(result.tokens, 18)
        self.assertEqual(result.tool_calls, 1)
        self.assertEqual(executed, [("write_file", {"path": "result.txt", "content": "hello"})])
        self.assertEqual(calls[0]["tool_choice"], "auto")
        self.assertEqual(calls[1]["messages"][-2]["tool_calls"][0]["id"], "call_write_1")
        self.assertEqual(calls[1]["messages"][-1]["role"], "tool")
        self.assertEqual(calls[1]["messages"][-1]["tool_call_id"], "call_write_1")
        self.assertNotIn("content", result.tool_trace[0])

    def test_workspace_read_result_keeps_long_test_file_and_valid_json(self):
        import json
        import tempfile
        from pathlib import Path

        from app.function_tools import WorkspaceToolExecutor
        from app.policy import ToolPolicy

        content = ('assert "覆盖验收"\n' * 620) + "# 最后一条断言"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "test_main.py").write_text(content, encoding="utf-8")
            executor = WorkspaceToolExecutor(root, ToolPolicy.for_role("coder"))

            def transport(**kwargs):
                message = kwargs["payload"]["messages"][-1]
                if message["role"] == "tool":
                    payload = json.loads(message["content"])
                    self.assertEqual(payload["content"], content)
                    self.assertFalse(payload["truncated"])
                    return {"choices": [{"message": {"content": "done"}}]}
                return {"choices": [{"message": {"tool_calls": [{
                    "id": "read", "function": {"name": "read_file", "arguments": '{"path":"test_main.py"}'},
                }]}}]}

            result = ModelGateway(environ=self.environ, transport=transport).complete(
                "system", "user", tool_definitions=executor.definitions(), tool_executor=executor.execute,
            )
        self.assertEqual(result.text, "done")

    def test_tool_loop_stops_after_configured_round_limit(self):
        def transport(**kwargs):
            return {
                "model": kwargs["payload"]["model"],
                "choices": [{"message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_forever",
                        "type": "function",
                        "function": {"name": "list_files", "arguments": "{}"},
                    }],
                }}],
                "usage": {"total_tokens": 1},
            }

        gateway = ModelGateway(environ=self.environ, transport=transport)
        with self.assertRaisesRegex(GatewayError, "工具调用轮数"):
            gateway.complete(
                "system",
                "user",
                model="deepseek-flash",
                tool_definitions=[{
                    "type": "function",
                    "function": {"name": "list_files", "parameters": {"type": "object"}},
                }],
                tool_executor=lambda name, arguments: {"ok": True},
                max_tool_rounds=1,
            )

    def test_github_tool_trace_contains_safe_mcp_evidence_only(self):
        responses = [
            {
                "model": "deepseek-flash",
                "choices": [{"message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_github_1",
                        "type": "function",
                        "function": {
                            "name": "github__get_file_contents",
                            "arguments": '{"owner":"acme","repo":"widget"}',
                        },
                    }],
                }}],
                "usage": {"total_tokens": 4},
            },
            {
                "model": "deepseek-flash",
                "choices": [{"message": {"role": "assistant", "content": "done"}}],
                "usage": {"total_tokens": 2},
            },
        ]

        gateway = ModelGateway(
            environ=self.environ,
            transport=lambda **kwargs: responses.pop(0),
        )
        result = gateway.complete(
            "system", "user", model="deepseek-flash",
            tool_definitions=[{"type": "function", "function": {
                "name": "github__get_file_contents", "parameters": {"type": "object"},
            }}],
            tool_executor=lambda name, arguments: {
                "ok": True,
                "server": "github",
                "repository": "acme/widget",
                "content": "private-response-body",
            },
        )

        trace = result.tool_trace[0]
        self.assertEqual(trace["source"], "mcp")
        self.assertEqual(trace["server"], "github")
        self.assertEqual(trace["repository"], "acme/widget")
        self.assertNotIn("content", trace)
        self.assertNotIn("arguments", trace)
        self.assertNotIn("private-response-body", repr(trace))

    def test_tool_observer_receives_trace_before_later_model_failure(self):
        responses = [{
            "model": "deepseek-flash",
            "choices": [{"message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_github_1",
                    "type": "function",
                    "function": {
                        "name": "github__get_file_contents",
                        "arguments": '{"owner":"acme","repo":"widget"}',
                    },
                }],
            }}],
            "usage": {"total_tokens": 4},
        }]

        def transport(**kwargs):
            if responses:
                return responses.pop(0)
            raise RuntimeError("later model failure")

        observed = []
        gateway = ModelGateway(environ=self.environ, transport=transport)
        with self.assertRaises(GatewayError):
            gateway.complete(
                "system", "user", model="deepseek-flash",
                tool_definitions=[{"type": "function", "function": {
                    "name": "github__get_file_contents", "parameters": {"type": "object"},
                }}],
                tool_executor=lambda name, arguments: {
                    "ok": True, "server": "github", "repository": "acme/widget"
                },
                tool_observer=observed.append,
            )

        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]["repository"], "acme/widget")


class QwenFallbackPolicyTests(unittest.TestCase):
    """Failure-category cooldowns and recorded fallback evidence."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        from app import db

        self.tmp = tempfile.TemporaryDirectory()
        db.reset_connection(Path(self.tmp.name) / "test.db")
        self.environ = {
            "MDATAPLUS_API_KEY": "qwen-eval-key",
            "AF_DEEPSEEK_API_KEY": "deepseek-key",
            "AF_MIMO_API_KEY": "mimo-key",
            "AF_DEFAULT_PROVIDER": "deepseek",
        }
        self.qwen_calls = []

    def tearDown(self):
        from app import db

        db.reset_connection()
        self.tmp.cleanup()

    def _gateway(self, qwen_error):
        def transport(**request):
            if "qweneval" in request["url"]:
                self.qwen_calls.append(request)
                raise qwen_error
            return {"model": request["payload"]["model"],
                    "choices": [{"message": {"content": "backup"}}],
                    "usage": {"total_tokens": 5}}

        return ModelGateway(self.environ, transport=transport, retry_delays=())

    def _fallback_rows(self):
        from app import db

        return db.query("SELECT * FROM gateway_fallbacks ORDER BY id")

    def test_timeout_falls_back_with_short_cooldown_and_records_evidence(self):
        from app.llm import track_usage

        gateway = self._gateway(TransientTransportError("网络错误：The read operation timed out"))
        with track_usage("planning", "run-1"):
            result = gateway.complete("system", "task", timeout=45)
        self.assertEqual(result.provider, "deepseek")
        self.assertEqual(len(self.qwen_calls), 1)
        self.assertLessEqual(self.qwen_calls[0]["timeout"], 20)
        # 冷却期内不再等待千问；冷却结束后恢复尝试。
        self.assertFalse(gateway.available("qwen"))
        with track_usage("planning", "run-1"):
            gateway.complete("system", "next")
        self.assertEqual(len(self.qwen_calls), 1)
        gateway._qwen_disabled_until = 0.0
        self.assertTrue(gateway.available("qwen"))
        rows = self._fallback_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["run_id"], "run-1")
        self.assertEqual(rows[0]["phase"], "planning")
        self.assertEqual(rows[0]["category"], "timeout")
        self.assertEqual(rows[0]["to_provider"], "deepseek")
        self.assertGreaterEqual(rows[0]["wait_seconds"], 0)

    def test_rate_limit_uses_short_cooldown(self):
        gateway = self._gateway(TransientTransportError("HTTP 429: too many requests"))
        self.assertEqual(gateway.complete("system", "task").provider, "deepseek")
        rows = self._fallback_rows()
        self.assertEqual(rows[0]["category"], "rate_limited")
        self.assertFalse(gateway.available("qwen"))
        gateway._qwen_disabled_until = 0.0
        self.assertTrue(gateway.available("qwen"))

    def test_server_error_keeps_long_cooldown(self):
        import time

        gateway = self._gateway(TransientTransportError("HTTP 503: unavailable"))
        self.assertEqual(gateway.complete("system", "task").provider, "deepseek")
        self.assertEqual(self._fallback_rows()[0]["category"], "server_error")
        self.assertGreater(gateway._qwen_disabled_until, time.monotonic() + 200)

    def test_auth_rejection_disables_qwen_permanently_and_records_category(self):
        gateway = self._gateway(RuntimeError("HTTP 401: invalid key"))
        self.assertEqual(gateway.complete("system", "task").provider, "deepseek")
        self.assertEqual(gateway.complete("system", "next").provider, "deepseek")
        self.assertEqual(len(self.qwen_calls), 1)
        self.assertEqual(self._fallback_rows()[0]["category"], "auth_rejected")
        self.assertFalse(gateway.available("qwen"))

    def test_empty_response_is_classified_and_falls_back(self):
        def transport(**request):
            if "qweneval" in request["url"]:
                return {"model": "qwen", "choices": [{"message": {"content": ""}}],
                        "usage": {"total_tokens": 3}}
            return {"model": request["payload"]["model"],
                    "choices": [{"message": {"content": "backup"}}]}

        gateway = ModelGateway(self.environ, transport=transport, retry_delays=())
        self.assertEqual(gateway.complete("system", "task").provider, "deepseek")
        self.assertEqual(self._fallback_rows()[0]["category"], "empty_response")

    def test_all_backups_failed_still_records_fallback_without_takeover(self):
        def transport(**request):
            if "qweneval" in request["url"]:
                raise TransientTransportError("网络错误：timed out")
            raise RuntimeError("HTTP 500: backup down")

        gateway = ModelGateway(self.environ, transport=transport, retry_delays=())
        with self.assertRaises(GatewayError):
            gateway.complete("system", "task")
        rows = self._fallback_rows()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["to_provider"])
        self.assertEqual(rows[0]["category"], "timeout")

    def test_execution_fallback_records_node_association(self):
        from app.llm import track_usage

        gateway = self._gateway(TransientTransportError("网络错误：timed out"))
        with track_usage("execution", "run-2", "build"):
            gateway.complete("system", "task")
        rows = self._fallback_rows()
        self.assertEqual(rows[0]["run_id"], "run-2")
        self.assertEqual(rows[0]["node_id"], "build")
        self.assertEqual(rows[0]["phase"], "execution")

    def test_pinned_worker_model_keeps_original_route_without_qwen(self):
        gateway = self._gateway(TransientTransportError("网络错误：timed out"))
        result = gateway.complete("system", "task", model="mimo-v2.6-pro")
        self.assertEqual(result.provider, "mimo")
        self.assertEqual(self.qwen_calls, [])
        self.assertEqual(self._fallback_rows(), [])

    def test_consecutive_timeouts_progress_cooldown_and_success_resets(self):
        state = {"fail": True}

        def transport(**request):
            if "qweneval" in request["url"]:
                self.qwen_calls.append(request)
                if state["fail"]:
                    raise TransientTransportError("网络错误：timed out")
                return {"model": "qwen", "choices": [{"message": {"content": "ok"}}],
                        "usage": {"total_tokens": 3}}
            return {"model": request["payload"]["model"],
                    "choices": [{"message": {"content": "backup"}}],
                    "usage": {"total_tokens": 5}}

        gateway = ModelGateway(self.environ, transport=transport, retry_delays=())
        for cooldown in (60, 120, 240, 300, 300):
            gateway.complete("system", "task")
            self.assertEqual(self._fallback_rows()[-1]["cooldown_seconds"], cooldown)
            gateway._qwen_disabled_until = 0.0  # 模拟冷却到期
        state["fail"] = False
        result = gateway.complete("system", "task")
        self.assertEqual(result.provider, "qwen")
        self.assertEqual(gateway._qwen_failures, 0)
        state["fail"] = True
        gateway.complete("system", "task")
        self.assertEqual(self._fallback_rows()[-1]["cooldown_seconds"], 60)

    def test_pinned_qwen_bypasses_cooldown_and_never_falls_back(self):
        import time

        gateway = self._gateway(TransientTransportError("网络错误：timed out"))
        gateway._qwen_disabled_until = time.monotonic() + 1000
        with self.assertRaises(GatewayError):
            gateway.complete("system", "task", provider="qwen")
        self.assertEqual(len(self.qwen_calls), 1)
        rows = self._fallback_rows()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["to_provider"])

    def test_pinned_qwen_respects_permanent_disable(self):
        gateway = self._gateway(TransientTransportError("网络错误：timed out"))
        gateway._qwen_disabled_until = float("inf")
        with self.assertRaises(GatewayError):
            gateway.complete("system", "task", provider="qwen")
        self.assertEqual(self.qwen_calls, [])


if __name__ == "__main__":
    unittest.main()
