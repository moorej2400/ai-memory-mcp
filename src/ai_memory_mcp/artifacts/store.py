from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from ai_memory_mcp.config import Settings

from .identity import artifact_id, canonical_json, event_id, sha256_text
from .models import (
    ArtifactEvent,
    ArtifactIngestReceipt,
    ArtifactPayload,
    ParsedArtifactBatch,
    RedactionPayload,
)
from .schema import connect_artifact_db, migrate_artifact_db


@dataclass(frozen=True, slots=True)
class StoredArtifact:
    artifact_id: str
    source: str
    source_instance: str
    entity: str
    external_id: str
    parent_artifact_id: str | None
    title: str
    author_id: str
    author_name: str
    author_id_confidence: str
    occurred_at: str | None
    source_updated_at: str | None
    source_version: str | None
    source_sequence: int | None
    text_content: str
    content_format: str
    payload_json: str
    payload_sha256: str
    first_observed_at: str
    last_observed_at: str
    last_event_id: str
    deleted_at: str | None
    redacted_at: str | None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _payload_document(
    payload: ArtifactPayload | RedactionPayload | None,
) -> dict[str, Any] | None:
    if payload is None:
        return None
    return payload.model_dump(mode="json", by_alias=True)


def _payload_fields(payload: ArtifactPayload) -> dict[str, Any]:
    author = payload.author
    return {
        "title": payload.title or "",
        "author_id": author.id if author and author.id else "",
        "author_name": author.name if author and author.name else "",
        "author_id_confidence": (
            author.id_confidence if author and author.id_confidence else ""
        ),
        "occurred_at": _iso(payload.occurred_at),
        "text_content": payload.text or "",
        "content_format": payload.content_format or "",
    }


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class ArtifactStore:
    """Apply connector-neutral batches to canonical raw artifact state."""

    def __init__(self, settings: Settings):
        self.settings = settings
        migrate_artifact_db(settings)

    def apply_batch(self, batch: ParsedArtifactBatch) -> ArtifactIngestReceipt:
        manifest = batch.manifest
        counters = {
            "accepted": 0,
            "unchanged": 0,
            "stale": 0,
            "conflicts": 0,
            "tombstones": 0,
            "redactions": 0,
        }
        changed = 0
        pending_links: list[tuple[str, ArtifactPayload]] = []
        distillation_roots: dict[str, str] = {}

        with connect_artifact_db(self.settings.artifact_db) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                prior = connection.execute(
                    "SELECT * FROM artifact_batches WHERE batch_id = ?",
                    (manifest.batch_id,),
                ).fetchone()
                if prior is not None:
                    if prior["input_sha256"] != batch.input_sha256:
                        raise ValueError(
                            "The artifact batch ID already has different input."
                        )
                    connection.rollback()
                    return self._receipt_from_batch(prior)

                started_at = _utc_now()
                connection.execute(
                    """
                    INSERT INTO artifact_batches(
                        batch_id, source, source_instance, observed_at,
                        input_sha256, expected_events, status, started_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'processing', ?)
                    """,
                    (
                        manifest.batch_id,
                        manifest.source,
                        manifest.source_instance,
                        _iso(manifest.observed_at),
                        batch.input_sha256,
                        manifest.event_count,
                        started_at,
                    ),
                )

                event_artifacts: list[tuple[ArtifactEvent, str, str]] = []
                for ordinal, event in enumerate(batch.events):
                    result = self._apply_event(
                        connection,
                        batch,
                        event,
                        ordinal,
                    )
                    disposition, event_value, artifact_value, material_changed = result
                    if disposition == "accepted":
                        counters["accepted"] += 1
                    elif disposition == "unchanged":
                        counters["unchanged"] += 1
                    elif disposition == "stale":
                        counters["stale"] += 1
                    elif disposition == "conflict":
                        counters["conflicts"] += 1
                    elif disposition == "tombstone":
                        counters["tombstones"] += 1
                    elif disposition == "redacted":
                        counters["redactions"] += 1
                    if material_changed:
                        changed += 1
                        self._remember_distillation_root(
                            connection,
                            event,
                            artifact_value,
                            event_value,
                            distillation_roots,
                        )
                    if disposition == "accepted" and isinstance(
                        event.payload, ArtifactPayload
                    ):
                        pending_links.append((artifact_value, event.payload))
                    event_artifacts.append((event, artifact_value, event_value))

                self._replace_links(connection, manifest, pending_links)
                coverage_changed = self._apply_coverage(
                    connection,
                    batch,
                    event_artifacts,
                    counters,
                    distillation_roots,
                )
                changed += coverage_changed
                self._update_distillation_state(
                    connection,
                    distillation_roots,
                )
                if changed:
                    connection.execute(
                        """
                        UPDATE artifact_metadata
                        SET value = CAST(value AS INTEGER) + 1
                        WHERE key = 'change_counter'
                        """
                    )

                completed_at = _utc_now()
                connection.execute(
                    """
                    UPDATE artifact_batches
                    SET accepted_events = ?, unchanged_events = ?,
                        stale_events = ?, conflict_events = ?,
                        tombstones = ?, redactions = ?, status = 'ok',
                        completed_at = ?
                    WHERE batch_id = ?
                    """,
                    (
                        counters["accepted"],
                        counters["unchanged"],
                        counters["stale"],
                        counters["conflicts"],
                        counters["tombstones"],
                        counters["redactions"],
                        completed_at,
                        manifest.batch_id,
                    ),
                )
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

        return ArtifactIngestReceipt(
            batch_id=manifest.batch_id,
            artifacts_changed=changed,
            **counters,
        )

    def _apply_event(
        self,
        connection: sqlite3.Connection,
        batch: ParsedArtifactBatch,
        event: ArtifactEvent,
        ordinal: int,
    ) -> tuple[
        Literal[
            "accepted",
            "unchanged",
            "stale",
            "conflict",
            "tombstone",
            "redacted",
        ],
        str,
        str,
        bool,
    ]:
        manifest = batch.manifest
        artifact_value = artifact_id(
            manifest.source,
            manifest.source_instance,
            event.entity,
            event.external_id,
        )
        payload_document = _payload_document(event.payload)
        payload_json = canonical_json(payload_document)
        payload_sha256 = sha256_text(payload_json)
        event_value = event_id(
            manifest.source,
            manifest.source_instance,
            {
                "entity": event.entity,
                "external_id": event.external_id,
                "operation": event.operation,
                "source_sequence": event.source_sequence,
                "source_updated_at": _iso(event.source_updated_at),
                "source_version": event.source_version,
                "payload_sha256": payload_sha256,
            },
        )
        existing_event = connection.execute(
            "SELECT artifact_id FROM artifact_events WHERE event_id = ?",
            (event_value,),
        ).fetchone()
        if existing_event is not None:
            connection.execute(
                """
                INSERT INTO artifact_batch_events(
                    batch_id, ordinal, event_id, disposition
                ) VALUES (?, ?, ?, 'unchanged')
                """,
                (manifest.batch_id, ordinal, event_value),
            )
            return "unchanged", event_value, str(existing_event[0]), False

        parent_value = self._resolve_parent(connection, manifest, event)
        current = connection.execute(
            "SELECT * FROM artifacts WHERE artifact_id = ?",
            (artifact_value,),
        ).fetchone()
        if current is not None:
            ordering = self._compare_ordering(current, event, payload_sha256)
            if ordering in {"stale", "conflict"}:
                self._insert_event(
                    connection,
                    batch,
                    event,
                    artifact_value,
                    event_value,
                    payload_json,
                    payload_sha256,
                )
                connection.execute(
                    """
                    INSERT INTO artifact_batch_events(
                        batch_id, ordinal, event_id, disposition
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (manifest.batch_id, ordinal, event_value, ordering),
                )
                return ordering, event_value, artifact_value, False

        observed_at = _iso(manifest.observed_at)
        if event.operation == "upsert":
            assert isinstance(event.payload, ArtifactPayload)
            fields = _payload_fields(event.payload)
            if current is None:
                connection.execute(
                    """
                    INSERT INTO artifacts(
                        artifact_id, source, source_instance, entity,
                        external_id, parent_artifact_id, title, author_id,
                        author_name, author_id_confidence, occurred_at,
                        source_updated_at, source_version, source_sequence,
                        text_content, content_format, payload_json,
                        payload_sha256, first_observed_at, last_observed_at,
                        last_event_id
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        artifact_value,
                        manifest.source,
                        manifest.source_instance,
                        event.entity,
                        event.external_id,
                        parent_value,
                        fields["title"],
                        fields["author_id"],
                        fields["author_name"],
                        fields["author_id_confidence"],
                        fields["occurred_at"],
                        _iso(event.source_updated_at),
                        event.source_version,
                        event.source_sequence,
                        fields["text_content"],
                        fields["content_format"],
                        payload_json,
                        payload_sha256,
                        observed_at,
                        observed_at,
                        event_value,
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE artifacts
                    SET parent_artifact_id = ?, title = ?, author_id = ?,
                        author_name = ?, author_id_confidence = ?,
                        occurred_at = ?, source_updated_at = ?,
                        source_version = ?, source_sequence = ?,
                        text_content = ?, content_format = ?, payload_json = ?,
                        payload_sha256 = ?, last_observed_at = ?,
                        last_event_id = ?, deleted_at = NULL, redacted_at = NULL
                    WHERE artifact_id = ?
                    """,
                    (
                        parent_value,
                        fields["title"],
                        fields["author_id"],
                        fields["author_name"],
                        fields["author_id_confidence"],
                        fields["occurred_at"],
                        _iso(event.source_updated_at),
                        event.source_version,
                        event.source_sequence,
                        fields["text_content"],
                        fields["content_format"],
                        payload_json,
                        payload_sha256,
                        observed_at,
                        event_value,
                        artifact_value,
                    ),
                )
            self._replace_aliases(
                connection,
                manifest.source,
                manifest.source_instance,
                artifact_value,
                event.payload,
            )
            disposition = "accepted"
        elif event.operation == "delete":
            if current is None:
                self._insert_empty_artifact(
                    connection,
                    batch,
                    event,
                    artifact_value,
                    parent_value,
                    event_value,
                    payload_json,
                    payload_sha256,
                    deleted_at=observed_at,
                )
            else:
                connection.execute(
                    """
                    UPDATE artifacts
                    SET parent_artifact_id = COALESCE(?, parent_artifact_id),
                        source_updated_at = ?, source_version = ?,
                        source_sequence = ?, last_observed_at = ?,
                        last_event_id = ?, deleted_at = ?
                    WHERE artifact_id = ?
                    """,
                    (
                        parent_value,
                        _iso(event.source_updated_at),
                        event.source_version,
                        event.source_sequence,
                        observed_at,
                        event_value,
                        observed_at,
                        artifact_value,
                    ),
                )
            self._clear_current_relations(connection, artifact_value)
            disposition = "tombstone"
        else:
            if current is None:
                self._insert_empty_artifact(
                    connection,
                    batch,
                    event,
                    artifact_value,
                    parent_value,
                    event_value,
                    payload_json,
                    payload_sha256,
                    redacted_at=observed_at,
                )
            else:
                # Redaction intentionally mutates prior raw revisions. Identity,
                # ordering metadata, and hashes remain available for audit.
                connection.execute(
                    "UPDATE artifact_events SET payload_json = NULL "
                    "WHERE artifact_id = ?",
                    (artifact_value,),
                )
                connection.execute(
                    """
                    UPDATE artifacts
                    SET parent_artifact_id = COALESCE(?, parent_artifact_id),
                        title = '', author_id = '', author_name = '',
                        author_id_confidence = '', occurred_at = NULL,
                        text_content = '', content_format = '',
                        source_updated_at = ?, source_version = ?,
                        source_sequence = ?, payload_json = ?,
                        payload_sha256 = ?, last_observed_at = ?,
                        last_event_id = ?, deleted_at = NULL, redacted_at = ?
                    WHERE artifact_id = ?
                    """,
                    (
                        parent_value,
                        _iso(event.source_updated_at),
                        event.source_version,
                        event.source_sequence,
                        payload_json,
                        payload_sha256,
                        observed_at,
                        event_value,
                        observed_at,
                        artifact_value,
                    ),
                )
            self._clear_current_relations(connection, artifact_value)
            disposition = "redacted"

        self._insert_event(
            connection,
            batch,
            event,
            artifact_value,
            event_value,
            payload_json,
            payload_sha256,
        )
        connection.execute(
            """
            INSERT INTO artifact_batch_events(
                batch_id, ordinal, event_id, disposition
            ) VALUES (?, ?, ?, ?)
            """,
            (manifest.batch_id, ordinal, event_value, disposition),
        )
        return disposition, event_value, artifact_value, True

    @staticmethod
    def _resolve_parent(
        connection: sqlite3.Connection,
        manifest: Any,
        event: ArtifactEvent,
    ) -> str | None:
        if event.parent is None:
            return None
        parent_value = artifact_id(
            manifest.source,
            manifest.source_instance,
            event.parent.entity,
            event.parent.external_id,
        )
        exists = connection.execute(
            "SELECT 1 FROM artifacts WHERE artifact_id = ?",
            (parent_value,),
        ).fetchone()
        if exists is None:
            raise ValueError(
                f"Artifact parent does not exist before child {event.external_id}."
            )
        return parent_value

    @staticmethod
    def _compare_ordering(
        current: sqlite3.Row,
        event: ArtifactEvent,
        payload_sha256: str,
    ) -> Literal["newer", "stale", "conflict"]:
        current_sequence = current["source_sequence"]
        if current_sequence is not None and event.source_sequence is not None:
            if event.source_sequence < current_sequence:
                return "stale"
            if event.source_sequence > current_sequence:
                return "newer"
            return (
                "newer"
                if payload_sha256 == current["payload_sha256"]
                else "conflict"
            )

        current_time = current["source_updated_at"]
        if current_time is not None and event.source_updated_at is not None:
            incoming = event.source_updated_at
            if incoming.tzinfo is None:
                incoming = incoming.replace(tzinfo=timezone.utc)
            incoming = incoming.astimezone(timezone.utc)
            previous = _parse_time(current_time)
            if incoming < previous:
                return "stale"
            if incoming > previous:
                return "newer"
            return (
                "newer"
                if payload_sha256 == current["payload_sha256"]
                else "conflict"
            )

        if event.source_version == current["source_version"]:
            return (
                "newer"
                if payload_sha256 == current["payload_sha256"]
                else "conflict"
            )
        return "conflict"

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        batch: ParsedArtifactBatch,
        event: ArtifactEvent,
        artifact_value: str,
        event_value: str,
        payload_json: str,
        payload_sha256: str,
    ) -> None:
        manifest = batch.manifest
        connection.execute(
            """
            INSERT INTO artifact_events(
                event_id, first_batch_id, artifact_id, source,
                source_instance, entity, external_id, operation,
                source_version, source_sequence, source_updated_at,
                observed_at, payload_json, payload_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_value,
                manifest.batch_id,
                artifact_value,
                manifest.source,
                manifest.source_instance,
                event.entity,
                event.external_id,
                event.operation,
                event.source_version,
                event.source_sequence,
                _iso(event.source_updated_at),
                _iso(manifest.observed_at),
                payload_json,
                payload_sha256,
            ),
        )

    @staticmethod
    def _insert_empty_artifact(
        connection: sqlite3.Connection,
        batch: ParsedArtifactBatch,
        event: ArtifactEvent,
        artifact_value: str,
        parent_value: str | None,
        event_value: str,
        payload_json: str,
        payload_sha256: str,
        *,
        deleted_at: str | None = None,
        redacted_at: str | None = None,
    ) -> None:
        observed_at = _iso(batch.manifest.observed_at)
        connection.execute(
            """
            INSERT INTO artifacts(
                artifact_id, source, source_instance, entity, external_id,
                parent_artifact_id, source_updated_at, source_version,
                source_sequence, payload_json, payload_sha256,
                first_observed_at, last_observed_at, last_event_id,
                deleted_at, redacted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                artifact_value,
                batch.manifest.source,
                batch.manifest.source_instance,
                event.entity,
                event.external_id,
                parent_value,
                _iso(event.source_updated_at),
                event.source_version,
                event.source_sequence,
                payload_json,
                payload_sha256,
                observed_at,
                observed_at,
                event_value,
                deleted_at,
                redacted_at,
            ),
        )

    @staticmethod
    def _replace_aliases(
        connection: sqlite3.Connection,
        source: str,
        source_instance: str,
        artifact_value: str,
        payload: ArtifactPayload,
    ) -> None:
        connection.execute(
            "DELETE FROM artifact_aliases WHERE artifact_id = ?",
            (artifact_value,),
        )
        for alias in payload.aliases:
            connection.execute(
                """
                INSERT INTO artifact_aliases(
                    artifact_id, source, source_instance,
                    alias_kind, alias_value
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    artifact_value,
                    source,
                    source_instance,
                    alias.kind,
                    alias.value,
                ),
            )

    @staticmethod
    def _clear_current_relations(
        connection: sqlite3.Connection,
        artifact_value: str,
    ) -> None:
        connection.execute(
            "DELETE FROM artifact_aliases WHERE artifact_id = ?",
            (artifact_value,),
        )
        connection.execute(
            "DELETE FROM artifact_links WHERE source_artifact_id = ?",
            (artifact_value,),
        )
        connection.execute(
            "DELETE FROM artifact_object_links WHERE artifact_id = ?",
            (artifact_value,),
        )

    @staticmethod
    def _replace_links(
        connection: sqlite3.Connection,
        manifest: Any,
        pending: list[tuple[str, ArtifactPayload]],
    ) -> None:
        created_at = _utc_now()
        for source_value, payload in pending:
            connection.execute(
                "DELETE FROM artifact_links WHERE source_artifact_id = ?",
                (source_value,),
            )
            for link in payload.links:
                target_value = artifact_id(
                    manifest.source,
                    manifest.source_instance,
                    link.target.entity,
                    link.target.external_id,
                )
                target = connection.execute(
                    "SELECT 1 FROM artifacts WHERE artifact_id = ?",
                    (target_value,),
                ).fetchone()
                if target is None:
                    raise ValueError(
                        f"Artifact link target does not exist: {link.target.external_id}."
                    )
                connection.execute(
                    """
                    INSERT INTO artifact_links(
                        source_artifact_id, relation,
                        target_artifact_id, created_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (source_value, link.relation, target_value, created_at),
                )

    def _apply_coverage(
        self,
        connection: sqlite3.Connection,
        batch: ParsedArtifactBatch,
        event_artifacts: list[tuple[ArtifactEvent, str, str]],
        counters: dict[str, int],
        distillation_roots: dict[str, str],
    ) -> int:
        changed = 0
        manifest = batch.manifest
        for claim in manifest.coverage:
            if not claim.complete:
                continue
            parent_value = artifact_id(
                manifest.source,
                manifest.source_instance,
                claim.parent.entity,
                claim.parent.external_id,
            )
            parent = connection.execute(
                "SELECT 1 FROM artifacts WHERE artifact_id = ?",
                (parent_value,),
            ).fetchone()
            if parent is None:
                raise ValueError(
                    "A complete coverage claim requires an existing parent."
                )

            present: set[str] = set()
            for event, artifact_value, _ in event_artifacts:
                if event.entity != claim.entity:
                    continue
                row = connection.execute(
                    "SELECT parent_artifact_id FROM artifacts "
                    "WHERE artifact_id = ?",
                    (artifact_value,),
                ).fetchone()
                if row is not None and row[0] == parent_value:
                    present.add(artifact_value)

            conditions = [
                "parent_artifact_id = ?",
                "entity = ?",
                "deleted_at IS NULL",
                "redacted_at IS NULL",
            ]
            parameters: list[Any] = [parent_value, claim.entity]
            if claim.covered_from is not None:
                conditions.append("occurred_at >= ?")
                parameters.append(_iso(claim.covered_from))
            if claim.covered_to is not None:
                conditions.append("occurred_at <= ?")
                parameters.append(_iso(claim.covered_to))
            rows = connection.execute(
                "SELECT artifact_id, last_event_id FROM artifacts WHERE "
                + " AND ".join(conditions),
                parameters,
            ).fetchall()
            tombstoned_at = _iso(manifest.observed_at)
            for row in rows:
                if row["artifact_id"] in present:
                    continue
                connection.execute(
                    "UPDATE artifacts SET deleted_at = ?, last_observed_at = ? "
                    "WHERE artifact_id = ?",
                    (tombstoned_at, tombstoned_at, row["artifact_id"]),
                )
                counters["tombstones"] += 1
                changed += 1
                self._remember_root_for_covered_child(
                    connection,
                    parent_value,
                    claim.entity,
                    str(row["last_event_id"]),
                    distillation_roots,
                )

            connection.execute(
                """
                INSERT INTO artifact_coverage(
                    batch_id, parent_artifact_id, entity,
                    covered_from, covered_to, complete
                ) VALUES (?, ?, ?, ?, ?, 1)
                """,
                (
                    manifest.batch_id,
                    parent_value,
                    claim.entity,
                    _iso(claim.covered_from) or "",
                    _iso(claim.covered_to) or "",
                ),
            )
        return changed

    def _remember_distillation_root(
        self,
        connection: sqlite3.Connection,
        event: ArtifactEvent,
        artifact_value: str,
        event_value: str,
        roots: dict[str, str],
    ) -> None:
        if event.entity == "message":
            payload = event.payload
            if isinstance(payload, ArtifactPayload) and (
                payload.classification or ""
            ).casefold() == "system":
                return
            row = connection.execute(
                "SELECT parent_artifact_id FROM artifacts WHERE artifact_id = ?",
                (artifact_value,),
            ).fetchone()
            if row is not None and row[0] is not None:
                parent = connection.execute(
                    "SELECT entity FROM artifacts WHERE artifact_id = ?",
                    (row[0],),
                ).fetchone()
                if parent is not None and parent[0] == "conversation":
                    roots[str(row[0])] = event_value
            return
        if event.entity == "meeting":
            roots[artifact_value] = event_value
            return
        if event.entity in {"recording", "transcript", "transcript-cue"}:
            root = self._meeting_ancestor(connection, artifact_value)
            if root is not None:
                roots[root] = event_value

    def _remember_root_for_covered_child(
        self,
        connection: sqlite3.Connection,
        parent_value: str,
        entity: str,
        event_value: str,
        roots: dict[str, str],
    ) -> None:
        parent = connection.execute(
            "SELECT entity FROM artifacts WHERE artifact_id = ?",
            (parent_value,),
        ).fetchone()
        if parent is None:
            return
        if entity == "message" and parent[0] == "conversation":
            roots[parent_value] = event_value
        elif entity in {"recording", "transcript", "transcript-cue"}:
            root = self._meeting_ancestor(connection, parent_value)
            if parent[0] == "meeting":
                root = parent_value
            if root is not None:
                roots[root] = event_value

    @staticmethod
    def _meeting_ancestor(
        connection: sqlite3.Connection,
        artifact_value: str,
    ) -> str | None:
        current = artifact_value
        for _ in range(8):
            row = connection.execute(
                "SELECT entity, parent_artifact_id FROM artifacts "
                "WHERE artifact_id = ?",
                (current,),
            ).fetchone()
            if row is None:
                return None
            if row["entity"] == "meeting":
                return current
            if row["parent_artifact_id"] is None:
                return None
            current = str(row["parent_artifact_id"])
        raise ValueError("Artifact parent depth exceeds the supported limit.")

    @staticmethod
    def _source_digest(
        connection: sqlite3.Connection,
        root: str,
    ) -> str:
        rows = connection.execute(
            """
            WITH RECURSIVE descendants(artifact_id, payload_sha256) AS (
                SELECT artifact_id, payload_sha256
                FROM artifacts
                WHERE parent_artifact_id = ?
                  AND deleted_at IS NULL AND redacted_at IS NULL
                UNION ALL
                SELECT child.artifact_id, child.payload_sha256
                FROM artifacts AS child
                JOIN descendants AS parent
                  ON child.parent_artifact_id = parent.artifact_id
                WHERE child.deleted_at IS NULL AND child.redacted_at IS NULL
            )
            SELECT artifact_id, payload_sha256
            FROM descendants
            ORDER BY artifact_id
            """,
            (root,),
        ).fetchall()
        digest = hashlib.sha256()
        for row in rows:
            digest.update(str(row["artifact_id"]).encode("utf-8"))
            digest.update(b"\0")
            digest.update(str(row["payload_sha256"]).encode("ascii"))
            digest.update(b"\n")
        return digest.hexdigest()

    def _update_distillation_state(
        self,
        connection: sqlite3.Connection,
        roots: dict[str, str],
    ) -> None:
        updated_at = _utc_now()
        for root, latest_event in roots.items():
            source_digest = self._source_digest(connection, root)
            connection.execute(
                """
                INSERT INTO distillation_state(
                    artifact_id, status, latest_event_id,
                    latest_source_digest, updated_at
                ) VALUES (?, 'pending', ?, ?, ?)
                ON CONFLICT(artifact_id) DO UPDATE SET
                    status = 'pending',
                    latest_event_id = excluded.latest_event_id,
                    latest_source_digest = excluded.latest_source_digest,
                    updated_at = excluded.updated_at
                """,
                (root, latest_event, source_digest, updated_at),
            )

    @staticmethod
    def _receipt_from_batch(row: sqlite3.Row) -> ArtifactIngestReceipt:
        return ArtifactIngestReceipt(
            batch_id=str(row["batch_id"]),
            accepted=int(row["accepted_events"]),
            unchanged=int(row["unchanged_events"]),
            stale=int(row["stale_events"]),
            conflicts=int(row["conflict_events"]),
            tombstones=int(row["tombstones"]),
            redactions=int(row["redactions"]),
            artifacts_changed=(
                int(row["accepted_events"])
                + int(row["tombstones"])
                + int(row["redactions"])
            ),
            status="ok",
        )

    def count(self, entity: str | None = None) -> int:
        query = (
            "SELECT count(*) FROM artifacts "
            "WHERE deleted_at IS NULL AND redacted_at IS NULL"
        )
        parameters: tuple[str, ...] = ()
        if entity is not None:
            query += " AND entity = ?"
            parameters = (entity,)
        with connect_artifact_db(
            self.settings.artifact_db, read_only=True
        ) as connection:
            return int(connection.execute(query, parameters).fetchone()[0])

    def get_by_external_id(
        self,
        source: str,
        source_instance: str,
        entity: str,
        external_id: str,
    ) -> StoredArtifact:
        with connect_artifact_db(
            self.settings.artifact_db, read_only=True
        ) as connection:
            row = connection.execute(
                """
                SELECT * FROM artifacts
                WHERE source = ? AND source_instance = ?
                  AND entity = ? AND external_id = ?
                """,
                (source, source_instance, entity, external_id),
            ).fetchone()
        if row is None:
            raise KeyError(f"Artifact does not exist: {external_id}")
        values = {field: row[field] for field in StoredArtifact.__annotations__}
        return StoredArtifact(**values)

    def event_count(self, artifact_value: str) -> int:
        with connect_artifact_db(
            self.settings.artifact_db, read_only=True
        ) as connection:
            return int(
                connection.execute(
                    "SELECT count(*) FROM artifact_events WHERE artifact_id = ?",
                    (artifact_value,),
                ).fetchone()[0]
            )

    def revision_payloads(self, artifact_value: str) -> list[str | None]:
        with connect_artifact_db(
            self.settings.artifact_db, read_only=True
        ) as connection:
            return [
                row[0]
                for row in connection.execute(
                    "SELECT payload_json FROM artifact_events "
                    "WHERE artifact_id = ? ORDER BY rowid",
                    (artifact_value,),
                )
            ]

    def distillation_status(
        self,
        source: str,
        source_instance: str,
        entity: str,
        external_id: str,
    ) -> str | None:
        value = artifact_id(source, source_instance, entity, external_id)
        with connect_artifact_db(
            self.settings.artifact_db, read_only=True
        ) as connection:
            row = connection.execute(
                "SELECT status FROM distillation_state WHERE artifact_id = ?",
                (value,),
            ).fetchone()
        return None if row is None else str(row[0])

    def rebuild_fts(self) -> None:
        with connect_artifact_db(self.settings.artifact_db) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "INSERT INTO artifacts_fts(artifacts_fts) VALUES ('rebuild')"
                )
                rows = connection.execute(
                    """
                    SELECT rowid, title, author_name, text_content, external_id
                    FROM artifacts
                    WHERE deleted_at IS NOT NULL OR redacted_at IS NOT NULL
                    """
                ).fetchall()
                for row in rows:
                    connection.execute(
                        """
                        INSERT INTO artifacts_fts(
                            artifacts_fts, rowid, title, author_name,
                            text_content, external_id
                        ) VALUES ('delete', ?, ?, ?, ?, ?)
                        """,
                        tuple(row),
                    )
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()
