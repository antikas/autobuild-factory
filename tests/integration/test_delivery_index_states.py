"""Delivery of every state a builder can leave its worktree and index in."""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Iterable
from pathlib import Path

import pytest

from autobuild.adapters import GitWorkspaceAdapter
from autobuild.domain import (
    AdapterError,
    CampaignRef,
    ChangeKind,
    CloseEvidence,
    CommandResult,
    DeliveryMode,
    DeliveryRequest,
    EvidenceError,
    FinaliseRequest,
    ReviewDecision,
    ReviewVerdict,
    ValidationEvidence,
    WorkItem,
    WorkspaceRef,
)

ITEM = "item-1"
MOVED = ("DESIGN.md", *(f"part-{number:02}.md" for number in range(1, 18)))
# Names with letters outside ASCII, built from code points so this file stays ASCII.
E_ACUTE = chr(0xE9)
U_UMLAUT = chr(0xFC)
CAFE = f"docs/caf{E_ACUTE}.md"
NEW_CAFE = f"docs/new caf{E_ACUTE} notes.md"
OLD_NOTES = f"old notes {E_ACUTE}.md"
NEW_NOTES = f"docs/new notes {U_UMLAUT}.md"
SEED = (
    "README.md",
    *MOVED,
    "docs/Design notes.md",
    CAFE,
    OLD_NOTES,
    "keep/one.md",
)


def git_bytes(root: Path, *args: str) -> bytes:
    for attempt in range(5):
        completed = subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=False)
        detail = (completed.stderr + completed.stdout).decode("utf-8", "replace").casefold()
        transient = "permission denied" in detail or "unpacker error" in detail
        if completed.returncode == 0 or not transient or attempt == 4:
            break
        time.sleep(0.2 * (attempt + 1))
    assert completed.returncode == 0, f"git {' '.join(args)} in {root}: {detail}"
    return completed.stdout


def git(root: Path, *args: str) -> str:
    return git_bytes(root, *args).decode("utf-8").strip()


def write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def close_for(diff) -> CloseEvidence:
    command = CommandResult("run:item:tests", 0, "stdout", "stderr", "start", "end")
    return CloseEvidence(
        ITEM,
        diff.workspace_revision,
        diff,
        ValidationEvidence("tests", diff.workspace_revision, command, diff.changed_paths),
        ReviewVerdict(ITEM, ReviewDecision.PASS, (), "review"),
        "trajectory",
    )


class Bench:
    def __init__(self, tmp_path: Path) -> None:
        self.remote = tmp_path / "remote.git"
        self.repo = tmp_path / "repo"
        git(tmp_path, "init", "--bare", str(self.remote))
        git(tmp_path, "init", "-b", "main", str(self.repo))
        git(self.repo, "config", "user.name", "AutoBuild Test")
        git(self.repo, "config", "user.email", "autobuild@example.invalid")
        write(self.repo, ".ergon/state.txt", "ready\n")
        for name in SEED:
            write(self.repo, name, f"base of {name}\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "initial")
        git(self.repo, "remote", "add", "origin", str(self.remote))
        git(self.repo, "push", "-u", "origin", "main")
        git(self.remote, "symbolic-ref", "HEAD", "refs/heads/main")
        self.adapter = GitWorkspaceAdapter(tmp_path / "scratch")
        self.workspace: WorkspaceRef = self.adapter.create_isolated(
            CampaignRef("campaign", self.repo),
            WorkItem(ITEM, "Change product", "brief", ("delivered",)),
        )
        self.root = self.workspace.root

    def commit_item(self, diff) -> str:
        return self.adapter.commit_item(
            self.workspace, FinaliseRequest(ITEM, close_for(diff), "CHANGED: product")
        )

    def tree(self, commit: str) -> dict[str, bytes]:
        listing = git_bytes(self.root, "ls-tree", "-r", "-z", commit).decode("utf-8")
        tree: dict[str, bytes] = {}
        for line in filter(None, listing.split("\0")):
            details, path = line.split("\t", 1)
            tree[path] = git_bytes(self.root, "cat-file", "blob", details.split()[2])
        return tree

    def files(self) -> dict[str, bytes]:
        files: dict[str, bytes] = {}
        for path in self.root.rglob("*"):
            relative = path.relative_to(self.root).as_posix()
            if relative == ".git" or relative.startswith(".ergon/") or (
                path.is_dir() and not path.is_symlink()
            ):
                continue
            files[relative] = (
                os.readlink(path).encode("utf-8") if path.is_symlink() else path.read_bytes()
            )
        return files

    def deliver(self, absent: Iterable[str] = ()) -> tuple[str, str]:
        """Deliver the worktree and assert what holds for every index state."""

        files = self.files()
        diff = self.adapter.diff(self.workspace)
        item_commit = self.commit_item(diff)

        product = {
            path: content
            for path, content in self.tree(item_commit).items()
            if not path.startswith(".ergon/")
        }
        assert product == files
        for path in absent:
            assert path not in product
        status = git(self.root, "status", "--porcelain", "--untracked-files=all", "--", ":(exclude).ergon")
        assert status == ""

        write(self.root, ".ergon/state.txt", "done\n")
        tracker_commit = self.adapter.commit_tracker(self.workspace, ITEM, item_commit)
        result = self.adapter.deliver(
            self.workspace,
            DeliveryRequest(
                ITEM,
                item_commit,
                tracker_commit,
                DeliveryMode.PROTECTED_DEFAULT,
                "main",
                git(self.repo, "rev-parse", "HEAD"),
            ),
        )
        assert result.pushed is True
        git(self.remote, "merge-base", "--is-ancestor", item_commit, "refs/heads/main")
        return item_commit, tracker_commit


@pytest.fixture
def bench(tmp_path: Path) -> Bench:
    return Bench(tmp_path)


def stage_rename(bench: Bench) -> None:
    (bench.root / "docs").mkdir(exist_ok=True)
    git(bench.root, "mv", "DESIGN.md", "docs/DESIGN.md")


def test_a_staged_rename_is_delivered(bench: Bench) -> None:
    stage_rename(bench)
    bench.deliver(absent=("DESIGN.md",))


def test_a_staged_deletion_is_delivered(bench: Bench) -> None:
    git(bench.root, "rm", "--quiet", "DESIGN.md")
    bench.deliver(absent=("DESIGN.md",))


def test_an_unstaged_deletion_is_delivered(bench: Bench) -> None:
    (bench.root / "DESIGN.md").unlink()
    bench.deliver(absent=("DESIGN.md",))


def test_files_moved_into_a_new_folder_with_the_moves_staged_are_delivered(bench: Bench) -> None:
    (bench.root / "design").mkdir()
    git(bench.root, "mv", *MOVED, "design")
    bench.deliver(absent=MOVED)


def test_files_moved_into_a_new_folder_on_disk_only_are_delivered(bench: Bench) -> None:
    (bench.root / "design").mkdir()
    for name in MOVED:
        (bench.root / name).rename(bench.root / "design" / name)
    bench.deliver(absent=MOVED)


def test_an_untracked_file_with_a_space_and_non_ascii_name_is_delivered(bench: Bench) -> None:
    write(bench.root, NEW_CAFE, "new\n")
    bench.deliver()


def test_a_modified_file_with_a_non_ascii_name_is_delivered(bench: Bench) -> None:
    write(bench.root, CAFE, "changed\n")
    bench.deliver()


def test_an_unstaged_deletion_of_a_name_with_a_space_is_delivered(bench: Bench) -> None:
    (bench.root / "docs" / "Design notes.md").unlink()
    bench.deliver(absent=("docs/Design notes.md",))


def test_a_staged_rename_of_a_name_with_a_space_and_non_ascii_is_delivered(bench: Bench) -> None:
    git(bench.root, "mv", OLD_NOTES, NEW_NOTES)
    bench.deliver(absent=(OLD_NOTES,))


def test_untracked_staged_and_restaged_files_are_delivered_from_the_worktree(bench: Bench) -> None:
    write(bench.root, "untracked.md", "untracked\n")
    write(bench.root, "staged.md", "staged\n")
    write(bench.root, "restaged.md", "staged content\n")
    git(bench.root, "add", "staged.md", "restaged.md")
    write(bench.root, "restaged.md", "worktree content\n")

    item_commit, _ = bench.deliver()

    assert bench.tree(item_commit)["restaged.md"] == (bench.root / "restaged.md").read_bytes()
    assert b"worktree content" in bench.tree(item_commit)["restaged.md"]


@pytest.mark.parametrize("stage", [("add",), ("add", "--intent-to-add")], ids=["staged", "intent"])
def test_a_new_file_staged_then_deleted_is_dropped(bench: Bench, stage: tuple[str, ...]) -> None:
    write(bench.root, "passing.md", "short lived\n")
    git(bench.root, *stage, "passing.md")
    (bench.root / "passing.md").unlink()
    write(bench.root, "README.md", "changed\n")

    changed = bench.adapter.diff(bench.workspace).changed_paths

    bench.deliver(absent=("passing.md",))

    assert {entry.path.as_posix(): entry.kind for entry in changed} == {
        "README.md": ChangeKind.MODIFIED,
        "passing.md": ChangeKind.DELETED,
    }


def test_an_edit_staged_then_undone_on_disk_is_unstaged(bench: Bench) -> None:
    base = (bench.root / "DESIGN.md").read_bytes()
    write(bench.root, "DESIGN.md", "staged and then undone\n")
    git(bench.root, "add", "DESIGN.md")
    (bench.root / "DESIGN.md").write_bytes(base)
    write(bench.root, "README.md", "changed\n")

    item_commit, _ = bench.deliver()

    assert bench.tree(item_commit)["DESIGN.md"] == base
    assert git(
        bench.root, "diff", "--name-only", f"{item_commit}^", item_commit
    ) == "README.md"


def test_a_new_file_staged_then_deleted_alone_is_refused_as_no_change(bench: Bench) -> None:
    write(bench.root, "passing.md", "short lived\n")
    git(bench.root, "add", "passing.md")
    (bench.root / "passing.md").unlink()
    write(bench.root, ".ergon/state.txt", "claimed\n")
    git(bench.root, "add", ".ergon")
    before = git(bench.root, "ls-files", "--stage")

    diff = bench.adapter.diff(bench.workspace)
    with pytest.raises(EvidenceError, match="no scoped product change"):
        bench.commit_item(diff)

    assert git(bench.root, "rev-parse", "HEAD") == bench.workspace.start_commit
    assert git(bench.root, "ls-files", "--stage") == before


@pytest.mark.parametrize("staged", [True, False])
def test_a_folder_moved_and_replaced_by_a_file_is_delivered(bench: Bench, staged: bool) -> None:
    if staged:
        git(bench.root, "mv", "keep", "kept")
    else:
        (bench.root / "keep").rename(bench.root / "kept")
    write(bench.root, "keep", "now a file\n")
    bench.deliver(absent=("keep/one.md",))


@pytest.mark.parametrize("staged", [True, False])
def test_a_file_replaced_by_a_folder_of_its_name_is_delivered(bench: Bench, staged: bool) -> None:
    if staged:
        git(bench.root, "rm", "--quiet", "DESIGN.md")
    else:
        (bench.root / "DESIGN.md").unlink()
    write(bench.root, "DESIGN.md/body.md", "now a folder\n")
    bench.deliver(absent=("DESIGN.md",))


@pytest.mark.parametrize("staged", [True, False])
def test_a_folder_moved_and_replaced_by_a_link_to_it_is_delivered(
    bench: Bench, staged: bool
) -> None:
    if staged:
        git(bench.root, "mv", "keep", "kept")
    else:
        (bench.root / "keep").rename(bench.root / "kept")
    try:
        (bench.root / "keep").symlink_to("kept", target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links are not available")
    bench.deliver(absent=("keep/one.md",))


def test_an_untracked_file_left_on_disk_under_an_ignore_rule_is_refused(bench: Bench) -> None:
    git(bench.root, "rm", "--quiet", "--cached", "DESIGN.md")
    write(bench.root, ".gitignore", "DESIGN.md\n")
    diff = bench.adapter.diff(bench.workspace)

    with pytest.raises(AdapterError, match="ignored"):
        bench.commit_item(diff)

    assert git(bench.root, "rev-parse", "HEAD") == bench.workspace.start_commit


def test_a_staged_rename_with_a_later_write_is_refused_before_staging(bench: Bench) -> None:
    stage_rename(bench)
    diff = bench.adapter.diff(bench.workspace)
    write(bench.root, "late.md", "not reviewed\n")
    before = git(bench.root, "ls-files", "--stage")

    with pytest.raises(EvidenceError, match="workspace changed after validation and review"):
        bench.commit_item(diff)

    assert git(bench.root, "rev-parse", "HEAD") == bench.workspace.start_commit
    assert git(bench.root, "ls-files", "--stage") == before


def test_a_failed_staging_leaves_the_start_commit_and_a_whole_snapshot(
    bench: Bench, tmp_path: Path
) -> None:
    stage_rename(bench)
    diff = bench.adapter.diff(bench.workspace)
    git_dir = Path(git(bench.root, "rev-parse", "--absolute-git-dir"))
    (git_dir / "index.lock").touch()

    with pytest.raises(AdapterError, match="index.lock"):
        bench.commit_item(diff)

    assert git(bench.root, "rev-parse", "HEAD") == bench.workspace.start_commit
    snapshot = bench.adapter.snapshot(bench.workspace)
    assert snapshot.start_commit == bench.workspace.start_commit
    verify = tmp_path / "verify"
    git(tmp_path, "clone", "--quiet", str(bench.repo), str(verify))
    git(verify, "checkout", "--quiet", snapshot.start_commit)
    patch_file = tmp_path / "changes.patch"
    patch_file.write_bytes(snapshot.patch)
    git(verify, "apply", str(patch_file))
    for file in snapshot.files:
        (verify / file.path).write_bytes(file.content)
    assert not (verify / "DESIGN.md").exists()
    assert (verify / "docs/DESIGN.md").read_bytes() == (bench.root / "docs/DESIGN.md").read_bytes()


def test_a_tracker_change_beside_a_staged_rename_stays_out_of_the_item_commit(bench: Bench) -> None:
    stage_rename(bench)
    write(bench.root, ".ergon/state.txt", "claimed\n")
    git(bench.root, "add", ".ergon")

    item_commit, tracker_commit = bench.deliver(absent=("DESIGN.md",))

    def changed_in(commit: str) -> list[str]:
        return git(
            bench.root, "diff", "--name-only", "--no-renames", f"{commit}^", commit
        ).splitlines()

    assert changed_in(item_commit) == ["DESIGN.md", "docs/DESIGN.md"]
    assert changed_in(tracker_commit) == [".ergon/state.txt"]
