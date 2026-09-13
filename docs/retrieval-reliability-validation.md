# Retrieval Reliability Validation

## Scope

This report records synthetic validation on macOS on September 12, 2026.
It contains no live messages, meeting text, private identifiers, or machine-specific paths.
Windows remained read-only throughout implementation and validation.
These checks did not deploy the changes to a live service.

The implementation does not parse sentence-derived criteria.
It does not verify people, dates, decisions, or exclusions from natural-language queries.
Callers must inspect the returned citations.

## Review repairs

The corrected implementation includes these changes:

- Explicit provider failures and partial coverage, including freshness across worker processes.
- Flat, versioned MCP response schemas with generation-pinned coverage and source references.
- Deadline supervision around queue admission, request writes, response reads, and audit locks.
- Worker replacement and recoverable lease cleanup after timeout or cancellation.
- Complete source segments, bounded cross-speaker context, and indexed source-order lookups.
- Immutable vector segments, small delta manifests, and bounded background compaction.
- Preserved matching passages, full lexical query coverage, and corrected mixed-source ranking.
- Indexed candidate fetching and compact identity indexes for large datasets.
- Covering compact-vector indexes and projected segment views for candidate counts.
- Bounded native-array candidate sorting with unchanged score and identity ordering.
- Optional full query logs with worker timing, result bodies, coverage, and failure counters.
- Serialized MCP quality checks and process-tree memory measurements.

The regression suite exercises the production worker pool, not a separate one-shot worker implementation.
The complete test suite passed all `514` tests.
The suite emits an existing Pydantic lifespan annotation warning.

## Release gates

The frozen benchmark contract requires these quality results:

- Pass rate: `1.0`.
- Recall at 5: `1.0`.
- No-answer accuracy: `1.0`.
- ANN candidate recall at 10: at least `0.95`.
- Scope leakage rate: `0.0`.
- Citation failure rate: `0.0`.

The regression profile also requires p95 recall below `2000 ms`.
It requires refresh throughput above `500 artifact events/second`.
It limits peak resident memory to `2147483648 bytes`.

The workload profile requires p95 recall below `5000 ms`.
It requires refresh throughput above `500 artifact events/second`.
It limits peak resident memory to `4294967296 bytes`.
The latency gate applies to serialized MCP requests, not only direct library calls.

## Measurement method

Each measured profile runs `17` contract cases three times.
The MCP check validates `53` timed requests, including two concurrent requests.
It also checks recall during intake, raw search during semantic lag, and recall after refresh.
Legacy compatibility and missing-answer cases check the serialized V1 answer status.

The memory sampler includes the benchmark runner, MCP server, and worker descendants.
It samples resident memory every `20 ms`.
An incomplete process-tree measurement fails the benchmark.

Both profiles use macOS ARM64, Python `3.12.13`, Model2Vec `0.9.0`, and NumPy `2.5.2`.
The model is `minishlab/potion-base-8M` with `256` dimensions.
The worker count is `2`, and the ANN candidate limit is `10000`.
Both profiles enable audit logging and full private query logging.
The benchmark does not raise the latency gate or reduce the candidate limit.

## Regression result

The Model2Vec regression run completed with `3060` artifact events and `6000` source representations.
The run used `minishlab/potion-base-8M` with `256` dimensions.
The derived candidate index used the `int8-flat-v4` backend.

| Metric | Result |
| --- | ---: |
| Pass rate | `1.0` |
| Recall at 5 | `1.0` |
| Mean reciprocal rank | `0.9333` |
| No-answer accuracy | `1.0` |
| ANN candidate recall at 10 | `1.0` |
| Scope leakage rate | `0.0` |
| Citation failure rate | `0.0` |
| Direct recall p95 | `146.68 ms` |
| Serialized MCP p50 | `162.56 ms` |
| Serialized MCP p95 | `221.04 ms` |
| Serialized MCP p99 | `928.20 ms` |
| Refresh throughput | `956.46 events/second` |
| Peak process-tree resident memory | `968392704 bytes` |
| Active-intake refresh check | `1064.89 ms` |

The release profile passed all quality and performance gates.
Its log contains `119` request starts and `119` terminal responses.
The run reported no worker log failures or rejected trace records.

## Candidate-index decision

The selected backend stores one normalized int8 vector for each full vector.
It scans compact vectors in bounded blocks and retains a bounded shortlist.
It then reranks the shortlist with the full stored vectors.

The selected backend achieved `1.0` tie-aware ANN candidate recall on the regression profile.
The metric accepts equivalent candidates at an equal-score boundary only with supporting exact scores.
An empty approximate candidate set receives no credit when exact search has candidates.
The implementation records candidates scored, full vectors scored, blocks read, and exhausted work budgets.

Schema `7` adds a covering index for candidate counts and compact-vector scans.
Projected segment views keep full vectors outside the multi-segment count query.
The views preserve scope conditions and exclusions for replaced or deleted anchors.
Candidate fetching and exact reranking still use full source rows.
Tests compare candidate ordering with the previous algorithm, including equal-score boundaries.

## Query log validation

The tests compare logged result bodies with serialized V1 and V2 MCP responses.
They also verify ordered artifact reads, request correlation, private file modes, rotation, and disabled content logging.
Worker tests cover provider errors, blocked log storage, queue-only expiry, cancellation, and terminal log failures.
The log-lock test verifies record retention during brief contention without delaying the query.
The parent status reports worker failure deltas without counting the same failure twice.

Content records use a bounded background queue in each process.
No query waits for a content-log disk write.
The logs remain best-effort diagnostics, not a durable transaction journal.
See [Query Logging](query-logging.md) for setup and loss conditions.

### Slop findings

The independent review found no remaining actionable findings after correction and verification.
The corrections include a real delta fixture, compact multi-segment counts, accurate failure timing, and worker log failure reports.

### Might not be needed

The review removed synchronous worker content-log writes.
The existing bounded queue supplies the required best-effort diagnostics.
No remaining existence-level concern was confirmed.

Human review remains useful for private log retention and representative production queries.

## Model and reranker decision

The current configuration retains Model2Vec and the existing bounded relevance reranker.
The hashed provider remains a compatible fallback with limited semantic behavior.
The fallback does not claim semantic equivalence.

These synthetic cases informed implementation changes.
They are not an independent held-out evaluation.
The historical fruit-link cases return the expected reply at rank `2`, not rank `1`.
An independent query set and replacement-model comparison remain open in T09 and T13.
The current results do not prove that a learned reranker would provide no benefit.

## Capacity result

The workload contains `152050` artifact events and `300000` source representations.
It also contains `2508` Markdown documents.

| Metric | Result |
| --- | ---: |
| Pass rate | `1.0` |
| Recall at 5 | `1.0` |
| Mean reciprocal rank | `0.9333` |
| No-answer accuracy | `1.0` |
| Tie-aware ANN candidate recall at 10 | `1.0` |
| Scope leakage rate | `0.0` |
| Citation failure rate | `0.0` |
| Direct recall p95 | `2490.56 ms` |
| Serialized MCP p50 | `1658.13 ms` |
| Serialized MCP p95 | `1925.40 ms` |
| Serialized MCP p99 | `3400.29 ms` |
| Refresh throughput | `685.06 events/second` |
| Peak resident memory for the release gate | `3371253760 bytes` |
| Peak process-tree resident memory during MCP checks | `3371253760 bytes` |
| Active-intake refresh check | `5093.88 ms` |

This run passed all quality, latency, throughput, and memory gates.
Both regression and workload profiles now have passing complete performance runs.
The growth profile remains outside the supported capacity claim.

The previous full run recorded MCP p95 latency of `7047.10 ms`.
An index probe found full-row reads during compact candidate counting.
The covering index reduced one cold count from `3693 ms` to `46 ms` on a copied synthetic segment.
The final implementation also avoids full-vector reads during multi-segment counts.
It does not change candidate limits, ranking, or the recall deadline.

An intermediate full run with content logging recorded MCP p95 latency of `1766.66 ms`.
The repeat above includes the multi-segment correction and asynchronous worker logging.
Its log contains `119` request starts and `119` terminal responses across direct and MCP calls.
The run reported no worker log failures or rejected trace records.

The supported capacity claim applies to these measured synthetic profiles.
Representative production queries still require evaluation before a live deployment.

## Verification commands

Run the complete test suite:

```bash
./.venv/bin/python -m pytest -q
```

Run the regression benchmark:

```bash
./.venv/bin/ai-memory-real-world-benchmark \
  --profile regression \
  --embedding-provider model2vec \
  --repeats 3 \
  --log-query-content
```

Run the workload benchmark separately:

```bash
./.venv/bin/ai-memory-real-world-benchmark \
  --profile workload \
  --embedding-provider model2vec \
  --repeats 3 \
  --log-query-content
```

An interrupted run writes an incomplete report.
An incomplete report does not pass a release gate.
Generated source data and complete reports remain outside the public commit.
