# Artifact Storage

## Authority

SQLite is the authority for raw external artifacts.
Markdown is the authority for distilled durable knowledge.

The two stores contain related information, but they do not have the same role.
The system can rebuild artifact search data from the canonical artifact database.
The system can rebuild memory search data from canonical Markdown.

## Raw artifacts

The canonical artifact database stores conversations, messages, meetings, recordings, transcripts, transcript cues, attachments, revisions, and tombstones.

Each message and transcript cue is one record.
The database does not store a complete conversation as one array.

## Distilled Markdown

A meeting note contains a summary, decisions, actions, open questions, and short evidence quotations.
A conversation note contains durable resolutions, decisions, and reusable context.

A distilled note links each important claim to an `artifact://` citation.
A distilled note does not contain a complete transcript or chat log.

## Provider boundary

The provider adapter owns authentication, paging, remote cursors, and complete-snapshot claims.
AI Memory owns validation, canonical storage, revisions, receipts, search, and citations.

## Retrieval

`memory_recall` searches distilled Markdown and raw artifact text.
Distilled evidence can answer a general question.
Raw evidence is a lead unless the query has an exact match.

Use `memory_artifact_read` to read ordered source context around an artifact citation.

## Attachment files

Attachment files stay in content-addressed object storage.
SQLite stores each object hash, media type, size, and relative object path.

## Recovery

Create a consistent SQLite backup before each schema migration.
Keep the migration source unchanged until count and digest checks pass.
