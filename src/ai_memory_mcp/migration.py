from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import time
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from .audit import file_lock
from .collections import create_collection
from .intake import inspect_vault
from .vault import initialize_vault
from .wikilinks import normalized_link_target


AUTOMATIC_ROOTS: dict[str, tuple[str, str]] = {
    "People": ("People", "person"),
    "Projects": ("Projects", "project"),
    "Repos": ("Repositories", "repository"),
    "Tools": ("Tools", "tool"),
    "Links": ("Links", "link"),
    "Articles": ("Links", "link"),
    "Recipes": ("Recipes", "recipe"),
}
AUTOMATIC_PREFIXES: dict[tuple[str, ...], tuple[str, str]] = {
    ("References", "Meetings"): ("Meetings", "meeting"),
    ("References", "Conversations"): ("Conversations", "conversation"),
}
REVIEW_ROOTS = {"Areas", "Decisions", "Sessions", "Workflows", "Skills", "Indexes"}
WIKILINK_RE = re.compile(r"\[\[([^\]]+)]]")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _state_root(root: Path) -> Path:
    return root / ".ai-memory" / "migration"


def _run_dir(root: Path, migration_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", migration_id):
        raise ValueError("The migration ID contains unsupported characters.")
    return _state_root(root) / migration_id


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".pending")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    _replace_file(temporary, path)


def _replace_file(source: Path, target: Path) -> None:
    for attempt in range(12):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == 11:
                raise
            # OneDrive and virus scanners can hold a file after close. Retry
            # the same atomic publication without changing migration state.
            time.sleep(min(0.05 * (2**attempt), 1.0))


def _safe_relative_path(root: Path, value: str) -> tuple[str, Path]:
    relative = PurePosixPath(value.replace("\\", "/"))
    if relative.is_absolute() or not relative.parts:
        raise ValueError("The migration path must be relative to the vault.")
    if any(part in {"", ".", ".."} or part.startswith(".") for part in relative.parts):
        raise ValueError("The migration path contains a hidden or unsafe segment.")
    normalized = relative.as_posix()
    target = root.joinpath(*relative.parts).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError("The migration path escapes the vault.")
    return normalized, target


def _safe_run_path(run_dir: Path, value: str) -> Path:
    relative = PurePosixPath(value.replace("\\", "/"))
    if relative.is_absolute() or not relative.parts:
        raise ValueError("The migration state path must be relative.")
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("The migration state path contains an unsafe segment.")
    target = run_dir.joinpath(*relative.parts).resolve()
    if not target.is_relative_to(run_dir.resolve()):
        raise ValueError("The migration state path escapes the migration run.")
    return target


def _quality_summary(root: Path) -> dict[str, Any]:
    report = inspect_vault(root)
    return {
        "notes": report["notes"],
        "error_count": report["error_count"],
        "warning_count": report["warning_count"],
        "issue_counts": report["issue_counts"],
    }


def _visible_files(root: Path) -> list[str]:
    return sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
        and not any(part.startswith(".") for part in path.relative_to(root).parts)
    )


def _manual_manifest(root: Path, migration_id: str) -> tuple[Path, dict[str, Any]]:
    manifest_path = _run_dir(root, migration_id) / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError("The migration has not started.")
    manifest = _read_json(manifest_path)
    if manifest.get("format_version") != 2:
        raise ValueError("The migration uses the legacy plan format.")
    if Path(str(manifest.get("root", ""))).resolve() != root.resolve():
        raise ValueError("The migration belongs to a different vault.")
    return manifest_path, manifest


def _require_active(manifest: dict[str, Any]) -> None:
    if manifest.get("status") != "active":
        raise ValueError("The migration is not active.")


def _operation_id(manifest: dict[str, Any]) -> str:
    return f"op-{len(manifest.get('operations', [])) + 1:06d}"


def _operation_backup(
    root: Path,
    run_dir: Path,
    operation_id: str,
    relative: str,
) -> str:
    source = root / relative
    backup = run_dir / "backup" / operation_id / relative
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, backup)
    return backup.relative_to(run_dir).as_posix()


def _preserve_rollback_conflict(
    run_dir: Path,
    operation: dict[str, Any],
    relative: str,
    target: Path,
) -> None:
    conflict = (
        run_dir
        / "rollback-conflicts"
        / str(operation["id"])
        / relative
    )
    conflict.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(target, conflict)
    operation["preserved_conflict"] = conflict.relative_to(run_dir).as_posix()


def start_manual_migration(root: Path, *, migration_id: str) -> dict[str, Any]:
    root = root.resolve()
    if not root.is_dir():
        raise ValueError("The migration root must be an existing vault.")
    run_dir = _run_dir(root, migration_id)
    manifest_path = run_dir / "manifest.json"
    with file_lock(run_dir / "migration.lock", 30.0):
        if manifest_path.exists():
            manifest = _read_json(manifest_path)
            if manifest.get("format_version") == 2:
                return manifest
            raise ValueError("The migration ID already uses the legacy plan format.")
        if (run_dir / "plan.json").exists():
            raise ValueError("The migration ID already has a legacy plan.")
        manifest = {
            "format_version": 2,
            "migration_id": migration_id,
            "root": str(root),
            "status": "active",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "baseline_quality": _quality_summary(root),
            "initial_files": _visible_files(root),
            "operations": [],
        }
        _write_json(manifest_path, manifest)
        return manifest


def snapshot_migration_file(
    root: Path,
    *,
    migration_id: str,
    path: str,
) -> dict[str, Any]:
    root = root.resolve()
    relative, target = _safe_relative_path(root, path)
    run_dir = _run_dir(root, migration_id)
    with file_lock(run_dir / "migration.lock", 30.0):
        manifest_path, manifest = _manual_manifest(root, migration_id)
        _require_active(manifest)
        if any(
            operation.get("status") == "prepared"
            and operation.get("path") == relative
            for operation in manifest["operations"]
        ):
            raise ValueError("The migration path already has a prepared operation.")
        operation_id = _operation_id(manifest)
        if target.is_file():
            operation = {
                "id": operation_id,
                "kind": "edit",
                "status": "prepared",
                "path": relative,
                "before_sha256": _sha256(target),
                "backup": _operation_backup(
                    root, run_dir, operation_id, relative
                ),
                "prepared_at": datetime.now(timezone.utc).isoformat(),
            }
        elif target.exists():
            raise ValueError("The migration path is not a regular file.")
        else:
            if relative in manifest.get("initial_files", []):
                raise ValueError("The migration path existed when the migration started.")
            operation = {
                "id": operation_id,
                "kind": "create",
                "status": "prepared",
                "path": relative,
                "prepared_at": datetime.now(timezone.utc).isoformat(),
            }
        manifest["operations"].append(operation)
        _write_json(manifest_path, manifest)
        return operation


def _complete_prepared_file(
    root: Path,
    *,
    migration_id: str,
    path: str,
    kind: str,
) -> dict[str, Any]:
    root = root.resolve()
    relative, target = _safe_relative_path(root, path)
    run_dir = _run_dir(root, migration_id)
    with file_lock(run_dir / "migration.lock", 30.0):
        manifest_path, manifest = _manual_manifest(root, migration_id)
        _require_active(manifest)
        operation = next(
            (
                item
                for item in reversed(manifest["operations"])
                if item.get("kind") == kind
                and item.get("status") == "prepared"
                and item.get("path") == relative
            ),
            None,
        )
        if operation is None:
            raise ValueError("The migration path has no prepared operation.")
        if not target.is_file():
            raise ValueError("The migration path is not a regular file.")
        operation["after_sha256"] = _sha256(target)
        operation["status"] = "completed"
        operation["completed_at"] = datetime.now(timezone.utc).isoformat()
        operation["changed"] = (
            kind == "create"
            or operation["after_sha256"] != operation.get("before_sha256")
        )
        _write_json(manifest_path, manifest)
        return operation


def record_migration_edit(
    root: Path, *, migration_id: str, path: str
) -> dict[str, Any]:
    return _complete_prepared_file(
        root, migration_id=migration_id, path=path, kind="edit"
    )


def record_migration_create(
    root: Path, *, migration_id: str, path: str
) -> dict[str, Any]:
    return _complete_prepared_file(
        root, migration_id=migration_id, path=path, kind="create"
    )


def record_migration_move(
    root: Path,
    *,
    migration_id: str,
    source: str,
    destination: str,
) -> dict[str, Any]:
    root = root.resolve()
    source_name, source_path = _safe_relative_path(root, source)
    destination_name, destination_path = _safe_relative_path(root, destination)
    if source_name == destination_name:
        raise ValueError("The migration move needs two different paths.")
    run_dir = _run_dir(root, migration_id)
    with file_lock(run_dir / "migration.lock", 30.0):
        manifest_path, manifest = _manual_manifest(root, migration_id)
        _require_active(manifest)
        operation = next(
            (
                item
                for item in reversed(manifest["operations"])
                if item.get("kind") == "move"
                and item.get("status") == "prepared"
                and item.get("source") == source_name
                and item.get("destination") == destination_name
            ),
            None,
        )
        if operation is None:
            if not source_path.is_file():
                raise ValueError("The migration source is not a regular file.")
            if destination_path.exists():
                raise ValueError("The migration destination already exists.")
            operation_id = _operation_id(manifest)
            operation = {
                "id": operation_id,
                "kind": "move",
                "status": "prepared",
                "source": source_name,
                "destination": destination_name,
                "before_sha256": _sha256(source_path),
                "backup": _operation_backup(
                    root, run_dir, operation_id, source_name
                ),
                "prepared_at": datetime.now(timezone.utc).isoformat(),
            }
            manifest["operations"].append(operation)
            # Persist the prepared operation before movement so an interrupted
            # move can be resumed or rolled back without guessing file history.
            _write_json(manifest_path, manifest)
        if source_path.is_file() and not destination_path.exists():
            if _sha256(source_path) != operation["before_sha256"]:
                raise ValueError("The migration source changed before movement.")
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.replace(destination_path)
        elif not source_path.exists() and destination_path.is_file():
            if _sha256(destination_path) != operation["before_sha256"]:
                raise ValueError("The interrupted migration destination changed.")
        else:
            raise ValueError("The migration move has an unexpected file state.")
        operation["after_sha256"] = _sha256(destination_path)
        operation["status"] = "completed"
        operation["completed_at"] = datetime.now(timezone.utc).isoformat()
        _write_json(manifest_path, manifest)
        return operation


def inspect_migration(root: Path) -> dict[str, Any]:
    report = inspect_vault(root)
    roots: dict[str, int] = {}
    for path in root.rglob("*.md"):
        relative = path.relative_to(root)
        if any(part.startswith(".") for part in relative.parts):
            continue
        roots[relative.parts[0]] = roots.get(relative.parts[0], 0) + 1
    return {**report, "top_level_markdown": dict(sorted(roots.items()))}


def _mapped_destination(relative: PurePosixPath) -> tuple[str, str, str] | None:
    for prefix, (collection, record_type) in AUTOMATIC_PREFIXES.items():
        if relative.parts[: len(prefix)] == prefix:
            tail = relative.parts[len(prefix) :]
            destination = PurePosixPath("Collections", collection, "Records", *tail)
            return destination.as_posix(), collection, record_type
    mapped = AUTOMATIC_ROOTS.get(relative.parts[0])
    if mapped is None:
        return None
    collection, record_type = mapped
    destination = PurePosixPath(
        "Collections", collection, "Records", *relative.parts[1:]
    )
    return destination.as_posix(), collection, record_type


def plan_migration(
    root: Path,
    *,
    migration_id: str | None = None,
) -> dict[str, Any]:
    root = root.resolve()
    if not root.is_dir():
        raise ValueError("The migration root must be an existing vault.")
    migration_id = migration_id or datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ"
    )
    run_dir = _run_dir(root, migration_id)
    plan_path = run_dir / "plan.json"
    if plan_path.exists():
        existing = _read_json(plan_path)
        if Path(str(existing.get("root", ""))).resolve() != root:
            raise ValueError("The migration plan belongs to a different vault.")
        return existing

    operations: list[dict[str, Any]] = []
    collections: dict[str, str] = {}
    markdown_paths = sorted(
        (
            path
            for path in root.rglob("*.md")
            if not any(part.startswith(".") for part in path.relative_to(root).parts)
        ),
        key=lambda path: path.as_posix().casefold(),
    )
    for path in markdown_paths:
        source = path.relative_to(root).as_posix()
        relative = PurePosixPath(source)
        mapped = _mapped_destination(relative)
        if mapped is not None:
            destination, collection, record_type = mapped
            collections[collection] = record_type
            collision = (root / destination).exists() and destination != source
            operations.append(
                {
                    "source": source,
                    "destination": destination,
                    "decision": "review" if collision else "automatic",
                    "approved": not collision,
                    "collection": collection,
                    "record_type": record_type,
                    "metadata_updates": {
                        "collection": collection,
                        "record_type": record_type,
                    },
                    "reason": (
                        "The destination already exists."
                        if collision
                        else "The legacy folder has a direct collection mapping."
                    ),
                    "before_sha256": _sha256(path),
                }
            )
        elif relative.parts[0] in REVIEW_ROOTS:
            operations.append(
                {
                    "source": source,
                    "destination": None,
                    "decision": "review",
                    "approved": False,
                    "collection": None,
                    "record_type": None,
                    "reason": "The note needs semantic classification before movement.",
                    "before_sha256": _sha256(path),
                }
            )
        else:
            operations.append(
                {
                    "source": source,
                    "destination": source,
                    "decision": "keep",
                    "approved": False,
                    "collection": None,
                    "record_type": None,
                    "reason": "The note has no safe automatic mapping.",
                    "before_sha256": _sha256(path),
                }
            )

    payload = {
        "format_version": 1,
        "migration_id": migration_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "root": str(root.resolve()),
        "status": "planned",
        "baseline_quality": _quality_summary(root),
        "collections": collections,
        "instructions": (
            "Review each review operation. Set destination and approved to true only "
            "when the destination preserves the note meaning."
        ),
        "operations": operations,
    }
    _write_json(plan_path, payload)
    return payload


def _backup_file(root: Path, run_dir: Path, relative: str) -> str:
    normalized, source = _safe_relative_path(root, relative)
    backup = run_dir / "backup" / normalized
    backup.parent.mkdir(parents=True, exist_ok=True)
    if not backup.exists():
        shutil.copy2(source, backup)
    return backup.relative_to(run_dir).as_posix()


def _link_map(applied: list[dict[str, Any]]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    source_stems: dict[str, int] = {}
    for operation in applied:
        source = PurePosixPath(operation["source"]).with_suffix("").as_posix()
        destination = PurePosixPath(operation["destination"]).with_suffix("").as_posix()
        mapping[source.casefold()] = destination
        stem = PurePosixPath(source).name.casefold()
        source_stems[stem] = source_stems.get(stem, 0) + 1
    for operation in applied:
        source = PurePosixPath(operation["source"]).with_suffix("")
        # A bare title is safe to rewrite only when the migration plan has one
        # source with that title. Qualified paths remain the preferred key.
        if source_stems[source.name.casefold()] == 1:
            mapping[source.name.casefold()] = PurePosixPath(
                operation["destination"]
            ).with_suffix("").as_posix()
    return mapping


def _rewrite_links(raw: str, mapping: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        original = match.group(1)
        target_part, separator, label = original.partition("|")
        path_part = re.split(r"[#^]", target_part, maxsplit=1)[0]
        suffix = target_part[len(path_part) :]
        normalized = normalized_link_target(path_part)
        replacement = mapping.get(normalized.casefold())
        if replacement is None:
            return match.group(0)
        rebuilt = replacement + suffix
        if separator:
            rebuilt += separator + label
        return f"[[{rebuilt}]]"

    return WIKILINK_RE.sub(replace, raw)


def _update_frontmatter(raw: str, updates: dict[str, Any]) -> str:
    if not raw.startswith("---"):
        raise ValueError("The migration cannot update metadata without frontmatter.")
    parts = raw.split("---", 2)
    if len(parts) != 3:
        raise ValueError("The migration cannot update incomplete frontmatter.")
    metadata = yaml.safe_load(parts[1]) or {}
    if not isinstance(metadata, dict):
        raise ValueError("The migration frontmatter must be a mapping.")
    metadata.update(updates)
    if "status" in updates and "updated" not in updates:
        metadata["updated"] = date.today().isoformat()
    frontmatter = yaml.safe_dump(
        metadata,
        sort_keys=False,
        allow_unicode=True,
    ).rstrip()
    return f"---\n{frontmatter}\n---\n{parts[2].lstrip()}"


def apply_migration(root: Path, *, migration_id: str) -> dict[str, Any]:
    root = root.resolve()
    run_dir = _run_dir(root, migration_id)
    plan_path = run_dir / "plan.json"
    if not plan_path.exists():
        raise ValueError("The migration plan does not exist.")
    manifest_path = run_dir / "manifest.json"
    with file_lock(run_dir / "migration.lock", 30.0):
        plan = _read_json(plan_path)
        if Path(str(plan.get("root", ""))).resolve() != root:
            raise ValueError("The migration plan belongs to a different vault.")
        if manifest_path.exists():
            manifest = _read_json(manifest_path)
            if manifest.get("status") == "applied":
                return manifest
            if manifest.get("status") == "rolled-back":
                raise ValueError("The migration has already been rolled back.")
        else:
            manifest = {
                "format_version": 1,
                "migration_id": migration_id,
                "root": str(root),
                "status": "applying",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "baseline_quality": plan.get("baseline_quality"),
                "operations": [],
                "modified_links": [],
            }
            _write_json(manifest_path, manifest)

        applied_by_key = {
            (str(item["source"]), str(item["destination"])): item
            for item in manifest["operations"]
        }
        links_by_path = {
            str(item["path"]): item for item in manifest["modified_links"]
        }
        resume_mapping = _link_map(
            [
                item
                for item in manifest["operations"]
                if item.get("status") == "completed"
            ]
        )
        for planned in plan["operations"]:
            if not planned.get("approved") or not planned.get("destination"):
                continue
            source_name, source = _safe_relative_path(
                root, str(planned["source"])
            )
            destination_name, destination = _safe_relative_path(
                root, str(planned["destination"])
            )
            if source_name == destination_name:
                continue
            if not source_name.casefold().endswith(".md") or not (
                destination_name.casefold().endswith(".md")
            ):
                raise ValueError("A planned migration move must use Markdown files.")
            key = (source_name, destination_name)
            operation = applied_by_key.get(key)
            if operation is None:
                if destination.exists():
                    raise ValueError(
                        f"The migration destination already exists: {destination_name}"
                    )
                if not source.is_file():
                    raise ValueError(
                        f"The migration source does not exist: {source_name}"
                    )
                if _sha256(source) != planned["before_sha256"]:
                    raise ValueError(
                        f"The migration source changed after planning: {source_name}"
                    )
                operation = {
                    **planned,
                    "source": source_name,
                    "destination": destination_name,
                    "status": "prepared",
                    "backup": _backup_file(root, run_dir, source_name),
                    "prepared_at": datetime.now(timezone.utc).isoformat(),
                }
                manifest["operations"].append(operation)
                applied_by_key[key] = operation
                # Record the backup and intent before movement. A retry can
                # then distinguish a completed move from an unrelated file.
                _write_json(manifest_path, manifest)

            if operation.get("status") == "completed":
                current_sha256 = (
                    _sha256(destination) if destination.is_file() else None
                )
                link_item = links_by_path.get(destination_name)
                link_sha256 = (
                    link_item.get("after_sha256") if link_item else None
                )
                if link_item and link_sha256 is None and link_item.get("backup"):
                    link_backup = _safe_run_path(
                        run_dir, str(link_item["backup"])
                    ).read_text(encoding="utf-8-sig")
                    link_sha256 = hashlib.sha256(
                        _rewrite_links(link_backup, resume_mapping).encode("utf-8")
                    ).hexdigest()
                if current_sha256 not in {
                    operation.get("after_sha256"),
                    link_sha256,
                }:
                    raise ValueError(
                        f"The completed migration destination changed: {destination_name}"
                    )
                continue
            if source.is_file() and not destination.exists():
                if _sha256(source) != operation["before_sha256"]:
                    raise ValueError(
                        f"The migration source changed after backup: {source_name}"
                    )
                destination.parent.mkdir(parents=True, exist_ok=True)
                source.replace(destination)
            elif source.exists() or not destination.is_file():
                raise ValueError(
                    f"The interrupted migration has an unexpected state: {source_name}"
                )

            updates = operation.get("metadata_updates")
            raw = destination.read_text(encoding="utf-8-sig")
            original = _safe_run_path(
                run_dir, str(operation["backup"])
            ).read_text(
                encoding="utf-8-sig"
            )
            expected = (
                _update_frontmatter(original, updates)
                if isinstance(updates, dict) and updates
                else original
            )
            if raw not in {original, expected}:
                raise ValueError(
                    f"The interrupted migration destination changed: {destination_name}"
                )
            if raw != expected:
                destination.write_text(
                    expected,
                    encoding="utf-8",
                    newline="\n",
                )
            operation["after_sha256"] = _sha256(destination)
            operation["status"] = "completed"
            operation["completed_at"] = datetime.now(timezone.utc).isoformat()
            _write_json(manifest_path, manifest)

        manifest["vault_initialization"] = initialize_vault(root)
        for name, record_type in plan.get("collections", {}).items():
            create_collection(root, name=name, record_type=record_type)

        mapping = _link_map(manifest["operations"])
        if mapping:
            for path in sorted(root.rglob("*.md")):
                relative = path.relative_to(root)
                if any(part.startswith(".") for part in relative.parts):
                    continue
                name = relative.as_posix()
                raw = path.read_text(encoding="utf-8-sig")
                changed = _rewrite_links(raw, mapping)
                item = links_by_path.get(name)
                if item is not None and item.get("status") == "completed":
                    if _sha256(path) != item.get("after_sha256"):
                        raise ValueError(
                            f"A completed migrated link file changed: {name}"
                        )
                    continue
                if changed == raw and item is None:
                    continue
                if item is None:
                    item = {
                        "path": name,
                        "status": "prepared",
                        "backup": _backup_file(root, run_dir, name),
                        "before_sha256": _sha256(path),
                        "prepared_at": datetime.now(timezone.utc).isoformat(),
                    }
                    manifest["modified_links"].append(item)
                    links_by_path[name] = item
                    _write_json(manifest_path, manifest)
                backup = _safe_run_path(run_dir, str(item["backup"]))
                original = backup.read_text(encoding="utf-8-sig")
                expected = _rewrite_links(original, mapping)
                if raw not in {original, expected}:
                    raise ValueError(
                        f"The interrupted migrated link file changed: {name}"
                    )
                if raw != expected:
                    path.write_text(expected, encoding="utf-8", newline="\n")
                item["after_sha256"] = _sha256(path)
                item["status"] = "completed"
                item["completed_at"] = datetime.now(timezone.utc).isoformat()
                _write_json(manifest_path, manifest)

        for operation in manifest["operations"]:
            _, destination = _safe_relative_path(
                root, str(operation["destination"])
            )
            operation["after_sha256"] = _sha256(destination)
        manifest["status"] = "applied"
        manifest["applied_at"] = datetime.now(timezone.utc).isoformat()
        _write_json(manifest_path, manifest)
        return manifest


def _verify_legacy_migration(root: Path, *, migration_id: str) -> dict[str, Any]:
    root = root.resolve()
    run_dir = _run_dir(root, migration_id)
    with file_lock(run_dir / "migration.lock", 30.0):
        manifest = _read_json(run_dir / "manifest.json")
        if Path(str(manifest.get("root", ""))).resolve() != root:
            raise ValueError("The migration belongs to a different vault.")
        issues: list[str] = []
        for operation in manifest.get("operations", []):
            if operation.get("status") not in {None, "completed"}:
                issues.append(
                    f"Prepared operation is incomplete: {operation['source']}"
                )
                continue
            _, destination = _safe_relative_path(
                root, str(operation["destination"])
            )
            backup = (
                _safe_run_path(run_dir, str(operation["backup"]))
                if operation.get("backup")
                else None
            )
            if backup is not None and not backup.is_file():
                issues.append(f"Missing backup: {operation['backup']}")
            if not destination.is_file():
                issues.append(f"Missing destination: {operation['destination']}")
            elif _sha256(destination) != operation.get("after_sha256"):
                issues.append(f"Changed destination: {operation['destination']}")
        for item in manifest.get("modified_links", []):
            if item.get("status") not in {None, "completed"}:
                issues.append(f"Prepared link edit is incomplete: {item['path']}")
                continue
            _, target = _safe_relative_path(root, str(item["path"]))
            backup = (
                _safe_run_path(run_dir, str(item["backup"]))
                if item.get("backup")
                else None
            )
            if backup is not None and not backup.is_file():
                issues.append(f"Missing backup: {item['backup']}")
            if not target.is_file():
                issues.append(f"Missing link file: {item['path']}")
            elif _sha256(target) != item.get("after_sha256"):
                issues.append(f"Changed link file: {item['path']}")

        quality = _quality_summary(root)
        baseline_counts = (manifest.get("baseline_quality") or {}).get(
            "issue_counts", {}
        )
        regressions = {
            rule: count - int(baseline_counts.get(rule, 0))
            for rule, count in quality["issue_counts"].items()
            if baseline_counts and count > int(baseline_counts.get(rule, 0))
        }
        for rule, count in sorted(regressions.items()):
            issues.append(f"Quality issue increased: {rule} (+{count})")
        result = {
            "migration_id": migration_id,
            "ok": not issues,
            "issues": issues,
            "quality_regressions": regressions,
            "memory_quality": quality,
            "verified_at": datetime.now(timezone.utc).isoformat(),
        }
        _write_json(run_dir / "verification.json", result)
        return result


def _rollback_legacy_migration(root: Path, *, migration_id: str) -> dict[str, Any]:
    root = root.resolve()
    run_dir = _run_dir(root, migration_id)
    manifest_path = run_dir / "manifest.json"
    with file_lock(run_dir / "migration.lock", 30.0):
        manifest = _read_json(manifest_path)
        if Path(str(manifest.get("root", ""))).resolve() != root:
            raise ValueError("The migration belongs to a different vault.")
        if manifest.get("status") == "rolled-back":
            return manifest

        link_states: list[tuple[dict[str, Any], Path, Path]] = []
        for item in reversed(manifest.get("modified_links", [])):
            if item.get("rollback_status") == "completed":
                continue
            _, target = _safe_relative_path(root, str(item["path"]))
            backup = _safe_run_path(run_dir, str(item["backup"]))
            if not backup.is_file():
                raise ValueError(f"A migration backup is missing: {item['backup']}")
            if not target.is_file():
                raise ValueError(f"A migrated link file is missing: {item['path']}")
            link_states.append((item, target, backup))

        operation_states: list[
            tuple[dict[str, Any], Path, Path, Path | None]
        ] = []
        for operation in reversed(manifest.get("operations", [])):
            if operation.get("rollback_status") == "completed":
                continue
            _, source = _safe_relative_path(root, str(operation["source"]))
            _, destination = _safe_relative_path(
                root, str(operation["destination"])
            )
            backup_name = operation.get("backup")
            backup = (
                _safe_run_path(run_dir, str(backup_name))
                if backup_name
                else None
            )
            if backup is not None and not backup.is_file():
                raise ValueError(f"A migration backup is missing: {backup_name}")
            if source.exists() == destination.exists():
                raise ValueError(
                    f"The rollback move has an unexpected state: {operation['source']}"
                )
            operation_states.append((operation, source, destination, backup))

        # Preserve every post-migration edit before rollback replaces content.
        # This makes rollback recoverable without guessing which edit was valid.
        for item, target, _ in link_states:
            expected = item.get("after_sha256")
            if expected and _sha256(target) != expected:
                conflict = (
                    run_dir
                    / "rollback-conflicts"
                    / "links"
                    / str(item["path"])
                )
                conflict.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, conflict)
                item["preserved_conflict"] = conflict.relative_to(
                    run_dir
                ).as_posix()
        for operation, source, destination, backup in operation_states:
            current = source if source.exists() else destination
            expected = operation.get("after_sha256")
            if expected and _sha256(current) != expected:
                conflict = (
                    run_dir
                    / "rollback-conflicts"
                    / "moves"
                    / str(operation["source"])
                )
                conflict.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(current, conflict)
                operation["preserved_conflict"] = conflict.relative_to(
                    run_dir
                ).as_posix()
            if backup is None and operation.get("metadata_updates"):
                raise ValueError(
                    f"A metadata-changing move has no backup: {operation['source']}"
                )

        manifest["status"] = "rolling-back"
        _write_json(manifest_path, manifest)
        for item, target, backup in link_states:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, target)
            item["rollback_status"] = "completed"
            _write_json(manifest_path, manifest)
        for operation, source, destination, backup in operation_states:
            if destination.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                destination.replace(source)
            if backup is not None:
                shutil.copy2(backup, source)
            operation["rollback_status"] = "completed"
            _write_json(manifest_path, manifest)
        manifest["status"] = "rolled-back"
        manifest["rolled_back_at"] = datetime.now(timezone.utc).isoformat()
        _write_json(manifest_path, manifest)
        return manifest


def _verify_manual_migration(root: Path, *, migration_id: str) -> dict[str, Any]:
    root = root.resolve()
    run_dir = _run_dir(root, migration_id)
    with file_lock(run_dir / "migration.lock", 30.0):
        manifest_path, manifest = _manual_manifest(root, migration_id)
        if manifest.get("status") == "rolled-back":
            raise ValueError("The migration has already been rolled back.")
        issues: list[str] = []
        expected_files: dict[str, str] = {}
        removed_paths: set[str] = set()
        for operation in manifest["operations"]:
            status = str(operation.get("status"))
            kind = str(operation.get("kind"))
            if status != "completed":
                issues.append(f"Prepared operation is incomplete: {operation['id']}")
                continue
            backup_name = operation.get("backup")
            if kind in {"edit", "move"}:
                if not backup_name:
                    issues.append(f"Operation has no backup: {operation['id']}")
                elif not _safe_run_path(
                    run_dir, str(backup_name)
                ).is_file():
                    issues.append(f"Missing backup: {backup_name}")
            if kind in {"edit", "create"}:
                path = str(operation["path"])
                expected_files[path] = str(operation["after_sha256"])
                removed_paths.discard(path)
            elif kind == "move":
                source = str(operation["source"])
                destination = str(operation["destination"])
                expected_files.pop(source, None)
                removed_paths.add(source)
                expected_files[destination] = str(operation["after_sha256"])
                removed_paths.discard(destination)
            else:
                issues.append(f"Unsupported operation kind: {kind}")

        # Verify the replayed final state because a later move can supersede an
        # earlier edit path without invalidating either recorded operation.
        for path, expected_sha256 in sorted(expected_files.items()):
            _, target = _safe_relative_path(root, path)
            if not target.is_file():
                issues.append(f"Missing file: {path}")
            elif _sha256(target) != expected_sha256:
                issues.append(f"Changed file: {path}")
        for path in sorted(removed_paths - expected_files.keys()):
            _, target = _safe_relative_path(root, path)
            if target.exists():
                issues.append(f"Moved source still exists: {path}")

        quality = _quality_summary(root)
        baseline_counts = manifest.get("baseline_quality", {}).get(
            "issue_counts", {}
        )
        regressions = {
            rule: count - int(baseline_counts.get(rule, 0))
            for rule, count in quality["issue_counts"].items()
            if count > int(baseline_counts.get(rule, 0))
        }
        for rule, count in sorted(regressions.items()):
            issues.append(f"Quality issue increased: {rule} (+{count})")
        result = {
            "migration_id": migration_id,
            "ok": not issues,
            "issues": issues,
            "quality_regressions": regressions,
            "memory_quality": quality,
            "verified_at": datetime.now(timezone.utc).isoformat(),
        }
        _write_json(run_dir / "verification.json", result)
        if result["ok"]:
            manifest["status"] = "verified"
            manifest["verified_at"] = result["verified_at"]
            _write_json(manifest_path, manifest)
        return result


def verify_migration(root: Path, *, migration_id: str) -> dict[str, Any]:
    manifest = _read_json(_run_dir(root.resolve(), migration_id) / "manifest.json")
    if manifest.get("format_version") == 2:
        return _verify_manual_migration(root, migration_id=migration_id)
    return _verify_legacy_migration(root, migration_id=migration_id)


def _rollback_manual_migration(root: Path, *, migration_id: str) -> dict[str, Any]:
    root = root.resolve()
    run_dir = _run_dir(root, migration_id)
    with file_lock(run_dir / "migration.lock", 30.0):
        manifest_path, manifest = _manual_manifest(root, migration_id)
        if manifest.get("status") == "rolled-back":
            return manifest
        manifest["status"] = "rolling-back"
        _write_json(manifest_path, manifest)
        for operation in reversed(manifest["operations"]):
            if operation.get("rollback_status") == "completed":
                continue
            kind = str(operation.get("kind"))
            status = str(operation.get("status"))
            if kind == "edit":
                _, target = _safe_relative_path(root, str(operation["path"]))
                backup = _safe_run_path(run_dir, str(operation["backup"]))
                if not backup.is_file():
                    raise ValueError(f"A migration backup is missing: {operation['backup']}")
                if status == "completed":
                    if not target.is_file():
                        raise ValueError(
                            f"The edited file is missing: {operation['path']}"
                        )
                    if _sha256(target) != operation["after_sha256"]:
                        _preserve_rollback_conflict(
                            run_dir,
                            operation,
                            str(operation["path"]),
                            target,
                        )
                elif target.is_file() and _sha256(target) != operation["before_sha256"]:
                    # A prepared edit has no trusted final hash. Preserve its
                    # current content before restoring the recorded baseline.
                    _preserve_rollback_conflict(
                        run_dir,
                        operation,
                        str(operation["path"]),
                        target,
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup, target)
            elif kind == "create":
                _, target = _safe_relative_path(root, str(operation["path"]))
                if target.exists():
                    if status == "completed" and (
                        not target.is_file()
                        or _sha256(target) != operation["after_sha256"]
                    ):
                        if not target.is_file():
                            raise ValueError(
                                f"The created path is not a file: {operation['path']}"
                            )
                    archive = (
                        run_dir
                        / "rollback-created"
                        / str(operation["id"])
                        / str(operation["path"])
                    )
                    archive.parent.mkdir(parents=True, exist_ok=True)
                    # Repository policy forbids destructive rollback. Keep a
                    # created file under the migration run instead of deleting it.
                    target.replace(archive)
                    operation["rollback_archive"] = archive.relative_to(
                        run_dir
                    ).as_posix()
            elif kind == "move":
                _, source = _safe_relative_path(root, str(operation["source"]))
                _, destination = _safe_relative_path(
                    root, str(operation["destination"])
                )
                backup = _safe_run_path(run_dir, str(operation["backup"]))
                if not backup.is_file():
                    raise ValueError(
                        f"A migration backup is missing: {operation['backup']}"
                    )
                if source.exists():
                    if destination.exists():
                        raise ValueError(
                            f"The move source was reused: {operation['source']}"
                        )
                elif destination.is_file():
                    expected = operation.get("after_sha256") or operation["before_sha256"]
                    if _sha256(destination) != expected:
                        _preserve_rollback_conflict(
                            run_dir,
                            operation,
                            str(operation["destination"]),
                            destination,
                        )
                    source.parent.mkdir(parents=True, exist_ok=True)
                    destination.replace(source)
                else:
                    raise ValueError(
                        f"The moved file is missing: {operation['destination']}"
                    )
                # A destination can change after movement. Preserve that state,
                # then restore the exact source bytes recorded before the move.
                shutil.copy2(backup, source)
            else:
                raise ValueError(f"Unsupported operation kind: {kind}")
            operation["rollback_status"] = "completed"
            _write_json(manifest_path, manifest)
        manifest["status"] = "rolled-back"
        manifest["rolled_back_at"] = datetime.now(timezone.utc).isoformat()
        _write_json(manifest_path, manifest)
        return manifest


def rollback_migration(root: Path, *, migration_id: str) -> dict[str, Any]:
    manifest = _read_json(_run_dir(root.resolve(), migration_id) / "manifest.json")
    if manifest.get("format_version") == 2:
        return _rollback_manual_migration(root, migration_id=migration_id)
    return _rollback_legacy_migration(root, migration_id=migration_id)


def migration_status(root: Path, *, migration_id: str) -> dict[str, Any]:
    run_dir = _run_dir(root, migration_id)
    for name in ("manifest.json", "plan.json"):
        path = run_dir / name
        if path.exists():
            payload = _read_json(path)
            operations = payload.get("operations", [])
            return {
                "migration_id": migration_id,
                "status": payload.get("status", "planned"),
                "run_directory": str(run_dir),
                "format_version": payload.get("format_version", 1),
                "operations": len(operations),
                "prepared_operations": sum(
                    operation.get("status") == "prepared"
                    for operation in operations
                ),
            }
    return {
        "migration_id": migration_id,
        "status": "not-found",
        "run_directory": str(run_dir),
        "operations": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Record and recover an AI-directed vault migration."
    )
    parser.add_argument("--root", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("inspect")
    start = commands.add_parser("start")
    start.add_argument("--id", required=True)
    snapshot = commands.add_parser("snapshot")
    snapshot.add_argument("--id", required=True)
    snapshot.add_argument("path")
    edit = commands.add_parser("record-edit")
    edit.add_argument("--id", required=True)
    edit.add_argument("path")
    create = commands.add_parser("record-create")
    create.add_argument("--id", required=True)
    create.add_argument("path")
    move = commands.add_parser("record-move")
    move.add_argument("--id", required=True)
    move.add_argument("source")
    move.add_argument("destination")
    for command in ("verify", "rollback", "status"):
        child = commands.add_parser(command)
        child.add_argument("--id", required=True)
    arguments = parser.parse_args()
    if arguments.command == "inspect":
        result = inspect_migration(arguments.root)
    elif arguments.command == "start":
        result = start_manual_migration(arguments.root, migration_id=arguments.id)
    elif arguments.command == "snapshot":
        result = snapshot_migration_file(
            arguments.root,
            migration_id=arguments.id,
            path=arguments.path,
        )
    elif arguments.command == "record-edit":
        result = record_migration_edit(
            arguments.root,
            migration_id=arguments.id,
            path=arguments.path,
        )
    elif arguments.command == "record-create":
        result = record_migration_create(
            arguments.root,
            migration_id=arguments.id,
            path=arguments.path,
        )
    elif arguments.command == "record-move":
        result = record_migration_move(
            arguments.root,
            migration_id=arguments.id,
            source=arguments.source,
            destination=arguments.destination,
        )
    elif arguments.command == "verify":
        result = verify_migration(arguments.root, migration_id=arguments.id)
    elif arguments.command == "rollback":
        result = rollback_migration(arguments.root, migration_id=arguments.id)
    else:
        result = migration_status(arguments.root, migration_id=arguments.id)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
