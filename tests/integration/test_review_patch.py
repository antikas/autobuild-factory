"""The review patch carries every added text file in full."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from test_git_workspace import git, repository

from autobuild.adapters import GitWorkspaceAdapter
from autobuild.domain import CampaignRef, WorkItem


def _workspace(tmp_path: Path, **options):
    repo, _ = repository(tmp_path)
    adapter = GitWorkspaceAdapter(tmp_path / "scratch", **options)
    item = WorkItem("item-patch", "Patch", "brief", ("patch",))
    return adapter, adapter.create_isolated(CampaignRef("campaign", repo), item)


def _patch(adapter: GitWorkspaceAdapter, workspace) -> str:
    return Path(adapter.diff(workspace).patch_ref).read_text(encoding="utf-8")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_an_added_text_file_is_a_new_file_hunk_and_a_binary_one_a_hash_line(
    tmp_path: Path,
) -> None:
    adapter, workspace = _workspace(tmp_path)
    (workspace.root / "README.md").write_text("base\nchanged\n", encoding="utf-8")
    (workspace.root / "tests").mkdir()
    text = workspace.root / "tests" / "test_new.py"
    text.write_text("def test_one():\n    assert 1 == 1\n", encoding="utf-8")
    binary = workspace.root / "image.bin"
    binary.write_bytes(b"\x89PNG\x00\x01\x02")

    patch = _patch(adapter, workspace)

    assert "+changed" in patch
    assert (
        "diff --git a/tests/test_new.py b/tests/test_new.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/tests/test_new.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+def test_one():\n"
        "+    assert 1 == 1\n"
    ) in patch
    assert f"# untracked tests/test_new.py sha256={_digest(text)}" in patch
    assert f"# untracked image.bin sha256={_digest(binary)}" in patch
    assert "b/image.bin" not in patch


def test_a_staged_added_file_appears_once(tmp_path: Path) -> None:
    adapter, workspace = _workspace(tmp_path)
    (workspace.root / "staged.txt").write_text("staged line\n", encoding="utf-8")
    git(workspace.root, "add", "staged.txt")

    patch = _patch(adapter, workspace)

    assert patch.count("diff --git a/staged.txt b/staged.txt") == 1
    assert patch.count("+staged line") == 1


def test_the_largest_added_file_falls_back_to_the_omission_line_under_the_limit(
    tmp_path: Path,
) -> None:
    limit = 4_000
    adapter, workspace = _workspace(tmp_path, review_patch_limit=limit)
    large = workspace.root / "large.txt"
    large.write_text("x" * 60 + "\n" * 1 + ("line\n" * 800), encoding="utf-8")
    small = workspace.root / "small.txt"
    small.write_text("small content\n", encoding="utf-8")

    patch = _patch(adapter, workspace)

    assert len(patch.encode("utf-8")) <= limit
    assert "+small content" in patch
    assert "b/large.txt" not in patch
    assert f"# untracked large.txt sha256={_digest(large)}" in patch
    assert (
        f"# content omitted: {large.stat().st_size} bytes, read large.txt in the workspace"
        in patch
    )


def test_an_added_file_that_is_not_utf8_keeps_only_its_hash_line(tmp_path: Path) -> None:
    adapter, workspace = _workspace(tmp_path)
    latin = workspace.root / "latin.txt"
    latin.write_bytes(b"caf\xe9\n")

    patch = _patch(adapter, workspace)

    assert f"# untracked latin.txt sha256={_digest(latin)}" in patch
    assert "diff --git a/latin.txt b/latin.txt" not in patch


def _windows_write_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace ``Path.write_text`` with the newline rule CPython applies on
    Windows, so default newline handling is visible on any platform."""

    def write_text(self, data, encoding=None, errors=None, newline=None):
        if newline is None:
            data = data.replace("\n", "\r\n")
        with open(self, "w", encoding=encoding, errors=errors, newline="") as handle:
            return handle.write(data)

    monkeypatch.setattr(Path, "write_text", write_text)


def _diff_as_on_windows(adapter: GitWorkspaceAdapter, workspace) -> Path:
    with pytest.MonkeyPatch.context() as monkeypatch:
        _windows_write_text(monkeypatch)
        patch_ref = adapter.diff(workspace).patch_ref
    return Path(patch_ref)


def test_the_patch_file_holds_the_patch_bytes_with_lf_line_ends(tmp_path: Path) -> None:
    adapter, workspace = _workspace(tmp_path)
    (workspace.root / "README.md").write_text("base\nchanged\n", encoding="utf-8")
    added = workspace.root / "notes.txt"
    added.write_text("first\nsecond\n", encoding="utf-8")

    data = _diff_as_on_windows(adapter, workspace).read_bytes()

    tracked = git(
        workspace.root, "diff", "--binary", "--no-ext-diff", workspace.start_commit, "--"
    )
    expected = "\n".join(
        [
            tracked,
            "diff --git a/notes.txt b/notes.txt\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/notes.txt\n"
            "@@ -0,0 +1,2 @@\n"
            "+first\n"
            "+second",
            f"# untracked notes.txt sha256={_digest(added)}",
        ]
    )
    assert "+changed" in tracked
    assert b"\r" not in data
    assert data == expected.encode("utf-8")


def test_a_patch_filled_to_the_limit_stays_under_it_on_disk(tmp_path: Path) -> None:
    limit = 4_000
    adapter, workspace = _workspace(tmp_path, review_patch_limit=limit)
    lines = workspace.root / "lines.txt"
    lines.write_text("a\n" * 1_200, encoding="utf-8")

    patch_path = _diff_as_on_windows(adapter, workspace)
    patch = patch_path.read_bytes().decode("utf-8")

    assert patch.count("\n") > limit - len(patch.encode("utf-8"))
    assert patch_path.stat().st_size <= limit
    assert "diff --git a/lines.txt b/lines.txt" in patch
    assert "# content omitted" not in patch
