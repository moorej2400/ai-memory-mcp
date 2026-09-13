from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import statistics
import sys
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .ann import tie_aware_candidate_recall_at_k
from .artifacts.identity import artifact_id
from .artifacts.ingest import ingest_artifact_batch, read_artifact_batch
from .artifacts.models import ArtifactScope
from .artifacts.models import ArtifactBatchManifest, ArtifactEvent, ParsedArtifactBatch
from .artifacts.store import ArtifactStore
from .artifacts.vector_index import search_artifact_vectors
from .config import Settings
from .service import MemoryService
from .models import RecallResponse, RecallResponseV2

SOURCE = "synthetic-chat"
SOURCE_INSTANCE = "real-world"


@dataclass(frozen=True, slots=True)
class WorkloadProfile:
    filler_notes: int
    conversations: int
    messages_per_conversation: int
    meetings: int
    cues_per_meeting: int

    @property
    def artifact_events(self) -> int:
        return (
            self.conversations
            + self.conversations * self.messages_per_conversation
            + self.meetings * 2
            + self.meetings * self.cues_per_meeting
        )


def _benchmark_root() -> Path:
    return Path(__file__).resolve().parents[2] / "benchmarks" / "real-world"


def load_contract(root: Path | None = None) -> dict[str, Any]:
    contract_root = root or _benchmark_root()
    return json.loads((contract_root / "contract.json").read_text(encoding="utf-8"))


def profile_from_contract(contract: dict[str, Any], name: str) -> WorkloadProfile:
    try:
        value = contract["profiles"][name]
    except KeyError as exc:
        raise ValueError(f"Unknown benchmark profile: {name}") from exc
    profile = WorkloadProfile(**{key: int(item) for key, item in value.items()})
    if min(
        profile.filler_notes,
        profile.conversations,
        profile.messages_per_conversation,
        profile.meetings,
        profile.cues_per_meeting,
    ) <= 0:
        raise ValueError("Each real-world benchmark profile value must be positive.")
    return profile


def _note(
    memory_id: str,
    title: str,
    body: str,
    *,
    primary_kind: str = "decision",
    primary_id: str | None = None,
    status: str = "active",
    related_repos: tuple[str, ...] = (),
    related: tuple[str, ...] = (),
    supersedes: str | None = None,
    superseded_by: str | None = None,
) -> str:
    lines = [
        "---",
        "type: memory",
        f"memory_id: {memory_id}",
        f"title: {title}",
        "root_scope: synthetic",
        "primary_scope:",
        f"  kind: {primary_kind}",
        f"  id: {primary_id or memory_id}",
        f"status: {status}",
        "created: 2026-01-01",
        "updated: 2026-06-01",
    ]
    if related_repos:
        lines.append(f"related_repos: [{', '.join(related_repos)}]")
    if related:
        lines.append("related:")
        lines.extend(f'  - "{item}"' for item in related)
    if supersedes:
        lines.append(f"supersedes: [{supersedes}]")
    if superseded_by:
        lines.append(f"superseded_by: {superseded_by}")
    lines.extend(["---", "", f"# {title}", "", body.strip(), ""])
    return "\n".join(lines)


def _target_notes() -> dict[str, str]:
    return {
        "Incidents/Lantern Cache.md": _note(
            "rw-lantern-incident",
            "Lantern cache incident",
            """
Incident LANTERN-248 reports error `FL-208` when a stale cache lease blocks a worker.
Release the expired lease, rebuild the cache pointer, and retry the job one time.
The recovery runbook contains the validation sequence.
""",
            primary_kind="ticket",
            primary_id="LANTERN-248",
            related_repos=("lantern",),
            related=("[[Runbooks/Lantern Recovery|Lantern recovery runbook]]",),
        ),
        "Runbooks/Lantern Recovery.md": _note(
            "rw-lantern-runbook",
            "Lantern recovery runbook",
            """
The runbook releases an expired lease before it rebuilds the cache pointer.
It then verifies one worker and retries the failed job one time.
""",
            primary_kind="reference",
            related_repos=("lantern",),
            related=("[[Incidents/Lantern Cache|Lantern cache incident]]",),
        ),
        "Reference/Tomato Sauce.md": _note(
            "rw-kitchen-tomato",
            "Tomato sauce ingredient",
            """
Use ripe tomatoes as the vegetable base for a rich pasta sauce.
Slow cooking concentrates their natural sweetness and produces a thick red sauce.
""",
            primary_kind="reference",
            primary_id="reference:tomato-sauce",
        ),
        "Repos/Cedar/Deployment Window.md": _note(
            "rw-cedar-window",
            "Cedar deployment window",
            "Cedar deploys on Tuesday at 09:00 UTC after the readiness review.",
            primary_kind="repository",
            primary_id="cedar",
            related_repos=("cedar",),
        ),
        "Repos/Harbor/Deployment Window.md": _note(
            "rw-harbor-window",
            "Harbor deployment window",
            "Harbor deploys on Thursday at 16:00 UTC after the readiness review.",
            primary_kind="repository",
            primary_id="harbor",
            related_repos=("harbor",),
        ),
        "Policies/Current Retention.md": _note(
            "rw-current-retention",
            "Current archive retention",
            "The current archive retention period is 180 days.",
            primary_kind="decision",
            primary_id="policy:archive-retention",
            supersedes="rw-old-retention",
        ),
        "Policies/Old Retention.md": _note(
            "rw-old-retention",
            "Retired archive retention",
            "The retired archive retention period was 30 days.",
            primary_kind="decision",
            primary_id="policy:archive-retention-v1",
            status="superseded",
            superseded_by="rw-current-retention",
        ),
        "Decisions/Studio Cooling.md": _note(
            "rw-studio-cooling",
            "Studio cooling threshold",
            """
The studio cooling fans start when the room reaches 27 degrees Celsius.
This decision came from a meeting with greetings, travel talk, and schedule discussion.
""",
            primary_kind="decision",
            primary_id="decision:studio-cooling",
        ),
    }


def _filler_note(index: int) -> tuple[str, str]:
    area = ("amber", "birch", "coral", "drift")[index % 4]
    memory_id = f"rw-filler-{index:05d}"
    path = f"Reference/Filler/{index:05d}.md"
    body = (
        f"Synthetic reference {index:05d} describes the {area} inventory cycle. "
        f"The cycle checks shelf group {index % 37:02d} and neutral sample {index:05d}."
    )
    return path, _note(
        memory_id,
        f"Synthetic inventory reference {index:05d}",
        body,
        primary_kind="reference",
        primary_id=f"reference:filler-{index:05d}",
    )


def write_vault(root: Path, profile: WorkloadProfile) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=False)
    documents = _target_notes()
    documents.update(dict(_filler_note(index) for index in range(profile.filler_notes)))
    digest = hashlib.sha256()
    byte_count = 0
    for relative, content in sorted(documents.items()):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = content.encode("utf-8")
        path.write_bytes(encoded)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(encoded)
        byte_count += len(encoded)
    return {
        "documents": len(documents),
        "bytes": byte_count,
        "sha256": digest.hexdigest(),
    }


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _event(
    entity: str,
    external_id: str,
    occurred_at: str,
    text: str = "",
    *,
    parent: tuple[str, str] | None = None,
    sequence: int | None = None,
    reply_to: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "title": f"Synthetic {entity} {external_id}",
        "occurred_at": occurred_at,
        "content_format": "plain",
    }
    if text:
        payload["text"] = text
    if reply_to:
        payload["links"] = [
            {
                "relation": "reply-to",
                "target": {"entity": "message", "external_id": reply_to},
            }
        ]
    value: dict[str, Any] = {
        "schema": "ai-memory/artifact-event@1",
        "record": "event",
        "entity": entity,
        "operation": "upsert",
        "external_id": external_id,
        "source_updated_at": occurred_at,
        "payload": payload,
    }
    if parent:
        value["parent"] = {"entity": parent[0], "external_id": parent[1]}
    if sequence is not None:
        value["source_sequence"] = sequence
    return value


def _artifact_records(profile: WorkloadProfile) -> Iterable[dict[str, Any]]:
    yield {
        "schema": "ai-memory/artifact-batch@1",
        "record": "batch",
        "batch_id": (
            f"real-world-{profile.conversations}-{profile.messages_per_conversation}-"
            f"{profile.meetings}-{profile.cues_per_meeting}"
        ),
        "source": SOURCE,
        "source_instance": SOURCE_INSTANCE,
        "observed_at": "2026-07-01T00:00:00Z",
        "event_count": profile.artifact_events,
    }
    base = datetime(2026, 2, 1, tzinfo=timezone.utc)
    for conversation_index in range(profile.conversations):
        conversation_id = f"conversation-{conversation_index:04d}"
        timestamp = base + timedelta(days=conversation_index)
        yield _event("conversation", conversation_id, _iso(timestamp))
        for message_index in range(profile.messages_per_conversation):
            sequence = message_index + 1
            external_id = f"message-{conversation_index:04d}-{sequence:06d}"
            text = (
                "The group discusses lunch, travel, calendar changes, and a neutral "
                f"status update for sequence {sequence}."
            )
            if conversation_index == 1 and message_index == 7:
                external_id = "target-chat-message"
                text = (
                    "The approved opal checkpoint is CP-731. Keep this exact marker "
                    "with the release evidence."
                )
            if conversation_index == 2 and message_index == 2:
                text = "Where can I buy apples for the weekend?"
            if conversation_index == 2 and message_index == 3:
                external_id = "target-fruit-link"
                text = "Here is the link to buy the fruit you like: https://fruit.example"
            if conversation_index > 2 and message_index % 97 == 0:
                text = (
                    "Here is a newer purchase link for an unrelated item: "
                    f"https://shop.example/item/{conversation_index}-{message_index}"
                )
            yield _event(
                "message",
                external_id,
                _iso(timestamp + timedelta(seconds=sequence)),
                text,
                parent=("conversation", conversation_id),
                sequence=sequence,
                reply_to=(
                    f"message-{conversation_index:04d}-{sequence - 1:06d}"
                    if conversation_index == 2 and message_index == 3
                    else None
                ),
            )
    meeting_base = datetime(2026, 5, 1, tzinfo=timezone.utc)
    for meeting_index in range(profile.meetings):
        meeting_id = f"meeting-{meeting_index:04d}"
        transcript_id = f"transcript-{meeting_index:04d}"
        timestamp = meeting_base + timedelta(days=meeting_index)
        yield _event("meeting", meeting_id, _iso(timestamp))
        yield _event(
            "transcript",
            transcript_id,
            _iso(timestamp),
            parent=("meeting", meeting_id),
        )
        for cue_index in range(profile.cues_per_meeting):
            sequence = cue_index + 1
            external_id = f"cue-{meeting_index:04d}-{sequence:06d}"
            text = (
                "The attendees discuss greetings, weather, schedules, and a neutral "
                f"follow-up item for sequence {sequence}."
            )
            if meeting_index == 1 and cue_index == 9:
                external_id = "target-meeting-cue"
                text = (
                    "The accepted silver signal is SG-419. Store the signal with the "
                    "meeting evidence."
                )
            yield _event(
                "transcript-cue",
                external_id,
                _iso(timestamp + timedelta(seconds=sequence)),
                text,
                parent=("transcript", transcript_id),
                sequence=sequence,
            )


def write_artifacts(path: Path, profile: WorkloadProfile) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    byte_count = 0
    with path.open("xb") as stream:
        for record in _artifact_records(profile):
            encoded = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode(
                "utf-8"
            )
            stream.write(encoded)
            digest.update(encoded)
            byte_count += len(encoded)
    return {
        "events": profile.artifact_events,
        "bytes": byte_count,
        "sha256": digest.hexdigest(),
    }


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1)
    return ordered[max(index, 0)]


def _directory_bytes(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def _peak_resident_bytes() -> int | None:
    try:
        import resource

        value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    except (ImportError, OSError, ValueError):
        return None
    return value if sys.platform == "darwin" else value * 1024


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _expected_ids(case: dict[str, Any]) -> set[str]:
    expected = set(case.get("expected_any", []))
    for value in case.get("expected_artifacts", []):
        entity, external_id = str(value).split(":", 1)
        expected.add(artifact_id(SOURCE, SOURCE_INSTANCE, entity, external_id))
    return expected


def compare_performance(
    metrics: dict[str, Any],
    baseline: dict[str, Any],
    maximum_ratio: float,
) -> dict[str, Any]:
    if maximum_ratio <= 1:
        raise ValueError("The maximum regression ratio must be greater than 1.")
    baseline_metrics = baseline.get("metrics", baseline)
    names = ("generation_seconds", "recall_p50_ms", "recall_p95_ms")
    ratios: dict[str, float] = {}
    failures: list[str] = []
    for name in names:
        old = float(baseline_metrics[name])
        new = float(metrics[name])
        ratio = new / old if old > 0 else 1.0
        ratios[name] = ratio
        if ratio > maximum_ratio:
            failures.append(name)
    return {"maximum_ratio": maximum_ratio, "ratios": ratios, "failures": failures}


def _mcp_environment(settings: Settings) -> dict[str, str]:
    environment = dict(os.environ)
    environment.update(
        {
            "AI_MEMORY_WORK_DIR": str(settings.memory_root),
            "AI_MEMORY_PRIMARY_SOURCE_ID": settings.primary_source_id,
            "AI_MEMORY_RETRIEVAL_SOURCES": "{}",
            "AI_MEMORY_PERSONAL_DIR": "",
            "AI_MEMORY_MCP_STATE_DIR": str(settings.state_dir),
            "AI_MEMORY_GRAPH_PATH": str(settings.graph_path),
            "GRAPHIFY_GLOBAL_MCP_URL": "",
            "AI_MEMORY_LOG_DIR": str(settings.resolved_log_dir),
            "AI_MEMORY_MCP_EMBEDDING_PROVIDER": settings.embedding_provider,
            "AI_MEMORY_MCP_EMBEDDING_MODEL": settings.embedding_model,
            "AI_MEMORY_MCP_SEMANTIC_DIMENSIONS": str(settings.semantic_dimensions),
            "AI_MEMORY_MCP_RRF_K": str(settings.rrf_k),
            "AI_MEMORY_MCP_GRAPH_DEPTH": str(settings.graph_depth),
            "AI_MEMORY_MCP_RESULT_LIMIT": str(settings.result_limit),
            "AI_MEMORY_MCP_HOST": "127.0.0.1",
            "AI_MEMORY_MCP_PORT": str(settings.port),
            "AI_MEMORY_MCP_RECALL_TIMEOUT_SECONDS": str(settings.recall_timeout_seconds),
            "AI_MEMORY_RECALL_WORKERS": str(settings.recall_worker_count),
            "AI_MEMORY_RECALL_QUEUE_CAPACITY": str(settings.recall_queue_capacity),
            "AI_MEMORY_RECALL_WORKER_MAX_REQUESTS": str(settings.recall_worker_max_requests),
            "AI_MEMORY_VECTOR_BLOCK_SIZE": str(settings.vector_block_size),
            "AI_MEMORY_VECTOR_MAX_VECTORS": str(settings.vector_max_vectors),
            "AI_MEMORY_VECTOR_MAX_SECONDS": str(settings.vector_max_seconds),
            "AI_MEMORY_ANN_CANDIDATE_LIMIT": str(settings.ann_candidate_limit),
            "AI_MEMORY_CONTEXT_MAX_CHARACTERS": str(settings.context_max_characters),
            "AI_MEMORY_INDEX_LOCK_TIMEOUT_SECONDS": str(settings.index_lock_timeout_seconds),
            "AI_MEMORY_ARTIFACT_BATCH_MAX_BYTES": str(settings.artifact_batch_max_bytes),
            "AI_MEMORY_GENERATION_RETENTION_COUNT": str(settings.generation_retention_count),
            "AI_MEMORY_GENERATION_LEASE_TTL_SECONDS": str(settings.generation_lease_ttl_seconds),
            "AI_MEMORY_ARTIFACT_DB": str(settings.artifact_db),
            "AI_MEMORY_ARTIFACT_OBJECTS_DIR": str(settings.artifact_objects_dir),
            "AI_MEMORY_ARTIFACT_BACKUP_DIR": str(settings.artifact_backup_dir),
            "AI_MEMORY_AUDIT_LOGGING": str(settings.audit_logging_enabled).lower(),
            "AI_MEMORY_QUERY_LOG_CONTENT": str(settings.query_log_content).lower(),
        }
    )
    return environment


def _validate_mcp_packet(
    payload: dict[str, Any], case: dict[str, Any], legacy: dict[str, Any] | None = None
) -> None:
    packet = RecallResponseV2.model_validate(payload)
    if packet.execution != "complete":
        raise RuntimeError(f"MCP execution was incomplete for {case['id']}: {packet.reason_codes}")
    returned = {item.memory_id for item in packet.evidence[:5]}
    expected = _expected_ids(case)
    if case.get("no_answer"):
        # V2 ranked results are leads, not an answer assertion. Check the retained
        # V1 answer gate through serialization instead of rejecting useful leads.
        if (packet.result_kind == "exact" or legacy is None
                or RecallResponse.model_validate(legacy).status != "no_answer"):
            raise RuntimeError(f"MCP asserted an unsupported answer for {case['id']}.")
    elif not expected & returned:
        raise RuntimeError(f"MCP missed the expected source for {case['id']}.")
    if returned & set(case.get("forbidden", [])):
        raise RuntimeError(f"MCP returned an excluded source for {case['id']}.")
    if case.get("expected_path") and not any(
        item.path.endswith(case["expected_path"]) for item in packet.citations
    ):
        raise RuntimeError(f"MCP returned an incorrect citation for {case['id']}.")
    if expected and not any(item.memory_id in expected for item in packet.citations):
        raise RuntimeError(f"MCP did not cite the expected source for {case['id']}.")
    if case.get("expected_warning") and not any(
        case["expected_warning"].casefold() in warning.casefold() for warning in packet.warnings
    ):
        raise RuntimeError(f"MCP omitted the required warning for {case['id']}.")
    if case.get("expected_status") and (
        legacy is None or RecallResponse.model_validate(legacy).status != case["expected_status"]
    ):
        raise RuntimeError(f"MCP returned an incorrect answer status for {case['id']}.")


def _process_tree_resident_bytes() -> int:
    import psutil

    root = psutil.Process()
    total = 0
    for process in [root, *root.children(recursive=True)]:
        try:
            total += process.memory_info().rss
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return total


def _mcp_latencies(settings: Settings, cases: list[dict[str, Any]], repeats: int = 1) -> dict[str, Any]:
    """Measure serialized stdio requests through the supervised worker boundary."""

    async def measure() -> dict[str, Any]:
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "ai_memory_mcp.server", "--transport", "stdio"],
            env=_mcp_environment(settings),
        )
        values: list[float] = []
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                async def recall(case):
                    arguments = {
                        "query": case["query"],
                        "limit": 5,
                        "response_version": "2",
                        **case.get("scope", {}),
                    }
                    started = time.perf_counter()
                    result = await session.call_tool("memory_recall", arguments)
                    values.append((time.perf_counter() - started) * 1000)
                    if result.isError or result.structuredContent is None:
                        raise RuntimeError(
                            "The serialized MCP benchmark request failed."
                        )
                    legacy = None
                    if case.get("no_answer") or case.get("expected_status"):
                        old = await session.call_tool("memory_recall", {**arguments, "response_version": "1"})
                        if old.isError or old.structuredContent is None:
                            raise RuntimeError("The serialized legacy MCP benchmark request failed.")
                        legacy = old.structuredContent
                    _validate_mcp_packet(result.structuredContent, case, legacy)
                for _ in range(repeats):
                    for case in cases:
                        await recall(case)
                # Exercise the real pool with overlapping accepted requests. These
                # calls use the same quality checks as sequential cold/warm calls.
                await asyncio.gather(*(recall(case) for case in cases[:settings.recall_worker_count]))
                intake_case = {
                    "id": "active-intake", "query": "durable horizon marker",
                    "expected_artifacts": ["message:active-intake-message"],
                    "scope": {"source_label": SOURCE, "source_instance": SOURCE_INSTANCE, "artifact_kind": "message"},
                }
                events = [ArtifactEvent.model_validate(_event(
                    "message", "active-intake-message", "2026-06-01T00:00:00Z",
                    "The durable horizon marker is HL-733. Keep the release instructions.",
                    parent=("conversation", "conversation-0000"),
                ))]
                batch = ParsedArtifactBatch(
                    manifest=ArtifactBatchManifest.model_validate({
                        "schema": "ai-memory/artifact-batch@1", "record": "batch",
                        "batch_id": "active-intake-probe", "source": SOURCE, "source_instance": SOURCE_INSTANCE,
                        "observed_at": "2026-06-01T01:00:00Z", "event_count": 1,
                    }), events=events, input_sha256="a" * 64,
                )
                # Intake and a real query overlap. Then test the new canonical
                # message while semantic publication still has the old watermark.
                async def during_intake():
                    result = await session.call_tool("memory_recall", {"query": cases[0]["query"], "limit": 5})
                    packet = RecallResponseV2.model_validate(result.structuredContent)
                    if result.isError or packet.execution == "failed" or not _expected_ids(cases[0]) & {e.memory_id for e in packet.evidence}:
                        raise RuntimeError("MCP lost known evidence during artifact intake.")
                intake_started = time.perf_counter()
                await asyncio.gather(asyncio.to_thread(ArtifactStore(settings).apply_batch, batch), during_intake())
                arguments = {"query": intake_case["query"], "limit": 5, **intake_case["scope"]}
                lagged = await session.call_tool("memory_recall", arguments)
                packet = RecallResponseV2.model_validate(lagged.structuredContent)
                if (lagged.isError or packet.execution != "partial" or packet.coverage.artifact_semantic_lag < 1
                        or not _expected_ids(intake_case) & {e.memory_id for e in packet.evidence}):
                    raise RuntimeError("MCP did not expose fresh raw evidence during semantic lag.")
                synced = await session.call_tool("memory_sync", {})
                if synced.isError or not synced.structuredContent or not synced.structuredContent.get("ok"):
                    raise RuntimeError("MCP failed the active-intake refresh.")
                refreshed = await session.call_tool("memory_recall", arguments)
                _validate_mcp_packet(refreshed.structuredContent, intake_case)
                intake_ms = (time.perf_counter() - intake_started) * 1000
        return {"latencies_ms": values, "validated_queries": len(values),
                "active_intake_checks": 3, "active_intake_refresh_ms": intake_ms}

    stop = threading.Event()
    peak = _process_tree_resident_bytes()
    sampling_errors: list[Exception] = []
    def sample():
        nonlocal peak
        try:
            while not stop.wait(.02):
                peak = max(peak, _process_tree_resident_bytes())
        except Exception as exc:
            sampling_errors.append(exc)
    sampler = threading.Thread(target=sample, name="benchmark-memory", daemon=True)
    sampler.start()
    try:
        result = asyncio.run(measure())
    finally:
        stop.set()
        sampler.join()
    if sampling_errors:
        raise RuntimeError("The process-tree memory measurement was incomplete.") from sampling_errors[0]
    result["process_tree_peak_resident_bytes"] = peak
    result["memory_sample_interval_ms"] = 20
    return result


def run_benchmark(
    run_dir: Path,
    *,
    profile_name: str = "standard",
    embedding_provider: str = "auto",
    repeats: int = 3,
    enforce_quality: bool = True,
    baseline_path: Path | None = None,
    maximum_regression_ratio: float = 1.5,
    query_log_content: bool = False,
) -> dict[str, Any]:
    if repeats <= 0:
        raise ValueError("The repeat count must be positive.")
    contract = load_contract()
    profile = profile_from_contract(contract, profile_name)
    run_dir.mkdir(parents=True, exist_ok=False)
    vault_metrics = write_vault(run_dir / "vault", profile)
    artifact_path = run_dir / "artifact-batch.jsonl"
    artifact_metrics = write_artifacts(artifact_path, profile)
    settings = Settings(
        memory_root=run_dir / "vault",
        state_dir=run_dir / "state",
        graph_path=run_dir / "graph.json",
        graphify_mcp_url="",
        embedding_provider=embedding_provider,
        artifact_db=run_dir / "artifacts.sqlite3",
        artifact_objects_dir=run_dir / "objects",
        artifact_backup_dir=run_dir / "backups",
        query_log_content=query_log_content,
    )

    started = time.perf_counter()
    with artifact_path.open("rb") as stream:
        batch = read_artifact_batch(stream, max_bytes=settings.artifact_batch_max_bytes)
    receipt = ingest_artifact_batch(settings, batch)
    intake_seconds = time.perf_counter() - started
    if receipt.accepted != profile.artifact_events:
        raise RuntimeError("The real-world artifact intake count is incorrect.")

    service = MemoryService(settings)
    started = time.perf_counter()
    sync = service.sync()
    generation_seconds = time.perf_counter() - started
    if not sync.ok or sync.index is None:
        raise RuntimeError("The real-world benchmark generation did not publish.")
    status = service.status()
    embedding_name = status.index.embedding_provider or "unknown"
    semantic_available = embedding_name != "hashed"

    results: list[dict[str, Any]] = []
    latencies: list[float] = []
    reciprocal_ranks: list[float] = []
    provider_latencies: dict[str, list[float]] = defaultdict(list)
    no_answer_total = no_answer_correct = scope_leaks = citation_failures = 0
    answered_total = recall_at_5 = 0
    tag_results: dict[str, list[bool]] = defaultdict(list)
    cases = [
        case
        for case in contract["cases"]
        if semantic_available or not case.get("requires_semantic")
    ]
    ann_recall_values: list[float] = []
    ann_incomplete = 0
    exact_settings = replace(
        settings,
        vector_max_vectors=min(
            5_000_000,
            max(
                settings.vector_max_vectors,
                sync.artifact_index.bursts if sync.artifact_index else 0,
            ),
        ),
        vector_max_seconds=120.0,
    )
    for case in cases:
        if not case.get("expected_artifacts"):
            continue
        raw_scope = case.get("scope", {})
        kind = raw_scope.get("artifact_kind")
        scope = ArtifactScope(
            source=raw_scope.get("source_label"),
            source_instance=raw_scope.get("source_instance"),
            entities=(kind,) if kind else (),
        )
        approximate = search_artifact_vectors(
            settings, case["query"], scope, 10
        )
        exact = search_artifact_vectors(
            exact_settings, case["query"], scope, 10, force_exact=True
        )
        if exact.budget_exhausted:
            ann_incomplete += 1
            continue
        ann_recall_values.append(
            tie_aware_candidate_recall_at_k(
                [(hit.segment_id or hit.artifact_uri, hit.score) for hit in exact.hits],
                [(hit.segment_id or hit.artifact_uri, hit.score) for hit in approximate.hits],
                10,
            )
        )
    for repeat in range(repeats):
        for case in cases:
            started = time.perf_counter()
            packet = service.recall(case["query"], limit=5, **case.get("scope", {}))
            elapsed = (time.perf_counter() - started) * 1000
            latencies.append(elapsed)
            returned = [item.memory_id for item in packet.evidence]
            expected = _expected_ids(case)
            forbidden = set(case.get("forbidden", []))
            if case.get("no_answer"):
                no_answer_total += 1
                passed = packet.status == "no_answer"
                no_answer_correct += int(passed)
                rank = None
            else:
                answered_total += 1
                ranks = [returned.index(value) + 1 for value in expected if value in returned]
                rank = min(ranks) if ranks else None
                passed = rank is not None and rank <= 5
                recall_at_5 += int(passed)
                reciprocal_ranks.append(1.0 / rank if rank else 0.0)
            leaked = bool(forbidden & set(returned))
            scope_leaks += int(leaked)
            expected_path = case.get("expected_path")
            citation_ok = not expected_path or any(
                citation.path.endswith(expected_path) for citation in packet.citations
            )
            if case.get("expected_artifacts"):
                citation_ok = citation_ok and any(
                    citation.path.startswith("artifact://") for citation in packet.citations
                )
            citation_failures += int(not citation_ok)
            status_ok = not case.get("expected_status") or packet.status == case[
                "expected_status"
            ]
            warning_ok = not case.get("expected_warning") or any(
                case["expected_warning"].casefold() in warning.casefold()
                for warning in packet.warnings
            )
            final_pass = passed and not leaked and citation_ok and status_ok and warning_ok
            for tag in case.get("tags", []):
                tag_results[tag].append(final_pass)
            if repeat == 0:
                results.append(
                    {
                        "id": case["id"],
                        "passed": final_pass,
                        "rank": rank,
                        "status": packet.status,
                        "status_ok": status_ok,
                        "warning_ok": warning_ok,
                        "returned": returned,
                        "latency_ms": round(elapsed, 3),
                    }
                )
            else:
                results[cases.index(case)]["passed"] &= final_pass
            log_path = settings.resolved_log_dir / "retrieval.jsonl"
            diagnostics = json.loads(log_path.read_text(encoding="utf-8").splitlines()[-1])[
                "diagnostics"
            ]
            for provider, value in diagnostics.get("provider_latency_ms", {}).items():
                if isinstance(value, (int, float)):
                    provider_latencies[str(provider)].append(float(value))

    mcp_measurement = _mcp_latencies(settings, cases, repeats)
    mcp_latencies = mcp_measurement["latencies_ms"]

    metrics = {
        "profile": profile_name,
        "embedding_provider": embedding_name,
        "embedding_model": status.index.embedding_model,
        "embedding_dimensions": status.index.semantic_dimensions,
        "ann_backend": status.artifact_vector.ann_backend,
        "reranker": "none",
        "ann_candidate_recall_at_10": (
            statistics.mean(ann_recall_values) if ann_recall_values else 1.0
        ),
        "ann_candidate_evaluations": len(ann_recall_values),
        "ann_candidate_incomplete": ann_incomplete,
        "model_revision": "package-resolved",
        "tokenizer": (
            "baai/bge-base-en-v1.5" if embedding_name == "model2vec" else "hashed"
        ),
        "model_license": "MIT" if embedding_name == "model2vec" else "internal",
        "semantic_cases_executed": semantic_available,
        "cases": len(cases),
        "repeats": repeats,
        "pass_rate": statistics.mean(result["passed"] for result in results),
        "recall_at_5": recall_at_5 / answered_total,
        "mrr": statistics.mean(reciprocal_ranks),
        "no_answer_accuracy": no_answer_correct / no_answer_total,
        "scope_leakage_rate": scope_leaks / (len(cases) * repeats),
        "citation_failure_rate": citation_failures / (len(cases) * repeats),
        "intake_seconds": intake_seconds,
        "generation_seconds": generation_seconds,
        "refresh_events_per_second": (
            profile.artifact_events / generation_seconds
            if generation_seconds > 0
            else 0.0
        ),
        "recall_p50_ms": statistics.median(latencies),
        "recall_p95_ms": _percentile(latencies, 0.95),
        "recall_p99_ms": _percentile(latencies, 0.99),
        "mcp_recall_p50_ms": statistics.median(mcp_latencies),
        "mcp_recall_p95_ms": _percentile(mcp_latencies, 0.95),
        "mcp_recall_p99_ms": _percentile(mcp_latencies, 0.99),
        "mcp_transport_queries": len(mcp_latencies),
        "mcp_quality_validated_queries": mcp_measurement["validated_queries"],
        "process_tree_peak_resident_bytes": mcp_measurement["process_tree_peak_resident_bytes"],
        "memory_sample_interval_ms": mcp_measurement["memory_sample_interval_ms"],
        "active_intake_checks": mcp_measurement["active_intake_checks"],
        "active_intake_refresh_ms": mcp_measurement["active_intake_refresh_ms"],
        "cold_recall_ms": latencies[0] if latencies else 0.0,
        "warm_recall_p50_ms": (
            statistics.median(latencies[1:]) if len(latencies) > 1 else 0.0
        ),
        "recall_queries_per_second": 1000.0 / statistics.mean(latencies),
        "per_layer_latency_ms": {
            provider: {
                "p50": statistics.median(values),
                "p95": _percentile(values, 0.95),
            }
            for provider, values in sorted(provider_latencies.items())
        },
        "corpus": {
            "markdown_documents": vault_metrics["documents"],
            "artifact_events": artifact_metrics["events"],
            "artifact_bursts": sync.artifact_index.bursts if sync.artifact_index else 0,
            "graph_nodes": status.graphify.nodes,
            "graph_edges": status.graphify.edges,
        },
        "storage_bytes": _directory_bytes(run_dir),
        "peak_resident_bytes": max(_peak_resident_bytes() or 0, mcp_measurement["process_tree_peak_resident_bytes"]),
        "resource_limits": {
            "query_log_content": settings.query_log_content,
            "audit_logging_enabled": settings.audit_logging_enabled,
            "vector_block_size": settings.vector_block_size,
            "vector_max_vectors": settings.vector_max_vectors,
            "vector_max_seconds": settings.vector_max_seconds,
            "ann_candidate_limit": settings.ann_candidate_limit,
            "worker_count": settings.recall_worker_count,
            "queue_capacity": settings.recall_queue_capacity,
        },
        "environment": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "packages": {
                name: _package_version(name)
                for name in ("ai-memory-mcp", "model2vec", "numpy")
            },
        },
        "tag_pass_rate": {
            tag: statistics.mean(values) for tag, values in sorted(tag_results.items())
        },
    }
    quality = contract["quality"]
    quality_failures = []
    if metrics["pass_rate"] < quality["minimum_pass_rate"]:
        quality_failures.append("pass_rate")
    if metrics["recall_at_5"] < quality["minimum_recall_at_5"]:
        quality_failures.append("recall_at_5")
    if metrics["no_answer_accuracy"] < quality["minimum_no_answer_accuracy"]:
        quality_failures.append("no_answer_accuracy")
    if (
        metrics["ann_candidate_recall_at_10"]
        < quality["minimum_ann_candidate_recall_at_10"]
    ):
        quality_failures.append("ann_candidate_recall_at_10")
    if metrics["ann_candidate_incomplete"]:
        quality_failures.append("ann_exact_budget_exhausted")
    if metrics["scope_leakage_rate"] > quality["maximum_scope_leakage_rate"]:
        quality_failures.append("scope_leakage_rate")
    if metrics["citation_failure_rate"] > quality["maximum_citation_failure_rate"]:
        quality_failures.append("citation_failure_rate")
    performance_failures: list[str] = []
    thresholds = contract.get("performance", {}).get(profile_name, {})
    if (
        "maximum_recall_p95_ms" in thresholds
        and metrics["mcp_recall_p95_ms"] > thresholds["maximum_recall_p95_ms"]
    ):
        performance_failures.append("mcp_recall_p95_ms")
    if (
        "minimum_refresh_events_per_second" in thresholds
        and metrics["refresh_events_per_second"]
        < thresholds["minimum_refresh_events_per_second"]
    ):
        performance_failures.append("refresh_events_per_second")
    peak = metrics["peak_resident_bytes"]
    if (
        peak is not None
        and "maximum_peak_resident_bytes" in thresholds
        and peak > thresholds["maximum_peak_resident_bytes"]
    ):
        performance_failures.append("peak_resident_bytes")
    report: dict[str, Any] = {
        "schema_version": 1,
        "completed": True,
        "run_at": datetime.now(timezone.utc).isoformat(),
        "fixture": {"vault": vault_metrics, "artifacts": artifact_metrics},
        "metrics": metrics,
        "quality_failures": quality_failures,
        "performance_failures": performance_failures,
        "cases": results,
    }
    if baseline_path:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        report["performance_comparison"] = compare_performance(
            metrics, baseline, maximum_regression_ratio
        )
    report_path = run_dir / "report.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    report["report"] = str(report_path)
    comparison_failures = report.get("performance_comparison", {}).get(
        "failures", []
    )
    performance_failures.extend(comparison_failures)
    if enforce_quality and (quality_failures or performance_failures):
        failures = quality_failures + performance_failures
        raise RuntimeError(f"The real-world benchmark failed: {', '.join(failures)}")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the mixed synthetic AI Memory benchmark."
    )
    parser.add_argument(
        "--profile",
        choices=(
            "smoke", "standard", "scale", "regression", "workload", "growth"
        ),
        default="standard",
    )
    parser.add_argument("--embedding-provider", default="auto")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--maximum-regression-ratio", type=float, default=1.5)
    parser.add_argument("--no-quality-gate", action="store_true")
    parser.add_argument("--log-query-content", action="store_true", help="Write private query and response traces during the benchmark.")
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = args.output_dir or _benchmark_root().parent / "runs" / f"real-world-{stamp}"
    try:
        report = run_benchmark(
            output,
            profile_name=args.profile,
            embedding_provider=args.embedding_provider,
            repeats=args.repeats,
            enforce_quality=not args.no_quality_gate,
            baseline_path=args.baseline,
            maximum_regression_ratio=args.maximum_regression_ratio,
            query_log_content=args.log_query_content,
        )
    except BaseException as exc:
        output.mkdir(parents=True, exist_ok=True)
        report_path = output / "report.json"
        if report_path.is_file():
            raise
        report = {
            "schema_version": 1,
            "completed": False,
            "run_at": datetime.now(timezone.utc).isoformat(),
            "profile": args.profile,
            "failure_type": type(exc).__name__,
        }
        report_path.write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8"
        )
        raise
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
