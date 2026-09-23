# Markdown Intake Contract

AI Memory accepts legacy notes for retrieval.
AI Memory requires schema version 2 for validated new writes.

## Layout

The vault uses a small generic layout.
Each folder is optional.

```text
AI-Memory/
  Home.md
  Notes/
  Collections/
    <collection>/
      <collection>.base
      Records/
  Views/
  Archive/
```

Use `Notes/` for an independently maintained topic.
Use a collection when typed fields or a Base view add value.
The folder path does not define record meaning.

Initialize the minimum layout with this command:

```text
ai-memory-vault --root <vault>
```

The command preserves each existing file.

## Required fields

Each new record requires these fields:

```yaml
schema_version: 2
memory_id: mem-example
title: Example
type: memory
record_type: note
domain: general
status: active
created: 2026-09-18
updated: 2026-09-18
provenance:
  - source: manual
```

The note body requires an H1 that equals `title`.
The note body requires a summary after the H1.

Use `scope_kind` and `scope_id` together.
Use `collection` only when the record belongs to a real collection.
Use `aliases` for stable alternate names.

## Validated writes

Use `memory_upsert` for a new or changed record.
The tool rejects unsafe paths, duplicate IDs, and invalid schema.

For an update, provide the current SHA-256 value as `expected_sha256`.
This value prevents an agent from replacing a newer file.

Call `memory_sync` after the complete write batch.
Call `memory_status` to inspect schema quality and retrieval health.

## Links

Use a path-qualified wikilink when a title is not unique.
Heading anchors and display labels do not change link identity.
The validator reports broken and ambiguous targets.

Add only useful relationships.
The intake contract does not require each note to have a link.

## Collections

Create a collection after its first useful record exists:

```text
ai-memory-collection --root <vault> --name Links --record-type link
```

The command creates `Records/` and one Obsidian Base.
The command does not replace a changed Base file.
