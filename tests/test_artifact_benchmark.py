from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from ai_memory_mcp.artifacts.ingest import ingest_artifact_batch, read_artifact_batch
from ai_memory_mcp.artifacts.models import ArtifactScope
from ai_memory_mcp.artifacts.search import ArtifactSearch
from ai_memory_mcp.artifacts.store import ArtifactStore
from ai_memory_mcp.config import Settings


def _benchmark_module(project_root: Path):
    path = project_root / "benchmarks" / "artifacts" / "generate_fixture.py"
    spec = importlib.util.spec_from_file_location("artifact_fixture_generator", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_small_fixture_has_deterministic_counts_digest_and_top_results(
    artifact_settings: Settings,
    project_root: Path,
    tmp_path: Path,
) -> None:
    module = _benchmark_module(project_root)
    spec = module.ArtifactFixtureSpec(
        conversations=2,
        messages_per_conversation=5,
        meetings=2,
        cues_per_meeting=4,
    )
    first = module.write_fixture(tmp_path / "first.jsonl", spec)
    second = module.write_fixture(tmp_path / "second.jsonl", spec)
    assert first.sha256 == second.sha256
    assert first.event_count == 24
    assert first.counts == {
        "conversation": 2,
        "message": 10,
        "meeting": 2,
        "transcript": 2,
        "transcript-cue": 8,
    }

    with Path(first.path).open(encoding="utf-8") as stream:
        receipt = ingest_artifact_batch(
            artifact_settings,
            read_artifact_batch(stream),
        )
    assert receipt.accepted == first.event_count
    store = ArtifactStore(artifact_settings)
    assert {
        entity: store.count(entity) for entity in first.counts
    } == first.counts

    search = ArtifactSearch(artifact_settings)
    for entity, marker in [
        ("message", module.MESSAGE_MARKER),
        ("transcript-cue", module.CUE_MARKER),
    ]:
        hits = search.search(
            marker,
            scope=ArtifactScope(
                source=module.SOURCE,
                source_instance=module.SOURCE_INSTANCE,
                entities=(entity,),
            ),
            limit=1,
        )
        assert hits[0].external_id == first.targets[entity]
