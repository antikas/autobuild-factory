from __future__ import annotations

import json
from pathlib import Path

import pytest
from scanner_retry import run_retrying

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


def run(*argv: str, cwd: Path | None = None) -> str:
    completed = run_retrying(argv, cwd=cwd)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"{' '.join(argv)} failed: {detail}")
    return completed.stdout.strip()


def git(root: Path, *args: str) -> str:
    return run("git", "-C", str(root), *args)


def pinax(root: Path, *args: str) -> str:
    return run("pinax", "--root", str(root), *args, cwd=root)


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


@pytest.mark.usefixtures("self_committing_pinax")
def test_pinax_claim_and_close_are_delivered_as_tracker_commits(tmp_path: Path) -> None:
    repo, item_id = pinax_repo(tmp_path)
    campaign = CampaignRef("campaign", repo)
    tracker = PinaxTrackerAdapter(repo)
    workspace_adapter = GitWorkspaceAdapter(tmp_path / "scratch", tracker_commits_itself=True)

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
    pinax_commit = git(workspace.root, "rev-parse", "HEAD")
    tracker_commit = workspace_adapter.commit_tracker(workspace, item_id, item_commit)
    assert tracker_commit == pinax_commit
    assert git(workspace.root, "rev-parse", f"{tracker_commit}^") == item_commit
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
    # A staged change is dirt; an untracked product file alone is not (see
    # test_recovery_gaps_delivery.py).
    (repo / "dirty.txt").write_text("preserve me\n", encoding="utf-8")
    git(repo, "add", "dirty.txt")

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


@pytest.mark.usefixtures("self_committing_pinax")
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


@pytest.mark.usefixtures("self_committing_pinax")
def test_pinax_known_item_ids_lists_created_items_in_every_status(tmp_path: Path) -> None:
    repo, ready_id = pinax_repo(tmp_path)
    campaign = CampaignRef("campaign", repo)
    tracker = PinaxTrackerAdapter(repo)

    def add(title: str) -> str:
        created = json.loads(
            pinax(
                repo, "add", "--title", title, "--prefix", "tst",
                "--actor", "coordinator@proof", "--json",
            )
        )
        return str(created.get("item_id") or created["id"])

    blocked_id = add("Blocked behind a gate")
    pinax(repo, "block", blocked_id, "--gate", "proposal", "--actor", "coordinator@proof", "--json")
    done_id = add("Already done")
    pinax(repo, "status", done_id, "done", "--actor", "coordinator@proof", "--json")

    def status_of(item_id: str) -> str:
        text = (repo / ".ergon" / "items" / f"{item_id}.md").read_text(encoding="utf-8")
        line = next(line for line in text.splitlines() if line.startswith("status:"))
        return line.partition(":")[2].strip()

    assert status_of(blocked_id) == "blocked"
    assert status_of(done_id) == "done"
    assert status_of(ready_id) != "blocked"

    assert tracker.known_item_ids(campaign) == frozenset({ready_id, blocked_id, done_id})
    assert [item.item_id for item in tracker.ready_items(campaign)] == [ready_id]

    late_id = add("Created after launch")
    assert late_id in tracker.known_item_ids(campaign)


def test_pinax_known_item_ids_refuses_another_repository(tmp_path: Path) -> None:
    repo, _ = pinax_repo(tmp_path)
    tracker = PinaxTrackerAdapter(repo)

    with pytest.raises(AdapterError):
        tracker.known_item_ids(CampaignRef("campaign", tmp_path / "other"))


def _add_with_brief(repo: Path, title: str) -> str:
    created = json.loads(
        pinax(
            repo, "add", "--title", title, "--prefix", "tst",
            "--actor", "coordinator@proof", "--json",
        )
    )
    item_id = str(created.get("item_id") or created["id"])
    pinax(
        repo, "note", "add", item_id, "--ref", "docs/brief.md",
        "--caption", "Measurable acceptance.", "--actor", "coordinator@proof", "--json",
    )
    return item_id


@pytest.mark.usefixtures("self_committing_pinax")
def test_pinax_ready_items_lead_with_the_pinax_next_head(tmp_path: Path) -> None:
    repo, first_id = pinax_repo(tmp_path)
    campaign = CampaignRef("campaign", repo)
    tracker = PinaxTrackerAdapter(repo)
    second_id = _add_with_brief(repo, "Second ready item")
    lower, higher = sorted((first_id, second_id))
    pinax(repo, "priority", higher, "top", "--actor", "coordinator@proof", "--json")
    pinax(repo, "priority", lower, "50", "--actor", "coordinator@proof", "--json")

    head = tracker.next_item(campaign)
    items = tracker.ready_items(campaign)

    assert head is not None and head.item_id == higher
    assert [item.item_id for item in items] == [higher, lower]
    assert items[0].item_id == head.item_id


@pytest.mark.usefixtures("self_committing_pinax")
def test_pinax_blocked_item_ids_names_gated_items_and_unfinished_dependencies(
    tmp_path: Path,
) -> None:
    repo, ready_id = pinax_repo(tmp_path)
    campaign = CampaignRef("campaign", repo)
    tracker = PinaxTrackerAdapter(repo)
    actor = ("--actor", "coordinator@proof", "--json")

    gated_id = _add_with_brief(repo, "Blocked behind a gate")
    pinax(repo, "block", gated_id, "--gate", "proposal", *actor)
    waiting_id = _add_with_brief(repo, "Waits on an unfinished item")
    pinax(repo, "dep", "add", ready_id, "--blocks", waiting_id, *actor)
    finished_id = _add_with_brief(repo, "Finished dependency")
    pinax(repo, "status", finished_id, "done", *actor)
    freed_id = _add_with_brief(repo, "Its dependency is done")
    pinax(repo, "dep", "add", finished_id, "--blocks", freed_id, *actor)

    assert tracker.blocked_item_ids(campaign) == frozenset({gated_id, waiting_id})
