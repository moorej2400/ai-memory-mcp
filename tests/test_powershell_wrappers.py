"""Structural checks for the PowerShell wrappers.

A real PowerShell parser is not available on every development machine, so
these tests assert the properties that have actually broken in practice:
Windows PowerShell 5.1 compatibility, balanced syntax, and each wrapper
pointing at an implementation that exists.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


def _wrappers(project_root: Path) -> list[Path]:
    return sorted(
        [
            *(project_root / "scripts").rglob("*.ps1"),
            *(project_root / "graphify-codebase" / "scripts").rglob("*.ps1"),
        ]
    )


def test_wrappers_exist(project_root: Path) -> None:
    assert len(_wrappers(project_root)) >= 9


def test_join_path_stays_compatible_with_windows_powershell_51(
    project_root: Path,
) -> None:
    """`Join-Path` accepts a third argument only from PowerShell 6.

    Windows ships 5.1 by default, where the three-argument form is a parse
    error, so the nested two-argument form is required.
    """
    # Matches `Join-Path a b c`, while allowing a nested `(Join-Path a b) c`.
    three_argument = re.compile(
        r"Join-Path\s+(?:[^\s()]+|\$\w+)\s+(?:'[^']*'|\"[^\"]*\"|[^\s()]+)"
        r"\s+(?:'[^']*'|\"[^\"]*\"|[^\s()]+)"
    )
    for wrapper in _wrappers(project_root):
        for number, line in enumerate(
            wrapper.read_text(encoding="utf-8-sig").splitlines(), start=1
        ):
            assert not three_argument.search(line), f"{wrapper}:{number}: {line}"


def test_wrappers_delegate_to_an_existing_implementation(
    project_root: Path,
) -> None:
    """Every wrapper must invoke a Python file that is actually present."""
    referenced = re.compile(r"'([A-Za-z0-9_./\\-]+\.py)'")
    checked = 0
    for wrapper in _wrappers(project_root):
        text = wrapper.read_text(encoding="utf-8-sig")
        for match in referenced.finditer(text):
            target = wrapper.parent / Path(match.group(1)).name
            assert target.is_file(), f"{wrapper} references missing {target}"
            checked += 1
    assert checked >= 9


@pytest.mark.parametrize("pair", [("{", "}"), ("(", ")")])
def test_wrappers_have_balanced_delimiters(
    project_root: Path, pair: tuple[str, str]
) -> None:
    opening, closing = pair
    for wrapper in _wrappers(project_root):
        text = wrapper.read_text(encoding="utf-8-sig")
        # Strip block comments and string literals before counting so quoted
        # or documented delimiters are not mistaken for code.
        stripped = re.sub(r"<#.*?#>", "", text, flags=re.DOTALL)
        stripped = re.sub(r"'[^'\n]*'", "''", stripped)
        stripped = re.sub(r'"[^"\n]*"', '""', stripped)
        assert stripped.count(opening) == stripped.count(closing), wrapper


def test_wrappers_declare_param_before_executable_statements(
    project_root: Path,
) -> None:
    """PowerShell requires a script-level `param(...)` to come first.

    Only an unindented `param(` is script level; the indented ones inside
    function bodies are declarations and may appear anywhere.
    """
    for wrapper in _wrappers(project_root):
        text = wrapper.read_text(encoding="utf-8-sig")
        without_comments = re.sub(r"<#.*?#>", "", text, flags=re.DOTALL)
        lines = [
            line
            for line in without_comments.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        if not any(line.startswith("param(") for line in lines):
            continue
        assert lines[0].startswith("param("), wrapper
