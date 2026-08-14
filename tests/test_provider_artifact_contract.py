from __future__ import annotations

from pathlib import Path

from ai_memory_mcp.artifacts.ingest import read_artifact_batch


def test_teams_cli_batch_fixture_parses_through_the_python_contract(
    project_root: Path,
) -> None:
    fixture = project_root / "tests/fixtures/artifacts/teams-cli-batch.jsonl"

    with fixture.open("rb") as stream:
        batch = read_artifact_batch(stream)

    assert batch.manifest.source == "teams"
    assert batch.manifest.event_count == len(batch.events) == 3
    assert [event.entity for event in batch.events] == [
        "conversation",
        "message",
        "attachment",
    ]
    assert batch.manifest.coverage[0].entity == "attachment"
