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
- Serialized MCP quality checks and process-tree memory measurements.

The regression suite exercises the production worker pool, not a separate one-shot worker implementation.
The complete test suite passed all `483` tests.
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
| Direct recall p95 | `173.48 ms` |
| Serialized MCP p50 | `275.42 ms` |
| Serialized MCP p95 | `386.69 ms` |
| Serialized MCP p99 | `1562.96 ms` |
| Refresh throughput | `684.36 events/second` |
| Peak process-tree resident memory | `926892032 bytes` |
| Active-intake refresh check | `1422.00 ms` |

The release profile passed all quality and performance gates.

## Candidate-index decision

The selected backend stores one normalized int8 vector for each full vector.
It scans compact vectors in bounded blocks and retains a bounded shortlist.
It then reranks the shortlist with the full stored vectors.

The selected backend achieved `1.0` tie-aware ANN candidate recall on the regression profile.
The metric accepts equivalent candidates at an equal-score boundary only with supporting exact scores.
An empty approximate candidate set receives no credit when exact search has candidates.
The implementation records candidates scored, full vectors scored, blocks read, and exhausted work budgets.

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
| Direct recall p95 | `7401.61 ms` |
| Serialized MCP p50 | `4860.59 ms` |
| Serialized MCP p95 | `7047.10 ms` |
| Serialized MCP p99 | `8226.18 ms` |
| Refresh throughput | `500.28 events/second` |
| Peak process-tree resident memory | `3481288704 bytes` |
| Active-intake refresh check | `13228.80 ms` |

This run passed the quality gates but failed the unchanged p95 latency gate.
It does not approve the workload profile for release.
The growth profile also remains outside the supported capacity claim.
Only the regression profile has a passing complete performance run for the final code.

An earlier full run recorded p95 latency of `5611.32 ms`.
The review then replaced full-table candidate fetching with indexed lookup.
It also added covering indexes for unscoped identity lookup.
A query-only check passed all `53` MCP validations with p95 latency of `3312.60 ms`.
That check reused an existing corpus and did not repeat the complete build and intake benchmark.
The fresh full run above remains the capacity result for the final code.

The host showed concurrent operating-system activity after the full run.
No thermal warning was available.
These observations do not establish the cause of the timing difference or excuse the failed gate.
Repeated full measurements under controlled load remain necessary before deployment at this capacity.

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
  --repeats 3
```

Run the workload benchmark separately:

```bash
./.venv/bin/ai-memory-real-world-benchmark \
  --profile workload \
  --embedding-provider model2vec \
  --repeats 3
```

An interrupted run writes an incomplete report.
An incomplete report does not pass a release gate.
Generated source data and complete reports remain outside the public commit.
