from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from ai_memory_mcp.artifacts.models import (
    ArtifactBatchManifest,
    ArtifactEvent,
    ArtifactPayload,
    ParsedArtifactBatch,
)
from ai_memory_mcp.artifacts.schema import connect_artifact_db, migrate_artifact_db
from ai_memory_mcp.artifacts.store import ArtifactStore
from ai_memory_mcp.config import Settings
from ai_memory_mcp.generation import (
    _cleanup_failed_generation,
    _publish_json,
    _retire_old_generations,
    lease_current_generation,
    load_current_generation,
)
from ai_memory_mcp.index import MemoryIndex
from ai_memory_mcp.service import MemoryService


def _settings(tmp_path: Path) -> Settings:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "Record.md").write_text(
        "\n".join(
            (
                "---",
                "memory_id: mem-generation-record",
                "title: Generation record",
                "status: active",
                "---",
                "",
                "# Generation record",
                "",
                "The coordinated generation keeps every layer aligned.",
            )
        ),
        encoding="utf-8",
    )
    settings = Settings(
        memory_root=vault,
        state_dir=tmp_path / "state",
        graph_path=tmp_path / "legacy-graph.json",
        graphify_mcp_url="",
        embedding_provider="hashed",
        artifact_db=tmp_path / "raw" / "artifacts.sqlite3",
        artifact_objects_dir=tmp_path / "raw" / "objects",
        artifact_backup_dir=tmp_path / "backups",
        log_dir=tmp_path / "logs",
    )
    migrate_artifact_db(settings)
    return settings


def _raw_batch(batch_id: str, text: str) -> ParsedArtifactBatch:
    return ParsedArtifactBatch(
        manifest=ArtifactBatchManifest.model_validate(
            {
                "schema": "ai-memory/artifact-batch@1",
                "record": "batch",
                "batch_id": batch_id,
                "source": "chat-source",
                "source_instance": "workspace",
                "observed_at": "2026-01-02T10:00:00Z",
                "event_count": 1,
            }
        ),
        events=[
            ArtifactEvent.model_validate(
                {
                    "schema": "ai-memory/artifact-event@1",
                    "record": "event",
                    "entity": "message",
                    "operation": "upsert",
                    "external_id": batch_id,
                    "source_updated_at": "2026-01-02T10:00:00Z",
                    "payload": ArtifactPayload(text=text).model_dump(mode="json"),
                }
            )
        ],
        input_sha256=(batch_id * 64)[:64],
    )


def test_response_coverage_stays_with_the_searched_generation(tmp_path):
    from ai_memory_mcp.server import _modern_response
    settings = _settings(tmp_path)
    service = MemoryService(settings)
    assert service.sync().ok
    ArtifactStore(settings).apply_batch(_raw_batch("new-event", "New raw evidence is available."))
    response = service.recall("new raw evidence")
    assert response._execution_state.coverage.artifact_semantic_lag == 1
    scoped = service.recall("generation record", root_scope="work")
    assert scoped._execution_state.execution == "complete"
    assert service.sync().ok
    assert _modern_response(response, settings).coverage.artifact_semantic_lag == 1


def test_corrupt_immutable_segment_cannot_pass_retention_validation(tmp_path):
    from ai_memory_mcp.artifacts.vector_index import artifact_segment_paths
    from ai_memory_mcp.generation import _valid_generation_components
    settings = _settings(tmp_path)
    assert MemoryService(settings).sync().ok
    generation = load_current_generation(settings)
    segment = artifact_segment_paths(settings.state_dir / generation["artifact_snapshot"])[0]
    # Corrupt only synthetic derived data. The source database stays untouched.
    with sqlite3.connect(segment) as connection:
        connection.execute("DROP TABLE bursts")
    assert not _valid_generation_components(settings, generation)


def test_sync_publishes_one_consistent_generation(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    result = MemoryService(settings).sync()
    generation = load_current_generation(settings)

    assert result.ok is True
    assert generation is not None
    assert result.generation_id == generation["generation_id"]
    assert result.graph_snapshot == generation["graph_snapshot"]
    graph = json.loads(
        (settings.state_dir / generation["graph_snapshot"]).read_text(
            encoding="utf-8"
        )
    )
    assert graph["graph"]["index_snapshot"] == generation["markdown_snapshot"]
    with sqlite3.connect(
        settings.state_dir / generation["artifact_snapshot"]
    ) as connection:
        counter = connection.execute(
            "SELECT value FROM metadata "
            "WHERE key = 'artifact_change_counter'"
        ).fetchone()[0]
    assert int(counter) == generation["artifact_change_counter"]
    status = MemoryService(settings).status()
    assert status.ok is True
    assert status.generation.consistent is True
    assert status.index.generation_id == generation["generation_id"]
    assert status.artifact_vector.stale is False
    assert status.graphify.stale is False


def test_status_releases_the_artifact_snapshot_file(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    service = MemoryService(settings)
    assert service.sync().ok is True
    generation = load_current_generation(settings)
    assert generation is not None
    snapshot = settings.state_dir / str(generation["artifact_snapshot"])
    moved = snapshot.with_name(f"moved-{snapshot.name}")

    assert service.status().artifact_vector.available is True
    snapshot.replace(moved)
    moved.replace(snapshot)


def test_sync_rejects_an_outdated_artifact_schema_before_indexing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _settings(tmp_path)
    with connect_artifact_db(settings.artifact_db) as connection:
        connection.execute(
            "DELETE FROM artifact_schema_migrations WHERE version = 6"
        )
        connection.commit()

    index_called = False

    def fail_if_indexed(*args, **kwargs):
        nonlocal index_called
        index_called = True
        raise AssertionError("Markdown indexing must follow schema validation.")

    monkeypatch.setattr("ai_memory_mcp.index.build_index", fail_if_indexed)
    result = MemoryService(settings).sync()

    assert result.ok is False
    assert index_called is False
    assert "ai-memory-artifact init" in result.errors[0]
    health = json.loads(settings.generation_health_path.read_text(encoding="utf-8"))
    assert health["last_failure"]["layer"] == "artifact-schema"


def test_json_publication_retries_a_transient_windows_lock(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "current-generation.json"
    original_replace = __import__("os").replace
    attempts = 0

    def flaky_replace(source: Path, destination: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("synthetic OneDrive lock")
        original_replace(source, destination)

    monkeypatch.setattr("ai_memory_mcp.generation.os.replace", flaky_replace)
    monkeypatch.setattr(
        "ai_memory_mcp.generation.POINTER_REPLACE_RETRY_INTERVAL_SECONDS",
        0.0,
    )

    _publish_json(target, {"generation_id": "retry-safe"})

    assert attempts == 2
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "generation_id": "retry-safe"
    }


def test_failed_layer_keeps_the_previous_generation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _settings(tmp_path)
    service = MemoryService(settings)
    first = service.sync()
    assert first.ok is True
    pointer_before = settings.generation_pointer_path.read_bytes()

    def fail_graph(*args, **kwargs):
        raise RuntimeError("synthetic graph failure")

    monkeypatch.setattr(
        "ai_memory_mcp.provider_graph.build_provider_graph",
        fail_graph,
    )
    failed = service.sync()

    assert failed.ok is False
    assert settings.generation_pointer_path.read_bytes() == pointer_before
    health = json.loads(settings.generation_health_path.read_text(encoding="utf-8"))
    assert health["last_failure"]["layer"] == "graphify"
    assert health["layers"]["graphify"]["last_success"]["at"]
    assert health["layers"]["graphify"]["last_failure"]["at"]
    assert "storage_growth_bytes" in health["layers"]["markdown"]["last_success"]
    assert "synthetic graph failure" not in json.dumps(health)
    assert service.status().ok is False
    recall = service.recall("coordinated generation")
    assert any("latest coordinated refresh failed" in item for item in recall.warnings)


def test_postpublication_failure_restores_the_previous_generation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _settings(tmp_path)
    service = MemoryService(settings)
    first = service.sync()
    assert first.ok is True
    pointer_before = settings.generation_pointer_path.read_bytes()
    note = settings.memory_root / "Record.md"
    note.write_text(
        note.read_text(encoding="utf-8") + "\nA later generation.\n",
        encoding="utf-8",
    )

    def fail_retention(*args, **kwargs):
        raise RuntimeError("synthetic retention failure")

    monkeypatch.setattr(
        "ai_memory_mcp.generation._retire_old_generations",
        fail_retention,
    )
    failed = service.sync()
    current = load_current_generation(settings)

    assert failed.ok is False
    assert current is not None
    assert current["generation_id"] == first.generation_id
    assert settings.generation_pointer_path.read_bytes() == pointer_before
    for component in (
        "markdown_snapshot",
        "artifact_snapshot",
        "graph_snapshot",
    ):
        assert (settings.state_dir / current[component]).is_file()


def test_retention_keeps_current_and_one_last_good_generation(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    ArtifactStore(settings).apply_batch(
        _raw_batch("retained-message", "Canonical artifact retention marker.")
    )
    settings.artifact_objects_dir.mkdir(parents=True, exist_ok=True)
    retained_object = settings.artifact_objects_dir / "retained-object"
    retained_object.write_bytes(b"canonical object bytes")
    service = MemoryService(settings)
    published: list[str] = []
    for index in range(3):
        note = settings.memory_root / "Record.md"
        note.write_text(
            note.read_text(encoding="utf-8") + f"\nGeneration {index}.\n",
            encoding="utf-8",
        )
        result = service.sync()
        assert result.ok is True
        published.append(str(result.generation_id))

    manifests = [
        path
        for path in settings.state_dir.glob("generation-*.json")
        if json.loads(path.read_text(encoding="utf-8")).get("schema")
        == "ai-memory/generation@1"
    ]
    markdown_snapshots = sorted(settings.state_dir.glob("index-*.sqlite"))
    graph_snapshots = sorted(settings.state_dir.glob("graph-*.json"))

    assert len(manifests) == 2
    assert len(markdown_snapshots) == 2
    assert len(graph_snapshots) == 2
    assert not (settings.state_dir / f"generation-{published[0]}.json").exists()
    assert ArtifactStore(settings).count("message") == 1
    assert retained_object.read_bytes() == b"canonical object bytes"

    standalone = settings.state_dir / "index-standalone.sqlite"
    standalone.write_bytes(b"independent derived snapshot")
    _retire_old_generations(settings, legacy_keep=set())
    assert standalone.read_bytes() == b"independent derived snapshot"
    stale_standalone = settings.state_dir / "index-stale-standalone.sqlite"
    stale_standalone.write_bytes(b"stale independent snapshot")
    _retire_old_generations(
        settings,
        legacy_keep=set(),
        legacy_candidates={stale_standalone},
    )
    assert not stale_standalone.exists()


def test_failed_generation_cleanup_removes_only_owned_outputs(
    tmp_path: Path,
) -> None:
    owned = tmp_path / "index-owned.sqlite"
    concurrent = tmp_path / "index-concurrent.sqlite"
    owned.write_bytes(b"owned")
    concurrent.write_bytes(b"concurrent")

    _cleanup_failed_generation({owned})

    assert not owned.exists()
    assert concurrent.read_bytes() == b"concurrent"


def test_status_and_recall_report_newer_canonical_markdown(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    service = MemoryService(settings)
    assert service.sync().ok is True
    note = settings.memory_root / "Record.md"
    note.write_text(
        note.read_text(encoding="utf-8") + "\nA canonical update.\n",
        encoding="utf-8",
    )

    status = service.status()
    recall = service.recall("coordinated generation")

    assert status.ok is False
    assert status.index.stale is True
    assert any("Canonical Markdown is newer" in item for item in recall.warnings)


def test_warm_recall_does_not_walk_the_canonical_markdown_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    service = MemoryService(settings)
    assert service.sync().ok is True
    generation = load_current_generation(settings)
    assert generation is not None
    assert service._engine_for_generation(generation) is not None

    def fail_if_reconciled(*_args, **_kwargs):
        raise AssertionError("Warm recall must not scan canonical Markdown.")

    monkeypatch.setattr(MemoryIndex, "canonical_stale", fail_if_reconciled)

    response = service.recall("coordinated generation")

    assert response.evidence


def test_status_reports_corrupt_generation_components_without_raising(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    service = MemoryService(settings)
    assert service.sync().ok is True
    generation = load_current_generation(settings)
    assert generation is not None
    (settings.state_dir / generation["markdown_snapshot"]).write_bytes(b"invalid")
    (settings.state_dir / generation["graph_snapshot"]).write_text(
        "{invalid",
        encoding="utf-8",
    )

    status = service.status()

    assert status.ok is False
    assert status.index.available is False
    assert status.graphify.available is False


def test_status_rejects_graph_content_that_does_not_match_manifest(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    service = MemoryService(settings)
    assert service.sync().ok is True
    generation = load_current_generation(settings)
    assert generation is not None
    graph_path = settings.state_dir / generation["graph_snapshot"]
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    graph["nodes"] = []
    graph["links"] = []
    graph_path.write_text(json.dumps(graph), encoding="utf-8")

    status = service.status()
    recall = service.recall("coordinated generation")

    assert status.ok is False
    assert status.graphify.stale is True
    assert any("graph component is unavailable" in item for item in recall.warnings)


def test_sync_replaces_a_corrupt_generation_without_a_last_good(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    service = MemoryService(settings)
    first = service.sync()
    assert first.ok is True
    first_generation = load_current_generation(settings)
    assert first_generation is not None
    (settings.state_dir / first_generation["graph_snapshot"]).write_text(
        "{invalid",
        encoding="utf-8",
    )
    (settings.memory_root / "Record.md").write_text(
        (settings.memory_root / "Record.md").read_text(encoding="utf-8")
        + "\nThe repaired generation is current.\n",
        encoding="utf-8",
    )

    repaired = service.sync()
    current = load_current_generation(settings)
    status = service.status()

    assert repaired.ok is True
    assert current is not None
    assert current["generation_id"] != first_generation["generation_id"]
    assert status.graphify.available is True
    assert status.graphify.stale is False
    assert status.generation.verified_generations == 1
    assert status.generation.last_good_available is False


def test_empty_graph_is_a_valid_available_generation(tmp_path: Path) -> None:
    memory_root = tmp_path / "vault"
    memory_root.mkdir()
    settings = Settings(
        memory_root=memory_root,
        state_dir=tmp_path / "state",
        graph_path=tmp_path / "graph.json",
        graphify_mcp_url="",
        embedding_provider="hashed",
        artifact_db=tmp_path / "artifacts.sqlite3",
        artifact_objects_dir=tmp_path / "objects",
        artifact_backup_dir=tmp_path / "backups",
    )
    ArtifactStore(settings).apply_batch(
        _raw_batch("raw-only", "Raw-only vault marker.")
    )
    service = MemoryService(settings)

    assert service.sync().ok is True
    status = service.status()
    assert status.graphify.available is True
    assert status.graphify.nodes == 0
    assert status.ok is True


def test_recall_keeps_raw_search_available_during_semantic_index_lag(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    service = MemoryService(settings)
    assert service.sync().ok is True
    ArtifactStore(settings).apply_batch(
        _raw_batch("new-message", "A newly delivered private marker.")
    )

    stale = service.recall("newly delivered private marker")
    stale_status = service.status()

    assert any(item.evidence_class == "raw" for item in stale.evidence)
    assert any("semantic data is older" in warning for warning in stale.warnings)
    assert stale_status.ok is False
    assert stale_status.artifact_vector.stale is True
    assert service.sync().ok is True
    refreshed = service.recall("newly delivered private marker")
    assert any(item.evidence_class == "raw" for item in refreshed.evidence)


def test_recall_does_not_fall_back_when_generation_components_are_missing(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    service = MemoryService(settings)
    assert service.sync().ok is True
    generation = load_current_generation(settings)
    assert generation is not None
    graph_path = settings.state_dir / generation["graph_snapshot"]
    artifact_path = settings.state_dir / generation["artifact_snapshot"]
    graph_path.rename(tmp_path / "held-graph.json")
    artifact_path.rename(tmp_path / "held-artifact.sqlite")
    settings.graph_path.write_text(
        json.dumps(
            {
                "graph": {},
                "nodes": [{"id": "wrong", "label": "wrong"}],
                "links": [],
            }
        ),
        encoding="utf-8",
    )

    response = service.recall("coordinated generation")
    status = service.status()

    assert response.status == "answered"
    assert any("graph component is missing" in item for item in response.warnings)
    assert any(
        "semantic component is missing" in item for item in response.warnings
    )
    assert service.engine.graph.graph_path != settings.graph_path
    assert status.ok is False
    assert status.graphify.available is False
    assert status.artifact_vector.available is False


def test_retention_preserves_a_generation_with_an_active_recall_lease(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    service = MemoryService(settings)
    first = service.sync()
    assert first.ok is True
    first_manifest = settings.state_dir / f"generation-{first.generation_id}.json"

    with lease_current_generation(settings) as leased:
        assert leased is not None
        for index in range(2):
            note = settings.memory_root / "Record.md"
            note.write_text(
                note.read_text(encoding="utf-8")
                + f"\nLeased generation update {index}.\n",
                encoding="utf-8",
            )
            assert service.sync().ok is True
        assert first_manifest.is_file()

    note = settings.memory_root / "Record.md"
    note.write_text(
        note.read_text(encoding="utf-8") + "\nLease released.\n",
        encoding="utf-8",
    )
    assert service.sync().ok is True
    assert first_manifest.exists() is False


def test_retention_keeps_an_older_verified_generation_when_newer_is_invalid(
    tmp_path: Path,
) -> None:
    settings = replace(_settings(tmp_path), generation_retention_count=3)
    service = MemoryService(settings)
    generations: list[dict[str, object]] = []
    for index in range(3):
        note = settings.memory_root / "Record.md"
        note.write_text(
            note.read_text(encoding="utf-8") + f"\nValidation {index}.\n",
            encoding="utf-8",
        )
        assert service.sync().ok is True
        current = load_current_generation(settings)
        assert current is not None
        generations.append(current)

    middle_graph = (
        settings.state_dir / str(generations[1]["graph_snapshot"])
    )
    middle_graph.write_bytes(b"invalid derived snapshot")
    reduced = replace(settings, generation_retention_count=2)
    _retire_old_generations(reduced, legacy_keep=set())

    assert (
        settings.state_dir / str(generations[0]["graph_snapshot"])
    ).is_file()
    assert (
        settings.state_dir / str(generations[2]["graph_snapshot"])
    ).is_file()
    assert middle_graph.exists() is False


def test_retention_skips_a_locked_obsolete_file_without_rolling_back(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = replace(_settings(tmp_path), generation_retention_count=3)
    service = MemoryService(settings)
    generations: list[dict[str, object]] = []
    for index in range(3):
        note = settings.memory_root / "Record.md"
        note.write_text(
            note.read_text(encoding="utf-8") + f"\nLocked cleanup {index}.\n",
            encoding="utf-8",
        )
        assert service.sync().ok is True
        generation = load_current_generation(settings)
        assert generation is not None
        generations.append(generation)

    locked = settings.state_dir / str(generations[0]["graph_snapshot"])
    current_id = str(generations[-1]["generation_id"])
    original_replace = Path.replace

    def selectively_locked(path: Path, *args, **kwargs) -> None:
        if path == locked:
            raise PermissionError("synthetic OneDrive lock")
        return original_replace(path, *args, **kwargs)

    monkeypatch.setattr(Path, "replace", selectively_locked)
    monkeypatch.setattr(
        "ai_memory_mcp.generation.RETENTION_ARCHIVE_RETRY_SECONDS",
        0.0,
    )
    result = _retire_old_generations(
        replace(settings, generation_retention_count=2),
        legacy_keep=set(),
    )

    current = load_current_generation(settings)
    assert current is not None
    assert current["generation_id"] == current_id
    assert locked.is_file()
    assert result["removal_errors"] == 1

    monkeypatch.setattr(Path, "replace", original_replace)
    retry = _retire_old_generations(
        replace(settings, generation_retention_count=2),
        legacy_keep=set(),
    )

    assert retry["removal_errors"] == 0
    assert locked.exists() is False
    assert list((settings.state_dir / "retired-generations").glob(locked.name))


def test_retention_retries_a_transient_archive_lock(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = replace(_settings(tmp_path), generation_retention_count=3)
    service = MemoryService(settings)
    generations: list[dict[str, object]] = []
    for index in range(3):
        note = settings.memory_root / "Record.md"
        note.write_text(
            note.read_text(encoding="utf-8") + f"\nRetry cleanup {index}.\n",
            encoding="utf-8",
        )
        assert service.sync().ok is True
        generation = load_current_generation(settings)
        assert generation is not None
        generations.append(generation)

    locked = settings.state_dir / str(generations[0]["graph_snapshot"])
    original_replace = Path.replace
    attempts = 0

    def transient_lock(path: Path, *args, **kwargs):
        nonlocal attempts
        if path == locked and attempts < 2:
            attempts += 1
            raise PermissionError("synthetic transient lock")
        if path == locked:
            attempts += 1
        return original_replace(path, *args, **kwargs)

    monkeypatch.setattr(Path, "replace", transient_lock)
    monkeypatch.setattr(
        "ai_memory_mcp.generation.RETENTION_ARCHIVE_RETRY_INTERVAL_SECONDS",
        0.0,
    )
    result = _retire_old_generations(
        replace(settings, generation_retention_count=2),
        legacy_keep=set(),
    )

    assert attempts == 3
    assert result["removal_errors"] == 0
    assert locked.exists() is False
