"""Function Calling tools constrained to one Worker workspace."""
from __future__ import annotations

from pathlib import Path

from . import tools
from .policy import ToolPolicy


MAX_READ_CHARS = 12_000
MAX_WRITE_CHARS = 200_000
MAX_FILES = 100


class WorkspaceToolExecutor:
    def __init__(self, root: Path, policy: ToolPolicy, writable_outputs: list[str] | None = None):
        self.root = Path(root)
        self.policy = policy
        self.writable_outputs = (
            {policy.resolve_path(self.root, name) for name in writable_outputs}
            if writable_outputs is not None else None
        )

    def definitions(self) -> list[dict]:
        definitions = []
        for name, policy_name, description, properties, required in (
            (
                "list_files", "search", "列出 Worker 工作目录中的文件。",
                {"path": {"type": "string", "description": "相对目录，默认为 ."}}, [],
            ),
            (
                "read_file", "read", "读取 Worker 工作目录中的 UTF-8 文本文件。",
                {"path": {"type": "string", "description": "相对文件路径"}}, ["path"],
            ),
            (
                "write_file", "write", "把完整 UTF-8 内容写入 Worker 工作目录。",
                {
                    "path": {"type": "string", "description": "相对文件路径"},
                    "content": {"type": "string", "description": "完整文件内容"},
                },
                ["path", "content"],
            ),
            (
                "append_file", "write",
                "向 Worker 工作目录中已写入的文件追加 UTF-8 内容。文件较长时分块写入："
                "先 write_file 写第一部分，再多次 append_file 追加其余部分。",
                {
                    "path": {"type": "string", "description": "相对文件路径"},
                    "content": {"type": "string", "description": "要追加的内容"},
                },
                ["path", "content"],
            ),
            (
                "run_tests", "run_tests", "在 Worker 工作目录运行 Python unittest。",
                {"pattern": {"type": "string", "description": "测试文件模式，默认 test_*.py"}}, [],
            ),
        ):
            if not self.policy.authorize(
                policy_name, write=policy_name in {"write", "run_tests"}
            ):
                continue
            definitions.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                        "additionalProperties": False,
                    },
                },
            })
        return definitions

    def execute(self, name: str, arguments: dict) -> dict:
        try:
            if not isinstance(arguments, dict):
                raise ValueError("工具参数必须是对象")
            if name == "list_files":
                return self._list_files(arguments)
            if name == "read_file":
                return self._read_file(arguments)
            if name == "write_file":
                return self._write_file(arguments)
            if name == "append_file":
                return self._append_file(arguments)
            if name == "run_tests":
                return self._run_tests(arguments)
            raise ValueError(f"未知工具：{name}")
        except (OSError, PermissionError, ValueError) as exc:
            return {"ok": False, "error": str(exc)[:1000]}

    def _list_files(self, arguments: dict) -> dict:
        self._authorize("search")
        relative = self._string(arguments, "path", default=".")
        directory = self.policy.resolve_path(self.root, relative)
        if not directory.is_dir():
            raise ValueError(f"目录不存在：{relative}")
        files = sorted(
            path.relative_to(self.root.resolve()).as_posix()
            for path in directory.rglob("*")
            if path.is_file()
        )[:MAX_FILES]
        return {"ok": True, "files": files, "truncated": len(files) == MAX_FILES}

    def _read_file(self, arguments: dict) -> dict:
        self._authorize("read")
        relative = self._string(arguments, "path")
        path = self.policy.resolve_path(self.root, relative)
        if not path.is_file():
            raise ValueError(f"文件不存在：{relative}")
        content = path.read_text(encoding="utf-8")
        return {
            "ok": True,
            "path": relative,
            "content": content[:MAX_READ_CHARS],
            "truncated": len(content) > MAX_READ_CHARS,
        }

    def _write_file(self, arguments: dict) -> dict:
        self._authorize("write", write=True)
        relative = self._string(arguments, "path")
        normalized = relative.replace("\\", "/").lstrip("./")
        if normalized == "inputs" or normalized.startswith("inputs/"):
            raise PermissionError("用户输入附件为只读，不能覆盖")
        content = self._string(arguments, "content")
        if len(content) > MAX_WRITE_CHARS:
            raise ValueError(f"文件内容超过 {MAX_WRITE_CHARS} 字符限制")
        path = self.policy.resolve_path(self.root, relative)
        if self.writable_outputs is not None and path not in self.writable_outputs:
            raise PermissionError("只能写入本节点声明的产物")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return {"ok": True, "path": relative, "chars": len(content)}

    def _append_file(self, arguments: dict) -> dict:
        self._authorize("write", write=True)
        relative = self._string(arguments, "path")
        normalized = relative.replace("\\", "/").lstrip("./")
        if normalized == "inputs" or normalized.startswith("inputs/"):
            raise PermissionError("用户输入附件为只读，不能覆盖")
        content = self._string(arguments, "content")
        path = self.policy.resolve_path(self.root, relative)
        if self.writable_outputs is not None and path not in self.writable_outputs:
            raise PermissionError("只能写入本节点声明的产物")
        if not path.is_file():
            raise ValueError(f"文件不存在，请先用 write_file 创建：{relative}")
        existing = path.read_text(encoding="utf-8")
        if len(existing) + len(content) > MAX_WRITE_CHARS:
            raise ValueError(f"文件内容超过 {MAX_WRITE_CHARS} 字符限制")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(content)
        return {"ok": True, "path": relative, "chars": len(existing) + len(content)}

    def _run_tests(self, arguments: dict) -> dict:
        self._authorize("run_tests", write=True)
        pattern = self._string(arguments, "pattern", default="test_*.py")
        if "/" in pattern or "\\" in pattern or ".." in pattern:
            raise ValueError("测试模式只能匹配当前工作目录中的文件名")
        return tools.run_python_tests(self.root, pattern=pattern, timeout=60)

    def _authorize(self, name: str, write: bool = False) -> None:
        if not self.policy.authorize(name, write=write):
            raise PermissionError(f"当前角色无权调用工具：{name}")

    @staticmethod
    def _string(arguments: dict, name: str, default: str | None = None) -> str:
        value = arguments.get(name, default)
        if not isinstance(value, str) or (default is None and not value):
            raise ValueError(f"参数 {name} 必须是非空字符串")
        return value


class CompositeToolExecutor:
    """Route workspace tools locally and namespaced MCP tools remotely."""

    def __init__(self, local: WorkspaceToolExecutor, remote, policy: ToolPolicy):
        self.local = local
        self.remote = remote
        self.policy = policy

    def definitions(self) -> list[dict]:
        definitions = list(self.local.definitions())
        if self.remote is not None and self.policy.authorize("mcp_read"):
            definitions.extend(self.remote.definitions())
        return definitions

    def execute(self, name: str, arguments: dict) -> dict:
        if name.startswith("github__"):
            if self.remote is None or not self.policy.authorize("mcp_read"):
                return {"ok": False, "error": "当前角色无权调用 GitHub MCP 只读工具"}
            return self.remote.execute(name, arguments)
        return self.local.execute(name, arguments)
