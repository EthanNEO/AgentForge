"""Read-only GitHub MCP configuration and policy boundary."""
from __future__ import annotations

import importlib.util
import asyncio
import json
import re
from dataclasses import dataclass
from typing import Mapping, Protocol
from urllib.parse import urlparse


DEFAULT_URL = "https://api.githubcopilot.com/mcp/readonly"
DEFAULT_READ_TOOLS = frozenset({
    "get_me",
    "get_file_contents",
    "list_issues",
    "issue_read",
    "list_pull_requests",
    "pull_request_read",
    "search_code",
    "actions_list",
    "actions_get",
    "get_job_logs",
})
REPOSITORY_OPTIONAL_TOOLS = frozenset({"get_me"})
_REPO_QUALIFIER = re.compile(r"(?i)(?:^|\s)repo:([^\s]+)")
MAX_MCP_RESULT_CHARS = 6000


@dataclass(frozen=True)
class GitHubMcpConfig:
    url: str
    credential: str
    repositories: frozenset[str]
    allowed_tools: frozenset[str]

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "GitHubMcpConfig":
        repositories = frozenset(
            item.strip().casefold()
            for item in environ.get("AF_GITHUB_REPOS", "").split(",")
            if cls._valid_repository(item.strip())
        )
        requested_tools = environ.get("AF_GITHUB_MCP_TOOLS", "").strip()
        allowed_tools = (
            frozenset(item.strip() for item in requested_tools.split(",") if item.strip())
            if requested_tools else DEFAULT_READ_TOOLS
        )
        return cls(
            url=environ.get("AF_GITHUB_MCP_URL", DEFAULT_URL).strip() or DEFAULT_URL,
            credential=environ.get("AF_GITHUB_TOKEN", "").strip(),
            repositories=repositories,
            allowed_tools=frozenset(allowed_tools & DEFAULT_READ_TOOLS),
        )

    @property
    def enabled(self) -> bool:
        return bool(
            self.credential and self.repositories and self.allowed_tools
            and self.endpoint_trusted
        )

    @property
    def endpoint_trusted(self) -> bool:
        try:
            parsed = urlparse(self.url)
            port = parsed.port
        except ValueError:
            return False
        return bool(
            parsed.scheme == "https"
            and parsed.hostname == "api.githubcopilot.com"
            and port in (None, 443)
            and not parsed.username
            and not parsed.password
            and parsed.path.startswith("/mcp")
            and not parsed.fragment
        )

    def status(self) -> dict:
        if not self.endpoint_trusted:
            state = "invalid_endpoint"
        elif not self.credential:
            state = "missing_credential"
        elif not self.repositories:
            state = "missing_repository_allowlist"
        elif not self.allowed_tools:
            state = "missing_tool_allowlist"
        elif not self.sdk_available():
            state = "missing_sdk"
        else:
            state = "configured"
        return {
            "enabled": self.enabled,
            "state": state,
            "mode": "read_only",
            "url": self.url,
            "repository_count": len(self.repositories),
            "allowed_tool_count": len(self.allowed_tools),
            "sdk_available": self.sdk_available(),
        }

    def authorize(self, tool_name: str, arguments: dict) -> str:
        if not self.enabled:
            raise PermissionError("GitHub MCP 未配置完整")
        if tool_name not in self.allowed_tools:
            raise PermissionError(f"GitHub MCP 工具未获只读授权：{tool_name}")
        if tool_name in REPOSITORY_OPTIONAL_TOOLS:
            return ""
        repositories = self._repositories_from_arguments(tool_name, arguments)
        if not repositories:
            raise PermissionError("GitHub MCP 调用缺少明确的仓库范围")
        if not repositories.issubset(self.repositories):
            raise PermissionError("GitHub MCP 调用超出仓库白名单")
        return sorted(repositories)[0]

    def sanitize(self, message: str) -> str:
        return message.replace(self.credential, "***") if self.credential else message

    @staticmethod
    def sdk_available() -> bool:
        return importlib.util.find_spec("mcp") is not None

    @staticmethod
    def _valid_repository(value: str) -> bool:
        parts = value.split("/")
        return len(parts) == 2 and all(part.strip() for part in parts)

    def _repositories_from_arguments(self, tool_name: str, arguments: dict) -> set[str]:
        if not isinstance(arguments, dict):
            return set()
        owner = arguments.get("owner")
        repo = arguments.get("repo")
        if isinstance(owner, str) and isinstance(repo, str) and owner and repo:
            return {f"{owner}/{repo}".casefold()}
        for key in ("repository", "full_name"):
            value = arguments.get(key)
            if isinstance(value, str) and self._valid_repository(value):
                return {value.casefold()}
        if tool_name == "search_code":
            query = arguments.get("query")
            if isinstance(query, str):
                return {match.casefold() for match in _REPO_QUALIFIER.findall(query)}
        return set()


class GitHubMcpTransport(Protocol):
    def discover(self) -> list[dict]: ...

    def call_tool(self, name: str, arguments: dict) -> object: ...


class OfficialGitHubMcpTransport:
    """Small synchronous adapter around the optional official async MCP SDK."""

    def __init__(self, config: GitHubMcpConfig, timeout: float = 30.0):
        self.config = config
        self.timeout = timeout

    def discover(self) -> list[dict]:
        return asyncio.run(self._discover())

    def call_tool(self, name: str, arguments: dict) -> object:
        return asyncio.run(self._call_tool(name, arguments))

    async def _discover(self) -> list[dict]:
        async with self._client() as client:
            found = await client.list_tools()
            tools = getattr(found, "tools", found)
            return [self._model_to_dict(tool) for tool in tools]

    async def _call_tool(self, name: str, arguments: dict) -> object:
        async with self._client() as client:
            return self._model_to_dict(await client.call_tool(name, arguments))

    def _client(self):
        try:
            from mcp import Client
            from mcp.client.streamable_http import streamable_http_client
            import httpx2
        except ImportError as exc:
            raise RuntimeError(
                "GitHub MCP SDK 未安装，请安装 backend/requirements-mcp.txt"
            ) from exc
        http_client = httpx2.AsyncClient(
            headers={
                "Authorization": f"Bearer {self.config.credential}",
                "X-MCP-Readonly": "true",
                "X-MCP-Tools": ",".join(sorted(self.config.allowed_tools)),
            },
            timeout=self.timeout,
        )
        transport = streamable_http_client(self.config.url, http_client=http_client)
        return _McpClientContext(http_client, Client(transport))

    @staticmethod
    def _model_to_dict(value: object) -> object:
        if isinstance(value, dict):
            return value
        dump = getattr(value, "model_dump", None)
        if callable(dump):
            return dump(by_alias=True, exclude_none=True)
        return value


class _McpClientContext:
    def __init__(self, http_client, client):
        self.http_client = http_client
        self.client = client

    async def __aenter__(self):
        await self.http_client.__aenter__()
        try:
            return await self.client.__aenter__()
        except Exception:
            await self.http_client.__aexit__(None, None, None)
            raise

    async def __aexit__(self, exc_type, exc, traceback):
        try:
            return await self.client.__aexit__(exc_type, exc, traceback)
        finally:
            await self.http_client.__aexit__(exc_type, exc, traceback)


class GitHubMcpClient:
    PREFIX = "github__"

    def __init__(self, config: GitHubMcpConfig,
                 transport: GitHubMcpTransport | None = None):
        self.config = config
        self.transport = transport or OfficialGitHubMcpTransport(config)

    def definitions(self) -> list[dict]:
        if not self.config.enabled:
            return []
        try:
            tools = self.transport.discover()
        except Exception:
            return []
        definitions = []
        for tool in tools:
            name = str(tool.get("name") or "")
            if name not in self.config.allowed_tools:
                continue
            parameters = tool.get("inputSchema") or tool.get("input_schema") or {
                "type": "object", "properties": {}
            }
            definitions.append({
                "type": "function",
                "function": {
                    "name": f"{self.PREFIX}{name}",
                    "description": f"GitHub MCP 只读：{tool.get('description') or name}",
                    "parameters": parameters,
                },
            })
        return definitions

    def execute(self, name: str, arguments: dict) -> dict:
        raw_name = name[len(self.PREFIX):] if name.startswith(self.PREFIX) else ""
        try:
            if not raw_name:
                raise PermissionError(f"未知 GitHub MCP 工具：{name}")
            repository = self.config.authorize(raw_name, arguments)
            raw_result = self.transport.call_tool(raw_name, arguments)
            rendered = self._render(raw_result)
            if self._is_protocol_error(raw_result):
                return {
                    "ok": False,
                    "server": "github",
                    "tool": raw_name,
                    "repository": repository,
                    "error": self.config.sanitize(rendered)[:1000],
                }
            return {
                "ok": True,
                "server": "github",
                "tool": raw_name,
                "repository": repository,
                "content": rendered[:MAX_MCP_RESULT_CHARS],
                "truncated": len(rendered) > MAX_MCP_RESULT_CHARS,
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "server": "github",
                "tool": raw_name or name,
                "error": self.config.sanitize(str(exc))[:1000],
            }

    @staticmethod
    def _render(value: object) -> str:
        if isinstance(value, str):
            return value
        if not isinstance(value, (dict, list, int, float, bool, type(None))):
            dump = getattr(value, "model_dump", None)
            value = dump(by_alias=True, exclude_none=True) if callable(dump) else str(value)
        return json.dumps(value, ensure_ascii=False, default=str)

    @staticmethod
    def _is_protocol_error(value: object) -> bool:
        if isinstance(value, dict):
            return bool(value.get("isError") or value.get("is_error"))
        return bool(getattr(value, "isError", False) or getattr(value, "is_error", False))
