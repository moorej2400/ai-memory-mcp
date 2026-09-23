from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from ai_memory_mcp.migration import (
    _write_json,
    apply_migration,
    migration_status,
    plan_migration,
    record_migration_create,
    record_migration_edit,
    record_migration_move,
    rollback_migration,
    snapshot_migration_file,
    start_manual_migration,
    verify_migration,
)


def _legacy(title: str, memory_id: str, body: str = "A durable summary.") -> str:
    return f"""---
memory_id: {memory_id}
title: {title}
type: memory
status: active
created: 2026-09-18
updated: 2026-09-18
---
# {title}

{body}
"""


def _current(title: str, memory_id: str, body: str = "A durable summary.") -> str:
    return f"""---
schema_version: 2
memory_id: {memory_id}
title: {title}
type: memory
record_type: note
domain: general
status: active
created: 2026-09-20
updated: 2026-09-20
provenance:
  - source: manual-test
---
# {title}

{body}
"""


def test_migration_plans_review_and_applies_safe_moves(tmp_path: Path) -> None:
    people = tmp_path / "People"
    people.mkdir()
    (people / "Example Person.md").write_text(
        _legacy("Example Person", "person_example"), encoding="utf-8"
    )
    notes = tmp_path / "Notes"
    notes.mkdir()
    (notes / "Reference.md").write_text(
        _legacy("Reference", "note_reference", "See [[People/Example Person#Details|the person]]."),
        encoding="utf-8",
    )
    decisions = tmp_path / "Decisions"
    decisions.mkdir()
    (decisions / "Choice.md").write_text(
        _legacy("Choice", "choice_example"), encoding="utf-8"
    )

    plan = plan_migration(tmp_path, migration_id="test-run")
    by_source = {item["source"]: item for item in plan["operations"]}
    assert by_source["People/Example Person.md"]["decision"] == "automatic"
    assert by_source["Decisions/Choice.md"]["decision"] == "review"

    manifest = apply_migration(tmp_path, migration_id="test-run")
    destination = tmp_path / "Collections/People/Records/Example Person.md"
    assert destination.is_file()
    metadata = yaml.safe_load(destination.read_text().split("---", 2)[1])
    assert metadata["collection"] == "People"
    assert metadata["record_type"] == "person"
    assert not (tmp_path / "People/Example Person.md").exists()
    assert "[[Collections/People/Records/Example Person#Details|the person]]" in (
        notes / "Reference.md"
    ).read_text()
    assert (tmp_path / ".ai-memory/migration/test-run/backup/People/Example Person.md").is_file()
    assert manifest["status"] == "applied"
    assert apply_migration(tmp_path, migration_id="test-run") == manifest
    assert verify_migration(tmp_path, migration_id="test-run")["ok"] is True

    rolled_back = rollback_migration(tmp_path, migration_id="test-run")
    assert rolled_back["status"] == "rolled-back"
    assert (tmp_path / "People/Example Person.md").is_file()
    assert "[[People/Example Person#Details|the person]]" in (
        notes / "Reference.md"
    ).read_text()
    assert migration_status(tmp_path, migration_id="test-run")["status"] == "rolled-back"


def test_review_operation_can_be_approved_by_editing_plan(tmp_path: Path) -> None:
    source = tmp_path / "Decisions/Choice.md"
    source.parent.mkdir()
    source.write_text(_legacy("Choice", "choice_example"), encoding="utf-8")
    plan_migration(tmp_path, migration_id="reviewed")
    plan_path = tmp_path / ".ai-memory/migration/reviewed/plan.json"
    payload = json.loads(plan_path.read_text())
    operation = payload["operations"][0]
    operation["approved"] = True
    operation["destination"] = "Notes/Choice.md"
    operation["metadata_updates"] = {"status": "archived"}
    plan_path.write_text(json.dumps(payload), encoding="utf-8")

    apply_migration(tmp_path, migration_id="reviewed")
    migrated = (tmp_path / "Notes/Choice.md").read_text()
    assert "status: archived" in migrated


def test_manual_migration_records_ai_operations_and_rolls_back(tmp_path: Path) -> None:
    original = tmp_path / "Notes/Original.md"
    original.parent.mkdir()
    original.write_text(_current("Original", "mem-original"), encoding="utf-8")

    start_manual_migration(tmp_path, migration_id="manual")
    snapshot_migration_file(
        tmp_path,
        migration_id="manual",
        path="Notes/Original.md",
    )
    original.write_text(
        _current("Original", "mem-original", "A revised summary."),
        encoding="utf-8",
    )
    record_migration_edit(
        tmp_path,
        migration_id="manual",
        path="Notes/Original.md",
    )
    record_migration_move(
        tmp_path,
        migration_id="manual",
        source="Notes/Original.md",
        destination="Collections/Topics/Records/Original.md",
    )
    snapshot_migration_file(
        tmp_path,
        migration_id="manual",
        path="Notes/New.md",
    )
    new_note = tmp_path / "Notes/New.md"
    new_note.write_text(_current("New", "mem-new"), encoding="utf-8")
    record_migration_create(
        tmp_path,
        migration_id="manual",
        path="Notes/New.md",
    )

    verification = verify_migration(tmp_path, migration_id="manual")
    assert verification["ok"] is True
    rolled_back = rollback_migration(tmp_path, migration_id="manual")

    assert rolled_back["status"] == "rolled-back"
    assert original.read_text(encoding="utf-8") == _current(
        "Original", "mem-original"
    )
    assert not new_note.exists()
    assert (
        tmp_path
        / ".ai-memory/migration/manual/rollback-created/op-000003/Notes/New.md"
    ).is_file()


def test_manual_migration_rejects_paths_outside_the_vault(tmp_path: Path) -> None:
    start_manual_migration(tmp_path, migration_id="manual")

    with pytest.raises(ValueError, match="unsafe segment"):
        snapshot_migration_file(
            tmp_path,
            migration_id="manual",
            path="../outside.md",
        )


def test_legacy_apply_resumes_after_a_prepared_move(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "People/Example Person.md"
    source.parent.mkdir()
    source.write_text(_legacy("Example Person", "person_example"), encoding="utf-8")
    plan_migration(tmp_path, migration_id="resume")

    from ai_memory_mcp import migration

    original_update = migration._update_frontmatter
    attempts = 0

    def fail_once(raw: str, updates: dict[str, object]) -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("synthetic interruption")
        return original_update(raw, updates)

    monkeypatch.setattr(migration, "_update_frontmatter", fail_once)
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        apply_migration(tmp_path, migration_id="resume")

    manifest = apply_migration(tmp_path, migration_id="resume")
    assert manifest["status"] == "applied"
    assert manifest["operations"][0]["status"] == "completed"


def test_legacy_plan_rejects_an_unsafe_review_destination(tmp_path: Path) -> None:
    source = tmp_path / "Decisions/Choice.md"
    source.parent.mkdir()
    source.write_text(_legacy("Choice", "choice_example"), encoding="utf-8")
    plan_migration(tmp_path, migration_id="unsafe")
    plan_path = tmp_path / ".ai-memory/migration/unsafe/plan.json"
    payload = json.loads(plan_path.read_text())
    payload["operations"][0]["approved"] = True
    payload["operations"][0]["destination"] = "../outside.md"
    plan_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="unsafe segment"):
        apply_migration(tmp_path, migration_id="unsafe")


def test_manual_rollback_preserves_a_post_migration_edit(tmp_path: Path) -> None:
    note = tmp_path / "Notes/Original.md"
    note.parent.mkdir()
    note.write_text(_current("Original", "mem-original"), encoding="utf-8")
    start_manual_migration(tmp_path, migration_id="conflict")
    snapshot_migration_file(
        tmp_path,
        migration_id="conflict",
        path="Notes/Original.md",
    )
    migrated = _current("Original", "mem-original", "Migrated summary.")
    note.write_text(migrated, encoding="utf-8")
    operation = record_migration_edit(
        tmp_path,
        migration_id="conflict",
        path="Notes/Original.md",
    )
    newer = _current("Original", "mem-original", "Newer user edit.")
    note.write_text(newer, encoding="utf-8")

    rolled_back = rollback_migration(tmp_path, migration_id="conflict")

    assert note.read_text(encoding="utf-8") == _current(
        "Original", "mem-original"
    )
    conflict = (
        tmp_path
        / ".ai-memory/migration/conflict/rollback-conflicts"
        / operation["id"]
        / "Notes/Original.md"
    )
    assert conflict.read_text(encoding="utf-8") == newer
    assert rolled_back["status"] == "rolled-back"


def test_manual_rollback_restores_move_source_and_preserves_later_edit(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Notes/Original.md"
    source.parent.mkdir()
    original = _current("Original", "mem-original")
    source.write_text(original, encoding="utf-8")
    start_manual_migration(tmp_path, migration_id="move-conflict")
    operation = record_migration_move(
        tmp_path,
        migration_id="move-conflict",
        source="Notes/Original.md",
        destination="Notes/Moved.md",
    )
    destination = tmp_path / "Notes/Moved.md"
    newer = _current("Original", "mem-original", "Newer destination edit.")
    destination.write_text(newer, encoding="utf-8")

    rollback_migration(tmp_path, migration_id="move-conflict")

    assert source.read_text(encoding="utf-8") == original
    assert not destination.exists()
    conflict = (
        tmp_path
        / ".ai-memory/migration/move-conflict/rollback-conflicts"
        / operation["id"]
        / "Notes/Moved.md"
    )
    assert conflict.read_text(encoding="utf-8") == newer


def test_manifest_write_retries_a_transient_file_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_memory_mcp import migration

    target = tmp_path / "manifest.json"
    original_replace = migration.os.replace
    attempts = 0

    def replace_once_locked(source_path: Path, target_path: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("synthetic sync-client lock")
        original_replace(source_path, target_path)

    monkeypatch.setattr(migration.os, "replace", replace_once_locked)
    monkeypatch.setattr(migration.time, "sleep", lambda _: None)

    _write_json(target, {"status": "active"})

    assert attempts == 2
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "status": "active"
    }
