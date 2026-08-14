from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from ai_memory_mcp.config import Settings

from .context import active_context_rows
from .identity import artifact_id, canonical_json, event_id, sha256_text
from .models import (
    ArtifactEvent,
    ArtifactIngestReceipt,
    ArtifactPayload,
    ArtifactReference,
    ParsedArtifactBatch,
    RedactionPayload,
    StoredObject,
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
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _payload_document(
    payload: ArtifactPayload | RedactionPayload | None,
) -> dict[str, Any] | None:
    if payload is None:
        return None
    document = payload.model_dump(mode="json", by_alias=True)
    if isinstance(payload, ArtifactPayload):
        object_input = document.get("object")
        if isinstance(object_input, dict):
            object_input.pop("local_source_path", None)
    return document


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
        replayed = self._replayed_batch_receipt(batch)
        if replayed is not None:
            return replayed
        redacted_artifacts = self._redacted_artifact_ids(batch)
        batch, prepared_objects = self._prepare_objects(batch, redacted_artifacts)
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
        redacted_object_hashes: set[str] = set()
        quarantined_objects: list[tuple[Path, str]] = []

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
                    if event.operation == "redact":
                        redacted_object_hashes.update(
                            self._artifact_object_hashes(
                                connection,
                                artifact_id(
                                    manifest.source,
                                    manifest.source_instance,
                                    event.entity,
                                    event.external_id,
                                ),
                            )
                        )
                    result = self._apply_event(
                        connection,
                        batch,
                        event,
                        ordinal,
                    )
                    (
                        disposition,
                        event_value,
                        artifact_value,
                        material_changed,
                        distillation_relevant,
                        prior_parent_value,
                    ) = result
                    prepared = prepared_objects.get(ordinal)
                    if prepared is not None:
                        # Revision payloads identify their object digest. Keep
                        # matching metadata for stale and conflict evidence too.
                        self._record_object(connection, prepared[0])
                    if disposition == "accepted":
                        counters["accepted"] += 1
                        # A later redaction in this transaction owns final
                        # object cleanup. Do not require its skipped handoff.
                        if artifact_value not in redacted_artifacts:
                            connection.execute(
                                "DELETE FROM artifact_object_links "
                                "WHERE artifact_id = ?",
                                (artifact_value,),
                            )
                            if prepared is not None:
                                self._link_object(
                                    connection,
                                    artifact_value,
                                    prepared[0].sha256,
                                    prepared[1],
                                )
                            elif isinstance(event.payload, ArtifactPayload):
                                self._link_recorded_object(
                                    connection,
                                    artifact_value,
                                    event.payload,
                                )
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
                        if prepared is not None:
                            redacted_object_hashes.add(prepared[0].sha256)
                    if material_changed:
                        changed += 1
                    if disposition == "tombstone" and material_changed:
                        changed += self._tombstone_descendants(
                            connection,
                            batch,
                            artifact_value,
                            event_value,
                            counters,
                        )
                    if material_changed and distillation_relevant:
                        self._remember_distillation_root(
                            connection,
                            event,
                            artifact_value,
                            event_value,
                            distillation_roots,
                        )
                        self._remember_prior_parent_root(
                            connection,
                            event.entity,
                            prior_parent_value,
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
                quarantined_objects = self._quarantine_unreferenced_objects(
                    connection,
                    redacted_object_hashes,
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
                self._restore_quarantined_objects(quarantined_objects)
                raise
            else:
                try:
                    connection.commit()
                except BaseException:
                    self._restore_quarantined_objects(quarantined_objects)
                    raise

        return ArtifactIngestReceipt(
            batch_id=manifest.batch_id,
            artifacts_changed=changed,
            **counters,
        )

    def _replayed_batch_receipt(
        self,
        batch: ParsedArtifactBatch,
    ) -> ArtifactIngestReceipt | None:
        # Receipt lookup must precede object handoff reads. Producers may move
        # a handoff file after the first accepted intake.
        with connect_artifact_db(
            self.settings.artifact_db,
            read_only=True,
        ) as connection:
            prior = connection.execute(
                "SELECT * FROM artifact_batches WHERE batch_id = ?",
                (batch.manifest.batch_id,),
            ).fetchone()
        if prior is None:
            return None
        if prior["input_sha256"] != batch.input_sha256:
            raise ValueError("The artifact batch ID already has different input.")
        return self._receipt_from_batch(prior)

    def _redacted_artifact_ids(self, batch: ParsedArtifactBatch) -> set[str]:
        manifest = batch.manifest
        values = {
            artifact_id(
                manifest.source,
                manifest.source_instance,
                event.entity,
                event.external_id,
            )
            for event in batch.events
        }
        redacted: set[str] = set()
        if values:
            placeholders = ",".join("?" for _ in values)
            with connect_artifact_db(
                self.settings.artifact_db,
                read_only=True,
            ) as connection:
                redacted.update(
                    str(row[0])
                    for row in connection.execute(
                        f"SELECT artifact_id FROM artifacts "
                        f"WHERE redacted_at IS NOT NULL "
                        f"AND artifact_id IN ({placeholders})",
                        tuple(values),
                    )
                )
        # A redaction earlier in one batch must block later handoff reads for
        # the same artifact before the database transaction starts.
        for event in batch.events:
            value = artifact_id(
                manifest.source,
                manifest.source_instance,
                event.entity,
                event.external_id,
            )
            if event.operation == "redact":
                redacted.add(value)
        return redacted

    def _prepare_objects(
        self,
        batch: ParsedArtifactBatch,
        redacted_artifacts: set[str],
    ) -> tuple[ParsedArtifactBatch, dict[int, tuple[StoredObject, str]]]:
        from .objects import store_object

        prepared_batch = batch.model_copy(deep=True)
        prepared: dict[int, tuple[StoredObject, str]] = {}
        for ordinal, event in enumerate(prepared_batch.events):
            value = artifact_id(
                batch.manifest.source,
                batch.manifest.source_instance,
                event.entity,
                event.external_id,
            )
            if value in redacted_artifacts:
                continue
            payload = event.payload
            if not isinstance(payload, ArtifactPayload) or payload.object is None:
                continue
            object_input = payload.object
            source_path = object_input.local_source_path
            if source_path is None:
                continue
            stored = store_object(
                self.settings,
                source_path,
                expected_sha256=object_input.expected_sha256,
            )
            media_type = object_input.media_type or stored.media_type
            if media_type != stored.media_type:
                stored = stored.model_copy(update={"media_type": media_type})
            original_name = object_input.original_name or source_path.name
            payload.object = object_input.model_copy(
                update={
                    "local_source_path": None,
                    "expected_sha256": stored.sha256,
                    "media_type": media_type or None,
                    "original_name": original_name,
                }
            )
            prepared[ordinal] = (stored, original_name)
        return prepared_batch, prepared

    def _quarantine_unreferenced_objects(
        self,
        connection: sqlite3.Connection,
        hashes: set[str],
    ) -> list[tuple[Path, str]]:
        from .objects import quarantine_object

        moved: list[tuple[Path, str]] = []
        try:
            for digest in sorted(hashes):
                referenced = connection.execute(
                    "SELECT 1 FROM artifact_object_links WHERE sha256 = ? LIMIT 1",
                    (digest,),
                ).fetchone()
                if referenced is not None or self._object_has_retained_revision(
                    connection,
                    digest,
                ):
                    continue
                row = connection.execute(
                    "SELECT relative_path FROM artifact_objects WHERE sha256 = ?",
                    (digest,),
                ).fetchone()
                relative_path = (
                    str(row["relative_path"])
                    if row is not None
                    else f"sha256/{digest[:2]}/{digest}"
                )
                active_path = self.settings.artifact_objects_dir / relative_path
                if active_path.is_file():
                    quarantine_path = quarantine_object(
                        self.settings,
                        digest,
                        relative_path,
                    )
                    # Track the move before the SQL write so either failure path
                    # can restore the filesystem to the active database state.
                    moved.append((quarantine_path, relative_path))
                connection.execute(
                    "DELETE FROM artifact_objects WHERE sha256 = ?",
                    (digest,),
                )
        except BaseException:
            self._restore_quarantined_objects(moved)
            raise
        return moved

    @staticmethod
    def _artifact_object_hashes(
        connection: sqlite3.Connection,
        artifact_value: str,
    ) -> set[str]:
        """Return current and revision object hashes before redaction scrubs them."""
        hashes = {
            str(row[0])
            for row in connection.execute(
                "SELECT sha256 FROM artifact_object_links WHERE artifact_id = ?",
                (artifact_value,),
            )
        }
        for row in connection.execute(
            "SELECT payload_json FROM artifact_events "
            "WHERE artifact_id = ? AND payload_json IS NOT NULL",
            (artifact_value,),
        ):
            digest = ArtifactStore._payload_object_hash(row[0])
            if digest is not None:
                hashes.add(digest)
        return hashes

    @staticmethod
    def _object_has_retained_revision(
        connection: sqlite3.Connection,
        digest: str,
    ) -> bool:
        # A non-redacted artifact can still cite an older object revision after
        # another artifact removes its current link to the same digest.
        for row in connection.execute(
            "SELECT event.payload_json FROM artifact_events AS event "
            "JOIN artifacts AS artifact USING(artifact_id) "
            "WHERE artifact.redacted_at IS NULL "
            "AND event.payload_json IS NOT NULL"
        ):
            if ArtifactStore._payload_object_hash(row[0]) == digest:
                return True
        return False

    @staticmethod
    def _payload_object_hash(payload_json: object) -> str | None:
        try:
            payload = json.loads(str(payload_json))
        except (TypeError, ValueError):
            return None
        object_payload = payload.get("object") if isinstance(payload, dict) else None
        digest = (
            object_payload.get("expected_sha256")
            if isinstance(object_payload, dict)
            else None
        )
        if (
            isinstance(digest, str)
            and len(digest) == 64
            and all(character in "0123456789abcdef" for character in digest)
        ):
            return digest
        return None

    def _restore_quarantined_objects(
        self,
        moved: list[tuple[Path, str]],
    ) -> None:
        from .objects import restore_quarantined_object

        for quarantine_path, relative_path in reversed(moved):
            restore_quarantined_object(
                self.settings,
                quarantine_path,
                relative_path,
            )

    @staticmethod
    def _record_object(
        connection: sqlite3.Connection,
        stored: StoredObject,
    ) -> None:
        verified_at = _utc_now()
        connection.execute(
            """
            INSERT INTO artifact_objects(
                sha256, byte_count, media_type, relative_path,
                first_observed_at, last_verified_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(sha256) DO UPDATE SET
                byte_count = excluded.byte_count,
                media_type = excluded.media_type,
                relative_path = excluded.relative_path,
                last_verified_at = excluded.last_verified_at
            """,
            (
                stored.sha256,
                stored.byte_count,
                stored.media_type,
                stored.relative_path,
                verified_at,
                verified_at,
            ),
        )

    @staticmethod
    def _link_object(
        connection: sqlite3.Connection,
        artifact_value: str,
        digest: str,
        original_name: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO artifact_object_links(
                artifact_id, sha256, relation, original_name
            ) VALUES (?, ?, 'content', ?)
            """,
            (artifact_value, digest, original_name),
        )

    def _link_recorded_object(
        self,
        connection: sqlite3.Connection,
        artifact_value: str,
        payload: ArtifactPayload,
    ) -> None:
        """Re-link an accepted metadata-only snapshot to verified stored bytes."""
        from .objects import verify_object

        object_input = payload.object
        digest = object_input.expected_sha256 if object_input is not None else None
        if digest is None:
            return
        row = connection.execute(
            "SELECT byte_count FROM artifact_objects WHERE sha256 = ?",
            (digest,),
        ).fetchone()
        if row is None:
            raise ValueError("The accepted object hash is not in object storage.")
        verification = verify_object(self.settings, digest)
        if not verification.ok or verification.byte_count != int(row["byte_count"]):
            raise ValueError("The accepted object hash failed stored-object verification.")
        connection.execute(
            "UPDATE artifact_objects SET last_verified_at = ? WHERE sha256 = ?",
            (_utc_now(), digest),
        )
        self._link_object(
            connection,
            artifact_value,
            digest,
            object_input.original_name or "",
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
        bool,
        str | None,
    ]:
        manifest = batch.manifest
        artifact_value = artifact_id(
            manifest.source,
            manifest.source_instance,
            event.entity,
            event.external_id,
        )
        parent_value = self._resolve_parent(connection, manifest, event)
        payload_document = _payload_document(event.payload)
        payload_json = canonical_json(payload_document)
        payload_sha256 = sha256_text(payload_json)
        event_value = event_id(
            manifest.source,
            manifest.source_instance,
            {
                "entity": event.entity,
                "external_id": event.external_id,
                "parent_artifact_id": parent_value,
                "operation": event.operation,
                "source_sequence": event.source_sequence,
                "source_updated_at": _iso(event.source_updated_at),
                "source_version": event.source_version,
                "payload_sha256": payload_sha256,
            },
        )
        existing_event = connection.execute(
            "SELECT artifact_id, payload_json FROM artifact_events WHERE event_id = ?",
            (event_value,),
        ).fetchone()
        if existing_event is not None:
            current = connection.execute(
                "SELECT * FROM artifacts WHERE artifact_id = ?",
                (artifact_value,),
            ).fetchone()
            can_restore_coverage = bool(
                current is not None
                and current["deleted_at"] is not None
                and current["redacted_at"] is None
                and event.operation == "upsert"
                and isinstance(event.payload, ArtifactPayload)
                and existing_event["payload_json"] is not None
                and _parse_time(str(_iso(manifest.observed_at)))
                > _parse_time(str(current["deleted_at"]))
            )
            if can_restore_coverage:
                last_event = connection.execute(
                    "SELECT source_version FROM artifact_events WHERE event_id = ?",
                    (current["last_event_id"],),
                ).fetchone()
                can_restore_coverage = bool(
                    last_event is not None
                    and str(last_event["source_version"] or "").startswith(
                        ("coverage:", "cascade:")
                    )
                )
            if can_restore_coverage:
                prior_parent_value = (
                    str(current["parent_artifact_id"])
                    if current["parent_artifact_id"] is not None
                    else None
                )
                prior_root_parent_value = (
                    prior_parent_value
                    if prior_parent_value is not None
                    and prior_parent_value != parent_value
                    else None
                )
                fields = _payload_fields(event.payload)
                observed_at = _iso(manifest.observed_at)
                # Reuse the existing raw revision. The new batch only restores
                # current materialized state after a later authoritative sighting.
                connection.execute(
                    """
                    UPDATE artifacts
                    SET parent_artifact_id = ?, title = ?, author_id = ?,
                        author_name = ?, author_id_confidence = ?,
                        occurred_at = ?, source_updated_at = ?,
                        source_version = ?, source_sequence = ?,
                        text_content = ?, content_format = ?, payload_json = ?,
                        payload_sha256 = ?, last_observed_at = ?,
                        last_event_id = ?, deleted_at = NULL
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
                connection.execute(
                    """
                    INSERT INTO artifact_batch_events(
                        batch_id, ordinal, event_id, disposition
                    ) VALUES (?, ?, ?, 'accepted')
                    """,
                    (manifest.batch_id, ordinal, event_value),
                )
                return (
                    "accepted",
                    event_value,
                    artifact_value,
                    True,
                    not self._is_system_only_change(current, event),
                    prior_root_parent_value,
                )
            connection.execute(
                """
                INSERT INTO artifact_batch_events(
                    batch_id, ordinal, event_id, disposition
                ) VALUES (?, ?, ?, 'unchanged')
                """,
                (manifest.batch_id, ordinal, event_value),
            )
            return (
                "unchanged",
                event_value,
                str(existing_event[0]),
                False,
                False,
                None,
            )

        current = connection.execute(
            "SELECT * FROM artifacts WHERE artifact_id = ?",
            (artifact_value,),
        ).fetchone()
        prior_parent_value = (
            str(current["parent_artifact_id"])
            if current is not None and current["parent_artifact_id"] is not None
            else None
        )
        effective_parent_value = parent_value
        if event.operation != "upsert" and effective_parent_value is None:
            effective_parent_value = prior_parent_value
        prior_root_parent_value = (
            prior_parent_value
            if prior_parent_value is not None
            and prior_parent_value != effective_parent_value
            else None
        )
        distillation_relevant = not self._is_system_only_change(current, event)
        if (
            current is not None
            and current["redacted_at"] is not None
            and event.operation != "redact"
        ):
            # Redaction is durable. Later provider replays retain ordering and
            # hashes for audit, but they cannot restore raw revision payloads.
            self._insert_event(
                connection,
                batch,
                event,
                artifact_value,
                parent_value,
                event_value,
                None,
                payload_sha256,
            )
            connection.execute(
                """
                INSERT INTO artifact_batch_events(
                    batch_id, ordinal, event_id, disposition
                ) VALUES (?, ?, ?, 'redacted')
                """,
                (manifest.batch_id, ordinal, event_value),
            )
            return (
                "redacted",
                event_value,
                artifact_value,
                True,
                False,
                None,
            )
        if current is not None and event.operation != "redact":
            current_delete_is_coverage = False
            if current["deleted_at"] is not None:
                last_event = connection.execute(
                    "SELECT source_version FROM artifact_events WHERE event_id = ?",
                    (current["last_event_id"],),
                ).fetchone()
                current_delete_is_coverage = bool(
                    last_event is not None
                    and str(last_event["source_version"] or "").startswith(
                        ("coverage:", "cascade:")
                    )
                )
            # A provider snapshot observed before a tombstone cannot restore
            # that tombstone, even when it carries a previously unseen edit.
            if (
                event.operation == "upsert"
                and current_delete_is_coverage
                and _parse_time(str(_iso(manifest.observed_at)))
                <= _parse_time(str(current["deleted_at"]))
            ):
                ordering = "stale"
            else:
                ordering = self._compare_ordering(current, event, payload_sha256)
            if ordering in {"stale", "conflict"}:
                self._insert_event(
                    connection,
                    batch,
                    event,
                    artifact_value,
                    parent_value,
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
                return (
                    ordering,
                    event_value,
                    artifact_value,
                    False,
                    False,
                    None,
                )

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
            parent_value,
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
        return (
            disposition,
            event_value,
            artifact_value,
            True,
            distillation_relevant,
            prior_root_parent_value,
        )

    @staticmethod
    def _is_system_only_change(
        current: sqlite3.Row | None,
        event: ArtifactEvent,
    ) -> bool:
        if event.entity != "message":
            return False
        current_is_system = bool(
            current is not None
            and ArtifactStore._payload_is_system(str(current["payload_json"]))
        )
        if isinstance(event.payload, ArtifactPayload):
            incoming_is_system = (
                event.payload.classification or ""
            ).casefold() == "system"
            # A normal-to-system correction removes prior summary evidence.
            # Only a new system row or a system-to-system edit is irrelevant.
            return incoming_is_system and (current is None or current_is_system)
        return current_is_system

    @staticmethod
    def _payload_is_system(payload_json: str) -> bool:
        try:
            payload = json.loads(payload_json)
        except (json.JSONDecodeError, TypeError):
            return False
        return (
            isinstance(payload, dict)
            and str(payload.get("classification") or "").casefold() == "system"
        )

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
        child_value = artifact_id(
            manifest.source,
            manifest.source_instance,
            event.entity,
            event.external_id,
        )
        creates_cycle = connection.execute(
            """
            WITH RECURSIVE ancestors(artifact_id) AS (
                SELECT ?
                UNION
                SELECT parent.parent_artifact_id
                FROM artifacts AS parent
                JOIN ancestors AS child
                  ON parent.artifact_id = child.artifact_id
                WHERE parent.parent_artifact_id IS NOT NULL
            )
            SELECT 1 FROM ancestors WHERE artifact_id = ? LIMIT 1
            """,
            (parent_value, child_value),
        ).fetchone()
        if creates_cycle is not None:
            raise ValueError("Artifact parent would create a hierarchy cycle.")
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
        parent_value: str | None,
        event_value: str,
        payload_json: str | None,
        payload_sha256: str,
    ) -> None:
        manifest = batch.manifest
        connection.execute(
            """
            INSERT INTO artifact_events(
                event_id, first_batch_id, artifact_id, source,
                source_instance, entity, external_id, operation,
                source_version, source_sequence, source_updated_at,
                observed_at, payload_json, payload_sha256,
                parent_artifact_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                parent_value,
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

    def _tombstone_descendants(
        self,
        connection: sqlite3.Connection,
        batch: ParsedArtifactBatch,
        root_artifact_id: str,
        root_event_id: str,
        counters: dict[str, int],
    ) -> int:
        rows = connection.execute(
            """
            WITH RECURSIVE descendants(
                artifact_id, external_id, entity, parent_artifact_id, depth
            ) AS (
                SELECT artifact_id, external_id, entity,
                       parent_artifact_id, 1
                FROM artifacts
                WHERE parent_artifact_id = ?
                  AND deleted_at IS NULL AND redacted_at IS NULL
                UNION ALL
                SELECT child.artifact_id, child.external_id, child.entity,
                       child.parent_artifact_id, parent.depth + 1
                FROM artifacts AS child
                JOIN descendants AS parent
                  ON child.parent_artifact_id = parent.artifact_id
                WHERE child.deleted_at IS NULL AND child.redacted_at IS NULL
            )
            SELECT descendant.*, parent.entity AS parent_entity,
                   parent.external_id AS parent_external_id
            FROM descendants AS descendant
            JOIN artifacts AS parent
              ON parent.artifact_id = descendant.parent_artifact_id
            ORDER BY descendant.depth, descendant.artifact_id
            """,
            (root_artifact_id,),
        ).fetchall()
        tombstoned_at = _iso(batch.manifest.observed_at)
        for row in rows:
            parent = ArtifactReference(
                entity=str(row["parent_entity"]),
                external_id=str(row["parent_external_id"]),
            )
            event = ArtifactEvent.model_validate(
                {
                    "schema": "ai-memory/artifact-event@1",
                    "record": "event",
                    "entity": row["entity"],
                    "operation": "delete",
                    "external_id": row["external_id"],
                    "parent": parent,
                    "source_version": f"cascade:{root_event_id}",
                }
            )
            payload_sha256 = sha256_text(canonical_json(None))
            descendant_event_id = event_id(
                batch.manifest.source,
                batch.manifest.source_instance,
                {
                    "entity": event.entity,
                    "external_id": event.external_id,
                    "parent_artifact_id": row["parent_artifact_id"],
                    "operation": "delete",
                    "source_version": event.source_version,
                    "payload_sha256": payload_sha256,
                },
            )
            self._insert_event(
                connection,
                batch,
                event,
                str(row["artifact_id"]),
                str(row["parent_artifact_id"]),
                descendant_event_id,
                None,
                payload_sha256,
            )
            connection.execute(
                "UPDATE artifacts SET deleted_at = ?, last_observed_at = ?, "
                "last_event_id = ? WHERE artifact_id = ?",
                (
                    tombstoned_at,
                    tombstoned_at,
                    descendant_event_id,
                    row["artifact_id"],
                ),
            )
            self._clear_current_relations(connection, str(row["artifact_id"]))
            counters["tombstones"] += 1
        return len(rows)

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
        seen_claims: set[tuple[str, str, str, str, str]] = set()
        for claim in manifest.coverage:
            if not claim.complete:
                continue
            claim_key = (
                claim.parent.entity,
                claim.parent.external_id,
                claim.entity,
                _iso(claim.covered_from) or "",
                _iso(claim.covered_to) or "",
            )
            if claim_key in seen_claims:
                continue
            seen_claims.add(claim_key)
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
                # A delayed snapshot cannot remove state observed after that snapshot.
                "last_observed_at <= ?",
            ]
            parameters: list[Any] = [
                parent_value,
                claim.entity,
                _iso(manifest.observed_at),
            ]
            if claim.covered_from is not None:
                conditions.append("occurred_at >= ?")
                parameters.append(_iso(claim.covered_from))
            if claim.covered_to is not None:
                conditions.append("occurred_at <= ?")
                parameters.append(_iso(claim.covered_to))
            rows = connection.execute(
                "SELECT artifact_id, external_id, last_event_id, payload_json "
                "FROM artifacts WHERE "
                + " AND ".join(conditions),
                parameters,
            ).fetchall()
            tombstoned_at = _iso(manifest.observed_at)
            for row in rows:
                if row["artifact_id"] in present:
                    continue
                # Coverage is provider evidence, not an input event. Materialize
                # its delete revision without adding a false input ordinal.
                coverage_event = ArtifactEvent.model_validate(
                    {
                        "schema": "ai-memory/artifact-event@1",
                        "record": "event",
                        "entity": claim.entity,
                        "operation": "delete",
                        "external_id": row["external_id"],
                        "parent": claim.parent,
                        "source_version": f"coverage:{manifest.batch_id}",
                    }
                )
                payload_sha256 = sha256_text(canonical_json(None))
                tombstone_event = event_id(
                    manifest.source,
                    manifest.source_instance,
                    {
                        "entity": claim.entity,
                        "external_id": row["external_id"],
                        "parent_artifact_id": parent_value,
                        "operation": "delete",
                        "source_version": coverage_event.source_version,
                        "payload_sha256": payload_sha256,
                    },
                )
                self._insert_event(
                    connection,
                    batch,
                    coverage_event,
                    str(row["artifact_id"]),
                    parent_value,
                    tombstone_event,
                    None,
                    payload_sha256,
                )
                connection.execute(
                    "UPDATE artifacts SET deleted_at = ?, last_observed_at = ?, "
                    "last_event_id = ? "
                    "WHERE artifact_id = ?",
                    (
                        tombstoned_at,
                        tombstoned_at,
                        tombstone_event,
                        row["artifact_id"],
                    ),
                )
                self._clear_current_relations(connection, str(row["artifact_id"]))
                counters["tombstones"] += 1
                changed += 1
                changed += self._tombstone_descendants(
                    connection,
                    batch,
                    str(row["artifact_id"]),
                    tombstone_event,
                    counters,
                )
                if not self._payload_is_system(str(row["payload_json"])):
                    self._remember_root_for_covered_child(
                        connection,
                        parent_value,
                        claim.entity,
                        tombstone_event,
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
                    conversation = str(row[0])
                    roots[conversation] = event_value
                    self._remember_related_meetings(
                        connection,
                        conversation,
                        event_value,
                        roots,
                    )
            return
        if event.entity == "conversation":
            self._remember_related_meetings(
                connection,
                artifact_value,
                event_value,
                roots,
            )
            return
        if event.entity == "attachment":
            owner = self._nearest_distillation_root(connection, artifact_value)
            if owner is None:
                return
            entity, root = owner
            roots[root] = event_value
            if entity == "conversation":
                self._remember_related_meetings(
                    connection,
                    root,
                    event_value,
                    roots,
                )
            return
        if event.entity == "meeting":
            roots[artifact_value] = event_value
            return
        if event.entity in {"recording", "transcript", "transcript-cue"}:
            root = self._meeting_ancestor(connection, artifact_value)
            if root is not None:
                roots[root] = event_value

    def _remember_prior_parent_root(
        self,
        connection: sqlite3.Connection,
        entity: str,
        prior_parent_value: str | None,
        event_value: str,
        roots: dict[str, str],
    ) -> None:
        if prior_parent_value is None or entity not in {
            "message",
            "attachment",
            "recording",
            "transcript",
            "transcript-cue",
        }:
            return
        owner = self._nearest_distillation_root(connection, prior_parent_value)
        if owner is None:
            return
        root_entity, root = owner
        roots[root] = event_value
        if root_entity == "conversation":
            self._remember_related_meetings(
                connection,
                root,
                event_value,
                roots,
            )

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
            self._remember_related_meetings(
                connection,
                parent_value,
                event_value,
                roots,
            )
        elif entity == "attachment":
            owner = self._nearest_distillation_root(connection, parent_value)
            if owner is None:
                return
            root_entity, root = owner
            roots[root] = event_value
            if root_entity == "conversation":
                self._remember_related_meetings(
                    connection,
                    root,
                    event_value,
                    roots,
                )
        elif entity in {"recording", "transcript", "transcript-cue"}:
            root = self._meeting_ancestor(connection, parent_value)
            if parent[0] == "meeting":
                root = parent_value
            if root is not None:
                roots[root] = event_value

    @staticmethod
    def _remember_related_meetings(
        connection: sqlite3.Connection,
        conversation_value: str,
        event_value: str,
        roots: dict[str, str],
    ) -> None:
        rows = connection.execute(
            """
            SELECT link.source_artifact_id
            FROM artifact_links AS link
            JOIN artifacts AS meeting
              ON meeting.artifact_id = link.source_artifact_id
            WHERE link.relation = 'related-chat'
              AND link.target_artifact_id = ?
              AND meeting.entity = 'meeting'
              AND meeting.deleted_at IS NULL
              AND meeting.redacted_at IS NULL
            """,
            (conversation_value,),
        ).fetchall()
        for row in rows:
            roots[str(row[0])] = event_value

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
    def _nearest_distillation_root(
        connection: sqlite3.Connection,
        artifact_value: str,
    ) -> tuple[str, str] | None:
        current = artifact_value
        for _ in range(8):
            row = connection.execute(
                "SELECT entity, parent_artifact_id FROM artifacts "
                "WHERE artifact_id = ?",
                (current,),
            ).fetchone()
            if row is None:
                return None
            entity = str(row["entity"])
            if entity in {"conversation", "meeting"}:
                return entity, current
            if row["parent_artifact_id"] is None:
                return None
            current = str(row["parent_artifact_id"])
        raise ValueError("Artifact parent depth exceeds the supported limit.")

    @staticmethod
    def _conversation_ancestor(
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
            if row["entity"] == "conversation":
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
        root_row = connection.execute(
            "SELECT entity FROM artifacts WHERE artifact_id = ?",
            (root,),
        ).fetchone()
        if root_row is None:
            raise ValueError("The distillation root does not exist.")
        rows = sorted(
            active_context_rows(connection, root, str(root_row["entity"])),
            key=lambda row: str(row["artifact_id"]),
        )
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
