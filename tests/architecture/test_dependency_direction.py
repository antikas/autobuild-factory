"""Architecture tripwires: mechanisms never leak into workflow logic."""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).parents[2] / "src" / "autobuild"

FORBIDDEN_IMPORTS = {
    "domain": ("autobuild.adapters", "autobuild.application", "autobuild.bootstrap", "autobuild.enforcement", "autobuild.ports", "platform", "subprocess"),
    "ports": ("autobuild.adapters", "autobuild.application", "autobuild.bootstrap", "autobuild.enforcement", "platform", "subprocess"),
    "application": ("autobuild.adapters", "autobuild.bootstrap", "platform", "subprocess"),
    "enforcement": ("autobuild.adapters", "autobuild.application", "autobuild.bootstrap"),
}
FORBIDDEN_LOGIC_WORDS = {"claude", "codex", "copilot", "windows", "darwin", "linux"}


def violations(root: Path) -> list[str]:
    found: list[str] = []
    for layer, prefixes in FORBIDDEN_IMPORTS.items():
        layer_root = root / layer
        if not layer_root.exists():
            continue
        for path in layer_root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = tuple(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    names = (node.module or "",)
                else:
                    names = ()
                for name in names:
                    if any(name == prefix or name.startswith(f"{prefix}.") for prefix in prefixes):
                        found.append(f"{path.relative_to(root)}:{node.lineno}: forbidden import {name}")
                if layer == "application" and isinstance(node, ast.Constant) and isinstance(node.value, str):
                    lowered = node.value.casefold()
                    for word in FORBIDDEN_LOGIC_WORDS:
                        if word in lowered:
                            found.append(f"{path.relative_to(root)}:{node.lineno}: provider/host word {word!r}")
    return found


def test_dependency_direction_is_clean() -> None:
    assert violations(SRC) == []


def test_tripwire_detects_a_mechanism_leak(tmp_path: Path) -> None:
    application = tmp_path / "application"
    application.mkdir()
    (application / "bad.py").write_text("import subprocess\nPROVIDER = 'codex'\n", encoding="utf-8")
    found = violations(tmp_path)
    assert any("forbidden import subprocess" in violation for violation in found)
    assert any("provider/host word 'codex'" in violation for violation in found)
