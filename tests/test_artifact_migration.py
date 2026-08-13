from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

from ai_memory_mcp.artifacts.cli import main
from ai_memory_mcp.artifacts.migrate_legacy import (
    plan_legacy_migration,
    run_legacy_migration,
)
from ai_memory_mcp.artifacts.schema import connect_artifact_db
from ai_memory_mcp.config import Settings


@dataclass(frozen=True)
class LegacyFixture:
    database: Path
    chat_notes: Path
    meeting_notes: Path

    @property
    def arguments(self) -> dict[str, object]:
        return {
            "source": "chat-source",
            "source_instance": "workspace",
            "sync_db": self.database,
            "chat_notes": self.chat_notes,
            "meeting_notes": self.meeting_notes,
        }


@pytest.fixture
def legacy_fixture(project_root: Path, tmp_path: Path) -> LegacyFixture:
    fixture_root = project_root / "tests" / "fixtures" / "artifacts"
    database = tmp_path / "legacy-sync.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            (fixture_root / "legacy-sync.sql").read_text(encoding="utf-8")
        )
    return LegacyFixture(
        database=database,
        chat_notes=fixture_root / "legacy-chat.md",
        meeting_notes=fixture_root / "legacy-meeting.md",
    )


def test_dry_run_reports_counts_without_writing(
    artifact_settings: Settings,
    legacy_fixture: LegacyFixture,
) -> None:
    before_db = hashlib.sha256(legacy_fixture.database.read_bytes()).hexdigest()
    before_chat = legacy_fixture.chat_notes.read_bytes()
    before_meeting = legacy_fixture.meeting_notes.read_bytes()
    plan = plan_legacy_migration(
        source="chat-source",
        source_instance="workspace",
        sync_db=legacy_fixture.database,
        chat_notes=legacy_fixture.chat_notes,
        meeting_notes=legacy_fixture.meeting_notes,
        immutable=True,
    )
    assert plan.conversations == 1
    assert plan.messages == 3
    assert plan.attachments == 1
    assert plan.meetings == 1
    assert plan.chat_notes == 1
    assert plan.meeting_notes == 1
    assert plan.transcript_cues == 3
    assert plan.unresolved_identities == 0
    assert plan.duplicate_natural_keys == 0
    assert not artifact_settings.artifact_db.exists()
    assert hashlib.sha256(legacy_fixture.database.read_bytes()).hexdigest() == before_db
    assert legacy_fixture.chat_notes.read_bytes() == before_chat
    assert legacy_fixture.meeting_notes.read_bytes() == before_meeting


def test_legacy_import_preserves_raw_and_summary_roles(
    artifact_settings: Settings,
    legacy_fixture: LegacyFixture,
) -> None:
    attachment = legacy_fixture.database.parent / "validation.txt"
    attachment.write_text("synthetic validation evidence\n", encoding="utf-8")
    with sqlite3.connect(legacy_fixture.database) as connection:
        connection.execute(
            "UPDATE attachments SET local_path = ?",
            (attachment.name,),
        )
    source_fingerprints = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (
            legacy_fixture.database,
            legacy_fixture.chat_notes,
            legacy_fixture.meeting_notes,
        )
    }
    receipt = run_legacy_migration(
        artifact_settings,
        **legacy_fixture.arguments,
    )
    assert receipt.messages == 3
    assert receipt.meetings == 1
    assert receipt.transcript_cues == 3
    assert receipt.source_files_changed == 0
    assert receipt.accepted_events == 10
    assert receipt.verified is True

    with connect_artifact_db(
        artifact_settings.artifact_db,
        read_only=True,
    ) as connection:
        entities = dict(
            connection.execute(
                "SELECT entity, count(*) FROM artifacts GROUP BY entity"
            )
        )
        conversation = connection.execute(
            "SELECT payload_json FROM artifacts WHERE entity = 'conversation'"
        ).fetchone()[0]
        migration_records = connection.execute(
            "SELECT count(*) FROM artifact_metadata "
            "WHERE key LIKE 'legacy_migration:%'"
        ).fetchone()[0]
        object_count = connection.execute(
            "SELECT count(*) FROM artifact_objects"
        ).fetchone()[0]
    assert entities == {
        "attachment": 1,
        "conversation": 1,
        "meeting": 1,
        "message": 3,
        "transcript": 1,
        "transcript-cue": 3,
    }
    assert "legacy_summary_candidate" in conversation
    assert "## Transcript" not in conversation
    assert migration_records == 1
    assert object_count == 1
    for path, digest in source_fingerprints.items():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


def test_repeated_legacy_import_is_a_no_op(
    artifact_settings: Settings,
    legacy_fixture: LegacyFixture,
) -> None:
    first = run_legacy_migration(artifact_settings, **legacy_fixture.arguments)
    second = run_legacy_migration(artifact_settings, **legacy_fixture.arguments)
    assert first.accepted_events > 0
    assert second.accepted_events == 0
    assert second.unchanged_events == 10


def test_missing_tables_are_rejected(
    artifact_settings: Settings,
    tmp_path: Path,
) -> None:
    database = tmp_path / "invalid.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE unrelated(value TEXT)")
    with pytest.raises(ValueError, match="table"):
        plan_legacy_migration(
            source="chat-source",
            source_instance="workspace",
            sync_db=database,
        )


def test_migration_cli_dry_run_writes_one_json_object(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    legacy_fixture: LegacyFixture,
) -> None:
    monkeypatch.setenv("AI_MEMORY_WORK_DIR", str(tmp_path / "vault"))
    monkeypatch.setenv("AI_MEMORY_MCP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv(
        "AI_MEMORY_ARTIFACT_DB",
        str(tmp_path / "artifacts.sqlite3"),
    )
    assert main(
        [
            "migrate-legacy",
            "--source",
            "chat-source",
            "--source-instance",
            "workspace",
            "--sync-db",
            str(legacy_fixture.database),
            "--chat-notes",
            str(legacy_fixture.chat_notes),
            "--meeting-notes",
            str(legacy_fixture.meeting_notes),
            "--immutable",
            "--dry-run",
        ]
    ) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    lines = captured.out.splitlines()
    assert len(lines) == 1
    result = json.loads(lines[0])
    assert result["messages"] == 3
    assert result["transcript_cues"] == 3
    assert not (tmp_path / "artifacts.sqlite3").exists()
