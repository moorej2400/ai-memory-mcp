from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from ai_memory_mcp.config import MemorySource, Settings
from ai_memory_mcp.memory_graph import MemoryGraph
from ai_memory_mcp.index import MemoryIndex, build_index, current_index_path
from ai_memory_mcp.models import ScopeFilter
from ai_memory_mcp.retrieval import RetrievalEngine
from ai_memory_mcp.service import MemoryService


def _write_memory(
    root: Path,
    relative: str,
    *,
    memory_id: str,
    title: str,
    text: str,
    related: list[str] | None = None,
) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        f"memory_id: {memory_id}",
        f"title: {title}",
        "status: active",
    ]
    if related:
        lines.append("related:")
        lines.extend(f"  - {value}" for value in related)
    lines.extend(("---", "", f"# {title}", "", text, ""))
    path.write_text("\n".join(lines), encoding="utf-8")


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
        retrieval_sources=(
            MemorySource(source_id="archive", root=archive),
        ),
    )

    status = MemoryService(settings).status()

    assert status.canonical_memory_root.source_id == "core"
    assert status.canonical_memory_root.writable is True
    assert status.retrieval_sources[0].source_id == "archive"
    assert status.retrieval_sources[0].writable is False


def test_index_detects_markdown_changes_after_publication(
    tmp_path: Path,
) -> None:
    core = tmp_path / "core"
    relative = "Notes/Freshness.md"
    _write_memory(
        core,
        relative,
        memory_id="mem-freshness",
        title="Freshness",
        text="The index matches this source.",
    )
    settings = Settings(
        memory_root=core,
        state_dir=tmp_path / "state",
        graph_path=tmp_path / "graph.json",
        embedding_provider="hashed",
    )
    result = build_index(settings, force=True)
    index = MemoryIndex(settings, path=Path(str(result["snapshot"])))

    assert index.canonical_stale() is False

    _write_memory(
        core,
        relative,
        memory_id="mem-freshness",
        title="Freshness",
        text="The canonical Markdown changed after publication.",
    )

    assert index.canonical_stale() is True


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


def test_scope_filters_treat_like_metacharacters_as_literals(
    tmp_path: Path,
) -> None:
    core = tmp_path / "core"
    _write_memory(
        core,
        "Repos/a%b/Percent.md",
        memory_id="mem-percent",
        title="Percent repository",
        text="The literal wildcard marker belongs to percent.",
    )
    _write_memory(
        core,
        "Repos/axb/Other.md",
        memory_id="mem-other",
        title="Other repository",
        text="The literal wildcard marker belongs to another repository.",
    )
    _write_memory(
        core,
        "Repos/a_b/Underscore.md",
        memory_id="mem-underscore",
        title="Underscore repository",
        text="The literal underscore marker belongs here.",
    )
    _write_memory(
        core,
        "Repos/acb/Letter.md",
        memory_id="mem-letter",
        title="Letter repository",
        text="The literal underscore marker belongs elsewhere.",
    )
    settings = Settings(
        memory_root=core,
        state_dir=tmp_path / "state",
        graph_path=tmp_path / "graph.json",
        embedding_provider="hashed",
    )
    build_index(settings, force=True)
    service = MemoryService(settings)

    percent = service.recall(
        "literal wildcard marker",
        repository="a%b",
        limit=10,
    )
    underscore = service.recall(
        "literal underscore marker",
        repository="a_b",
        limit=10,
    )

    assert {item.memory_id for item in percent.evidence} == {"mem-percent"}
    assert {item.memory_id for item in underscore.evidence} == {"mem-underscore"}


def test_exact_document_lookup_treats_like_metacharacters_as_literals(
    benchmark_settings: Settings,
) -> None:
    build_index(benchmark_settings, force=True)
    index = MemoryIndex(benchmark_settings)

    assert index.document("%") is None
    assert index.document("_") is None


def test_graph_traversal_cannot_cross_a_scoped_source(tmp_path: Path) -> None:
    core = tmp_path / "core"
    archive = tmp_path / "archive"
    _write_memory(
        core,
        "Notes/A.md",
        memory_id="mem-core-a",
        title="Core A",
        text="First scoped graph endpoint.",
        related=["[[Archive bridge]]"],
    )
    _write_memory(
        core,
        "Notes/B.md",
        memory_id="mem-core-b",
        title="Core B",
        text="Second scoped graph endpoint.",
    )
    _write_memory(
        archive,
        "Notes/Bridge.md",
        memory_id="mem-archive-bridge",
        title="Archive bridge",
        text="This document must not bridge scoped traversal.",
    )
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "id": "a",
                        "label": "Core A",
                        "source_file": "core/Notes/A.md",
                    },
                    {
                        "id": "bridge",
                        "label": "Archive bridge",
                        "source_file": "archive/Notes/Bridge.md",
                    },
                    {
                        "id": "b",
                        "label": "Core B",
                        "source_file": "core/Notes/B.md",
                    },
                ],
                "links": [
                    {"source": "a", "target": "bridge"},
                    {"source": "bridge", "target": "b"},
                ],
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(
        memory_root=core,
        state_dir=tmp_path / "state",
        graph_path=graph_path,
        retrieval_sources=(
            MemorySource(source_id="archive", root=archive),
        ),
        embedding_provider="hashed",
    )
    build_index(settings, force=True)
    engine = RetrievalEngine(settings)
    scope = ScopeFilter(source_id="core", status="active")

    neighbors = engine.neighbors("mem-core-a", depth=4, scope=scope)
    relationship_path = engine.path("mem-core-a", "mem-core-b", scope=scope)

    assert neighbors["graph_neighbors"] == []
    assert neighbors["declared_related"] == []
    assert relationship_path["found"] is False
    assert relationship_path["path"] == []


def test_weighted_multi_hop_decay_applies_once_per_edge(tmp_path: Path) -> None:
    graph_path = tmp_path / "weighted-graph.json"
    graph_path.write_text(
        json.dumps(
            {
                "graph": {},
                "nodes": [
                    {"id": "a", "label": "A", "source_file": "core/A.md"},
                    {"id": "x", "label": "Scope node"},
                    {"id": "b", "label": "B", "source_file": "core/B.md"},
                    {"id": "c", "label": "C", "source_file": "core/C.md"},
                ],
                "links": [
                    {"source": "a", "target": "x", "confidence": "high"},
                    {"source": "x", "target": "b", "confidence": "high"},
                    {"source": "a", "target": "c", "confidence": "low"},
                ],
            }
        ),
        encoding="utf-8",
    )
    adapter = MemoryGraph(graph_path, source_ids=("core",))

    ranked = adapter.rank("", ["core/A.md"], limit=4, max_depth=2)

    assert ranked.index("core/B.md") < ranked.index("core/C.md")


def test_weighted_traversal_replaces_a_weaker_path_found_first(
    tmp_path: Path,
) -> None:
    graph_path = tmp_path / "weighted-relaxation.json"
    graph_path.write_text(
        json.dumps(
            {
                "graph": {},
                "nodes": [
                    {"id": "a", "label": "A", "source_file": "core/A.md"},
                    {"id": "x", "label": "X"},
                    {"id": "y", "label": "Y"},
                    {"id": "u", "label": "U"},
                    {"id": "z", "label": "Z", "source_file": "core/Z.md"},
                    {"id": "w", "label": "W", "source_file": "core/W.md"},
                ],
                "links": [
                    {"source": "a", "target": "x", "confidence": "low"},
                    {"source": "a", "target": "y", "confidence": "high"},
                    {"source": "y", "target": "x", "confidence": "high"},
                    {"source": "x", "target": "z", "confidence": "high"},
                    {"source": "a", "target": "u", "confidence": "high"},
                    {"source": "u", "target": "w", "confidence": "low"},
                ],
            }
        ),
        encoding="utf-8",
    )
    adapter = MemoryGraph(graph_path, source_ids=("core",))

    ranked = adapter.rank("", ["core/A.md"], limit=6, max_depth=3)

    assert ranked.index("core/Z.md") < ranked.index("core/W.md")
