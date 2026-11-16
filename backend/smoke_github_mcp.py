"""Real read-only GitHub MCP smoke test. Never registers or calls write tools."""
from __future__ import annotations

import json
import os

from app.github_mcp import GitHubMcpClient, GitHubMcpConfig


def main() -> int:
    config = GitHubMcpConfig.from_env(os.environ)
    if not config.enabled:
        print(json.dumps({"ok": False, "state": config.status()["state"]}))
        return 2

    client = GitHubMcpClient(config)
    names = {item["function"]["name"] for item in client.definitions()}
    tool = "github__get_file_contents"
    if tool not in names:
        print(json.dumps({
            "ok": False,
            "state": "tool_discovery_failed",
            "discovered_read_tools": len(names),
        }))
        return 1

    repository = sorted(config.repositories)[0]
    owner, repo = repository.split("/", 1)
    result = client.execute(tool, {
        "owner": owner,
        "repo": repo,
        "path": os.environ.get("AF_GITHUB_SMOKE_PATH", "README.md"),
    })
    safe = {
        "ok": bool(result.get("ok")),
        "server": result.get("server"),
        "tool": result.get("tool"),
        "repository": result.get("repository", repository),
        "content_chars": len(result.get("content", "")),
        "truncated": bool(result.get("truncated")),
    }
    if not safe["ok"]:
        safe["error"] = result.get("error", "GitHub MCP 只读调用失败")
    print(json.dumps(safe, ensure_ascii=False))
    return 0 if safe["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
