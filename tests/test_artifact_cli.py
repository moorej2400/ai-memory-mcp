from __future__ import annotations

import json
import sqlite3
from hashlib import sha256
from pathlib import Path

import pytest

from ai_memory_mcp.artifacts.cli import main


def _configure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AI_MEMORY_WORK_DIR", str(tmp_path / "vault"))
    monkeypatch.setenv("AI_MEMORY_MCP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AI_MEMORY_ARTIFACT_DB", str(tmp_path / "artifacts.sqlite3"))
    monkeypatch.setenv(
        "AI_MEMORY_ARTIFACT_OBJECTS_DIR",
        str(tmp_path / "objects"),
    )
    monkeypatch.setenv(
        "AI_MEMORY_ARTIFACT_BACKUP_DIR",
        str(tmp_path / "backups"),
    )


def _batch_file(tmp_path: Path) -> Path:
    records = [
        {
            "schema": "ai-memory/artifact-batch@1",
            "record": "batch",
            "batch_id": "cli-batch-1",
            "source": "chat-source",
            "source_instance": "workspace",
            "observed_at": "2026-01-02T12:00:00Z",
            "event_count": 2,
        },
        {
            "schema": "ai-memory/artifact-event@1",
            "record": "event",
            "entity": "conversation",
            "operation": "upsert",
            "external_id": "conversation-1",
            "source_updated_at": "2026-01-02T10:00:00Z",
            "payload": {
                "title": "Example conversation",
                "occurred_at": "2026-01-02T10:00:00Z",
                "text": "Rotation discussion",
                "content_format": "plain",
            },
        },
        {
            "schema": "ai-memory/artifact-event@1",
            "record": "event",
            "entity": "message",
            "operation": "upsert",
            "external_id": "message-1",
            "parent": {
                "entity": "conversation",
                "external_id": "conversation-1",
            },
            "source_updated_at": "2026-01-02T10:05:00Z",
            "payload": {
                "occurred_at": "2026-01-02T10:05:00Z",
                "text": "Use the documented rotation procedure.",
                "content_format": "plain",
            },
        },
    ]
    path = tmp_path / "batch.jsonl"
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    return path


def _one_json(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    captured = capsys.readouterr()
    assert captured.err == ""
    lines = captured.out.splitlines()
    assert len(lines) == 1
    value = json.loads(lines[0])
    assert isinstance(value, dict)
    return value


def test_ingest_status_search_and_read_write_json_objects(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure(monkeypatch, tmp_path)
    batch_path = _batch_file(tmp_path)

    assert main(["ingest", "--input", str(batch_path)]) == 0
    receipt = _one_json(capsys)
    assert receipt["accepted"] == 2

    assert main(["status"]) == 0
    status = _one_json(capsys)
    assert status["available"] is True
    assert status["schema_version"] == 3
    assert status["artifacts"] == 2
    assert status["active_artifacts"] == 2
    assert status["batches"] == 1

    assert main(
        [
            "search",
            "--query",
            "documented procedure",
            "--source",
            "chat-source",
            "--entity",
            "message",
        ]
    ) == 0
    search = _one_json(capsys)
    assert len(search["results"]) == 1
    reference = search["results"][0]["artifact_uri"]

    assert main(["read", "--reference", reference, "--limit", "20"]) == 0
    read = _one_json(capsys)
    assert read["focus"] == reference
    assert read["records"][0]["text"].startswith("Use the documented")


def test_status_reports_a_missing_database_without_creating_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure(monkeypatch, tmp_path)
    database = tmp_path / "artifacts.sqlite3"

    assert main(["status"]) == 0

    status = _one_json(capsys)
    assert status["available"] is False
    assert status["integrity"] == "missing"
    assert status["schema_version"] == 0
    assert database.exists() is False


def test_init_creates_current_artifact_database_and_is_repeatable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure(monkeypatch, tmp_path)

    assert main(["init"]) == 0
    first = _one_json(capsys)
    assert first["to_version"] == 3
    assert first["applied"] == [1, 2, 3]
    data_root = tmp_path / "vault" / ".ai-memory"
    for relative in ("migration", "provider-state"):
        assert (data_root / relative).is_dir()
    assert (tmp_path / "backups").is_dir()
    assert (tmp_path / "state").is_dir()
    assert (tmp_path / "objects").is_dir()

    assert main(["init"]) == 0
    second = _one_json(capsys)
    assert second["to_version"] == 3
    assert second["applied"] == []


def test_status_reports_an_old_schema_without_changing_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure(monkeypatch, tmp_path)
    database = tmp_path / "artifacts.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE artifact_schema_migrations("
            "version INTEGER PRIMARY KEY, applied_at TEXT, "
            "application_version TEXT)"
        )
        connection.execute(
            "INSERT INTO artifact_schema_migrations VALUES (1, ?, ?)",
            ("2026-01-01T00:00:00Z", "0.1.0"),
        )
    before = database.read_bytes()
    before_digest = sha256(before).hexdigest()

    assert main(["status"]) == 0

    status = _one_json(capsys)
    assert status["available"] is False
    assert status["schema_version"] == 1
    assert "not current" in str(status["error"])
    after = database.read_bytes()
    assert after == before
    assert sha256(after).hexdigest() == before_digest
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        assert connection.execute(
            "SELECT MAX(version) FROM artifact_schema_migrations"
        ).fetchone()[0] == 1


def test_validation_error_uses_stderr_and_exit_code_two(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure(monkeypatch, tmp_path)
    invalid = tmp_path / "invalid.jsonl"
    invalid.write_text("not JSON\n", encoding="utf-8")
    assert main(["ingest", "--input", str(invalid)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "invalid JSON" in captured.err


def test_intake_failure_uses_stderr_and_exit_code_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure(monkeypatch, tmp_path)
    batch_path = _batch_file(tmp_path)
    assert main(["ingest", "--input", str(batch_path)]) == 0
    capsys.readouterr()

    text = batch_path.read_text(encoding="utf-8")
    batch_path.write_text(
        text.replace("documented", "approved"),
        encoding="utf-8",
    )
    assert main(["ingest", "--input", str(batch_path)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "batch ID" in captured.err


def test_pending_and_no_durable_memory_commands(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure(monkeypatch, tmp_path)
    batch_path = _batch_file(tmp_path)
    assert main(["ingest", "--input", str(batch_path)]) == 0
    capsys.readouterr()

    assert main(["pending", "--entity", "conversation", "--limit", "10"]) == 0
    pending = _one_json(capsys)
    candidate = pending["candidates"][0]
    assert candidate["entity"] == "conversation"

    assert main(
        [
            "mark-no-durable-memory",
            "--reference",
            candidate["artifact_uri"],
            "--event-id",
            candidate["latest_event_id"],
            "--source-digest",
            candidate["source_digest"],
            "--reason",
            "Only scheduling messages were present.",
        ]
    ) == 0
    result = _one_json(capsys)
    assert result["status"] == "no-durable-memory"

    assert main(["pending", "--limit", "10"]) == 0
    assert _one_json(capsys)["candidates"] == []


def test_backup_check_and_restore_commands(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure(monkeypatch, tmp_path)
    assert main(["ingest", "--input", str(_batch_file(tmp_path))]) == 0
    capsys.readouterr()

    assert main(["check"]) == 0
    checked = _one_json(capsys)
    assert checked["ok"] is True
    assert checked["artifacts"] == 2

    assert main(["backup"]) == 0
    backup = _one_json(capsys)
    backup_path = backup["path"]
    destination = tmp_path / "restored" / "artifacts.sqlite3"
    assert main(
        [
            "restore",
            "--backup",
            backup_path,
            "--destination",
            str(destination),
        ]
    ) == 0
    restored = _one_json(capsys)
    assert restored["destination"] == str(destination)
    assert destination.is_file()
