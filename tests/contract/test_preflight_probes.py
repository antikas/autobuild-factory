"""The local filesystem probe reads a brief back as UTF-8 text."""

from __future__ import annotations

from pathlib import Path

from autobuild.adapters.preflight_probes import LocalFilesystemProbe


def test_read_text_returns_a_utf8_brief_unchanged(tmp_path: Path) -> None:
    brief = tmp_path / "brief.md"
    text = "# Brief\n\nItem nature: repository\n\nGrüße, café: `src/app.py`\n"
    brief.write_bytes(text.encode("utf-8"))

    assert LocalFilesystemProbe().read_text(brief) == text
