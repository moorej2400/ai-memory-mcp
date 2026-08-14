# AI Agent Setup For A New System

This procedure lets an AI agent initialize AI Memory and one artifact provider on a new system.

The procedure keeps provider fetch state separate from canonical AI Memory data.

## Required inputs

Collect these values before setup:

- The AI Memory repository path.
- The provider repository path.
- The writable Markdown memory directory.
- A stable provider source instance.
- The local provider output directory.

Keep machine paths and private values in each repository `.env` file.

## Safety rules

1. Read each repository `AGENTS.md` file.
2. Keep the artifact database on a local filesystem.
3. Never point the provider at the artifact database.
4. Do not commit `.env`, batches, receipts, databases, objects, or memory records.
5. Preserve every legacy source until migration verification succeeds.

## Initialize AI Memory

1. Open a terminal in the AI Memory repository.
2. Create the writable Markdown memory directory when it is absent.
3. Run the setup command for the operating system.

Windows PowerShell:

```powershell
.\scripts\setup.ps1 -MemoryRoot 'C:\path\to\AI-Memory' -InstallClients
```

macOS or Linux:

```bash
./scripts/setup.sh --memory-root /path/to/AI-Memory --install-clients
```

The setup creates `.env`, installs the application, initializes artifact schema version 3, and registers selected clients.

4. Verify the raw artifact database.

Windows PowerShell:

```powershell
.\.venv\Scripts\ai-memory-artifact.exe status
.\.venv\Scripts\ai-memory-artifact.exe check
```

macOS or Linux:

```bash
./.venv/bin/ai-memory-artifact status
./.venv/bin/ai-memory-artifact check
```

The status result must show the current schema and a local database path.

## Initialize the provider

1. Open a terminal in the provider repository.
2. Run `./setup.sh` from Bash.

The command creates `.env` from `.env.template` when necessary.
The command archives an installed adapter before it installs a replacement.

3. Set `TEAMS_CLI_SOURCE_INSTANCE` in the provider `.env` file.
4. Keep this value stable after the first published batch.
5. Load the printed browser extension path in a supported Chromium browser.
6. Keep the browser signed in to the required provider services.
7. Run the doctor command below.

Run provider commands from the provider repository:

```bash
set -a
source .env
set +a
"$OPENCLI_DIR/node_modules/.bin/tsx" "$OPENCLI_DIR/src/main.ts" doctor
```

## Publish and ingest the first batch

1. Create the provider message batch.

```bash
"$OPENCLI_DIR/node_modules/.bin/tsx" "$OPENCLI_DIR/src/main.ts" teams messages-sync \
  --db "$TEAMS_CLI_SYNC_DB" \
  --since-hours 1 \
  --artifact-out "$TEAMS_CLI_OUTPUT_DIR/message-batches/" \
  --source-instance "$TEAMS_CLI_SOURCE_INSTANCE"
```

2. Ingest each new JSONL file with `ai-memory-artifact ingest`.
3. Keep the JSON receipt with the related batch.
4. Create the provider transcript batch.

```bash
"$OPENCLI_DIR/node_modules/.bin/tsx" "$OPENCLI_DIR/src/main.ts" teams transcripts-sync \
  --db "$TEAMS_CLI_SYNC_DB" \
  --artifact-out "$TEAMS_CLI_OUTPUT_DIR/transcript-batches/" \
  --source-instance "$TEAMS_CLI_SOURCE_INSTANCE"
```

5. Ingest each new transcript JSONL file.
6. Run `memory_sync` after artifact data changes.
7. Verify one raw search and one ordered artifact read.

## Import legacy data

Use this section only when the new system has legacy provider data.

1. Copy an active network SQLite source to a stable local snapshot.
2. Run `ai-memory-artifact backup`.
3. Run `ai-memory-artifact check`.
4. Run `migrate-legacy` with `--dry-run`.
5. Review unresolved identities, duplicates, counts, and source digests.
6. Run the same migration command without `--dry-run`.
7. Keep all legacy inputs unchanged.

Use the exact migration commands in the [operations guide](operations.md#import-legacy-artifacts).

## Scheduler contract

Run these operations in order:

1. Run the provider message fetch.
2. Publish the message batch to a batch directory.
3. Run the provider transcript fetch.
4. Publish the transcript batch to a batch directory.
5. Ingest each unpublished batch into AI Memory.
6. Save each intake receipt.
7. Run `memory_sync` when artifact data changes.
8. Queue agent distillation for pending meetings and durable conversations.

Schedule bounded reconciliation at least once during each configured interval.
Do not advance provider state before successful batch publication.

## Completion checks

The setup is complete when all these conditions are true:

- `memory_status` is satisfactory.
- `ai-memory-artifact check` passes.
- The provider emits a batch without a credential or signed URL.
- Replaying the batch creates no duplicate artifact or event.
- Raw search returns the ingested source text.
- `memory_artifact_read` returns ordered source context.
- A meeting enters the distillation queue.
- No complete transcript appears in Markdown.
