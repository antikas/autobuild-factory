from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from autobuild.adapters import GitWorkspaceAdapter, PinaxTrackerAdapter
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
from autobuild.domain.errors import EvidenceError


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


def pinax(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["pinax", "--root", str(root), *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"pinax {args[0]} failed: {detail}")
    return completed.stdout.strip()


def pinax_repo(tmp_path: Path) -> tuple[Path, str]:
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    run("git", "init", "--bare", str(remote))
    run("git", "init", "-b", "main", str(repo))
    git(repo, "config", "user.name", "AutoBuild Test")
    git(repo, "config", "user.email", "autobuild@example.invalid")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    pinax(repo, "init", "--actor", "coordinator@proof")
    created = json.loads(
        pinax(
            repo,
            "add",
            "--title",
            "Prove tracker adapter",
            "--prefix",
            "tst",
            "--actor",
            "coordinator@proof",
            "--json",
        )
    )
    item_id = str(created.get("item_id") or created["id"])
    (repo / "docs").mkdir()
    (repo / "docs" / "brief.md").write_text(
        "# Brief\n\n## Acceptance\n\n- Product and tracker commits reach the remote default branch.\n",
        encoding="utf-8",
    )
    pinax(
        repo,
        "note",
        "add",
        item_id,
        "--ref",
        "docs/brief.md",
        "--caption",
        "Product and tracker commits reach the remote default branch.",
        "--actor",
        "coordinator@proof",
        "--json",
    )
    git(repo, "add", ".")
    git(repo, "commit", "-m", "initial")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-u", "origin", "main")
    return repo, item_id


def test_pinax_claim_and_close_are_delivered_as_tracker_commits(tmp_path: Path) -> None:
    repo, item_id = pinax_repo(tmp_path)
    campaign = CampaignRef("campaign", repo)
    tracker = PinaxTrackerAdapter(repo)
    workspace_adapter = GitWorkspaceAdapter(tmp_path / "scratch")

    item = tracker.next_item(campaign)
    assert item is not None
    assert item.item_id == item_id
    claim = tracker.claim(item, "builder@proof")
    assert claim.item_id == item_id
    assert git(repo, "status", "--porcelain") == ""

    workspace = workspace_adapter.create_isolated(campaign, item)
    (workspace.root / "product.txt").write_text("accepted\n", encoding="utf-8")
    diff = workspace_adapter.diff(workspace)
    command = CommandResult("run:item:tests", 0, "stdout", "stderr", "start", "end")
    evidence = CloseEvidence(
        item_id,
        diff.workspace_revision,
        diff,
        ValidationEvidence("tests", diff.workspace_revision, command, diff.changed_paths),
        ReviewVerdict(item_id, ReviewDecision.PASS, (), "review"),
        "trajectory",
    )
    item_commit = workspace_adapter.commit_item(
        workspace, FinaliseRequest(item_id, evidence, "ADDED: product")
    )
    tracker.close(evidence, item_commit, workspace, "coordinator@proof")
    tracker_commit = workspace_adapter.commit_tracker(workspace, item_id, item_commit)
    result = workspace_adapter.deliver(
        workspace,
        DeliveryRequest(
            item_id, item_commit, tracker_commit, DeliveryMode.PROTECTED_DEFAULT, "main", git(repo, "rev-parse", "HEAD")
        ),
    )
    workspace_adapter.release(workspace)

    status = json.loads(pinax(repo, "status", "--json"))
    proposal = tracker.propose(
        Proposal("Candidate", "What should be built?", "Queue is dry", "docs/brief.md"),
        "coordinator@proof",
    )
    assert result.pushed is True
    assert any(entry["id"] == item_id for entry in status["repo"]["shipped_recent"])
    assert proposal.runnable is False
    assert tracker.next_item(campaign) is None
    assert git(repo, "rev-parse", "HEAD") == git(repo, "ls-remote", "origin", "refs/heads/main").split()[0]


def test_pinax_ready_items_lists_ready_work_with_briefs(tmp_path: Path) -> None:
    repo, item_id = pinax_repo(tmp_path)
    tracker = PinaxTrackerAdapter(repo)

    items = tracker.ready_items(CampaignRef("campaign", repo))

    assert [item.item_id for item in items] == [item_id]
    assert items[0].brief_ref == "docs/brief.md"


def test_pinax_refuses_a_claim_before_mutating_a_dirty_primary_checkout(
    tmp_path: Path,
) -> None:
    repo, _ = pinax_repo(tmp_path)
    tracker = PinaxTrackerAdapter(repo)
    item = tracker.next_item(CampaignRef("campaign", repo))
    assert item is not None
    before = {
        path.name: path.read_bytes() for path in (repo / ".ergon" / "log").glob("*.jsonl")
    }
    (repo / "dirty.txt").write_text("preserve me\n", encoding="utf-8")

    with pytest.raises(AdapterError, match="clean before a tracker claim"):
        tracker.claim(item, "builder@proof")

    after = {
        path.name: path.read_bytes() for path in (repo / ".ergon" / "log").glob("*.jsonl")
    }
    assert after == before


@pytest.mark.parametrize(
    "brief_ref",
    ("docs/brief.md", "briefs/candidate.md", "koine://plans/candidate"),
)
def test_pinax_accepts_portable_proposal_references(brief_ref: str) -> None:
    PinaxTrackerAdapter.validate_proposal(
        Proposal("Candidate", "What should be built?", "Queue is dry", brief_ref)
    )


@pytest.mark.parametrize(
    "brief_ref",
    ("../private.md", "~/private/brief.md", "/tmp/brief.md", "C:\\briefs\\item.md"),
)
def test_pinax_rejects_machine_specific_proposal_references(brief_ref: str) -> None:
    with pytest.raises(EvidenceError, match="repository-relative path or durable URI"):
        PinaxTrackerAdapter.validate_proposal(
            Proposal("Candidate", "What should be built?", "Queue is dry", brief_ref)
        )


def test_pinax_resumable_claims_lists_builder_claimed_and_not_terminal(tmp_path: Path) -> None:
    repo, item_id = pinax_repo(tmp_path)
    campaign = CampaignRef("campaign", repo)
    tracker = PinaxTrackerAdapter(repo)

    # Nothing is claimed yet.
    assert tracker.resumable_claims(campaign) == ()

    item = tracker.next_item(campaign)
    assert item is not None
    tracker.claim(item, "builder@lane-one")

    claims = tracker.resumable_claims(campaign)
    assert [claim.item_id for claim in claims] == [item_id]
    assert claims[0].brief_ref == "docs/brief.md"
