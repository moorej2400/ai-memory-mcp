from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .identity import BATCH_ID_PATTERN, ENTITY_PATTERN, SOURCE_PATTERN

ArtifactEntity = Literal[
    "conversation",
    "message",
    "meeting",
    "recording",
    "transcript",
    "transcript-cue",
    "attachment",
]
ArtifactOperation = Literal["upsert", "delete", "redact"]
ArtifactEvidenceClass = Literal["distilled", "raw", "burst"]
ArtifactDisposition = Literal[
    "accepted",
    "unchanged",
    "stale",
    "conflict",
    "tombstone",
    "redacted",
]
DistillationStatus = Literal[
    "pending",
    "distilled",
    "no-durable-memory",
    "needs-review",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ArtifactReference(StrictModel):
    entity: ArtifactEntity
    external_id: str = Field(min_length=1, max_length=2048)


class ArtifactActor(StrictModel):
    id: str | None = Field(default=None, max_length=2048)
    name: str | None = Field(default=None, max_length=500)
    id_confidence: Literal["stable", "inferred", "display-name-only"] | None = None


class ArtifactAlias(StrictModel):
    kind: str = Field(pattern=ENTITY_PATTERN.pattern)
    value: str = Field(min_length=1, max_length=4096)


class ArtifactLink(StrictModel):
    relation: str = Field(pattern=ENTITY_PATTERN.pattern)
    target: ArtifactReference


class ArtifactObjectInput(StrictModel):
    local_source_path: Path | None = None
    expected_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    media_type: str | None = Field(default=None, max_length=500)
    original_name: str | None = Field(default=None, max_length=1000)


class ArtifactPayload(StrictModel):
    title: str | None = Field(default=None, max_length=2000)
    occurred_at: datetime | None = None
    text: str | None = None
    content_format: Literal["plain", "markdown", "html", "vtt"] | None = None
    author: ArtifactActor | None = None
    aliases: list[ArtifactAlias] = Field(default_factory=list)
    links: list[ArtifactLink] = Field(default_factory=list)
    object: ArtifactObjectInput | None = None
    source_payload: dict[str, Any] = Field(default_factory=dict)
    classification: str | None = Field(default=None, max_length=200)
    participants: list[ArtifactActor] = Field(default_factory=list)
    reactions: list[str] = Field(default_factory=list)


class ConversationPayload(ArtifactPayload):
    pass


class MessagePayload(ArtifactPayload):
    pass


class MeetingPayload(ArtifactPayload):
    pass


class RecordingPayload(ArtifactPayload):
    pass


class TranscriptPayload(ArtifactPayload):
    pass


class TranscriptCuePayload(ArtifactPayload):
    pass


class AttachmentPayload(ArtifactPayload):
    pass


class RedactionPayload(StrictModel):
    scope: Literal["artifact"] = "artifact"
    reason: str = Field(min_length=1, max_length=500)


class CoverageClaim(StrictModel):
    parent: ArtifactReference
    entity: ArtifactEntity
    covered_from: datetime | None = None
    covered_to: datetime | None = None
    complete: bool = False


class ArtifactBatchManifest(StrictModel):
    schema_name: Literal["ai-memory/artifact-batch@1"] = Field(alias="schema")
    record: Literal["batch"]
    batch_id: str = Field(pattern=BATCH_ID_PATTERN.pattern)
    source: str = Field(pattern=SOURCE_PATTERN.pattern)
    source_instance: str = Field(pattern=SOURCE_PATTERN.pattern)
    observed_at: datetime
    event_count: int = Field(ge=0)
    coverage: list[CoverageClaim] = Field(default_factory=list)


class ArtifactEvent(StrictModel):
    schema_name: Literal["ai-memory/artifact-event@1"] = Field(alias="schema")
    record: Literal["event"]
    entity: ArtifactEntity
    operation: ArtifactOperation
    external_id: str = Field(min_length=1, max_length=2048)
    parent: ArtifactReference | None = None
    source_version: str | None = Field(default=None, max_length=1000)
    source_sequence: int | None = Field(default=None, ge=0)
    source_updated_at: datetime | None = None
    payload: ArtifactPayload | RedactionPayload | None = None

    @model_validator(mode="after")
    def validate_payload_for_operation(self) -> "ArtifactEvent":
        if self.operation == "upsert" and not isinstance(self.payload, ArtifactPayload):
            raise ValueError("An upsert event requires an artifact payload.")
        if self.operation == "delete" and self.payload is not None:
            raise ValueError("A delete event must not contain a payload.")
        if self.operation == "redact" and not isinstance(
            self.payload, RedactionPayload
        ):
            raise ValueError("A redact event requires a redaction payload.")
        return self


class ParsedArtifactBatch(StrictModel):
    manifest: ArtifactBatchManifest
    events: list[ArtifactEvent]
    input_sha256: str


class ArtifactIngestReceipt(StrictModel):
    batch_id: str
    input_sha256: str = Field(min_length=64, max_length=64)
    committed_at: datetime
    committed: Literal[True] = True
    accepted: int = 0
    unchanged: int = 0
    stale: int = 0
    conflicts: int = 0
    tombstones: int = 0
    redactions: int = 0
    artifacts_changed: int = 0
    status: Literal["ok", "error"] = "ok"


class ArtifactScope(StrictModel):
    source: str | None = Field(default=None, pattern=SOURCE_PATTERN.pattern)
    source_instance: str | None = Field(
        default=None, pattern=SOURCE_PATTERN.pattern
    )
    entities: tuple[ArtifactEntity, ...] = ()
    parent: str | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None


class ArtifactSearchHit(StrictModel):
    artifact_id: str
    artifact_uri: str
    entity: ArtifactEntity
    source: str
    source_instance: str
    external_id: str = ""
    title: str = ""
    text: str = ""
    author_name: str = ""
    occurred_at: datetime | None = None
    score: float = 0.0
    evidence_class: ArtifactEvidenceClass = "raw"


class ArtifactReadRecord(StrictModel):
    reference: str
    entity: ArtifactEntity
    title: str = ""
    text: str = ""
    author_name: str = ""
    occurred_at: datetime | None = None
    payload: dict[str, Any] | None = None


class ArtifactReadResponse(StrictModel):
    focus: str
    records: list[ArtifactReadRecord] = Field(default_factory=list)
    previous_cursor: str | None = None
    next_cursor: str | None = None


class StoredObject(StrictModel):
    sha256: str
    byte_count: int = Field(ge=0)
    media_type: str = ""
    relative_path: str


class ObjectVerification(StrictModel):
    sha256: str
    ok: bool
    byte_count: int = Field(ge=0)


class DistillationCandidate(StrictModel):
    artifact_id: str
    artifact_uri: str
    entity: Literal["meeting", "conversation"]
    source: str
    source_instance: str
    title: str
    occurred_at: datetime | None = None
    latest_event_id: str
    source_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: DistillationStatus = "pending"


class ArtifactBurstRecord(StrictModel):
    artifact_id: str
    artifact_uri: str
    parent_artifact_id: str
    parent_title: str = ""
    source: str
    source_instance: str
    entity: ArtifactEntity
    author_id: str = ""
    author_name: str = ""
    participant_names: tuple[str, ...] = ()
    occurred_at: datetime
    text: str
    classification: str = ""
    reactions: tuple[str, ...] = ()
    attachment_link: bool = False
    deleted: bool = False
    redacted: bool = False


class ArtifactBurst(StrictModel):
    burst_id: str
    source: str
    source_instance: str
    entity: ArtifactEntity
    parent_artifact_id: str
    parent_title: str = ""
    author_id: str = ""
    author_name: str = ""
    first_artifact_uri: str
    last_artifact_uri: str
    started_at: datetime
    ended_at: datetime
    record_count: int = Field(ge=1, le=8)
    text: str
    embed: bool


class ArtifactVectorSearchResult(StrictModel):
    hits: list[ArtifactSearchHit] = Field(default_factory=list)
    available: bool = False
    stale: bool = False
    backend: str = "exact"
    candidate_count: int = Field(default=0, ge=0)


class ArtifactIntegrityResult(StrictModel):
    path: Path
    ok: bool
    quick_check: str
    foreign_key_violations: int = Field(ge=0)
    artifacts: int = Field(ge=0)
    active_artifacts: int = Field(ge=0)
    batches: int = Field(ge=0)
    events: int = Field(ge=0)
    objects: int = Field(ge=0)


class ArtifactBackupResult(StrictModel):
    path: Path
    byte_count: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    artifacts: int = Field(ge=0)
    active_artifacts: int = Field(ge=0)
    batches: int = Field(ge=0)
    events: int = Field(ge=0)
    objects: int = Field(ge=0)


class ArtifactRestoreResult(StrictModel):
    source: Path
    destination: Path
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    destination_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    byte_count: int = Field(ge=0)
    artifacts: int = Field(ge=0)
    batches: int = Field(ge=0)


class LegacyMigrationPlan(StrictModel):
    source: str
    source_instance: str
    database_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    note_manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    conversations: int = Field(ge=0)
    messages: int = Field(ge=0)
    attachments: int = Field(ge=0)
    meetings: int = Field(ge=0)
    meeting_notes: int = Field(ge=0)
    chat_notes: int = Field(ge=0)
    transcript_cues: int = Field(ge=0)
    unresolved_identities: int = Field(ge=0)
    duplicate_natural_keys: int = Field(ge=0)


class LegacyMigrationReceipt(LegacyMigrationPlan):
    batch_id: str
    accepted_events: int = Field(ge=0)
    unchanged_events: int = Field(ge=0)
    source_files_changed: int = Field(ge=0)
    verified: bool
