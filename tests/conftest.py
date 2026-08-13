from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from ai_memory_mcp.config import Settings
from ai_memory_mcp.index import build_index


@pytest.fixture(scope="session")
def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def benchmark_settings(project_root: Path) -> Settings:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    benchmark = project_root / "benchmarks"
    settings = Settings(
        memory_root=benchmark / "fixtures" / "vault",
        state_dir=benchmark / "runs" / f"pytest-state-{stamp}",
        graph_path=benchmark / "fixtures" / "graph.json",
        graphify_mcp_url="",
        embedding_provider="hashed",
        artifact_db=(
            benchmark / "runs" / f"pytest-artifacts-{stamp}.sqlite3"
        ),
        artifact_objects_dir=(
            benchmark / "runs" / f"pytest-objects-{stamp}"
        ),
        artifact_backup_dir=(
            benchmark / "runs" / f"pytest-backups-{stamp}"
        ),
    )
    build_index(settings, force=True)
    return settings


@pytest.fixture
def artifact_settings(tmp_path: Path) -> Settings:
    memory_root = tmp_path / "vault"
    memory_root.mkdir()
    return Settings(
        memory_root=memory_root,
        state_dir=tmp_path / "state",
        graph_path=tmp_path / "graph.json",
        graphify_mcp_url="",
        embedding_provider="hashed",
        artifact_db=tmp_path / "artifact-state" / "artifacts.sqlite3",
        artifact_objects_dir=tmp_path / "artifact-state" / "objects",
        artifact_backup_dir=tmp_path / "artifact-state" / "backups",
    )
