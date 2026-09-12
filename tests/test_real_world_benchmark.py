from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from ai_memory_mcp.real_world_benchmark import (
    WorkloadProfile,
    compare_performance,
    run_benchmark,
    write_artifacts,
    write_vault,
    _validate_mcp_packet,
    _process_tree_resident_bytes,
)


def test_real_world_fixture_is_deterministic_and_neutral(tmp_path: Path) -> None:
    profile = WorkloadProfile(
        filler_notes=2,
        conversations=2,
        messages_per_conversation=10,
        meetings=2,
        cues_per_meeting=12,
    )
    first_vault = write_vault(tmp_path / "first-vault", profile)
    second_vault = write_vault(tmp_path / "second-vault", profile)
    first_artifacts = write_artifacts(tmp_path / "first.jsonl", profile)
    second_artifacts = write_artifacts(tmp_path / "second.jsonl", profile)

    assert first_vault == second_vault
    assert first_artifacts == second_artifacts
    assert first_vault["documents"] == 10
    assert first_artifacts["events"] == 50

    generated = "\n".join(
        path.read_text(encoding="utf-8")
        for path in [tmp_path / "first.jsonl", *(tmp_path / "first-vault").rglob("*.md")]
    ).casefold()
    for private_term in ("sample-person", "sample-company", "/users/", "c:\\users\\"):
        assert private_term not in generated
    assert re.search(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b", generated) is None


def test_real_world_smoke_profile_uses_memory_recall(tmp_path: Path) -> None:
    report = run_benchmark(
        tmp_path / "run",
        profile_name="smoke",
        embedding_provider="hashed",
        repeats=1,
    )
    metrics = report["metrics"]

    assert report["quality_failures"] == []
    assert metrics["semantic_cases_executed"] is False
    assert metrics["cases"] == 15
    assert metrics["recall_at_5"] == 1.0
    assert metrics["no_answer_accuracy"] == 1.0
    assert metrics["scope_leakage_rate"] == 0.0
    assert metrics["citation_failure_rate"] == 0.0
    assert metrics["corpus"]["markdown_documents"] == 32
    assert metrics["corpus"]["artifact_events"] == 138
    assert metrics["corpus"]["graph_nodes"] > 0
    assert metrics["storage_bytes"] > 0
    assert metrics["recall_p95_ms"] >= metrics["recall_p50_ms"] > 0
    assert metrics["mcp_recall_p95_ms"] >= metrics["mcp_recall_p50_ms"] > 0
    assert metrics["mcp_transport_queries"] == 17
    assert metrics["mcp_quality_validated_queries"] == 17
    assert metrics["process_tree_peak_resident_bytes"] > 0
    assert "artifact_vector" in metrics["per_layer_latency_ms"]
    assert "semantic" in metrics["per_layer_latency_ms"]


def test_performance_comparison_reports_only_configured_regressions() -> None:
    current = {
        "generation_seconds": 12.0,
        "recall_p50_ms": 15.0,
        "recall_p95_ms": 42.0,
    }
    baseline = {
        "metrics": {
            "generation_seconds": 10.0,
            "recall_p50_ms": 10.0,
            "recall_p95_ms": 40.0,
        }
    }

    comparison = compare_performance(current, baseline, 1.25)

    assert comparison["failures"] == ["recall_p50_ms"]
    assert comparison["ratios"]["generation_seconds"] == pytest.approx(1.2)


def test_real_world_contract_contains_no_private_identifiers(
    project_root: Path,
) -> None:
    contract_path = project_root / "benchmarks" / "real-world" / "contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    text = json.dumps(contract).casefold()

    assert contract["schema_version"] == 1
    assert len(contract["cases"]) >= 14
    for private_term in ("sample-person", "sample-company", "/users/", "c:\\users\\"):
        assert private_term not in text
    assert re.search(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b", text) is None


def test_serialized_quality_rejects_failed_execution_and_missing_citations():
    from ai_memory_mcp.models import RecallResponseV2, RecallEvidence

    case = {"id": "synthetic-case", "expected_any": ["expected"]}
    payload = RecallResponseV2(execution="failed", result_kind="empty", intent="search",
                               query="synthetic query").model_dump()
    with pytest.raises(RuntimeError, match="incomplete"):
        _validate_mcp_packet(payload, case)
    payload["execution"] = "complete"
    with pytest.raises(RuntimeError, match="missed"):
        _validate_mcp_packet(payload, case)
    payload["evidence"] = [RecallEvidence(memory_id="expected", source_id="core", heading="",
                                          text="Expected evidence", score=.5).model_dump()]
    payload["result_kind"] = "ranked"
    with pytest.raises(RuntimeError, match="cite"):
        _validate_mcp_packet(payload, case)


def test_memory_measurement_includes_all_worker_descendants(monkeypatch):
    import psutil
    from types import SimpleNamespace

    class Process:
        def __init__(self, size=100):
            self.size = size
        def children(self, recursive=False):
            assert recursive
            return [Process(200), Process(300)]
        def memory_info(self):
            return SimpleNamespace(rss=self.size)
    monkeypatch.setattr(psutil, "Process", Process)
    assert _process_tree_resident_bytes() == 600


def test_mcp_benchmark_overrides_ambient_resource_settings(artifact_settings, monkeypatch):
    from ai_memory_mcp.real_world_benchmark import _mcp_environment
    monkeypatch.setenv("AI_MEMORY_RECALL_WORKERS", "15")
    monkeypatch.setenv("AI_MEMORY_ANN_CANDIDATE_LIMIT", "99000")
    values = _mcp_environment(artifact_settings)
    assert values["AI_MEMORY_RECALL_WORKERS"] == str(artifact_settings.recall_worker_count)
    assert values["AI_MEMORY_ANN_CANDIDATE_LIMIT"] == str(artifact_settings.ann_candidate_limit)
