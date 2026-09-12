# Mixed Real-World Benchmark

This benchmark generates a synthetic Markdown vault and synthetic artifacts.
The committed contract contains no live user or organization data.

The benchmark tests the complete `memory_recall` pipeline.
It covers semantic recall, exact identifiers, scopes, wildcards, supersession, graph relationships, citations, chats, meetings, noise, and missing answers.

The generator provides six workload profiles:

- `smoke` supports automated tests and quick checks.
- `standard` supports normal workstation measurements.
- `scale` contains more than 150,000 artifact events.
- `regression` supplies the release-quality profile.
- `workload` models sustained workstation data.
- `growth` tests the configured resource limits.

Generated data and reports stay in the ignored `benchmarks/runs/` directory.
The repository stores only the deterministic generator and its public-safe contract.

## Run the benchmark

On macOS or Linux, run this command:

```bash
./.venv/bin/ai-memory-real-world-benchmark --profile standard
```

On Windows, run this command:

```powershell
.\.venv\Scripts\ai-memory-real-world-benchmark.exe --profile standard
```

Use the `scale` profile for a larger performance test.
The `scale` profile can require substantial time and disk space.

```bash
./.venv/bin/ai-memory-real-world-benchmark --profile scale --repeats 5
```

The default provider uses Model2Vec when Model2Vec is available.
The runner skips cases that require semantic retrieval when it uses the hashed fallback.

## Compare performance

Use a prior report from the same machine as the baseline.
This method reduces differences that hardware can cause.

```bash
./.venv/bin/ai-memory-real-world-benchmark \
  --profile standard \
  --baseline benchmarks/runs/real-world-BASELINE/report.json \
  --maximum-regression-ratio 1.5
```

The comparison checks generation time and recall latency.
The command fails when a checked metric exceeds the selected ratio.

The report includes corpus size, storage size, intake time, refresh throughput, and latency percentiles.
It samples resident memory for the runner, MCP server, and worker descendants every 20 milliseconds.
It records model metadata, resource limits, and package versions.
It measures ANN candidate recall against exact search on identical vectors.
It marks exhausted exact comparisons as incomplete.
It validates serialized V2 evidence, citations, and execution state.
It checks the V1 answer status for absence and compatibility cases.
It tests unscoped historical links, concurrent calls, and recall during intake.

Install the `dev` and `semantic` extras before a measured semantic run.
The memory sampler requires process-inspection access.
An environment that blocks process inspection cannot produce a valid memory measurement.
Synthetic contract results are not an independent held-out model comparison.
