from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

import ai_memory_mcp.artifacts.migrate_legacy as migration_module
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
            connection.execute("SELECT entity, count(*) FROM artifacts GROUP BY entity")
        )
        conversation = connection.execute(
            "SELECT payload_json FROM artifacts WHERE entity = 'conversation'"
        ).fetchone()[0]
        migration_records = connection.execute(
            "SELECT count(*) FROM artifact_metadata WHERE key LIKE 'legacy_migration:%'"
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


def test_supplied_missing_note_path_is_rejected(
    legacy_fixture: LegacyFixture,
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing-chat-note.md"

    with pytest.raises(FileNotFoundError, match="note path"):
        plan_legacy_migration(
            source="chat-source",
            source_instance="workspace",
            sync_db=legacy_fixture.database,
            chat_notes=missing,
            meeting_notes=legacy_fixture.meeting_notes,
        )


def test_composite_legacy_message_and_attachment_identities_do_not_collide(
    artifact_settings: Settings,
    legacy_fixture: LegacyFixture,
) -> None:
    with sqlite3.connect(legacy_fixture.database) as connection:
        connection.execute(
            """
            INSERT INTO conversations VALUES (
              'conversation-2', 'chat', 'Second Conversation', NULL, NULL,
              'chat', 'standard', 0, 0, 0, 0, 1, 0,
              '2026-01-02T11:00:00Z', '2026-01-02T11:00:00Z', 1,
              '{}', '2026-01-02T11:00:00Z', '2026-01-02T11:00:00Z',
              '2026-01-02T11:00:00Z'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO messages VALUES (
              'conversation-2', 'message-3', NULL, 'Actor C',
              '2026-01-02T11:00:00Z', 'Second conversation message.',
              'message', 'text', '1', '{"authorId":"actor-c"}',
              '2026-01-02T11:00:00Z', '2026-01-02T11:00:00Z'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO attachments VALUES (
              'conversation-2', 'message-3', 0, 'attachment-1', 'file',
              'second.txt', 'https://example.invalid/second.txt', NULL,
              'text/plain', '{}', 'remote',
              '2026-01-02T11:00:00Z', '2026-01-02T11:00:00Z'
            )
            """
        )

    receipt = run_legacy_migration(
        artifact_settings,
        **legacy_fixture.arguments,
    )

    assert receipt.messages == 4
    assert receipt.attachments == 2
    with connect_artifact_db(
        artifact_settings.artifact_db,
        read_only=True,
    ) as connection:
        message_rows = connection.execute(
            "SELECT artifact_id, parent_artifact_id FROM artifacts "
            "WHERE entity = 'message' ORDER BY artifact_id"
        ).fetchall()
        attachment_rows = connection.execute(
            "SELECT artifact_id, parent_artifact_id FROM artifacts "
            "WHERE entity = 'attachment' ORDER BY artifact_id"
        ).fetchall()
    assert len({row["artifact_id"] for row in message_rows}) == 4
    assert len({row["artifact_id"] for row in attachment_rows}) == 2
    assert len({row["parent_artifact_id"] for row in attachment_rows}) == 2


def test_duplicate_output_identity_fails_before_the_intake_database_is_created(
    artifact_settings: Settings,
    legacy_fixture: LegacyFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build_events = migration_module._events

    def duplicate_first_event(*args: object, **kwargs: object):
        events = build_events(*args, **kwargs)
        return [*events, events[0]]

    monkeypatch.setattr(migration_module, "_events", duplicate_first_event)

    with pytest.raises(ValueError, match="duplicate artifact identities"):
        run_legacy_migration(
            artifact_settings,
            **legacy_fixture.arguments,
        )

    assert not artifact_settings.artifact_db.exists()


def test_wal_rows_are_included_in_the_stable_logical_snapshot(
    artifact_settings: Settings,
    legacy_fixture: LegacyFixture,
) -> None:
    writer = sqlite3.connect(legacy_fixture.database)
    try:
        assert writer.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute(
            "UPDATE messages SET content_markdown = ? WHERE message_id = ?",
            ("WAL-only correction.", "message-1"),
        )
        writer.commit()
        wal_path = Path(f"{legacy_fixture.database}-wal")
        assert wal_path.is_file()
        source_hashes = {
            path: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (legacy_fixture.database, wal_path)
        }

        plan = plan_legacy_migration(
            **legacy_fixture.arguments,
            immutable=True,
        )
        receipt = run_legacy_migration(
            artifact_settings,
            **legacy_fixture.arguments,
            immutable=True,
        )

        assert plan.database_sha256 == receipt.database_sha256
        assert plan.database_sha256 != source_hashes[legacy_fixture.database]
        with connect_artifact_db(
            artifact_settings.artifact_db,
            read_only=True,
        ) as connection:
            payloads = [
                json.loads(row[0])
                for row in connection.execute(
                    "SELECT payload_json FROM artifacts WHERE entity = 'message'"
                )
            ]
        assert any(payload["text"] == "WAL-only correction." for payload in payloads)
        for path, digest in source_hashes.items():
            assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    finally:
        writer.close()


def test_migration_sanitizes_normalized_text_and_uses_source_allowlists(
    artifact_settings: Settings,
    legacy_fixture: LegacyFixture,
    tmp_path: Path,
) -> None:
    chat_note = tmp_path / "legacy-chat.md"
    meeting_note = tmp_path / "legacy-meeting.md"
    chat_note.write_text(
        legacy_fixture.chat_notes.read_text(encoding="utf-8")
        + "\nDownload https://files.example.invalid/item?sig=secret-value.\n",
        encoding="utf-8",
    )
    meeting_note.write_text(
        legacy_fixture.meeting_notes.read_text(encoding="utf-8").replace(
            "We need one setting for new deployments.",
            ("Read https://user:secret-value@127.0.0.1/item?token=secret-value."),
        ),
        encoding="utf-8",
    )
    with sqlite3.connect(legacy_fixture.database) as connection:
        connection.execute(
            "UPDATE conversations SET raw_json = ?",
            ('{"accessToken":"secret-value","unknown":"private-value"}',),
        )
        connection.execute(
            "UPDATE messages SET content_markdown = ?, raw_json = ? "
            "WHERE message_id = 'message-1'",
            (
                "See https://files.example.invalid/item?X-Amz-Signature=secret-value.",
                (
                    '{"authorId":"actor-a","accessToken":"secret-value",'
                    '"reactions":["like"],"unknown":"private-value"}'
                ),
            ),
        )
        connection.execute(
            "UPDATE attachments SET remote_url = ?, raw_json = ?",
            (
                (
                    "https://redirect.example.invalid/open?next="
                    "https%3A%2F%2Ffiles.example.invalid%2Fitem%3Fsig%3Dsecret-value"
                ),
                '{"temporaryDownloadUrl":"https://files.example.invalid/private"}',
            ),
        )
        connection.execute(
            "UPDATE meetings SET join_url = ?",
            ("https://user:secret-value@127.0.0.1/meeting?sig=secret-value",),
        )

    run_legacy_migration(
        artifact_settings,
        source="chat-source",
        source_instance="workspace",
        sync_db=legacy_fixture.database,
        chat_notes=chat_note,
        meeting_notes=meeting_note,
    )

    allowed = {
        "conversation": {"kind", "thread_type", "chat_sub_type"},
        "message": {"kind", "message_type", "parent_id"},
        "attachment": {"kind", "remote_url", "status"},
        "meeting": {"end_time", "join_url", "meeting_type"},
    }
    with connect_artifact_db(
        artifact_settings.artifact_db,
        read_only=True,
    ) as connection:
        payload_rows = connection.execute(
            "SELECT entity, payload_json FROM artifacts ORDER BY entity"
        ).fetchall()
    serialized = "\n".join(str(row["payload_json"]) for row in payload_rows)
    assert "secret-value" not in serialized
    assert "private-value" not in serialized
    assert "accessToken" not in serialized
    assert "temporaryDownloadUrl" not in serialized
    for row in payload_rows:
        if row["entity"] not in allowed:
            continue
        payload = json.loads(row["payload_json"])
        assert set(payload["source_payload"]["legacy"]) <= allowed[row["entity"]]


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
    assert (
        main(
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
        )
        == 0
    )
    captured = capsys.readouterr()
    assert captured.err == ""
    lines = captured.out.splitlines()
    assert len(lines) == 1
    result = json.loads(lines[0])
    assert result["messages"] == 3
    assert result["transcript_cues"] == 3
    assert not (tmp_path / "artifacts.sqlite3").exists()
