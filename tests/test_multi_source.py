from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from ai_memory_mcp.config import MemorySource, Settings
from ai_memory_mcp.index import build_index, current_index_path
from ai_memory_mcp.service import MemoryService


def _write_memory(
    root: Path,
    relative: str,
    *,
    memory_id: str,
    title: str,
    text: str,
) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            (
                "---",
                f"memory_id: {memory_id}",
                f"title: {title}",
                "status: active",
                "---",
                "",
                f"# {title}",
                "",
                text,
                "",
            )
        ),
        encoding="utf-8",
    )


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.md")):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def test_recall_searches_primary_and_retrieval_only_sources(
    tmp_path: Path,
) -> None:
    core = tmp_path / "core"
    archive = tmp_path / "archive"
    _write_memory(
        core,
        "Notes/Shared.md",
        memory_id="mem-core-shared",
        title="Primary record",
        text="The primary vault remains the only writable source.",
    )
    _write_memory(
        archive,
        "Notes/Shared.md",
        memory_id="mem-archive-shared",
        title="Archive record",
        text="The amber compass identifies the retrieval-only archive.",
    )
    settings = Settings(
        memory_root=core,
        state_dir=tmp_path / "state",
        graph_path=tmp_path / "graph.json",
        graphify_mcp_url="",
        retrieval_sources=(
            MemorySource(source_id="archive", root=archive),
        ),
    )
    before = (_tree_digest(core), _tree_digest(archive))
    result = build_index(settings, force=True)
    after = (_tree_digest(core), _tree_digest(archive))

    response = MemoryService(settings).recall("amber compass", limit=2)

    assert result["documents"] == 2
    assert result["parse_errors"] == []
    assert before == after
    assert response.status == "answered"
    assert response.citations[0].source_id == "archive"
    assert response.citations[0].path == "archive/Notes/Shared.md"


def test_status_marks_only_the_primary_source_writable(tmp_path: Path) -> None:
    core = tmp_path / "core"
    archive = tmp_path / "archive"
    core.mkdir()
    archive.mkdir()
    settings = Settings(
        memory_root=core,
        state_dir=tmp_path / "state",
        graph_path=tmp_path / "graph.json",
        graphify_mcp_url="",
        retrieval_sources=(
            MemorySource(source_id="archive", root=archive),
        ),
    )

    status = MemoryService(settings).status()

    assert status.canonical_memory_root.source_id == "core"
    assert status.canonical_memory_root.writable is True
    assert status.retrieval_sources[0].source_id == "archive"
    assert status.retrieval_sources[0].writable is False


def test_settings_load_named_retrieval_sources(
    tmp_path: Path,
    monkeypatch,
) -> None:
    core = tmp_path / "core"
    archive = tmp_path / "archive"
    reference = tmp_path / "reference"
    monkeypatch.setenv("AI_MEMORY_WORK_DIR", str(core))
    monkeypatch.setenv("AI_MEMORY_PRIMARY_SOURCE_ID", "core")
    monkeypatch.setenv(
        "AI_MEMORY_RETRIEVAL_SOURCES",
        json.dumps(
            {
                "archive": str(archive),
                "reference": str(reference),
            }
        ),
    )
    monkeypatch.setenv("AI_MEMORY_PERSONAL_DIR", "")

    settings = Settings.from_env()

    assert settings.memory_root == core
    assert [
        source.source_id for source in settings.retrieval_sources
    ] == ["archive", "reference"]


def test_graph_merge_prefixes_each_memory_source(
    project_root: Path,
    tmp_path: Path,
) -> None:
    source_arguments: list[str] = []
    for source_id in ("core", "archive"):
        output = tmp_path / source_id / "graphify-out"
        output.mkdir(parents=True)
        (output / "graph.json").write_text(
            json.dumps(
                {
                    "nodes": [
                        {
                            "id": "shared",
                            "label": source_id,
                            "source_file": "Notes/Shared.md",
                        }
                    ],
                    "links": [],
                }
            ),
            encoding="utf-8",
        )
        (output / "manifest.json").write_text(
            json.dumps({"Notes/Shared.md": {"sha256": source_id}}),
            encoding="utf-8",
        )
        source_arguments.extend(
            ("--source", f"{source_id}={output / 'graph.json'}")
        )

    merged = tmp_path / "merged"
    result = subprocess.run(
        [
            sys.executable,
            str(
                project_root
                / "scripts"
                / "graphify"
                / "merge-memory-source-graphs.py"
            ),
            *source_arguments,
            "--output-dir",
            str(merged),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    graph = json.loads((merged / "graph.json").read_text(encoding="utf-8"))
    manifest = json.loads(
        (merged / "manifest.json").read_text(encoding="utf-8")
    )

    assert result.returncode == 0, result.stderr
    assert {node["id"] for node in graph["nodes"]} == {
        "core::shared",
        "archive::shared",
    }
    assert {node["source_file"] for node in graph["nodes"]} == {
        "core/Notes/Shared.md",
        "archive/Notes/Shared.md",
    }
    assert set(manifest) == {
        "core/Notes/Shared.md",
        "archive/Notes/Shared.md",
    }


def test_changed_memory_id_collision_preserves_last_valid_record(
    tmp_path: Path,
) -> None:
    core = tmp_path / "core"
    state = tmp_path / "state"
    first = "Notes/First.md"
    second = "Notes/Second.md"
    _write_memory(
        core,
        first,
        memory_id="mem-first",
        title="First",
        text="First valid record.",
    )
    _write_memory(
        core,
        second,
        memory_id="mem-second",
        title="Second",
        text="Second valid record.",
    )
    settings = Settings(
        memory_root=core,
        state_dir=state,
        graph_path=tmp_path / "graph.json",
        graphify_mcp_url="",
    )
    build_index(settings, force=True)
    _write_memory(
        core,
        second,
        memory_id="mem-first",
        title="Second",
        text="This changed record has a duplicate ID.",
    )

    result = build_index(settings)
    index_path = current_index_path(settings)
    assert index_path is not None

    with sqlite3.connect(index_path) as connection:
        rows = connection.execute(
            "SELECT memory_id, path FROM documents ORDER BY path"
        ).fetchall()

    assert len(result["parse_errors"]) == 1
    assert rows == [
        ("mem-first", "core/Notes/First.md"),
        ("mem-second", "core/Notes/Second.md"),
    ]


def test_graphify_extraction_excludes_restricted_directories(
    project_root: Path,
) -> None:
    script = (
        project_root / "scripts" / "graphify" / "extract-ai-memory.ps1"
    ).read_text(encoding="utf-8")

    assert "'**/Restricted/**'" in script
    assert "'**/.trash/**'" in script
