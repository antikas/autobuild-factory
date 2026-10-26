"""A relaunch resumes an item a killed run left built, on a real repository."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from autobuild.adapters import (
    BacklogTrackerAdapter,
    FilesystemLeaseAdapter,
    GitWorkspaceAdapter,
    LocalRunRecordAdapter,
)
from autobuild.application import CampaignRunner, WorkflowPorts
from autobuild.domain import (
    AdapterIdentity,
    CampaignContext,
    CampaignRef,
    CommandResult,
    DeliveryMode,
    ItemDisposition,
    ItemExecutionSpec,
    ReviewDecision,
    ReviewVerdict,
    SeatOutcome,
    SeatResult,
    SeatUsage,
    ToolPolicy,
    WorkItem,
)
from autobuild.testing import (
    FakeCommandAdapter,
    FakeHarnessAdapter,
    FakeKnowledgeAdapter,
)


def run(*argv: str) -> str:
    attempts = 5 if argv and argv[0] == "git" else 1
    for attempt in range(attempts):
        completed = subprocess.run(
            list(argv), capture_output=True, text=True, encoding="utf-8", check=False
        )
        if completed.returncode == 0 or attempt == attempts - 1:
            break
        time.sleep(0.2 * (attempt + 1))
    if completed.returncode != 0:
        raise RuntimeError(f"{' '.join(argv)} failed: {completed.stderr.strip()}")
    return completed.stdout.strip()


def git(root: Path, *args: str) -> str:
    return run("git", "-C", str(root), *args)


def _repo(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    run("git", "init", "--bare", str(remote))
    run("git", "init", "-b", "main", str(repo))
    git(repo, "config", "user.name", "AutoBuild Test")
    git(repo, "config", "user.email", "autobuild@example.invalid")
    (repo / "docs").mkdir()
    (repo / "docs" / "brief.md").write_text(
        "# Brief\n\n## Acceptance\n\n- Product reaches the default branch.\n",
        encoding="utf-8",
    )
    (repo / "BACKLOG.md").write_text(
        "# Backlog\n\n"
        "| Item | Title | Status | Brief |\n"
        "|---|---|---|---|\n"
        "| TST-001 | Resume me | Ready | docs/brief.md |\n",
        encoding="utf-8",
    )
    git(repo, "add", ".")
    git(repo, "commit", "-m", "initial")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-u", "origin", "main")
    git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    return repo


def _reviewer_pass(item_id: str) -> SeatResult:
    return SeatResult(
        "reviewer",
        SeatOutcome.SUCCEEDED,
        ReviewVerdict(item_id, ReviewDecision.PASS, (), "verdict:resume"),
        "raw:reviewer",
        SeatUsage(source="fake"),
        "2026-09-04T00:00:00+00:00",
        "2026-09-04T00:00:01+00:00",
        exit_code=0,
        model="reviewer-model",
    )


def _spec_for(repo: Path, target_revision: str):
    def build(item: WorkItem) -> ItemExecutionSpec:
        return ItemExecutionSpec(
            item=item,
            brief_path=Path("docs/brief.md"),
            validator_id="tests",
            validator_argv=("python", "-c", "pass"),
            tool_policy=ToolPolicy(
                frozenset({"read", "write", "shell", "python", "git"}), (repo,)
            ),
            reviewer_tool_policy=ToolPolicy(frozenset({"read"}), (repo,)),
            builder_model_class="builder",
            reviewer_model_class="reviewer",
            specialist_model_class="specialist",
            delivery_mode=DeliveryMode.PROTECTED_DEFAULT,
            delivery_target_branch="main",
            delivery_target_revision=target_revision,
        )

    return build


def test_a_second_campaign_resumes_a_built_worktree_and_delivers(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    scratch = tmp_path / "scratch"
    campaign = CampaignRef("resume-campaign", repo, max_items=3)
    item = WorkItem("TST-001", "Resume me", "docs/brief.md", ("Product reaches the default branch.",))

    # --- The interrupted first run: claim, build in a worktree, record a
    # 'built' phase marker, then leave without releasing (as a kill would). ---
    tracker_setup = BacklogTrackerAdapter(repo)
    workspace_setup = GitWorkspaceAdapter(scratch, tracker_paths=("BACKLOG.md",))
    records_setup = LocalRunRecordAdapter(scratch / "runs")
    tracker_setup.claim(item, "builder@lane-one")
    workspace = workspace_setup.create_isolated(campaign, item)
    (workspace.root / "product.txt").write_text("built by the killed run\n", encoding="utf-8")
    diff = workspace_setup.diff(workspace)
    record = records_setup.create(campaign)
    marker = {
        "schema": "autobuild.item-phase.v1",
        "item_id": "TST-001",
        "state": "built",
        "worktree_root": str(workspace.root),
        "branch": workspace.branch,
        "head_commit": diff.head_commit,
        "workspace_revision": diff.workspace_revision,
        "correction_count": 0,
        "timestamp": "2026-09-04T00:00:00+00:00",
    }
    records_setup.write_evidence_file(
        record, "TST-001-phase.json", json.dumps(marker).encode("utf-8")
    )
    # The worktree is deliberately not released.

    # --- The second campaign, in fresh adapters, resumes and delivers. ---
    tracker = BacklogTrackerAdapter(repo)
    workspace_adapter = GitWorkspaceAdapter(scratch, tracker_paths=("BACKLOG.md",))
    records = LocalRunRecordAdapter(scratch / "runs")
    lease = FilesystemLeaseAdapter(scratch)
    harness = FakeHarnessAdapter(
        AdapterIdentity("harness", "1", frozenset({"test"})),
        scripted_results=[_reviewer_pass("TST-001")],
    )
    command = FakeCommandAdapter(
        AdapterIdentity("command", "1", frozenset({"test"})),
        scripted_results=[
            CommandResult("cmd:TST-001:tests", 0, "stdout", "stderr", "start", "end")
        ],
    )
    knowledge = FakeKnowledgeAdapter(AdapterIdentity("knowledge", "1", frozenset({"test"})))
    ports = WorkflowPorts(tracker, workspace_adapter, harness, command, records, knowledge, lease=lease)
    context = CampaignContext(
        harness="",
        models={},
        delivery_mode=DeliveryMode.PROTECTED_DEFAULT,
        validator_id="tests",
        target_branch="main",
        target_revision=git(repo, "rev-parse", "HEAD"),
    )

    outcome = CampaignRunner(ports).run(
        campaign, _spec_for(repo, git(repo, "rev-parse", "HEAD")), context=context
    )

    assert [entry.item_id for entry in outcome.items] == ["TST-001"]
    assert outcome.items[0].disposition is ItemDisposition.ACCEPTED
    # The reviewer ran; the good build was not respent.
    assert [request.seat.value for request in harness.requests] == ["reviewer"]
    # Product reached the default branch and the tracker shows it done.
    assert (repo / "product.txt").read_text(encoding="utf-8") == "built by the killed run\n"
    assert "| TST-001 | Resume me | Done (" in (repo / "BACKLOG.md").read_text(encoding="utf-8")
    # The worktree was resumed then released.
    assert workspace.root.exists() is False
    run_dirs = sorted((scratch / "runs").iterdir())
    events = [
        json.loads(line)
        for run_dir in run_dirs
        for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert any(event["event_type"] == "item.resumed" for event in events)


def test_a_finalised_worktree_whose_head_is_an_integration_merge_resumes_and_delivers(
    tmp_path: Path,
) -> None:
    """A run killed after delivery merged the target branch into the item
    branch leaves a worktree whose head is that integration merge. The marker
    pinned the tracker commit; the resume steps over the merge, validates the
    integrated candidate once, and delivers it."""

    from autobuild.adapters.git_workspace import INTEGRATION_SUBJECT
    from autobuild.domain import (
        CloseEvidence,
        FinaliseRequest,
        ValidationEvidence,
    )

    repo = _repo(tmp_path)
    remote = tmp_path / "remote.git"
    scratch = tmp_path / "scratch"
    campaign = CampaignRef("resume-merge", repo, max_items=3)
    item = WorkItem("TST-001", "Resume me", "docs/brief.md", ("Product reaches the default branch.",))

    # --- The interrupted first run: claim, build, commit, close, then the
    # delivery merged origin/main into the item branch and the run was killed.
    tracker_setup = BacklogTrackerAdapter(repo)
    workspace_setup = GitWorkspaceAdapter(scratch, tracker_paths=("BACKLOG.md",))
    records_setup = LocalRunRecordAdapter(scratch / "runs")
    tracker_setup.claim(item, "builder@lane-one")
    workspace = workspace_setup.create_isolated(campaign, item)
    (workspace.root / "product.txt").write_text("built by the killed run\n", encoding="utf-8")
    diff = workspace_setup.diff(workspace)
    command = CommandResult("run:TST-001:tests", 0, "stdout", "stderr", "start", "end")
    evidence = CloseEvidence(
        "TST-001",
        diff.workspace_revision,
        diff,
        ValidationEvidence("tests", diff.workspace_revision, command, diff.changed_paths),
        ReviewVerdict("TST-001", ReviewDecision.PASS, (), "review"),
        "trajectory",
    )
    item_commit = workspace_setup.commit_item(
        workspace, FinaliseRequest("TST-001", evidence, "ADDED: Resume me")
    )
    tracker_setup.close(evidence, item_commit, workspace, "coordinator@autobuild")
    tracker_commit = workspace_setup.commit_tracker(workspace, "TST-001", item_commit)
    other = tmp_path / "other"
    run("git", "clone", "--quiet", str(remote), str(other))
    git(other, "config", "user.name", "Upstream Writer")
    git(other, "config", "user.email", "upstream@example.invalid")
    (other / "upstream.txt").write_text("another writer\n", encoding="utf-8")
    git(other, "add", "upstream.txt")
    git(other, "commit", "-m", "upstream change")
    git(other, "push", "origin", "HEAD:main")
    git(workspace.root, "fetch", "origin")
    git(
        workspace.root,
        "merge",
        "--no-ff",
        "-m",
        f"{INTEGRATION_SUBJECT} origin/main into {workspace.branch}",
        "origin/main",
    )
    merge_head = git(workspace.root, "rev-parse", "HEAD")
    assert merge_head != tracker_commit
    record = records_setup.create(campaign)
    marker = {
        "schema": "autobuild.item-phase.v1",
        "item_id": "TST-001",
        "state": "finalised",
        "worktree_root": str(workspace.root),
        "branch": workspace.branch,
        "head_commit": tracker_commit,
        "workspace_revision": diff.workspace_revision,
        "correction_count": 0,
        "timestamp": "2026-09-04T00:00:00+00:00",
    }
    records_setup.write_evidence_file(
        record, "TST-001-phase.json", json.dumps(marker).encode("utf-8")
    )

    # --- The second campaign resumes the finalised worktree and delivers it.
    tracker = BacklogTrackerAdapter(repo)
    workspace_adapter = GitWorkspaceAdapter(scratch, tracker_paths=("BACKLOG.md",))
    records = LocalRunRecordAdapter(scratch / "runs")
    harness = FakeHarnessAdapter(AdapterIdentity("harness", "1", frozenset({"test"})))
    validator = FakeCommandAdapter(
        AdapterIdentity("command", "1", frozenset({"test"})),
        scripted_results=[
            CommandResult("cmd:TST-001:tests", 0, "stdout", "stderr", "start", "end")
        ],
    )
    knowledge = FakeKnowledgeAdapter(AdapterIdentity("knowledge", "1", frozenset({"test"})))
    ports = WorkflowPorts(
        tracker,
        workspace_adapter,
        harness,
        validator,
        records,
        knowledge,
        lease=FilesystemLeaseAdapter(scratch),
    )
    context = CampaignContext(
        harness="",
        models={},
        delivery_mode=DeliveryMode.PROTECTED_DEFAULT,
        validator_id="tests",
        target_branch="main",
        target_revision=git(repo, "rev-parse", "HEAD"),
    )

    outcome = CampaignRunner(ports).run(
        campaign, _spec_for(repo, git(repo, "rev-parse", "HEAD")), context=context
    )

    assert [entry.item_id for entry in outcome.items] == ["TST-001"]
    assert outcome.items[0].disposition is ItemDisposition.ACCEPTED, outcome.items[0].reason
    assert harness.requests == []
    # The merge brought another writer's product in, so the integrated
    # candidate was validated exactly once more before it was pushed.
    assert len(validator.requests) == 1
    # The campaign report is committed on top of the delivered commit.
    pushed = git(repo, "ls-remote", str(remote), "refs/heads/main").split()[0]
    assert git(repo, "rev-parse", f"{pushed}^") == outcome.items[0].finalise.merged_commit
    assert git(repo, "merge-base", "--is-ancestor", merge_head, pushed) == ""
    assert git(repo, "show", f"{pushed}:product.txt") == "built by the killed run"
    assert git(repo, "show", f"{pushed}:upstream.txt") == "another writer"
    assert "| TST-001 | Resume me | Done (" in git(repo, "show", f"{pushed}:BACKLOG.md")
    assert workspace.root.exists() is False
