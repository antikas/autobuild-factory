from __future__ import annotations

import subprocess
from pathlib import Path

from autobuild.adapters import GitWorkspaceAdapter
from autobuild.domain import (
    CampaignRef,
    CloseEvidence,
    CommandResult,
    DeliveryMode,
    DeliveryRequest,
    FinaliseRequest,
    ReviewDecision,
    ReviewVerdict,
    ValidationEvidence,
    WorkItem,
)


def git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return completed.stdout.strip()


def repository(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    git(repo, "config", "user.name", "AutoBuild Test")
    git(repo, "config", "user.email", "autobuild@example.invalid")
    (repo / ".ergon").mkdir()
    (repo / ".ergon" / "state.txt").write_text("ready\n", encoding="utf-8")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "initial")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-u", "origin", "main")
    git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    return repo, remote


def close_for(item_id: str, diff) -> CloseEvidence:
    command = CommandResult("run:item:tests", 0, "stdout", "stderr", "start", "end")
    return CloseEvidence(
        item_id,
        diff.workspace_revision,
        diff,
        ValidationEvidence("tests", diff.workspace_revision, command, diff.changed_paths),
        ReviewVerdict(item_id, ReviewDecision.PASS, (), "review"),
        "trajectory",
    )


def test_accept_commits_product_then_tracker_and_verifies_remote_delivery(tmp_path: Path) -> None:
    repo, _ = repository(tmp_path)
    adapter = GitWorkspaceAdapter(tmp_path / "scratch")
    item = WorkItem("item-1", "Add product", "brief", ("file exists",))
    workspace = adapter.create_isolated(CampaignRef("campaign", repo), item)
    (workspace.root / "product.txt").write_text("accepted\n", encoding="utf-8")
    (workspace.root / ".ergon" / "state.txt").write_text("claimed\n", encoding="utf-8")

    diff = adapter.diff(workspace)
    item_commit = adapter.commit_item(
        workspace,
        FinaliseRequest("item-1", close_for("item-1", diff), "ADDED: product"),
    )
    (workspace.root / ".ergon" / "state.txt").write_text("done\n", encoding="utf-8")
    tracker_commit = adapter.commit_tracker(workspace, "item-1", item_commit)
    result = adapter.deliver(
        workspace,
        DeliveryRequest(
            "item-1", item_commit, tracker_commit, DeliveryMode.PROTECTED_DEFAULT, "main", git(repo, "rev-parse", "HEAD")
        ),
    )
    adapter.release(workspace)

    assert [entry.path.as_posix() for entry in diff.changed_paths] == ["product.txt"]
    assert (repo / "product.txt").read_text(encoding="utf-8") == "accepted\n"
    assert (repo / ".ergon" / "state.txt").read_text(encoding="utf-8") == "done\n"
    assert result.pushed is True
    assert git(repo, "rev-parse", "HEAD") == git(repo, "ls-remote", "origin", "refs/heads/main").split()[0]
    assert workspace.root.exists() is False


def test_park_delivers_only_tracker_state_and_discards_product_work(tmp_path: Path) -> None:
    repo, _ = repository(tmp_path)
    adapter = GitWorkspaceAdapter(tmp_path / "scratch")
    item = WorkItem("item-2", "Park product", "brief", ("parked",))
    workspace = adapter.create_isolated(CampaignRef("campaign", repo), item)
    (workspace.root / "rejected.txt").write_text("must not merge\n", encoding="utf-8")
    (workspace.root / ".ergon" / "state.txt").write_text("parked\n", encoding="utf-8")

    tracker_commit = adapter.commit_tracker(workspace, "item-2", item_commit=None)
    result = adapter.deliver(
        workspace,
        DeliveryRequest(
            "item-2", None, tracker_commit, DeliveryMode.PROTECTED_DEFAULT, "main", git(repo, "rev-parse", "HEAD")
        ),
    )
    adapter.release(workspace)

    assert result.item_commit is None
    assert (repo / "rejected.txt").exists() is False
    assert (repo / ".ergon" / "state.txt").read_text(encoding="utf-8") == "parked\n"


def test_current_branch_delivery_keeps_default_branch_local_and_remote_unchanged(
    tmp_path: Path, monkeypatch
) -> None:
    repo, remote = repository(tmp_path)
    default_revision = git(repo, "rev-parse", "main")
    remote_default_revision = git(repo, "ls-remote", str(remote), "refs/heads/main").split()[0]
    git(repo, "checkout", "-b", "feature/local-pr")
    adapter = GitWorkspaceAdapter(tmp_path / "scratch")
    item = WorkItem("item-3", "Deliver locally", "brief", ("delivered",))
    workspace = adapter.create_isolated(CampaignRef("campaign", repo), item)
    (workspace.root / "product.txt").write_text("accepted\n", encoding="utf-8")
    diff = adapter.diff(workspace)
    item_commit = adapter.commit_item(
        workspace,
        FinaliseRequest("item-3", close_for("item-3", diff), "ADDED: product"),
    )
    (workspace.root / ".ergon" / "state.txt").write_text("done\n", encoding="utf-8")
    tracker_commit = adapter.commit_tracker(workspace, "item-3", item_commit)
    commands: list[tuple[str, ...]] = []
    original_git = adapter._git

    def record_git(root: Path, *args: str, **kwargs) -> str:
        commands.append(args)
        return original_git(root, *args, **kwargs)

    monkeypatch.setattr(adapter, "_git", record_git)
    result = adapter.deliver(
        workspace,
        DeliveryRequest(
            "item-3",
            item_commit,
            tracker_commit,
            DeliveryMode.CURRENT_BRANCH_PR,
            "feature/local-pr",
            default_revision,
        ),
    )
    adapter.release(workspace)

    assert result.pushed is False
    assert result.merged_commit == git(repo, "rev-parse", "HEAD")
    assert (repo / "product.txt").read_text(encoding="utf-8") == "accepted\n"
    assert (repo / ".ergon" / "state.txt").read_text(encoding="utf-8") == "done\n"
    assert git(repo, "rev-parse", "main") == default_revision
    assert git(repo, "ls-remote", str(remote), "refs/heads/main").split()[0] == remote_default_revision
    assert git(repo, "ls-remote", str(remote), "refs/heads/feature/local-pr") == ""
    assert git(repo, "status", "--porcelain") == ""
    assert not any(command[0] == "push" for command in commands)
    assert ("checkout", "main") not in commands


def test_current_branch_push_option_pushes_only_the_invoking_branch(tmp_path: Path) -> None:
    repo, remote = repository(tmp_path)
    default_revision = git(repo, "rev-parse", "main")
    git(repo, "checkout", "-b", "feature/push")
    adapter = GitWorkspaceAdapter(tmp_path / "scratch")
    item = WorkItem("item-4", "Push feature", "brief", ("delivered",))
    workspace = adapter.create_isolated(CampaignRef("campaign", repo), item)
    (workspace.root / "product.txt").write_text("accepted\n", encoding="utf-8")
    diff = adapter.diff(workspace)
    item_commit = adapter.commit_item(
        workspace,
        FinaliseRequest("item-4", close_for("item-4", diff), "ADDED: product"),
    )
    (workspace.root / ".ergon" / "state.txt").write_text("done\n", encoding="utf-8")
    tracker_commit = adapter.commit_tracker(workspace, "item-4", item_commit)
    result = adapter.deliver(
        workspace,
        DeliveryRequest(
            "item-4",
            item_commit,
            tracker_commit,
            DeliveryMode.CURRENT_BRANCH_PR,
            "feature/push",
            default_revision,
            push_current_branch=True,
        ),
    )
    adapter.release(workspace)

    assert result.pushed is True
    assert git(repo, "ls-remote", str(remote), "refs/heads/feature/push").split()[0] == result.merged_commit
    assert git(repo, "ls-remote", str(remote), "refs/heads/main").split()[0] == default_revision


def test_protected_delivery_accepts_a_detached_primary_checkout(tmp_path: Path) -> None:
    repo, _ = repository(tmp_path)
    starting_revision = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "--detach")
    adapter = GitWorkspaceAdapter(tmp_path / "scratch")
    item = WorkItem("item-5", "Deliver from detached", "brief", ("delivered",))
    workspace = adapter.create_isolated(CampaignRef("campaign", repo), item)
    (workspace.root / "product.txt").write_text("accepted\n", encoding="utf-8")
    diff = adapter.diff(workspace)
    item_commit = adapter.commit_item(
        workspace,
        FinaliseRequest("item-5", close_for("item-5", diff), "ADDED: product"),
    )
    (workspace.root / ".ergon" / "state.txt").write_text("done\n", encoding="utf-8")
    tracker_commit = adapter.commit_tracker(workspace, "item-5", item_commit)
    result = adapter.deliver(
        workspace,
        DeliveryRequest(
            "item-5",
            item_commit,
            tracker_commit,
            DeliveryMode.PROTECTED_DEFAULT,
            "main",
            starting_revision,
        ),
    )
    adapter.release(workspace)

    assert result.pushed is True
    assert (repo / "product.txt").read_text(encoding="utf-8") == "accepted\n"
    assert git(repo, "branch", "--show-current") == "main"
