from __future__ import annotations

import hashlib
import os
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any

from .audit import file_lock
from .intake import parse_markdown, validate_new_markdown


def _digest(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def safe_memory_path(root: Path, relative: str) -> Path:
    normalized = PurePosixPath(relative.replace("\\", "/"))
    if normalized.is_absolute() or not normalized.parts:
        raise ValueError("The memory path must be relative to the writable vault.")
    if any(part in {"", ".", ".."} or part.startswith(".") for part in normalized.parts):
        raise ValueError("The memory path contains a hidden or unsafe segment.")
    if normalized.suffix.casefold() != ".md":
        raise ValueError("The memory path must end with .md.")
    if any(part.casefold() == "restricted" for part in normalized.parts):
        raise ValueError("The memory path cannot use the Restricted directory.")
    target = root.joinpath(*normalized.parts).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError("The memory path escapes the writable vault.")
    return target


def _existing_identity_paths(root: Path, memory_id: str, target: Path) -> list[str]:
    collisions: list[str] = []
    for path in root.rglob("*.md"):
        if path.resolve() == target:
            continue
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        metadata, _, error = parse_markdown(path.read_text(encoding="utf-8-sig"))
        if error is None and str(metadata.get("memory_id") or "") == memory_id:
            collisions.append(path.relative_to(root).as_posix())
    return collisions


def _validate_collection_path(path: str, metadata: dict[str, Any]) -> None:
    parts = PurePosixPath(path).parts
    if len(parts) < 3 or parts[0] != "Collections" or parts[2] != "Records":
        return
    expected = parts[1]
    if str(metadata.get("collection") or "") != expected:
        raise ValueError(
            f"A record under Collections/{expected}/Records must use "
            f"collection: {expected}."
        )


def _replace_file(source: Path, target: Path) -> None:
    for attempt in range(12):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == 11:
                raise
            # A sync client can hold the target after close. Retry the same
            # atomic publication so readers never see partial Markdown.
            time.sleep(min(0.05 * (2**attempt), 1.0))


def upsert_memory(
    root: Path,
    *,
    relative_path: str,
    markdown: str,
    expected_sha256: str | None = None,
    lock_timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    root = root.resolve()
    # The lock covers identity discovery, compare-and-swap, and publication.
    # Separate MCP server processes can otherwise pass the same stale digest.
    with file_lock(root / ".ai-memory" / "capture.lock", lock_timeout_seconds):
        target = safe_memory_path(root, relative_path)
        normalized_path = target.relative_to(root).as_posix()
        metadata = validate_new_markdown(markdown, path=normalized_path)
        _validate_collection_path(normalized_path, metadata)
        memory_id = str(metadata["memory_id"])
        collisions = _existing_identity_paths(root, memory_id, target)
        if collisions:
            raise ValueError(
                f"The memory_id {memory_id!r} already exists at {collisions[0]}."
            )

        existing = (
            target.read_text(encoding="utf-8-sig") if target.is_file() else None
        )
        new_digest = _digest(markdown)
        if existing is not None and _digest(existing) == new_digest:
            return {
                "path": normalized_path,
                "memory_id": memory_id,
                "sha256": new_digest,
                "created": False,
                "changed": False,
                "indexed": False,
            }
        if existing is not None:
            if expected_sha256 is None:
                raise ValueError(
                    "expected_sha256 is required to update an existing record."
                )
            if _digest(existing) != expected_sha256:
                raise ValueError(
                    "The existing memory record changed before this update."
                )
        elif expected_sha256 is not None:
            raise ValueError(
                "The memory record does not exist for the expected_sha256 value."
            )

        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".pending",
            dir=target.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(
                descriptor, "w", encoding="utf-8", newline="\n"
            ) as stream:
                stream.write(markdown)
                stream.flush()
                os.fsync(stream.fileno())
            # Atomic replacement keeps readers from observing partial Markdown.
            _replace_file(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()
        return {
            "path": normalized_path,
            "memory_id": memory_id,
            "sha256": new_digest,
            "created": existing is None,
            "changed": True,
            "indexed": False,
        }
