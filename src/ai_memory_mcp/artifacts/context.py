from __future__ import annotations

import sqlite3


# A related chat can contain sibling meeting occurrences. Restrict each branch
# by entity so one meeting cannot absorb another meeting's transcript context.
MEETING_CONTEXT_SQL = """
WITH RECURSIVE
meeting_records(artifact_id) AS (
    SELECT child.artifact_id
    FROM artifacts AS child
    WHERE child.parent_artifact_id = ?
      AND child.entity IN (
          'recording', 'transcript', 'transcript-cue', 'attachment'
      )
      AND child.deleted_at IS NULL
      AND child.redacted_at IS NULL
    UNION
    SELECT target.artifact_id
    FROM artifact_links AS link
    JOIN artifacts AS target
      ON target.artifact_id = link.target_artifact_id
    WHERE link.source_artifact_id = ?
      AND link.relation = 'contains'
      AND target.entity IN (
          'recording', 'transcript', 'transcript-cue', 'attachment'
      )
      AND target.deleted_at IS NULL
      AND target.redacted_at IS NULL
    UNION
    SELECT child.artifact_id
    FROM artifacts AS child
    JOIN meeting_records AS parent_record
      ON child.parent_artifact_id = parent_record.artifact_id
    WHERE child.entity IN (
          'recording', 'transcript', 'transcript-cue', 'attachment'
      )
      AND child.deleted_at IS NULL
      AND child.redacted_at IS NULL
),
chat_records(artifact_id) AS (
    SELECT target.artifact_id
    FROM artifact_links AS link
    JOIN artifacts AS target
      ON target.artifact_id = link.target_artifact_id
    WHERE link.source_artifact_id = ?
      AND link.relation = 'related-chat'
      AND target.entity = 'conversation'
      AND target.deleted_at IS NULL
      AND target.redacted_at IS NULL
    UNION
    SELECT child.artifact_id
    FROM artifacts AS child
    JOIN chat_records AS parent_record
      ON child.parent_artifact_id = parent_record.artifact_id
    JOIN artifacts AS parent
      ON parent.artifact_id = parent_record.artifact_id
    WHERE (
          (parent.entity = 'conversation' AND child.entity IN ('message', 'attachment'))
          OR (parent.entity = 'message' AND child.entity = 'attachment')
      )
      AND child.deleted_at IS NULL
      AND child.redacted_at IS NULL
),
context(artifact_id) AS (
    SELECT artifact_id FROM meeting_records
    UNION
    SELECT artifact_id FROM chat_records
)
SELECT a.*
FROM context
JOIN artifacts AS a USING(artifact_id)
WHERE a.deleted_at IS NULL AND a.redacted_at IS NULL
"""


CONVERSATION_CONTEXT_SQL = """
WITH RECURSIVE context(artifact_id) AS (
    SELECT child.artifact_id
    FROM artifacts AS child
    WHERE child.parent_artifact_id = ?
      AND child.entity IN ('message', 'attachment')
      AND child.deleted_at IS NULL
      AND child.redacted_at IS NULL
    UNION
    SELECT child.artifact_id
    FROM artifacts AS child
    JOIN context AS parent_record
      ON child.parent_artifact_id = parent_record.artifact_id
    JOIN artifacts AS parent
      ON parent.artifact_id = parent_record.artifact_id
    WHERE parent.entity = 'message'
      AND child.entity = 'attachment'
      AND child.deleted_at IS NULL
      AND child.redacted_at IS NULL
)
SELECT a.*
FROM context
JOIN artifacts AS a USING(artifact_id)
WHERE a.deleted_at IS NULL AND a.redacted_at IS NULL
"""


def active_context_rows(
    connection: sqlite3.Connection,
    root_artifact_id: str,
    root_entity: str,
) -> list[sqlite3.Row]:
    if root_entity == "meeting":
        return connection.execute(
            MEETING_CONTEXT_SQL,
            (root_artifact_id, root_artifact_id, root_artifact_id),
        ).fetchall()
    if root_entity == "conversation":
        return connection.execute(
            CONVERSATION_CONTEXT_SQL,
            (root_artifact_id,),
        ).fetchall()
    return []


def active_context_ids(
    connection: sqlite3.Connection,
    root_artifact_id: str,
    root_entity: str,
) -> set[str]:
    return {
        root_artifact_id,
        *(
            str(row["artifact_id"])
            for row in active_context_rows(
                connection,
                root_artifact_id,
                root_entity,
            )
        ),
    }
