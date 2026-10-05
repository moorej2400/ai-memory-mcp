# Operations

This guide gives the common operating procedures.

Each procedure shows the PowerShell form first and the POSIX shell form second.
Both wrappers call the same cross-platform Python implementation, so either form
produces the same result on any supported platform.

The console scripts live in `.venv\Scripts` on Windows and `.venv/bin` on macOS
and Linux.

## Update the coordinated generation

Use `memory_sync` after canonical Markdown or raw artifact data changes.
The tool stages the Markdown index, the artifact index, and the note graph together.
AI Memory builds the note graph from the Markdown index without an external service.
The tool validates every component before it changes the generation pointer.
The previous generation stays active when one component fails.

Use the `ai-memory-sync` command when no MCP client is available:

```powershell
.\.venv\Scripts\ai-memory-sync.exe
```

```bash
./.venv/bin/ai-memory-sync
```

The command publishes the same generation as `memory_sync`.
The command prints the sync result as JSON.
The command exits with status 1 when publication fails.

Use `ai-memory-index` only for isolated Markdown index maintenance:

```powershell
.\.venv\Scripts\ai-memory-index.exe
```

```bash
./.venv/bin/ai-memory-index
```

The command reads Markdown from all configured memory sources.
The command publishes one Markdown SQLite snapshot.
The command does not change the Markdown files.
The command skips publication when no Markdown file changed.

This maintenance command does not publish a coordinated generation.

Concurrent commands wait for the current index publisher.
The default wait limit is 300 seconds.

## Create a collection

Initialize a new generic vault first when no structure exists:

```text
ai-memory-vault --root <vault>
```

The command creates `Home.md`, `Notes/`, and three operational Bases.
The command preserves each existing file.

Create a collection only after its first useful record exists.

```powershell
.\.venv\Scripts\ai-memory-collection.exe --root <vault> --name Links --record-type link
```

```bash
./.venv/bin/ai-memory-collection --root <vault> --name Links --record-type link
```

The command creates one `Records` directory and one Obsidian Base.
The command does not replace a changed Base.

## Migrate a Markdown vault

Use the [vault migration procedure](migrations.md) for a layout or schema migration.
The AI reads the notes and selects their structure and destinations.
Use the migration commands to record and protect each file operation.
Use one stable migration ID for all commands in one run.

## Manage raw artifacts

Put the artifact database and object directory on local storage.
Do not put the active SQLite database on a network file system.
The default paths are under `AI_MEMORY_WORK_DIR/.ai-memory/raw/`.

Initialize a new artifact database:

```powershell
.\.venv\Scripts\ai-memory-artifact.exe init
```

```bash
./.venv/bin/ai-memory-artifact init
```

The command applies each required schema migration once.

Ingest one complete JSONL batch:

```powershell
.\.venv\Scripts\ai-memory-artifact.exe ingest --input batch.jsonl
```

```bash
./.venv/bin/ai-memory-artifact ingest --input batch.jsonl
```

Use `--input -` to read the complete batch from standard input.
The command validates the complete batch before it changes SQLite.
The intake receipt confirms a durable database commit.
The receipt contains the exact batch ID and input hash.
An idempotent retry returns the stored receipt.
The receipt is not a provider fetch cursor.

Check the database status:

```powershell
.\.venv\Scripts\ai-memory-artifact.exe status
```

```bash
./.venv/bin/ai-memory-artifact status
```

Search active raw artifacts:

```powershell
.\.venv\Scripts\ai-memory-artifact.exe search --query "rotation procedure" --source chat-source
```

```bash
./.venv/bin/ai-memory-artifact search --query "rotation procedure" --source chat-source
```

Read ordered context from one stable citation:

```powershell
.\.venv\Scripts\ai-memory-artifact.exe read --reference artifact://message/<artifact-id> --limit 20
```

```bash
./.venv/bin/ai-memory-artifact read --reference artifact://message/<artifact-id> --limit 20
```

Use `pending` to list artifacts that need Markdown distillation.
Use `mark-distilled` after you validate the current Markdown note.
Use `mark-no-durable-memory` for a reviewed meeting or conversation.
Use `backup` to create a consistent SQLite database backup.
Use `migrate-legacy` to stage a supported legacy import.

## Back up and check raw artifacts

Treat the active database file and its WAL files as one database unit.
Do not copy an active SQLite file with a normal file-copy command.
The copy can omit committed WAL data or contain an inconsistent page set.

Create a consistent database backup:

```powershell
.\.venv\Scripts\ai-memory-artifact.exe backup
```

```bash
./.venv/bin/ai-memory-artifact backup
```

The command uses the SQLite backup API.
The command checks database integrity and foreign keys before publication.
The command does not remove an older backup.
The database backup does not contain attachment object files.
Back up `AI_MEMORY_ARTIFACT_OBJECTS_DIR` with the applicable filesystem backup.

Check the active artifact database:

```powershell
.\.venv\Scripts\ai-memory-artifact.exe check
```

```bash
./.venv/bin/ai-memory-artifact check
```

Restore a verified backup to a new staging path:

```powershell
.\.venv\Scripts\ai-memory-artifact.exe restore --backup <backup-file> --destination <new-database-file>
```

```bash
./.venv/bin/ai-memory-artifact restore --backup <backup-file> --destination <new-database-file>
```

The restore command rejects an existing destination.
The restore command does not replace the active database.
Stop all writers before an operator-controlled cutover.
Keep the source backup until the restored database passes operational checks.

## Import legacy artifacts

Keep each legacy source active until all verification checks pass.
The migration opens the legacy SQLite database in read-only mode.
The migration creates a stable logical snapshot with the SQLite backup API.
The snapshot includes committed WAL data.
The migration does not change the database or Markdown notes.

Run a dry-run check on Windows:

```powershell
.\.venv\Scripts\ai-memory-artifact.exe migrate-legacy --source chat-source --source-instance workspace --sync-db <sync-database> --chat-notes <chat-notes> --meeting-notes <meeting-notes> --dry-run
```

Run a dry-run check on macOS or Linux:

```bash
./.venv/bin/ai-memory-artifact migrate-legacy --source chat-source --source-instance workspace --sync-db <sync-database> --chat-notes <chat-notes> --meeting-notes <meeting-notes> --dry-run
```

Check the reported counts and unresolved identities.
Check `synthetic_note_identities` when old Markdown has no provider identity.
Each synthetic identity uses the note name and content SHA-256.
Treat synthetic identities as separate review records.
Check `duplicate_note_mappings` for multiple notes linked to one provider record.
The migration keeps each duplicate note as a separate transcript identity.
The `database_sha256` value identifies the logical database snapshot.
Stop the import if a required count is incorrect.

Import the checked sources on Windows:

```powershell
.\.venv\Scripts\ai-memory-artifact.exe migrate-legacy --source chat-source --source-instance workspace --sync-db <sync-database> --chat-notes <chat-notes> --meeting-notes <meeting-notes>
```

Import the checked sources on macOS or Linux:

```bash
./.venv/bin/ai-memory-artifact migrate-legacy --source chat-source --source-instance workspace --sync-db <sync-database> --chat-notes <chat-notes> --meeting-notes <meeting-notes>
```

Use `--immutable` only with a closed database copy that has no active WAL file.
Do not use `--immutable` with the active provider database.

The import keeps manual summaries as distillation candidates.
The canonical database stores raw messages, meetings, transcripts, and transcript cues.
The migration does not create transcript-heavy Markdown notes.

Compare the receipt with the dry-run counts after the import.
Keep the legacy sources in backup coverage during the transition.
After approval, move obsolete outputs to a recoverable archive.
Do not automate the archive move.
Do not delete the legacy database or notes.

## Switch a provider pipeline

Use one complete JSONL batch as the handoff boundary.
Keep provider state separate from the canonical artifact database.

Before the switch, create a verified artifact backup.
Before the switch, complete the legacy migration dry run.

1. Run the provider fetch.
2. Publish one complete JSONL batch.
3. Let the provider call `ai-memory-artifact ingest`.
4. Verify the exact receipt in provider state.
5. If message or cue data changed, run `memory_sync`.
6. Queue agent distillation for pending meetings and conversations.

Do not use the intake receipt as a provider fetch cursor.
Do not point the provider at the canonical artifact database.
Do not remove the handoff before receipt validation.
Keep the old scheduled process available for rollback.

Test one provider batch before the scheduled switch.
Verify that the receipt has no conflict.
Verify that raw search returns the expected text.
Verify that artifact read returns ordered context.
Verify that each new meeting enters the pending queue.
Verify that Markdown does not contain a full transcript.

Run one bounded reconciliation after the scheduled switch.
Stop the cutover if the provider reports incomplete coverage as complete.
Check each expected tombstone and unchanged record.

After all checks pass, request approval for the archive move.
Move legacy outputs only after approval.
Keep the archive recoverable.

## Validate artifact performance

Run the synthetic artifact benchmark on macOS or Linux:

```bash
PYTHONPATH=src ./.venv/bin/python benchmarks/artifacts/generate_fixture.py
```

Run the frozen retrieval benchmark:

```bash
./.venv/bin/ai-memory-benchmark --label artifact-store-validation
```

The benchmark calls the complete `memory_recall` pipeline.
It reports recall, MRR, ANN candidate recall, source safety, resource use, and layer latency.
The benchmark writes generated data under the ignored `benchmarks/runs/` directory.
The benchmark does not use live messages, meetings, or memory notes.
Do not use one benchmark run as a strict performance limit.

Run the regression profile before release:

```bash
./.venv/bin/ai-memory-real-world-benchmark --profile regression --repeats 3
```

Use `workload` and `growth` only for separate capacity tests.
An interrupted run writes an incomplete report.
An incomplete report cannot pass a release gate.

## Run the MCP server

Use the standard input and output transport for local agents:

```powershell
.\.venv\Scripts\ai-memory-mcp.exe --transport stdio
```

```bash
./.venv/bin/ai-memory-mcp --transport stdio
```

Use the HTTP transport when a local client needs an endpoint:

```powershell
.\.venv\Scripts\ai-memory-mcp.exe --transport streamable-http
```

```bash
./.venv/bin/ai-memory-mcp --transport streamable-http
```

The default HTTP endpoint is `http://127.0.0.1:4334/mcp`.
The server rejects non-loopback hosts because this transport has no authentication.

## Interpret recall results

Use response version 2 for new clients.
This version separates execution state from result kind.

`execution` is `complete`, `partial`, or `failed`.
`result_kind` is `exact`, `ranked`, or `empty`.
Do not interpret failed execution as an absent memory.

Read `reason_codes` when execution is partial or failed.
Read `coverage` for semantic lag, source availability, and representation counts.
Use `memory_artifact_read` to inspect a raw citation.
Use `supporting_artifact_uris` to inspect evidence for a distilled memory.

Response version 1 remains available during client migration.
Version 1 returns a tool error when it cannot represent an incomplete result safely.

## Update client registrations

Run this command after you move the repository:

```powershell
.\scripts\install-clients.ps1
```

```bash
./scripts/install-clients.sh
```

The installer updates each command path.
The installer preserves the previous configuration in a timestamped backup.
Restart each configured client after the command finishes.

## Manage derived generation retention

Use `memory_sync` after canonical Markdown or artifact data changes.
The tool publishes one generation for Markdown, artifact vectors, and the note graph.
The system keeps the active generation and one verified previous generation.
An active recall lease prevents removal of its pinned generation.
Retention removes only old derived snapshots after pointer and integrity checks.
Retention never removes raw artifacts, revisions, tombstones, or required attachment objects.

## Recover from a damaged graph

AI Memory validates the graph snapshot each time it reads the snapshot.
A snapshot is not valid when, for example, it has no node list or a duplicate node `id`.
An edge to a missing node also makes the snapshot not valid.

When the snapshot is not valid, `memory_status` reports `false` for `ok` and for `graph.available`.
The `graph.error` field then contains the validation error.
Recall continues with lexical and semantic results.
The recall response contains the warning `The graph component is unavailable.`

To publish a new valid graph, do these steps:

1. Call `memory_status` and read `graph.error`.
2. Call `memory_sync`, or run the `ai-memory-sync` command.
3. Call `memory_status` again.
4. Make sure that `graph.available` is `true`.

If publication fails, the previous generation stays active.
Read the sync result and the generation log for the failed component.

## Run the retrieval evaluation

The retrieval evaluation sends real questions through the `memory_recall` pipeline.
Each case contains one question and one expected evidence marker.

Use real questions and expected evidence markers from the configured vault.
Set `AI_MEMORY_RETRIEVAL_EVAL_CASES` before you run the script.
The [configuration guide](configuration.md#retrieval-evaluation) gives the value format.

Run the retrieval evaluation:

```powershell
.\scripts\run-retrieval-eval.ps1
```

```bash
./scripts/run-retrieval-eval.sh
```

The script prints a JSON summary.
The summary contains the `passed`, `generationConsistent`, and `graphAvailable` fields.
The script exits with status 1 when a case does not return its marker.

## Review local logs

The generation log records component latency, corpus size, bytes, growth, generation IDs, and sanitized failures.
The index log records source counts, changes, errors, lock waits, and elapsed time.
The retrieval log records safe counts, digests, component latency, and generation IDs.
Artifact routes record query hashes, evidence digests, and metadata.
Intake and distillation logs record counts, latency, storage size, and sanitized failures.

Read these files under `AI_MEMORY_LOG_DIR`:

- `index.jsonl`
- `generation.jsonl`
- `artifact-intake.jsonl`
- `distillation.jsonl`
- `retrieval.jsonl`

The default directory is `AI_MEMORY_WORK_DIR\.ai-memory\logs`.
The logger moves a full active log to a timestamped local archive.

The standard audit logs do not contain raw artifact text or sensitive queries.
The optional [private query log](query-logging.md) contains full query arguments and returned values.
Do not copy these logs into the repository.

## Upgrade an installation that used Graphify

Earlier releases installed the external Graphify package.
These releases published the note graph to a Graphify MCP service on port 4324.
A login launcher started that service.
AI Memory does not use Graphify, the service, or the launcher.
The earlier scripts are in the [Graphify pipeline archive](../archive/graphify-memory-pipeline/README.md).
AI Memory does not use the archived scripts.

1. Get the current release of the repository:

   ```bash
   git pull
   ```

2. Run the setup command with the memory root that `AI_MEMORY_WORK_DIR` sets:

   ```powershell
   .\scripts\setup.ps1 -MemoryRoot <memory-root>
   ```

   ```bash
   ./scripts/setup.sh --memory-root <memory-root>
   ```

   The setup command keeps the existing `.env` file.
   The setup command does not install Graphify.
   The setup command runs `ai-memory-sync` and publishes a native generation.
   The first synchronization parses every note again, because the index schema changed.

3. If you updated the package without the setup command, run `ai-memory-sync`.
4. Restart each configured client.
5. Call `memory_status` from an MCP client.
6. Make sure that `generation.consistent` and `graph.available` are `true`.
7. Run the retirement script without the apply option:

   ```powershell
   .\scripts\retire-graphify-memory.ps1
   ```

   ```bash
   ./scripts/retire-graphify-memory.sh
   ```

   The script reports each earlier Graphify item.
   The script makes no change in this mode.

8. Examine the report.
9. Run the retirement script with the apply option:

   ```powershell
   .\scripts\retire-graphify-memory.ps1 -Apply
   ```

   ```bash
   ./scripts/retire-graphify-memory.sh --apply
   ```

10. If the script keeps the legacy state, run `ai-memory-sync` and do step 9 again.
11. If the report lists `GRAPHIFY_*` keys in `.env`, remove the keys that no other tool uses.

With the apply option, the script stops a `graphify-mcp` process that serves the legacy `global-graph.json` file.
The script unregisters the earlier login launcher and moves the launcher file to the `launchers/` archive directory.
The script changes a launcher only when the launcher starts a script in this repository.
The script reports and keeps a launcher that starts another checkout.
The script moves the legacy Graphify state directory to the `provider-state/` archive directory.
Both archive directories are under `AI_MEMORY_WORK_DIR/.ai-memory/backups/graphify-retirement/<stamp>/`.
The script does not delete a file.

The script looks for the earlier login launcher at these locations:

| Platform | Mechanism | Location |
| --- | --- | --- |
| Windows | Startup folder script | `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\graphify-global-mcp-start.vbs` |
| macOS | launchd LaunchAgent | `~/Library/LaunchAgents/com.graphify-global-mcp.plist` |
| Linux | systemd user unit | `~/.config/systemd/user/graphify-global-mcp.service` |
| Linux | XDG autostart entry | `~/.config/autostart/graphify-global-mcp.desktop` |

The script keeps the legacy state when no native generation exists.
Recall reads the legacy graph until a native generation exists.

The script does not change `.env`.
The script does not change `.graphify-runtime`.
The independent Graphify Codebase skill can use that runtime.

## Check health

Call `memory_status` from an MCP client.
Check the `canonical_memory_root`, `retrieval_sources`, `index`, `generation`, `graph`, and `runtime` fields.
The `graph` field reports the native note graph of the active generation.

A saved Markdown file can exist before its derived indexes change.
Report the Markdown and index results as separate results.
