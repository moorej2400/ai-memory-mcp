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
`artifact://<entity>/<artifact-id>` is the stable raw citation format.
A distilled note does not contain a complete transcript or chat log.

Each managed note contains one distillation region.
The agent replaces only text between the managed begin and end markers.
Manual notes stay outside this region.

AI Memory stores the latest reviewed event and source digest in SQLite.
Completion fails when the source changes during distillation.
Meeting completion also requires a valid Markdown summary and artifact-linked evidence.

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

Each object uses the path `sha256/<digest-prefix>/<digest>`.
The intake process verifies the complete SHA-256 digest before publication.
The intake process copies the file and does not change the provider source file.
SQLite never stores the local provider source path.

A redaction removes unshared object bytes from active content-addressed storage.
The system moves those bytes to a private quarantine because automatic file deletion is not permitted.
The system preserves an object while another active artifact still references it.
Later events for a redacted artifact do not read or copy provider handoff files.

## Recovery

Create a consistent SQLite backup before each schema migration.
Keep the migration source unchanged until count and digest checks pass.
