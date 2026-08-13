from __future__ import annotations

import base64
import json
import sqlite3
from datetime import datetime
from typing import Literal

from ai_memory_mcp.config import Settings
from ai_memory_mcp.text import fts_expression, tokenize

from .identity import (
    ARTIFACT_ID_PATTERN,
    artifact_uri,
    canonical_json,
    parse_artifact_uri,
)
from .models import (
    ArtifactReadRecord,
    ArtifactReadResponse,
    ArtifactScope,
    ArtifactSearchHit,
)
from .schema import connect_artifact_db, migrate_artifact_db

ReadDirection = Literal["around", "before", "after"]


def _parent_id(reference: str) -> str:
    if reference.startswith("artifact://"):
        return parse_artifact_uri(reference)[1]
    if not ARTIFACT_ID_PATTERN.fullmatch(reference):
        raise ValueError("The artifact parent reference has an invalid format.")
    return reference


def _cursor_encode(row: sqlite3.Row) -> str:
    payload = canonical_json(
        {
            "artifact_id": str(row["artifact_id"]),
            "occurred_at": str(row["occurred_at"] or ""),
        }
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _cursor_decode(value: str) -> tuple[str, str]:
    try:
        padding = "=" * (-len(value) % 4)
        payload = json.loads(
            base64.urlsafe_b64decode(value + padding).decode("utf-8")
        )
        artifact_value = payload["artifact_id"]
        occurred_at = payload["occurred_at"]
    except (KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
        raise ValueError("The artifact cursor has an invalid format.") from exc
    if (
        not isinstance(artifact_value, str)
        or not ARTIFACT_ID_PATTERN.fullmatch(artifact_value)
        or not isinstance(occurred_at, str)
    ):
        raise ValueError("The artifact cursor has an invalid format.")
    return occurred_at, artifact_value


def _comparison(
    operator: Literal["<", ">"],
) -> str:
    return (
        f"(COALESCE(a.occurred_at, '') {operator} ? OR "
        f"(COALESCE(a.occurred_at, '') = ? AND a.artifact_id {operator} ?))"
    )


class ArtifactSearch:
    """Search and read canonical raw artifacts without using Markdown state."""

    def __init__(self, settings: Settings):
        self.settings = settings
        migrate_artifact_db(settings)

    def search(
        self,
        query: str,
        scope: ArtifactScope | None = None,
        limit: int = 20,
    ) -> list[ArtifactSearchHit]:
        if limit <= 0:
            raise ValueError("The artifact search limit must be positive.")
        limit = min(limit, 100)
        if not tokenize(query):
            return []
        selected_scope = scope or ArtifactScope()
        conditions = [
            "artifacts_fts MATCH ?",
            "a.deleted_at IS NULL",
            "a.redacted_at IS NULL",
        ]
        parameters: list[object] = [fts_expression(query)]
        if selected_scope.source is not None:
            conditions.append("a.source = ?")
            parameters.append(selected_scope.source)
        if selected_scope.source_instance is not None:
            conditions.append("a.source_instance = ?")
            parameters.append(selected_scope.source_instance)
        if selected_scope.entities:
            placeholders = ", ".join("?" for _ in selected_scope.entities)
            conditions.append(f"a.entity IN ({placeholders})")
            parameters.extend(selected_scope.entities)
        if selected_scope.parent is not None:
            conditions.append("a.parent_artifact_id = ?")
            parameters.append(_parent_id(selected_scope.parent))
        if selected_scope.date_from is not None:
            conditions.append("a.occurred_at >= ?")
            parameters.append(selected_scope.date_from.isoformat())
        if selected_scope.date_to is not None:
            conditions.append("a.occurred_at <= ?")
            parameters.append(selected_scope.date_to.isoformat())
        parameters.append(limit)

        with connect_artifact_db(
            self.settings.artifact_db,
            read_only=True,
        ) as connection:
            rows = connection.execute(
                f"""
                SELECT a.*, bm25(
                    artifacts_fts, 2.0, 1.0, 5.0, 1.0
                ) AS lexical_score
                FROM artifacts_fts
                JOIN artifacts AS a ON a.rowid = artifacts_fts.rowid
                WHERE {' AND '.join(conditions)}
                ORDER BY lexical_score, a.artifact_id
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [self._search_hit(row) for row in rows]

    @staticmethod
    def _search_hit(row: sqlite3.Row) -> ArtifactSearchHit:
        relevance = max(0.0, -float(row["lexical_score"]))
        score = relevance / (1.0 + relevance)
        return ArtifactSearchHit(
            artifact_id=str(row["artifact_id"]),
            artifact_uri=artifact_uri(
                str(row["entity"]),
                str(row["artifact_id"]),
            ),
            entity=str(row["entity"]),
            source=str(row["source"]),
            source_instance=str(row["source_instance"]),
            title=str(row["title"]),
            text=str(row["text_content"])[:5000],
            author_name=str(row["author_name"]),
            occurred_at=row["occurred_at"],
            score=score,
            evidence_class="raw",
        )

    def read(
        self,
        reference: str,
        cursor: str | None = None,
        direction: ReadDirection = "around",
        limit: int = 20,
        *,
        include_payload: bool = False,
    ) -> ArtifactReadResponse:
        if direction not in {"around", "before", "after"}:
            raise ValueError("The artifact read direction is invalid.")
        if limit <= 0:
            raise ValueError("The artifact read limit must be positive.")
        limit = min(limit, 100)
        entity, artifact_value = parse_artifact_uri(reference)
        with connect_artifact_db(
            self.settings.artifact_db,
            read_only=True,
        ) as connection:
            focus = connection.execute(
                """
                SELECT * FROM artifacts
                WHERE artifact_id = ? AND entity = ?
                  AND deleted_at IS NULL AND redacted_at IS NULL
                """,
                (artifact_value, entity),
            ).fetchone()
            if focus is None:
                raise KeyError("The artifact reference does not exist or is inactive.")
            if include_payload:
                return ArtifactReadResponse(
                    focus=reference,
                    records=[self._read_record(focus, include_payload=True)],
                )

            base_sql, base_parameters, includes_focus = self._context_query(focus)
            rows, previous, following = self._page(
                connection,
                base_sql,
                base_parameters,
                focus,
                cursor,
                direction,
                limit,
                includes_focus,
            )
        return ArtifactReadResponse(
            focus=reference,
            records=[self._read_record(row) for row in rows],
            previous_cursor=previous,
            next_cursor=following,
        )

    @staticmethod
    def _context_query(
        focus: sqlite3.Row,
    ) -> tuple[str, list[object], bool]:
        entity = str(focus["entity"])
        active = "a.deleted_at IS NULL AND a.redacted_at IS NULL"
        if entity == "conversation":
            return (
                f"SELECT a.* FROM artifacts AS a WHERE {active} "
                "AND a.parent_artifact_id = ? AND a.entity = 'message'",
                [focus["artifact_id"]],
                False,
            )
        if entity == "transcript":
            return (
                f"SELECT a.* FROM artifacts AS a WHERE {active} "
                "AND a.parent_artifact_id = ? AND a.entity = 'transcript-cue'",
                [focus["artifact_id"]],
                False,
            )
        if entity in {"message", "transcript-cue"}:
            return (
                f"SELECT a.* FROM artifacts AS a WHERE {active} "
                "AND a.parent_artifact_id = ? AND a.entity = ?",
                [focus["parent_artifact_id"], entity],
                True,
            )
        if entity == "meeting":
            return (
                f"""
                WITH RECURSIVE context(artifact_id) AS (
                    SELECT target_artifact_id
                    FROM artifact_links
                    WHERE source_artifact_id = ?
                      AND relation IN ('contains', 'related-chat')
                    UNION
                    SELECT child.artifact_id
                    FROM artifacts AS child
                    JOIN context AS parent
                      ON child.parent_artifact_id = parent.artifact_id
                    WHERE child.deleted_at IS NULL
                      AND child.redacted_at IS NULL
                )
                SELECT a.* FROM context
                JOIN artifacts AS a USING(artifact_id)
                WHERE {active}
                """,
                [focus["artifact_id"]],
                False,
            )
        return (
            f"SELECT a.* FROM artifacts AS a WHERE {active} "
            "AND a.artifact_id = ?",
            [focus["artifact_id"]],
            True,
        )

    def _page(
        self,
        connection: sqlite3.Connection,
        base_sql: str,
        base_parameters: list[object],
        focus: sqlite3.Row,
        cursor: str | None,
        direction: ReadDirection,
        limit: int,
        includes_focus: bool,
    ) -> tuple[list[sqlite3.Row], str | None, str | None]:
        if direction == "around" and includes_focus and cursor is None:
            before_limit = limit // 2
            after_limit = limit - before_limit - 1
            key = (str(focus["occurred_at"] or ""), str(focus["artifact_id"]))
            before = self._query_page(
                connection,
                base_sql,
                base_parameters,
                "<",
                key,
                before_limit,
                descending=True,
            )
            after = self._query_page(
                connection,
                base_sql,
                base_parameters,
                ">",
                key,
                after_limit,
            )
            rows = [*reversed(before), focus, *after]
            previous = _cursor_encode(rows[0]) if before_limit and len(before) == before_limit else None
            following = _cursor_encode(rows[-1]) if after_limit and len(after) == after_limit else None
            return rows, previous, following

        if cursor is not None:
            key = _cursor_decode(cursor)
        elif includes_focus:
            key = (str(focus["occurred_at"] or ""), str(focus["artifact_id"]))
        elif direction == "before":
            key = ("\U0010ffff", "\U0010ffff")
        else:
            key = ("", "")
        operator: Literal["<", ">"] = "<" if direction == "before" else ">"
        rows = self._query_page(
            connection,
            base_sql,
            base_parameters,
            operator,
            key,
            limit,
            descending=direction == "before",
        )
        if direction == "before":
            rows.reverse()
        previous = (
            _cursor_encode(rows[0])
            if rows and direction == "before" and len(rows) == limit
            else None
        )
        following = (
            _cursor_encode(rows[-1])
            if rows and direction != "before" and len(rows) == limit
            else None
        )
        return rows, previous, following

    @staticmethod
    def _query_page(
        connection: sqlite3.Connection,
        base_sql: str,
        base_parameters: list[object],
        operator: Literal["<", ">"],
        key: tuple[str, str],
        limit: int,
        *,
        descending: bool = False,
    ) -> list[sqlite3.Row]:
        if limit <= 0:
            return []
        order = "DESC" if descending else "ASC"
        return connection.execute(
            f"""
            SELECT a.* FROM ({base_sql}) AS a
            WHERE {_comparison(operator)}
            ORDER BY COALESCE(a.occurred_at, '') {order}, a.artifact_id {order}
            LIMIT ?
            """,
            [*base_parameters, key[0], key[0], key[1], limit],
        ).fetchall()

    @staticmethod
    def _read_record(
        row: sqlite3.Row,
        *,
        include_payload: bool = False,
    ) -> ArtifactReadRecord:
        payload = json.loads(row["payload_json"]) if include_payload else None
        return ArtifactReadRecord(
            reference=artifact_uri(
                str(row["entity"]),
                str(row["artifact_id"]),
            ),
            entity=str(row["entity"]),
            title=str(row["title"]),
            text=str(row["text_content"]),
            author_name=str(row["author_name"]),
            occurred_at=row["occurred_at"],
            payload=payload,
        )
