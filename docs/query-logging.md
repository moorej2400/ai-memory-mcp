# Query Logging

## Purpose

The private query log records query arguments, elapsed time, and returned values.
It also records worker queue time, retrieval stages, coverage, and failure details.
These records support later latency and missing-evidence analysis.

Content logging is disabled by default.
The standard audit log continues to store query hashes and evidence digests.
The private query log does not change search scope, candidate limits, or result ranking.

## Enable private logs

1. Set `AI_MEMORY_QUERY_LOG_CONTENT=true` in the local server environment.
2. Keep `AI_MEMORY_AUDIT_LOGGING=true` in that environment.
3. Set `AI_MEMORY_LOG_DIR` to a private directory outside the repository.
4. Restart the applicable MCP server processes.
5. Call `memory_recall` with a known query.
6. Call `memory_status`.
7. Check `logging.query_log.content_enabled` and `logging.query_log.failed_events`.
8. Check the new records in `queries/requests.jsonl` under `AI_MEMORY_LOG_DIR`.

The default log directory is `.ai-memory/logs` under the configured primary memory root.
Existing MCP server processes retain their previous configuration until restart.
Setting `AI_MEMORY_QUERY_LOG_CONTENT=false` stops new content records after restart.
Existing records remain on disk.

## Protect the records

The private log contains the original query, scope arguments, evidence text, citations, and returned relationships.
Ordered artifact reads can include source payloads when the caller requests those payloads.
Queries and results can contain private information or credentials.
The private log does not redact these values.

Keep these records in private local storage.
Do not commit these records or attach them to public issues.
Remove sensitive values from any copy before you share that copy.

On POSIX systems, the query directory uses mode `0700` and content files use mode `0600`.
On Windows, files inherit the parent directory access controls.
Restrict that directory to the intended local account.

## Read one request

Each line contains one JSON object.
The `schema_version` field is `1`.
The `request_id` field joins the parent request with its worker and stage records.
The timestamp uses UTC.
Asynchronous writes can place records out of timestamp order.

| Event | Meaning |
| --- | --- |
| `query_started` | The handler received the query arguments. |
| `worker_started` | The assigned worker began the request. |
| `stage_started` | Retrieval entered the named stage. |
| `stage_completed` | The stage returned normally. |
| `stage_failed` | The stage raised an error. |
| `worker_completed` | The worker completed the service call. |
| `worker_failed` | The worker service call raised an error. |
| `query_completed` | The handler returned the recorded response. |
| `query_failed` | The handler raised the recorded error. |
| `query_cancelled` | The caller cancelled the request. |

The `operation` field distinguishes recall from an ordered artifact read.
The `transport` field distinguishes MCP, worker, and direct library records.
The `process_id` field identifies the writer process.
The terminal `response` contains the returned response body without log-specific fields.
Worker records omit this duplicate response body.

For response version `2`, check `response.execution` before interpreting an empty result.
A `query_completed` event can contain a partial or failed response.
The event name means the handler returned, not that every retrieval provider succeeded.
For response version `1`, incomplete execution produces `query_failed` instead of a false successful absence.

MCP argument validation occurs before the tool handler.
Requests that fail this validation do not enter the private query log.
Client transport errors after the handler returns are also outside this log.

## Examine slow requests

The terminal `elapsed_ms` measures handler time through response conversion for the log.
It excludes subsequent log writes and client transport time.
The benchmark measures serialized client latency separately.

The `timing` object contains worker queue time, executor queue time, worker time, and total supervised time.
It also identifies cold workers, worker replacement, and the assigned worker process.
Rejected requests have no worker duration.
Direct library calls do not use the worker queue.

The `diagnostics` object contains available provider durations, candidate counts, generation identity, and the raw artifact revision.
It also contains embedding metadata and search-budget results when those data are available.
Stage records include lexical search, semantic search, compact-vector selection, candidate fetching, and response assembly when those stages run.
Nested stage names show the enclosing provider.
The `generation_pin` and `retrieval` records are one-way progress markers without matching terminal stage records.
The `pin_ms` diagnostic gives the generation-pin duration.

A killed worker can leave a `stage_started` record without a matching terminal stage record.
That record identifies the last entered stage, not necessarily the root cause.
The parent terminal record identifies a timeout, cancellation, or worker failure.
The next request uses the existing worker replacement mechanism.

The following commands use `jq` from the query log directory.

Show terminal records above five seconds:

```bash
jq -c 'select(.elapsed_ms >= 5000 and (.event | startswith("query_")))' requests*.jsonl
```

Show full query arguments and returned values:

```bash
jq -c 'select(.event == "query_started" or .event == "query_completed")' requests*.jsonl
```

Show failed or cancelled requests and incomplete V2 responses:

```bash
jq -c 'select(.event == "query_failed" or .event == "query_cancelled" or (.event == "query_completed" and .response_version == "2" and .response.execution != "complete"))' requests*.jsonl
```

## Examine missing evidence

Compare the original `arguments` with the terminal `response` for the same `request_id`.
Check returned citations, evidence, warnings, and coverage before deciding that source data are absent.
Check candidate counts and exhausted search budgets in `diagnostics`.
Compare the recorded generation and raw artifact revision with the query timestamp.

The log records returned values, not every rejected candidate or every source record.
It cannot prove that an omitted source should have matched.
That question still requires source inspection or a reproducible retrieval test.
The implementation does not add sentence-derived criterion filtering.

## Storage and failure limits

The logger archives an active file before the next write after `AI_MEMORY_AUDIT_LOG_MAX_BYTES` is reached.
Archives use timestamped `requests-*.jsonl` names in the same directory.
One complete record can exceed the rotation threshold.
The logger does not delete archives or truncate returned values.
Total retained disk use grows until an operator manages the private archive.

Each process uses a background queue with limits of `256` pending events and `32 MiB` of encoded data.
One active write can coexist with those pending events.
Worker stage records use the same queue.
Slow log storage does not block retrieval.
Background writers use `AI_MEMORY_AUDIT_LOCK_TIMEOUT_SECONDS` for log lock waits.

If storage fails or the queue fills, retrieval continues without the rejected record.
The status reports parent-process pending bytes, pending events, failed events, and the last log error.
These counters cover the current process lifetime and reset on restart.
Worker responses report new worker failures and pending counts to the parent.
The parent status accumulates these reports in `worker_failed_events` and `last_worker_error`.
A background failure after a response can appear in the next worker response.
A killed worker cannot report a log write that never completed.

Shutdown waits up to one second for queued writes.
An abrupt exit or forced worker termination can lose pending records, including stage markers.
Planned worker recycling uses an asynchronous graceful stop to flush queued records.
The stop waits up to five seconds before it forces worker termination.
The retiring worker count cannot exceed the configured active worker count.
If that limit is full, the healthy worker stays available until a later request can recycle it.
The log is best-effort diagnostic storage, not a durable transaction journal.
The absence of a terminal record can indicate unfinished work or a lost log write.
Check process status and log failure counters before drawing conclusions.

## Validate with synthetic data

Run the regression benchmark with content logging:

```bash
./.venv/bin/ai-memory-real-world-benchmark \
  --profile regression \
  --embedding-provider model2vec \
  --repeats 3 \
  --log-query-content
```

The generated report records whether content and audit logging were enabled.
The benchmark uses isolated synthetic source data.
See [Retrieval Reliability Validation](retrieval-reliability-validation.md) for measured capacity and remaining evaluation work.
