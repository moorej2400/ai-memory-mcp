from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from ai_memory_mcp.config import _load_dotenv


PUBLIC_REPOSITORY_PATTERNS = (
    ("Windows user path", re.compile(r"[A-Za-z]:\\Users\\[^\\\r\n]+", re.IGNORECASE)),
    (
        "Unix user path",
        re.compile(r"(?<![A-Za-z0-9])/(?:home|Users)/[^/\s\"'`]+"),
    ),
    (
        "organization-specific OneDrive path",
        re.compile(r"OneDrive\s*-\s*[^\\/\r\n]+[\\/]", re.IGNORECASE),
    ),
    (
        "email address",
        re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
    ),
    (
        "GitHub token",
        re.compile(r"\b(?:gh[opusr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    ),
    ("OpenAI token", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("AWS access key", re.compile(r"\bAKIA[A-Z0-9]{16}\b")),
    (
        "private key",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    ),
)


def _commit_eligible_files(project_root: Path) -> list[tuple[Path, str]]:
    # Git defines the publication boundary, so ignored local configuration stays outside this scan.
    result = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard"],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return [
        (project_root / relative, relative)
        for relative in result.stdout.splitlines()
        if (project_root / relative).is_file()
    ]


def test_dotenv_preserves_explicit_process_overrides(
    tmp_path: Path,
    monkeypatch,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        'AI_MEMORY_TEST_FIRST="from-file"\n'
        'AI_MEMORY_TEST_SECOND="from-file"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("AI_MEMORY_TEST_FIRST", raising=False)
    monkeypatch.setenv("AI_MEMORY_TEST_SECOND", "from-process")

    _load_dotenv(env_file)

    assert os.environ["AI_MEMORY_TEST_FIRST"] == "from-file"
    assert os.environ["AI_MEMORY_TEST_SECOND"] == "from-process"


def test_runtime_scripts_do_not_depend_on_the_original_machine_path(
    project_root: Path,
) -> None:
    candidates = [
        *project_root.joinpath("src").rglob("*.py"),
        *project_root.joinpath("scripts").rglob("*.py"),
        *project_root.joinpath("scripts").rglob("*.ps1"),
    ]
    forbidden = (
        re.compile(r"[A-Za-z]:\\Users\\[^\\]+", re.IGNORECASE),
        re.compile(r"\.graphify\\services", re.IGNORECASE),
    )
    for path in candidates:
        text = path.read_text(encoding="utf-8-sig")
        assert not any(pattern.search(text) for pattern in forbidden), path


def test_commit_eligible_files_exclude_local_private_terms(
    project_root: Path,
) -> None:
    _load_dotenv(project_root / ".env")
    terms = tuple(
        term.strip().casefold()
        for term in os.getenv(
            "AI_MEMORY_PRIVATE_REPOSITORY_TERMS",
            "",
        ).split("|")
        if term.strip()
    )
    if not terms:
        return

    matches: list[tuple[Path, str]] = []
    for path, relative in _commit_eligible_files(project_root):
        try:
            text = path.read_text(encoding="utf-8-sig").casefold()
        except UnicodeDecodeError:
            continue
        for term in terms:
            if term in text or term in relative.casefold():
                matches.append((path, term))

    assert not matches, matches


def test_commit_eligible_files_exclude_private_data_shapes(
    project_root: Path,
) -> None:
    matches: list[tuple[Path, str]] = []
    for path, relative in _commit_eligible_files(project_root):
        try:
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            continue
        inspected = f"{relative}\n{text}"
        for label, pattern in PUBLIC_REPOSITORY_PATTERNS:
            if pattern.search(inspected):
                matches.append((path, label))

    assert not matches, matches
