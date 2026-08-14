from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from ai_memory_mcp.artifacts.identity import artifact_id
from ai_memory_mcp.artifacts.ingest import (
    ingest_artifact_batch,
    read_artifact_batch,
)
from ai_memory_mcp.artifacts.models import ArtifactScope
from ai_memory_mcp.artifacts.search import ArtifactSearch
from ai_memory_mcp.artifacts.store import ArtifactStore
from ai_memory_mcp.artifacts.vector_index import build_artifact_vector_index
from ai_memory_mcp.config import Settings
from ai_memory_mcp.service import MemoryService

SOURCE = "synthetic-chat"
SOURCE_INSTANCE = "benchmark"
MESSAGE_MARKER = "violet checkpoint token qx-042-769"
CUE_MARKER = "silver rollback beacon rb-037-273"


@dataclass(frozen=True, slots=True)
class ArtifactFixtureSpec:
    conversations: int = 100
    messages_per_conversation: int = 1000
    meetings: int = 100
    cues_per_meeting: int = 500

    @property
    def messages(self) -> int:
        return self.conversations * self.messages_per_conversation

    @property
    def transcripts(self) -> int:
        return self.meetings

    @property
    def transcript_cues(self) -> int:
        return self.meetings * self.cues_per_meeting

    @property
    def event_count(self) -> int:
        return (
            self.conversations
            + self.messages
            + self.meetings
            + self.transcripts
            + self.transcript_cues
        )


@dataclass(frozen=True, slots=True)
class ArtifactFixtureResult:
    path: str
    sha256: str
    bytes: int
    event_count: int
    counts: dict[str, int]
    targets: dict[str, str]


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _json_line(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _upsert(
    entity: str,
    external_id: str,
    occurred_at: str,
    payload: dict[str, Any],
    *,
    parent: tuple[str, str] | None = None,
    source_sequence: int | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "schema": "ai-memory/artifact-event@1",
        "record": "event",
        "entity": entity,
        "operation": "upsert",
        "external_id": external_id,
        "source_updated_at": occurred_at,
        "payload": payload,
    }
    if parent is not None:
        event["parent"] = {
            "entity": parent[0],
            "external_id": parent[1],
        }
    if source_sequence is not None:
        event["source_sequence"] = source_sequence
    return event


def _target_indexes(spec: ArtifactFixtureSpec) -> tuple[int, int, int, int]:
    # Each marker starts an eight-record burst. Raw and burst evidence then
    # share one stable citation during fused-recall validation.
    return (
        min(42, spec.conversations - 1),
        min(768, spec.messages_per_conversation - 1),
        min(37, spec.meetings - 1),
        min(272, spec.cues_per_meeting - 1),
    )


def _records(spec: ArtifactFixtureSpec) -> Iterable[dict[str, Any]]:
    if min(
        spec.conversations,
        spec.messages_per_conversation,
        spec.meetings,
        spec.cues_per_meeting,
    ) <= 0:
        raise ValueError("Each artifact fixture count must be positive.")
    message_conversation, message_index, cue_meeting, cue_index = _target_indexes(
        spec
    )
    yield {
        "schema": "ai-memory/artifact-batch@1",
        "record": "batch",
        "batch_id": (
            f"synthetic-{spec.conversations}-{spec.messages}-"
            f"{spec.meetings}-{spec.transcript_cues}"
        ),
        "source": SOURCE,
        "source_instance": SOURCE_INSTANCE,
        "observed_at": "2026-09-01T00:00:00Z",
        "event_count": spec.event_count,
    }

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for conversation_index in range(spec.conversations):
        conversation_id = f"conversation-{conversation_index:03d}"
        conversation_time = base + timedelta(days=conversation_index)
        yield _upsert(
            "conversation",
            conversation_id,
            _iso(conversation_time),
            {
                "title": f"Synthetic Conversation {conversation_index:03d}",
                "occurred_at": _iso(conversation_time),
                "content_format": "plain",
            },
        )
        for index in range(spec.messages_per_conversation):
            sequence = index + 1
            occurred_at = _iso(conversation_time + timedelta(seconds=sequence))
            marker = (
                f" The approved marker is {MESSAGE_MARKER}."
                if (
                    conversation_index == message_conversation
                    and index == message_index
                )
                else ""
            )
            yield _upsert(
                "message",
                f"message-{conversation_index:03d}-{sequence:06d}",
                occurred_at,
                {
                    "text": (
                        "The release team reviews the neutral change sequence, "
                        f"approval record, and recovery check {sequence}.{marker}"
                    ),
                    "occurred_at": occurred_at,
                    "content_format": "plain",
                    "author": {
                        "id": f"actor-{conversation_index:03d}",
                        "name": "Synthetic Actor",
                        "id_confidence": "stable",
                    },
                },
                parent=("conversation", conversation_id),
                source_sequence=sequence,
            )

    meeting_base = datetime(2026, 5, 1, tzinfo=timezone.utc)
    for meeting_index in range(spec.meetings):
        meeting_id = f"meeting-{meeting_index:03d}"
        transcript_id = f"transcript-{meeting_index:03d}"
        conversation_id = (
            f"conversation-{meeting_index % spec.conversations:03d}"
        )
        meeting_time = meeting_base + timedelta(days=meeting_index)
        yield _upsert(
            "meeting",
            meeting_id,
            _iso(meeting_time),
            {
                "title": f"Synthetic Meeting {meeting_index:03d}",
                "occurred_at": _iso(meeting_time),
                "content_format": "plain",
                "links": [
                    {
                        "relation": "related-chat",
                        "target": {
                            "entity": "conversation",
                            "external_id": conversation_id,
                        },
                    },
                    {
                        "relation": "contains",
                        "target": {
                            "entity": "transcript",
                            "external_id": transcript_id,
                        },
                    },
                ],
            },
        )
        yield _upsert(
            "transcript",
            transcript_id,
            _iso(meeting_time),
            {
                "title": f"Synthetic Transcript {meeting_index:03d}",
                "occurred_at": _iso(meeting_time),
                "content_format": "vtt",
            },
            parent=("meeting", meeting_id),
        )
        for index in range(spec.cues_per_meeting):
            sequence = index + 1
            occurred_at = _iso(meeting_time + timedelta(seconds=sequence))
            marker = (
                f" The accepted marker is {CUE_MARKER}."
                if meeting_index == cue_meeting and index == cue_index
                else ""
            )
            yield _upsert(
                "transcript-cue",
                f"cue-{meeting_index:03d}-{sequence:06d}",
                occurred_at,
                {
                    "text": (
                        "The meeting reviews the neutral release evidence, "
                        f"owner action, and follow-up check {sequence}.{marker}"
                    ),
                    "occurred_at": occurred_at,
                    "content_format": "plain",
                    "author": {
                        "id": f"speaker-{meeting_index:03d}",
                        "name": "Synthetic Speaker",
                        "id_confidence": "stable",
                    },
                },
                parent=("transcript", transcript_id),
                source_sequence=sequence,
            )


def write_fixture(path: Path, spec: ArtifactFixtureSpec) -> ArtifactFixtureResult:
    """Write one deterministic JSONL fixture without replacing an existing file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    byte_count = 0
    with path.open("xb") as stream:
        for record in _records(spec):
            encoded = _json_line(record)
            stream.write(encoded)
            digest.update(encoded)
            byte_count += len(encoded)
    message_conversation, message_index, cue_meeting, cue_index = _target_indexes(
        spec
    )
    return ArtifactFixtureResult(
        path=str(path),
        sha256=digest.hexdigest(),
        bytes=byte_count,
        event_count=spec.event_count,
        counts={
            "conversation": spec.conversations,
            "message": spec.messages,
            "meeting": spec.meetings,
            "transcript": spec.transcripts,
            "transcript-cue": spec.transcript_cues,
        },
        targets={
            "message": (
                f"message-{message_conversation:03d}-{message_index + 1:06d}"
            ),
            "transcript-cue": (
                f"cue-{cue_meeting:03d}-{cue_index + 1:06d}"
            ),
        },
    )


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1)
    return ordered[max(index, 0)]


def _latencies(action, repeats: int) -> tuple[float, float]:
    action()
    values: list[float] = []
    for _ in range(repeats):
        started = time.perf_counter()
        action()
        values.append((time.perf_counter() - started) * 1000)
    return statistics.median(values), _percentile(values, 0.95)


def _database_bytes(path: Path) -> int:
    # WAL and shared-memory files are part of the active SQLite footprint.
    candidates = [path, Path(f"{path}-wal"), Path(f"{path}-shm")]
    return sum(candidate.stat().st_size for candidate in candidates if candidate.exists())


def _contract() -> dict[str, Any]:
    return json.loads(
        Path(__file__).with_name("cases.json").read_text(encoding="utf-8")
    )


def run_benchmark(
    run_dir: Path,
    *,
    repeats: int = 20,
    generate_only: bool = False,
) -> dict[str, Any]:
    if repeats <= 0:
        raise ValueError("The repeat count must be positive.")
    contract = _contract()
    fixture = contract["fixture"]
    if (
        int(fixture["messages"]) % int(fixture["conversations"])
        or int(fixture["transcript_cues"]) % int(fixture["meetings"])
    ):
        raise RuntimeError("The artifact fixture counts do not divide evenly.")
    spec = ArtifactFixtureSpec(
        conversations=int(fixture["conversations"]),
        messages_per_conversation=(
            int(fixture["messages"]) // int(fixture["conversations"])
        ),
        meetings=int(fixture["meetings"]),
        cues_per_meeting=(
            int(fixture["transcript_cues"]) // int(fixture["meetings"])
        ),
    )
    if spec.transcripts != int(fixture["transcripts"]):
        raise RuntimeError("The artifact fixture transcript count is incorrect.")
    run_dir.mkdir(parents=True, exist_ok=False)
    generated = write_fixture(run_dir / "artifact-batch.jsonl", spec)
    expected_digest = fixture.get("expected_jsonl_sha256")
    if not isinstance(expected_digest, str) or len(expected_digest) != 64:
        raise RuntimeError("The artifact fixture contract has no SHA-256 value.")
    if generated.sha256 != expected_digest:
        raise RuntimeError("The generated artifact fixture digest changed.")
    report: dict[str, Any] = {"fixture": asdict(generated)}
    if generate_only:
        return report

    memory_root = run_dir / "vault"
    memory_root.mkdir()
    settings = Settings(
        memory_root=memory_root,
        state_dir=run_dir / "state",
        graph_path=run_dir / "graph.json",
        graphify_mcp_url="",
        embedding_provider="hashed",
        artifact_db=run_dir / "artifacts.sqlite3",
        artifact_objects_dir=run_dir / "objects",
        artifact_backup_dir=run_dir / "backups",
        audit_logging_enabled=False,
    )

    started = time.perf_counter()
    with Path(generated.path).open("rb") as stream:
        batch = read_artifact_batch(
            stream,
            max_bytes=settings.artifact_batch_max_bytes,
        )
    validation_seconds = time.perf_counter() - started
    started = time.perf_counter()
    receipt = ingest_artifact_batch(settings, batch)
    ingest_seconds = time.perf_counter() - started
    # Release the validated input before the derived burst build starts.
    del batch

    store = ArtifactStore(settings)
    actual_counts = {
        entity: store.count(entity)
        for entity in generated.counts
    }
    if receipt.accepted != generated.event_count or actual_counts != generated.counts:
        raise RuntimeError("The artifact benchmark intake counts are incorrect.")

    search = ArtifactSearch(settings)
    cases = contract["cases"]
    search_hits: dict[str, Any] = {}
    for case in cases:
        hits = search.search(
            str(case["query"]),
            scope=ArtifactScope(
                source=SOURCE,
                source_instance=SOURCE_INSTANCE,
                entities=(str(case["entity"]),),
            ),
            limit=5,
        )
        if not hits or hits[0].external_id != case["external_id"]:
            raise RuntimeError(f"Artifact benchmark case failed: {case['id']}")
        search_hits[str(case["id"])] = hits[0]

    case_index = 0

    def fts_action() -> None:
        nonlocal case_index
        case = cases[case_index % len(cases)]
        case_index += 1
        hits = search.search(
            str(case["query"]),
            scope=ArtifactScope(
                source=SOURCE,
                source_instance=SOURCE_INSTANCE,
                entities=(str(case["entity"]),),
            ),
            limit=5,
        )
        if not hits or hits[0].external_id != case["external_id"]:
            raise RuntimeError("A warm artifact FTS result changed.")

    fts_p50, fts_p95 = _latencies(fts_action, repeats)
    message_reference = search_hits["message-decision"].artifact_uri

    def read_action() -> None:
        response = search.read(message_reference, direction="around", limit=50)
        if not response.records:
            raise RuntimeError("The ordered artifact read returned no records.")

    read_p50, read_p95 = _latencies(read_action, repeats)
    started = time.perf_counter()
    burst_result = build_artifact_vector_index(settings, force=True)
    burst_seconds = time.perf_counter() - started

    service = MemoryService(settings)
    recall_index = 0

    def recall_action() -> None:
        nonlocal recall_index
        case = cases[recall_index % len(cases)]
        recall_index += 1
        response = service.recall(
            f'"{case["query"]}"',
            source_label=SOURCE,
            source_instance=SOURCE_INSTANCE,
            artifact_kind=str(case["entity"]),
            limit=5,
        )
        expected = artifact_id(
            SOURCE,
            SOURCE_INSTANCE,
            str(case["entity"]),
            str(case["external_id"]),
        )
        if not response.evidence or response.evidence[0].memory_id != expected:
            raise RuntimeError("A warm fused-recall result changed.")

    recall_p50, recall_p95 = _latencies(recall_action, repeats)
    report.update(
        {
            "validation_seconds": round(validation_seconds, 3),
            "ingest_seconds": round(ingest_seconds, 3),
            "database_bytes": _database_bytes(settings.artifact_db),
            "warm_fts_p50_ms": round(fts_p50, 3),
            "warm_fts_p95_ms": round(fts_p95, 3),
            "ordered_read_p50_ms": round(read_p50, 3),
            "ordered_read_p95_ms": round(read_p95, 3),
            "burst_index_seconds": round(burst_seconds, 3),
            "bursts": burst_result.bursts,
            "embedded_bursts": burst_result.embedded_bursts,
            "warm_fused_recall_p50_ms": round(recall_p50, 3),
            "warm_fused_recall_p95_ms": round(recall_p95, 3),
            "counts": actual_counts,
        }
    )
    report_path = run_dir / "report.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    report["report"] = str(report_path)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate and measure the synthetic artifact benchmark."
    )
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--repeats", type=int, default=20)
    parser.add_argument("--generate-only", action="store_true")
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    default = Path(__file__).resolve().parents[1] / "runs" / f"artifacts-{stamp}"
    report = run_benchmark(
        args.output_dir or default,
        repeats=args.repeats,
        generate_only=args.generate_only,
    )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
