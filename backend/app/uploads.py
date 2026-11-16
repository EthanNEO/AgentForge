"""Staged, bounded user uploads for later attachment to a Run."""
from __future__ import annotations

import base64
import binascii
import json
import shutil
import uuid
from pathlib import Path

from . import config

MAX_FILES = 8
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 25 * 1024 * 1024

MIME_BY_SUFFIX = {
    ".txt": "text/plain", ".md": "text/markdown", ".csv": "text/csv",
    ".json": "application/json", ".yaml": "application/yaml", ".yml": "application/yaml",
    ".py": "text/x-python", ".js": "text/javascript", ".ts": "text/typescript",
    ".html": "text/html", ".css": "text/css", ".xml": "application/xml",
    ".pdf": "application/pdf", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp",
}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
TEXT_SUFFIXES = {".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".py", ".js", ".ts", ".html", ".css", ".xml"}


def describe_file(path: Path) -> dict:
    suffix = path.suffix.lower()
    kind = "image" if suffix in IMAGE_SUFFIXES else "text" if suffix in TEXT_SUFFIXES else "document"
    return {
        "name": path.name,
        "size": path.stat().st_size,
        "kind": kind,
        "mime": MIME_BY_SUFFIX.get(suffix, "application/octet-stream"),
    }


def _safe_name(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 180:
        raise ValueError("附件文件名无效")
    if Path(value).name != value or "/" in value or "\\" in value or value in {".", ".."}:
        raise ValueError("附件文件名不能包含路径")
    if Path(value).suffix.lower() not in MIME_BY_SUFFIX:
        raise ValueError(f"不支持的附件类型：{Path(value).suffix or '<none>'}")
    return value


def create_upload(body: dict) -> dict:
    raw_files = body.get("files") if isinstance(body, dict) else None
    if not isinstance(raw_files, list) or not raw_files or len(raw_files) > MAX_FILES:
        raise ValueError(f"每次需上传 1 到 {MAX_FILES} 个文件")
    decoded: list[tuple[str, str, bytes]] = []
    total = 0
    names: set[str] = set()
    for raw in raw_files:
        if not isinstance(raw, dict):
            raise ValueError("附件记录必须是对象")
        name = _safe_name(raw.get("name"))
        if name.lower() in names:
            raise ValueError(f"附件文件名重复：{name}")
        names.add(name.lower())
        value = raw.get("data")
        if not isinstance(value, str):
            raise ValueError(f"附件 {name} 缺少 base64 数据")
        try:
            content = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError(f"附件 {name} 的 base64 数据无效") from None
        if len(content) > MAX_FILE_BYTES:
            raise ValueError(f"附件 {name} 超过 10 MiB 限制")
        total += len(content)
        if total > MAX_TOTAL_BYTES:
            raise ValueError("附件总大小超过 25 MiB 限制")
        mime = MIME_BY_SUFFIX[Path(name).suffix.lower()]
        decoded.append((name, mime, content))

    upload_id = uuid.uuid4().hex
    root = config.UPLOADS_DIR / upload_id
    root.mkdir(parents=True, exist_ok=False)
    files = []
    for name, _mime, content in decoded:
        path = root / name
        path.write_bytes(content)
        files.append(describe_file(path))
    (root / "metadata.json").write_text(
        json.dumps({"upload_id": upload_id, "files": files}, ensure_ascii=False), encoding="utf-8"
    )
    return {"upload_id": upload_id, "files": files}


def resolve_uploads(upload_ids: object) -> list[dict]:
    if upload_ids in (None, []):
        return []
    if not isinstance(upload_ids, list) or len(upload_ids) > 20:
        raise ValueError("upload_ids 必须是列表")
    items = []
    seen = set()
    for upload_id in upload_ids:
        if not isinstance(upload_id, str) or not upload_id or upload_id in seen:
            raise ValueError("upload_id 无效或重复")
        seen.add(upload_id)
        root = config.UPLOADS_DIR / upload_id
        metadata = root / "metadata.json"
        if not metadata.is_file() or root.is_symlink():
            raise KeyError(upload_id)
        payload = json.loads(metadata.read_text(encoding="utf-8"))
        for item in payload.get("files", []):
            path = root / _safe_name(item.get("name"))
            if not path.is_file() or path.is_symlink():
                raise KeyError(item.get("name"))
            current = describe_file(path)
            items.append({**current, "upload_id": upload_id, "source_path": path})
    return items


def materialize_uploads(upload_ids: object, target: Path) -> list[dict]:
    items = resolve_uploads(upload_ids)
    if not items:
        return []
    lowered = [item["name"].lower() for item in items]
    if len(lowered) != len(set(lowered)):
        raise ValueError("不同上传批次中存在同名附件")
    target.mkdir(parents=True, exist_ok=True)
    copied = []
    for item in items:
        destination = target / item["name"]
        shutil.copyfile(item["source_path"], destination)
        copied.append({
            key: value for key, value in item.items()
            if key not in {"source_path", "upload_id"}
        } | {"relative_path": f"inputs/{item['name']}"})
    return copied
