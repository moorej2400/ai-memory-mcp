# Storage and Schema Contract

Read this reference before you create a record, collection, view, link, or migration plan.

## Canonical Layout

Create only the folders that contain useful records or views.

```text
<memory-root>/
  Home.md
  Notes/
  Collections/
    <collection>/
      <collection>.base
      Records/
  Views/
    Action Dashboard.base
    Review Queue.base
    Memory Health.base
  Archive/
  .ai-memory/
```

`Notes/` is the default destination for durable topic notes.
`Collections/` contains optional typed record sets with Obsidian Bases views.
`Views/` contains cross-vault operational views.
`Archive/` contains inactive material that must remain recoverable.

Do not create an `Objects/` folder only to group unrelated nouns.
The collection name already identifies an optional object type.

Common collections include `People`, `Projects`, `Links`, `Recipes`, `Meetings`, and `Tools`.
These names are examples, not required taxonomy.

Use this project structure when a project needs several independent notes:

```text
Collections/Projects/Records/example/
  example.md
  Notes/
    Deployment process.md
    Production configuration.md
```

Create a collection with `ai-memory-collection` only after the first useful record exists.

## Record Boundaries

Write one independently maintained topic in each note.
Keep related facts together when they change from the same evidence.
Split a note when one section needs separate updates, ownership, status, or evidence.

Do not create one file for each fact.
Do not keep a complete project history in one large note.
Let the search index create retrieval chunks inside each note.

## Schema Version 2

Use this frontmatter for each new canonical record:

```yaml
---
schema_version: 2
memory_id: mem-<unique-id>
title: Human-readable title
type: memory
record_type: note
collection: Projects
domain: work
scope_kind: project
scope_id: project:example
status: active
created: YYYY-MM-DD
updated: YYYY-MM-DD
review_after: null
aliases: []
related: []
provenance:
  - source: manual
    reference: task:<stable-id>
    verified: YYYY-MM-DD
supersedes: []
superseded_by: null
---
```

The `collection`, `scope_kind`, and `scope_id` fields are optional.
If one scope field exists, both scope fields must exist.
The `memory_id` stays stable after a file move or title change.

Use `record_type` to state what the record is.
Examples include `note`, `person`, `project`, `link`, `recipe`, `meeting`, and `tool`.
Use `domain` for a broad privacy or operating boundary.
Do not use the folder path as record meaning.

Use `record_type: action` only for an actionable item.
Add `owner` and `due` when those values are known.
The Action Dashboard shows active action records.

Keep the filename, frontmatter `title`, and H1 equal.
Start the body with a useful summary.

## Legacy Compatibility

The indexer continues to read schema-version-1 fields.
It maps `root_scope` to `domain` during retrieval.
It maps nested `primary_scope` fields to generic scope filters.

Do not rewrite a legacy note only to change its schema number.
Use the migration workflow when a content update or structure change also requires conversion.

## Stable Identity

Use `memory_id` as the canonical record identity.
Use provider-native identities for repositories, tickets, projects, and external records.
Do not use a display title as the only identity.

Use `promotion_id` as `session_id:entry_id` when a session produces durable memory.
Reprocessing one promotion must update or no-op.

## Links Collection

Use `Collections/Links/Records/` for saved web pages, products, articles, and external resources.
Use `record_type: link` for each link record.
Add fields such as `url`, `site`, `author`, `published`, and `accessed` when available.

Keep the source URL even when the record also contains a durable summary.
Do not require article fields for a product or general bookmark.

## Provenance

Keep provenance short and auditable.
Record the source, stable reference, and verification date when available.
Do not copy transcripts, long logs, or complete session entries into Markdown.

## Link Grammar

Use path-qualified links when titles are not unique.
For example, use `[[Collections/People/Records/Example Person|Example Person]]`.
Headings and display labels do not change the target identity.

Add a link only when it gives useful context or supports navigation.
An unlinked note is valid when no real relationship exists.
Do not add weak links only to change the graph view.

## Deduplication and Supersession

Match stable identity and provenance before title similarity.
Merge active records when one precise note can own the same topic.
Mark unresolved contradictions as `needs-review`.
Keep a superseded predecessor readable and linked.

## Distilled Artifact Records

An artifact can produce zero, one, or many durable notes.
Store each note by its durable topic or collection.
Do not force all meeting content into one meeting note.

Use `Collections/Meetings/Records/` for meeting records that remain useful as meetings.
Use `Collections/Conversations/Records/` for useful conversation records.
Store reusable facts in the collection or topic that owns those facts.

Add `artifact_kind`, `source_artifact`, `distilled_through_event`, and `source_digest` when one artifact controls the managed region.
Keep complete source material in the artifact database.
