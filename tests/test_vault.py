from __future__ import annotations

from pathlib import Path

import yaml

from ai_memory_mcp.intake import inspect_markdown
from ai_memory_mcp.vault import initialize_vault


def test_initialize_vault_creates_minimum_generic_structure(tmp_path: Path) -> None:
    first = initialize_vault(tmp_path)
    second = initialize_vault(tmp_path)

    assert first["changed"] is True
    assert second["changed"] is False
    assert (tmp_path / "Notes").is_dir()
    home = (tmp_path / "Home.md").read_text()
    assert inspect_markdown(home, path="Home.md", require_current=True).issues == ()
    review = yaml.safe_load((tmp_path / "Views/Review Queue.base").read_text())
    assert review["filters"]["and"] == ['status == "needs-review"']
    actions = yaml.safe_load((tmp_path / "Views/Action Dashboard.base").read_text())
    assert actions["filters"]["and"] == [
        'record_type == "action"',
        'status == "active"',
    ]


def test_initialize_vault_does_not_replace_an_existing_file(tmp_path: Path) -> None:
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "Home.md").write_text("# Existing\n", encoding="utf-8")

    result = initialize_vault(tmp_path)
    assert (tmp_path / "Home.md").read_text() == "# Existing\n"
    assert "Home.md" not in result["created"]
