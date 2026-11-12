"""本地产物清单：记录版本、哈希和输入谱系，避免修复覆盖唯一证据。"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import GENERATED_DIR

TASK_ID_PATTERN = re.compile(r"^task-[0-9a-f]{6,12}$")


def task_directory(task_id: str) -> Path:
    """仅允许编排器生成的任务标识，拒绝路径穿越。"""
    if not TASK_ID_PATTERN.fullmatch(task_id):
        raise ValueError("非法 task_id，仅允许 task-加 6~12 位小写十六进制字符")
    task_dir = (GENERATED_DIR / task_id).resolve()
    if GENERATED_DIR.resolve() not in task_dir.parents:
        raise ValueError("任务目录越界")
    task_dir.mkdir(parents=True, exist_ok=True)
    return task_dir


def contained_file(task_dir: Path, candidate: str | Path) -> Path:
    path = Path(candidate).resolve()
    if path != task_dir.resolve() and task_dir.resolve() not in path.parents:
        raise ValueError("产物路径不在当前任务目录内")
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def register_artifact(
    task_id: str,
    path: str | Path,
    artifact_type: str,
    producer: str,
    version: int = 1,
    inputs: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    task_dir = task_directory(task_id)
    artifact_path = contained_file(task_dir, path)
    manifest_path = task_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        manifest = {"schema_version": "1.0", "task_id": task_id, "artifacts": []}
    digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    record = {
        "artifact_id": f"{task_id}:{artifact_type}:v{version}",
        "type": artifact_type,
        "version": version,
        "uri": artifact_path.relative_to(task_dir).as_posix(),
        "bytes": artifact_path.stat().st_size,
        "sha256": digest,
        "producer": producer,
        "inputs": inputs or [],
        "metadata": metadata or {},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    artifacts = manifest.setdefault("artifacts", [])
    artifacts[:] = [a for a in artifacts if a.get("artifact_id") != record["artifact_id"]]
    artifacts.append(record)
    temp_path = manifest_path.with_suffix(".tmp")
    temp_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(manifest_path)
    return record
