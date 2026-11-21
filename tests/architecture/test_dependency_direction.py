"""Architecture tripwires: mechanisms never leak into workflow logic."""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).parents[2] / "src" / "autobuild"

HOST_MECHANISMS = ("ctypes", "platform", "subprocess")

FORBIDDEN_IMPORTS = {
    "domain": ("autobuild.adapters", "autobuild.application", "autobuild.bootstrap", "autobuild.enforcement", "autobuild.ports", *HOST_MECHANISMS),
    "ports": ("autobuild.adapters", "autobuild.application", "autobuild.bootstrap", "autobuild.enforcement", *HOST_MECHANISMS),
    "application": ("autobuild.adapters", "autobuild.bootstrap", *HOST_MECHANISMS),
    "enforcement": ("autobuild.adapters", "autobuild.application", "autobuild.bootstrap", *HOST_MECHANISMS),
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


def test_tripwire_detects_a_host_mechanism_in_a_logic_layer(tmp_path: Path) -> None:
    enforcement = tmp_path / "enforcement"
    enforcement.mkdir()
    (enforcement / "capacity.py").write_text(
        "import ctypes\nfrom ctypes import wintypes\nimport platform\nimport subprocess\n",
        encoding="utf-8",
    )
    domain = tmp_path / "domain"
    domain.mkdir()
    (domain / "models.py").write_text("import ctypes.util\n", encoding="utf-8")

    found = sorted(violation.replace("\\", "/") for violation in violations(tmp_path))

    assert found == [
        "domain/models.py:1: forbidden import ctypes.util",
        "enforcement/capacity.py:1: forbidden import ctypes",
        "enforcement/capacity.py:2: forbidden import ctypes",
        "enforcement/capacity.py:3: forbidden import platform",
        "enforcement/capacity.py:4: forbidden import subprocess",
    ]


CLOCK_LAYERS = ("application", "domain")

# Each wall-clock read, by its fully qualified name, and the name it is reported
# under. ``time.monotonic`` and ``time.sleep`` are interval mechanisms the core
# receives injected, so they are not listed.
WALL_CLOCK_READS = {
    "datetime.datetime.now": "datetime.now",
    "datetime.datetime.utcnow": "datetime.utcnow",
    "datetime.datetime.today": "datetime.today",
    "datetime.date.today": "date.today",
    "time.time": "time.time",
    "time.time_ns": "time.time_ns",
    "time.localtime": "time.localtime",
    "time.gmtime": "time.gmtime",
    "time.ctime": "time.ctime",
}


def _qualified(node: ast.AST, aliases: dict[str, str]) -> str | None:
    if isinstance(node, ast.Name):
        return aliases.get(node.id)
    if isinstance(node, ast.Attribute):
        base = _qualified(node.value, aliases)
        return f"{base}.{node.attr}" if base else None
    return None


def wall_clock_reads(root: Path) -> list[str]:
    found: list[str] = []
    for layer in CLOCK_LAYERS:
        layer_root = root / layer
        if not layer_root.exists():
            continue
        for path in sorted(layer_root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            aliases: dict[str, str] = {}
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.asname:
                            aliases[alias.asname] = alias.name
                        else:
                            head = alias.name.split(".")[0]
                            aliases[head] = head
                elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                    for alias in node.names:
                        aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Name, ast.Attribute)):
                    continue
                name = WALL_CLOCK_READS.get(_qualified(node, aliases) or "")
                if name:
                    found.append(f"{path.relative_to(root)}:{node.lineno}: wall-clock read {name}")
    return found


def test_logic_layers_read_no_wall_clock() -> None:
    assert wall_clock_reads(SRC) == []


def test_tripwire_detects_a_wall_clock_read(tmp_path: Path) -> None:
    application = tmp_path / "application"
    application.mkdir()
    (application / "stamp.py").write_text(
        "from datetime import UTC, datetime\n\n\ndef stamp():\n    return datetime.now(UTC)\n",
        encoding="utf-8",
    )
    (application / "interval.py").write_text(
        "import time\n\n\ndef wait():\n    start = time.monotonic()\n    time.sleep(0)\n    return start\n",
        encoding="utf-8",
    )
    domain = tmp_path / "domain"
    domain.mkdir()
    (domain / "clock.py").write_text(
        "from time import time\n\n\ndef now():\n    return time()\n",
        encoding="utf-8",
    )

    found = sorted(violation.replace("\\", "/") for violation in wall_clock_reads(tmp_path))

    assert found == [
        "application/stamp.py:5: wall-clock read datetime.now",
        "domain/clock.py:5: wall-clock read time.time",
    ]


def test_tripwire_resolves_module_imports_and_aliases(tmp_path: Path) -> None:
    application = tmp_path / "application"
    application.mkdir()
    (application / "aliases.py").write_text(
        "import datetime as dt\nimport time as clock\nfrom datetime import date as day\n"
        "a = dt.datetime.utcnow()\nb = clock.time_ns()\nc = day.today()\nd = clock.gmtime\n",
        encoding="utf-8",
    )

    found = sorted(violation.replace("\\", "/") for violation in wall_clock_reads(tmp_path))

    assert found == [
        "application/aliases.py:4: wall-clock read datetime.utcnow",
        "application/aliases.py:5: wall-clock read time.time_ns",
        "application/aliases.py:6: wall-clock read date.today",
        "application/aliases.py:7: wall-clock read time.gmtime",
    ]
