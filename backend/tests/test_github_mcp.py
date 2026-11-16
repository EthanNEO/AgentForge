import unittest

from app.github_mcp import GitHubMcpClient, GitHubMcpConfig
from app.policy import ToolPolicy


class GitHubMcpConfigTests(unittest.TestCase):
    def test_role_policy_allows_remote_reads_but_never_remote_writes(self):
        policy = ToolPolicy.for_role("coder")

        self.assertTrue(policy.authorize("mcp_read"))
        self.assertFalse(policy.authorize("mcp_write", write=True))

    def test_disabled_status_never_exposes_credentials(self):
        config = GitHubMcpConfig.from_env({"AF_GITHUB_TOKEN": "secret-value"})

        status = config.status()

        self.assertFalse(config.enabled)
        self.assertEqual(status["state"], "missing_repository_allowlist")
        self.assertNotIn("secret-value", repr(status))
        self.assertNotIn("token", " ".join(status).lower())

    def test_allows_explicit_read_tool_for_allowlisted_repository(self):
        config = GitHubMcpConfig.from_env({
            "AF_GITHUB_TOKEN": "secret-value",
            "AF_GITHUB_REPOS": "Acme/Widget, acme/Docs",
        })

        repository = config.authorize(
            "get_file_contents", {"owner": "ACME", "repo": "widget", "path": "README.md"}
        )

        self.assertEqual(repository, "acme/widget")

    def test_rejects_write_unknown_and_outside_repository_tools(self):
        config = GitHubMcpConfig.from_env({
            "AF_GITHUB_TOKEN": "secret-value",
            "AF_GITHUB_REPOS": "acme/widget",
        })

        with self.assertRaises(PermissionError):
            config.authorize("create_issue", {"owner": "acme", "repo": "widget"})
        with self.assertRaises(PermissionError):
            config.authorize("future_read_tool", {"owner": "acme", "repo": "widget"})
        with self.assertRaises(PermissionError):
            config.authorize("get_file_contents", {"owner": "other", "repo": "widget"})

    def test_code_search_requires_only_allowlisted_repo_qualifiers(self):
        config = GitHubMcpConfig.from_env({
            "AF_GITHUB_TOKEN": "secret-value",
            "AF_GITHUB_REPOS": "acme/widget",
        })

        self.assertEqual(
            config.authorize("search_code", {"query": "Widget repo:acme/widget"}),
            "acme/widget",
        )
        with self.assertRaises(PermissionError):
            config.authorize("search_code", {"query": "Widget"})
        with self.assertRaises(PermissionError):
            config.authorize(
                "search_code", {"query": "Widget repo:acme/widget repo:other/private"}
            )


class FakeTransport:
    def __init__(self, *, error="", result=None):
        self.error = error
        self.result = result if result is not None else {"content": [{"type": "text", "text": "ok"}]}
        self.calls = []

    def discover(self):
        return [
            {
                "name": "get_file_contents",
                "description": "Read repository contents",
                "inputSchema": {
                    "type": "object",
                    "properties": {"owner": {"type": "string"}, "repo": {"type": "string"}},
                    "required": ["owner", "repo"],
                },
            },
            {
                "name": "create_issue",
                "description": "Write an issue",
                "inputSchema": {"type": "object"},
            },
        ]

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self.error:
            raise RuntimeError(self.error)
        return self.result


class GitHubMcpClientTests(unittest.TestCase):
    def setUp(self):
        self.config = GitHubMcpConfig.from_env({
            "AF_GITHUB_TOKEN": "top-secret-token",
            "AF_GITHUB_REPOS": "acme/widget",
        })

    def test_discovery_exposes_only_locally_allowlisted_read_tools(self):
        client = GitHubMcpClient(self.config, FakeTransport())

        definitions = client.definitions()

        self.assertEqual([item["function"]["name"] for item in definitions], [
            "github__get_file_contents"
        ])
        self.assertEqual(
            definitions[0]["function"]["parameters"]["required"], ["owner", "repo"]
        )

    def test_execute_authorizes_repository_and_forwards_original_tool_name(self):
        transport = FakeTransport()
        client = GitHubMcpClient(self.config, transport)

        result = client.execute(
            "github__get_file_contents",
            {"owner": "acme", "repo": "widget", "path": "README.md"},
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["server"], "github")
        self.assertEqual(result["repository"], "acme/widget")
        self.assertEqual(transport.calls, [(
            "get_file_contents",
            {"owner": "acme", "repo": "widget", "path": "README.md"},
        )])

    def test_execute_truncates_results_and_sanitizes_transport_errors(self):
        long_client = GitHubMcpClient(
            self.config, FakeTransport(result={"content": "x" * 7000})
        )
        error_client = GitHubMcpClient(
            self.config, FakeTransport(error="failed with top-secret-token")
        )

        long_result = long_client.execute(
            "github__get_file_contents", {"owner": "acme", "repo": "widget"}
        )
        error_result = error_client.execute(
            "github__get_file_contents", {"owner": "acme", "repo": "widget"}
        )

        self.assertTrue(long_result["truncated"])
        self.assertLessEqual(len(long_result["content"]), 6000)
        self.assertFalse(error_result["ok"])
        self.assertIn("***", error_result["error"])
        self.assertNotIn("top-secret-token", repr(error_result))

    def test_protocol_error_result_is_never_reported_as_success(self):
        client = GitHubMcpClient(
            self.config,
            FakeTransport(result={
                "isError": True,
                "content": [{"type": "text", "text": "repository unavailable"}],
            }),
        )

        result = client.execute(
            "github__get_file_contents", {"owner": "acme", "repo": "widget"}
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["server"], "github")
        self.assertIn("repository unavailable", result["error"])

    def test_rejects_non_official_or_insecure_mcp_endpoint(self):
        for url in ("http://api.githubcopilot.com/mcp/readonly", "https://example.com/mcp"):
            with self.subTest(url=url):
                config = GitHubMcpConfig.from_env({
                    "AF_GITHUB_TOKEN": "secret-value",
                    "AF_GITHUB_REPOS": "acme/widget",
                    "AF_GITHUB_MCP_URL": url,
                })

                self.assertFalse(config.enabled)
                self.assertEqual(config.status()["state"], "invalid_endpoint")


if __name__ == "__main__":
    unittest.main()
