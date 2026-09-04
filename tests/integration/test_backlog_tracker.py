from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from autobuild.adapters import BacklogTrackerAdapter, GitWorkspaceAdapter
from autobuild.domain import (
    AdapterError,
    CampaignRef,
    CloseEvidence,
    CommandResult,
    DeliveryMode,
    DeliveryRequest,
    FinaliseRequest,
    Proposal,
    ReviewDecision,
    ReviewVerdict,
    ValidationEvidence,
)


def run(*argv: str) -> str:
    attempts = 5 if argv and argv[0] == "git" else 1
    for attempt in range(attempts):
        completed = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode == 0 or attempt == attempts - 1:
            break
        time.sleep(0.2 * (attempt + 1))
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"{' '.join(argv)} failed: {detail}")
    return completed.stdout.strip()


def git(root: Path, *args: str) -> str:
    return run("git", "-C", str(root), *args)


def backlog_repo(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    run("git", "init", "--bare", str(remote))
    run("git", "init", "-b", "main", str(repo))
    git(repo, "config", "user.name", "AutoBuild Test")
    git(repo, "config", "user.email", "autobuild@example.invalid")
    (repo / "docs").mkdir()
    (repo / "docs" / "brief.md").write_text(
        "# Brief\n\n## Acceptance\n\n- Product and tracker commits reach the remote.\n",
        encoding="utf-8",
    )
    (repo / "BACKLOG.md").write_text(
        "# Backlog\n\n"
        "| Item | Title | Status | Brief |\n"
        "|---|---|---|---|\n"
        "| TST-001 | Prove backlog adapter | Ready | docs/brief.md |\n",
        encoding="utf-8",
    )
    git(repo, "add", ".")
    git(repo, "commit", "-m", "initial")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-u", "origin", "main")
    return repo


def test_backlog_claim_close_and_proposal_use_separate_tracker_commits(
    tmp_path: Path,
) -> None:
    repo = backlog_repo(tmp_path)
    campaign = CampaignRef("campaign", repo)
    tracker = BacklogTrackerAdapter(repo)
    workspace_adapter = GitWorkspaceAdapter(
        tmp_path / "scratch", tracker_paths=("BACKLOG.md",)
    )

    assert tracker.probe().available is True
    item = tracker.next_item(campaign)
    assert item is not None
    assert item.item_id == "TST-001"
    tracker.claim(item, "builder@proof")
    assert git(repo, "status", "--porcelain") == ""

    workspace = workspace_adapter.create_isolated(campaign, item)
    (workspace.root / "product.txt").write_text("accepted\n", encoding="utf-8")
    diff = workspace_adapter.diff(workspace)
    assert {path.path.as_posix() for path in diff.changed_paths} == {"product.txt"}
    command = CommandResult("run:item:tests", 0, "stdout", "stderr", "start", "end")
    evidence = CloseEvidence(
        item.item_id,
        diff.workspace_revision,
        diff,
        ValidationEvidence("tests", diff.workspace_revision, command, diff.changed_paths),
        ReviewVerdict(item.item_id, ReviewDecision.PASS, (), "review"),
        "trajectory",
    )
    item_commit = workspace_adapter.commit_item(
        workspace, FinaliseRequest(item.item_id, evidence, "ADDED: product")
    )
    tracker.close(evidence, item_commit, workspace, "coordinator@proof")
    tracker_commit = workspace_adapter.commit_tracker(workspace, item.item_id, item_commit)
    result = workspace_adapter.deliver(
        workspace,
        DeliveryRequest(
            item.item_id, item_commit, tracker_commit, DeliveryMode.PROTECTED_DEFAULT, "main", git(repo, "rev-parse", "HEAD")
        ),
    )
    workspace_adapter.release(workspace)

    proposal = tracker.propose(
        Proposal("Candidate", "What should be built?", "Queue is dry", "docs/brief.md"),
        "coordinator@proof",
    )
    backlog = (repo / "BACKLOG.md").read_text(encoding="utf-8")
    assert result.pushed is True
    assert "| TST-001 | Prove backlog adapter | Done (" in backlog
    assert (
        "| ABP-001 | Candidate | Proposed: What should be built? Queue is dry | docs/brief.md |"
        in backlog
    )
    assert proposal.runnable is False
    assert tracker.next_item(campaign) is None
    assert git(repo, "rev-parse", "HEAD") == git(
        repo, "ls-remote", "origin", "refs/heads/main"
    ).split()[0]


def test_backlog_ready_items_lists_ready_rows_in_table_order(tmp_path: Path) -> None:
    repo = backlog_repo(tmp_path)
    (repo / "BACKLOG.md").write_text(
        "# Backlog\n\n"
        "| Item | Title | Status | Brief |\n"
        "|---|---|---|---|\n"
        "| TST-001 | First | Ready | docs/brief.md |\n"
        "| TST-002 | Second | Claimed by other | docs/brief.md |\n"
        "| TST-003 | Third | Ready | docs/brief.md |\n",
        encoding="utf-8",
    )
    tracker = BacklogTrackerAdapter(repo)

    items = tracker.ready_items(CampaignRef("campaign", repo))

    assert [item.item_id for item in items] == ["TST-001", "TST-003"]
    assert items[0].brief_ref == "docs/brief.md"


def test_backlog_refuses_a_claim_before_mutating_a_dirty_checkout(
    tmp_path: Path,
) -> None:
    repo = backlog_repo(tmp_path)
    tracker = BacklogTrackerAdapter(repo)
    item = tracker.next_item(CampaignRef("campaign", repo))
    assert item is not None
    before = (repo / "BACKLOG.md").read_bytes()
    (repo / "dirty.txt").write_text("preserve me\n", encoding="utf-8")

    with pytest.raises(AdapterError, match="clean before a tracker claim"):
        tracker.claim(item, "builder@proof")

    assert (repo / "BACKLOG.md").read_bytes() == before


def test_backlog_resumable_claims_lists_only_builder_claimed_rows(tmp_path: Path) -> None:
    repo = backlog_repo(tmp_path)
    (repo / "BACKLOG.md").write_text(
        "# Backlog\n\n"
        "| Item | Title | Status | Brief |\n"
        "|---|---|---|---|\n"
        "| TST-001 | Interrupted | Claimed by builder@lane-one | docs/brief.md |\n"
        "| TST-002 | Owned by a person | Claimed by alice | docs/brief.md |\n"
        "| TST-003 | Finished | Done (abc123) | docs/brief.md |\n"
        "| TST-004 | Set aside | Parked: resume:head-moved | docs/brief.md |\n"
        "| TST-005 | Waiting | Ready | docs/brief.md |\n",
        encoding="utf-8",
    )
    tracker = BacklogTrackerAdapter(repo)

    claims = tracker.resumable_claims(CampaignRef("campaign", repo))

    assert [item.item_id for item in claims] == ["TST-001"]
    assert claims[0].brief_ref == "docs/brief.md"
