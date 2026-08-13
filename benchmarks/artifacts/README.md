# Artifact Store Benchmark

This benchmark generates neutral synthetic artifact data.
The generated data stays in the ignored `benchmarks/runs/` directory.

The default fixture contains these records:

- 100 conversations
- 100,000 messages
- 100 meetings
- 100 transcripts
- 50,000 transcript cues

Run the benchmark on macOS or Linux:

```bash
PYTHONPATH=src ./.venv/bin/python benchmarks/artifacts/generate_fixture.py
```

Run the benchmark on Windows:

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe benchmarks\artifacts\generate_fixture.py
```

The command does not replace an existing run directory.
The command validates the fixed fixture digest before intake.

The report contains intake, search, ordered-read, burst-index, and fused-recall measurements.
The measurements do not define strict performance limits.
