"""Every `photos_shrink` name a tool reaches for must still exist.

The tools are scripts: nothing imports them, most of their main() has no test,
and ruff cannot tell that `takeout.sha256` names a function that is gone. So a
dead-code pass removed `takeout.sha256` while `tools/takeout_upload.py` still
called it -- `--help` worked, every test passed, and the first real upload
would have died with an AttributeError.

This walks each tool's source for attribute access on an imported
`photos_shrink` module and for names imported from one, and checks each
against the real module. It is the cheapest test that makes that class of
break loud.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

TOOLS = sorted((Path(__file__).resolve().parents[1] / "tools").glob("*.py"))


def _references(source: str) -> list[tuple[str, str]]:
    """(module, name) pairs a tool uses from photos_shrink."""

    tree = ast.parse(source)
    aliases: dict[str, str] = {}
    found: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("photos_shrink"):
            for alias in node.names:
                if node.module == "photos_shrink":
                    # `from photos_shrink import takeout` binds a module.
                    aliases[alias.asname or alias.name] = f"photos_shrink.{alias.name}"
                else:
                    found.append((node.module, alias.name))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in aliases
        ):
            found.append((aliases[node.value.id], node.attr))
    return found


@pytest.mark.parametrize("tool", TOOLS, ids=lambda p: p.name)
def test_every_photos_shrink_reference_resolves(tool: Path) -> None:
    missing = [
        f"{module}.{name}"
        for module, name in _references(tool.read_text(encoding="utf-8"))
        if not hasattr(importlib.import_module(module), name)
    ]
    assert not missing, f"{tool.name} references names that no longer exist: {missing}"


def test_the_checker_catches_a_deleted_function() -> None:
    """Prove the test can fail, on the exact shape that shipped broken."""

    broken = "from photos_shrink import takeout\nx = takeout.sha256('f')\n"
    refs = _references(broken)
    assert ("photos_shrink.takeout", "sha256") in refs
    assert not hasattr(importlib.import_module("photos_shrink.takeout"), "sha256")
