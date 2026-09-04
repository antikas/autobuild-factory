from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from autobuild.adapters import FilesystemLeaseAdapter, GitWorkspaceAdapter
from autobuild.domain import (
    AdapterError,
    CampaignRef,
    CampaignReport,
    CloseEvidence,
    CommandResult,
    DeliveryMode,
    DeliveryRequest,
    EvidenceError,
    FinaliseRequest,
    LeaseHeld,
    LeaseRequest,
    LeaseSurface,
    ReviewDecision,
    ReviewVerdict,
    SurfaceKind,
    ValidationEvidence,
    WorkItem,
)


def git(root: Path, *args: str) -> str:
    # Mirrors GitWorkspaceAdapter._git: a fresh object file on this host can be
    # held briefly by another process, so transient write failures are retried.
    for attempt in range(5):
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        detail = (completed.stderr + completed.stdout).casefold()
        transient = "permission denied" in detail or "unpacker error" in detail
        if completed.returncode == 0 or not transient or attempt == 4:
            break
        time.sleep(0.2 * (attempt + 1))
    assert completed.returncode == 0, (
        f"git {' '.join(args)} in {root} exited {completed.returncode}: "
        f"{completed.stderr.strip()} {completed.stdout.strip()}"
    )
    return completed.stdout.strip()


def repository(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    git(tmp_path, "init", "--bare", str(remote))
    git(tmp_path, "init", "-b", "main", str(repo))
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


def test_a_worktree_lease_admits_one_writer_and_releases_idempotently(tmp_path: Path) -> None:
    repo, _ = repository(tmp_path)
    workspace_adapter = GitWorkspaceAdapter(tmp_path / "scratch")
    item = WorkItem("item-lease", "Guarded worktree", "brief", ("guarded",))
    workspace = workspace_adapter.create_isolated(CampaignRef("campaign", repo), item)
    surface = LeaseSurface(workspace.root, SurfaceKind.WORKTREE)

    holder = FilesystemLeaseAdapter(
        tmp_path / "scratch", host="host-a", process_id=111, is_alive=lambda pid: True
    )
    grant = holder.acquire(LeaseRequest(surface, "campaign-a"))
    assert grant.record.surface_kind is SurfaceKind.WORKTREE

    contender = FilesystemLeaseAdapter(
        tmp_path / "scratch", host="host-a", process_id=222, is_alive=lambda pid: True
    )
    with pytest.raises(LeaseHeld):
        contender.acquire(LeaseRequest(surface, "campaign-b"))

    first = holder.release(grant)
    second = holder.release(grant)
    assert first.released is True
    assert second.released is False

    workspace_adapter.release(workspace)


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


def test_report_commit_lands_on_default_branch_touching_only_its_path(tmp_path: Path) -> None:
    repo, remote = repository(tmp_path)
    adapter = GitWorkspaceAdapter(tmp_path / "scratch")
    last_tracker_commit = git(repo, "rev-parse", "HEAD")

    result = adapter.deliver_report(
        CampaignReport(
            "camp-1",
            repo,
            "docs/campaigns/camp-1.md",
            "# Campaign camp-1\n\n## Shipped\n\nnone\n",
            DeliveryMode.PROTECTED_DEFAULT,
            "main",
            last_tracker_commit,
        )
    )

    report_commit = git(repo, "rev-parse", "HEAD")
    assert result.pushed is True
    assert (repo / "docs" / "campaigns" / "camp-1.md").read_text(encoding="utf-8").startswith(
        "# Campaign camp-1"
    )
    touched = git(repo, "show", "--name-only", "--format=", "HEAD").split()
    assert touched == ["docs/campaigns/camp-1.md"]
    assert git(repo, "rev-parse", "HEAD^") == last_tracker_commit
    assert git(repo, "log", "-1", "--format=%s", "HEAD") == "ADDED: campaign camp-1 report"
    assert git(repo, "ls-remote", str(remote), "refs/heads/main").split()[0] == report_commit
    assert git(repo, "status", "--porcelain") == ""


def test_current_branch_report_commits_locally_without_push(tmp_path: Path) -> None:
    repo, remote = repository(tmp_path)
    git(repo, "checkout", "-b", "feature/report")
    adapter = GitWorkspaceAdapter(tmp_path / "scratch")
    last = git(repo, "rev-parse", "HEAD")

    result = adapter.deliver_report(
        CampaignReport(
            "camp-2",
            repo,
            "docs/campaigns/camp-2.md",
            "# Campaign camp-2\n",
            DeliveryMode.CURRENT_BRANCH_PR,
            "feature/report",
            last,
        )
    )

    assert result.pushed is False
    assert git(repo, "branch", "--show-current") == "feature/report"
    assert git(repo, "rev-parse", "HEAD^") == last
    assert git(repo, "ls-remote", str(remote), "refs/heads/feature/report") == ""
    assert git(repo, "status", "--porcelain") == ""


def test_release_tolerates_residue_after_git_deregisters_worktree(
    tmp_path: Path, monkeypatch
) -> None:
    repo, _ = repository(tmp_path)
    adapter = GitWorkspaceAdapter(tmp_path / "scratch")
    workspace = adapter.create_isolated(
        CampaignRef("campaign", repo),
        WorkItem("item-residue", "Leave residue", "brief", ("released",)),
    )
    original_git = adapter._git

    def fail_after_unregister(root: Path, *args: str, **kwargs) -> str:
        if args[:3] == ("worktree", "remove", "--force"):
            original_git(root, *args, **kwargs)
            workspace.root.mkdir(parents=True)
            (workspace.root / "residue.txt").write_text("locked earlier\n", encoding="utf-8")
            raise AdapterError("git worktree remove failed: Directory not empty")
        return original_git(root, *args, **kwargs)

    monkeypatch.setattr(adapter, "_git", fail_after_unregister)
    adapter.release(workspace)
    adapter.release(workspace)

    assert workspace.root.exists()
    assert str(workspace.root.resolve()) not in git(repo, "worktree", "list", "--porcelain")


def test_release_refuses_while_uncommitted_tracker_files_exist(tmp_path: Path) -> None:
    repo, _ = repository(tmp_path)
    adapter = GitWorkspaceAdapter(tmp_path / "scratch")
    item = WorkItem("item-log", "Preserve tracker log", "brief", ("preserved",))
    workspace = adapter.create_isolated(CampaignRef("campaign", repo), item)
    (workspace.root / ".ergon" / "log").mkdir(parents=True)
    (workspace.root / ".ergon" / "log" / "x.jsonl").write_text(
        '{"event": "claim"}\n', encoding="utf-8"
    )

    with pytest.raises(EvidenceError, match="uncommitted tracker files"):
        adapter.release(workspace)
    assert workspace.root.exists()
    assert adapter._worktree_registered(repo, workspace.root)

    adapter.commit_tracker(workspace, "item-log", item_commit=None)
    adapter.release(workspace)
    assert workspace.root.exists() is False


def test_park_snapshot_patch_reproduces_the_parked_tree(tmp_path: Path) -> None:
    repo, _ = repository(tmp_path)
    adapter = GitWorkspaceAdapter(tmp_path / "scratch")
    item = WorkItem("item-snap", "Snapshot before release", "brief", ("preserved",))
    workspace = adapter.create_isolated(CampaignRef("campaign", repo), item)
    (workspace.root / "README.md").write_text("base\nmore\n", encoding="utf-8")
    (workspace.root / "added.txt").write_text("brand new\n", encoding="utf-8")

    parked_readme = (workspace.root / "README.md").read_bytes()
    parked_added = (workspace.root / "added.txt").read_bytes()

    snapshot = adapter.snapshot(workspace)

    assert snapshot.start_commit == workspace.start_commit
    assert snapshot.patch
    assert {file.path: file.content for file in snapshot.files} == {"added.txt": parked_added}

    verify = tmp_path / "verify"
    subprocess.run(
        ["git", "clone", "--quiet", str(repo), str(verify)], check=True, capture_output=True
    )
    git(verify, "checkout", "--quiet", workspace.start_commit)
    patch_file = tmp_path / "changes.patch"
    patch_file.write_bytes(snapshot.patch)
    git(verify, "apply", str(patch_file))
    for file in snapshot.files:
        (verify / file.path).write_bytes(file.content)

    assert (verify / "README.md").read_bytes() == parked_readme
    assert (verify / "added.txt").read_bytes() == parked_added


def test_a_seat_commit_after_diff_trips_the_head_pin(tmp_path: Path) -> None:
    repo, _ = repository(tmp_path)
    adapter = GitWorkspaceAdapter(tmp_path / "scratch")
    item = WorkItem("item-head", "Head pin", "brief", ("pinned",))
    workspace = adapter.create_isolated(CampaignRef("campaign", repo), item)
    (workspace.root / "product.txt").write_text("accepted\n", encoding="utf-8")

    diff = adapter.diff(workspace)
    recorded = diff.head_commit
    assert recorded == workspace.start_commit

    git(workspace.root, "add", "product.txt")
    git(workspace.root, "commit", "-m", "seat sneaks a commit after review")
    observed = git(workspace.root, "rev-parse", "HEAD")
    assert observed != recorded

    with pytest.raises(EvidenceError) as raised:
        adapter.commit_item(
            workspace,
            FinaliseRequest("item-head", close_for("item-head", diff), "ADDED: product"),
        )

    message = str(raised.value)
    assert recorded in message
    assert observed in message


def test_a_partial_commit_leaves_the_untracked_file_and_keeps_it_in_the_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    repo, _ = repository(tmp_path)
    adapter = GitWorkspaceAdapter(tmp_path / "scratch")
    item = WorkItem("item-partial", "Full tree", "brief", ("complete",))
    workspace = adapter.create_isolated(CampaignRef("campaign", repo), item)
    (workspace.root / "product.txt").write_text("shipped\n", encoding="utf-8")
    (workspace.root / "left.txt").write_text("left behind\n", encoding="utf-8")

    diff = adapter.diff(workspace)
    assert {entry.path.as_posix() for entry in diff.changed_paths} == {"product.txt", "left.txt"}

    original_git = adapter._git

    def drop_left(root: Path, *args: str, **kwargs) -> str:
        if args and args[0] in {"add", "commit"}:
            args = tuple(arg for arg in args if arg != "left.txt")
        return original_git(root, *args, **kwargs)

    monkeypatch.setattr(adapter, "_git", drop_left)

    with pytest.raises(EvidenceError, match="uncommitted product paths remain"):
        adapter.commit_item(
            workspace,
            FinaliseRequest("item-partial", close_for("item-partial", diff), "ADDED: product"),
        )

    snapshot = adapter.snapshot(workspace)
    assert "left.txt" in {file.path for file in snapshot.files}
    assert (workspace.root / "left.txt").read_text(encoding="utf-8") == "left behind\n"


def test_release_preserves_lease_when_worktree_remains_registered(
    tmp_path: Path, monkeypatch
) -> None:
    repo, _ = repository(tmp_path)
    adapter = GitWorkspaceAdapter(tmp_path / "scratch")
    workspace = adapter.create_isolated(
        CampaignRef("campaign", repo),
        WorkItem("item-retry", "Retry cleanup", "brief", ("released",)),
    )
    original_git = adapter._git
    fail_remove = True

    def fail_while_registered(root: Path, *args: str, **kwargs) -> str:
        nonlocal fail_remove
        if fail_remove and args[:3] == ("worktree", "remove", "--force"):
            raise AdapterError("git worktree remove failed before deregistration")
        return original_git(root, *args, **kwargs)

    monkeypatch.setattr(adapter, "_git", fail_while_registered)
    with pytest.raises(AdapterError, match="before deregistration"):
        adapter.release(workspace)

    fail_remove = False
    adapter.release(workspace)
    assert workspace.root.exists() is False


def test_list_worktrees_reports_a_built_worktree_matching_its_marker(tmp_path: Path) -> None:
    repo, _ = repository(tmp_path)
    adapter = GitWorkspaceAdapter(tmp_path / "scratch")
    campaign = CampaignRef("campaign", repo)
    item = WorkItem("item-w", "Interrupted build", "brief", ("built",))
    workspace = adapter.create_isolated(campaign, item)
    (workspace.root / "product.txt").write_text("in progress\n", encoding="utf-8")

    # The digest the diff records for the built worktree is what a phase marker
    # would have stored, so a resume can compare them.
    diff = adapter.diff(workspace)

    statuses = adapter.list_worktrees(campaign)

    matched = [status for status in statuses if status.root == workspace.root.resolve()]
    assert len(matched) == 1
    status = matched[0]
    assert status.head_commit == diff.head_commit
    assert status.workspace_revision == diff.workspace_revision
    assert status.branch == workspace.branch
    adapter.release(workspace)


def test_adopt_worktree_takes_a_working_handle_on_an_existing_worktree(tmp_path: Path) -> None:
    repo, _ = repository(tmp_path)
    adapter = GitWorkspaceAdapter(tmp_path / "scratch")
    campaign = CampaignRef("campaign", repo)
    item = WorkItem("item-a", "Adopt me", "brief", ("adopted",))
    created = adapter.create_isolated(campaign, item)
    (created.root / "product.txt").write_text("carried over\n", encoding="utf-8")

    adopted = adapter.adopt_worktree(campaign, item, created.root)

    assert adopted.root == created.root.resolve()
    assert adopted.branch == created.branch
    assert adopted.lease_id != created.lease_id
    # the adopted handle can diff the work already on disk
    diff = adapter.diff(adopted)
    assert [entry.path.as_posix() for entry in diff.changed_paths] == ["product.txt"]
    adapter.release(adopted)


def test_adopt_worktree_refuses_a_path_that_is_not_a_registered_worktree(tmp_path: Path) -> None:
    repo, _ = repository(tmp_path)
    adapter = GitWorkspaceAdapter(tmp_path / "scratch")
    stray = tmp_path / "scratch" / "worktrees" / "stray"
    stray.mkdir(parents=True)

    with pytest.raises((EvidenceError, AdapterError)):
        adapter.adopt_worktree(
            CampaignRef("campaign", repo),
            WorkItem("item-x", "Stray", "brief", ("stray",)),
            stray,
        )
