from __future__ import annotations

from pathlib import Path

import pytest

from autobuild.domain import (
    AdapterIdentity,
    ChangedPath,
    ChangeKind,
    CloseEvidence,
    CommandRequest,
    CommandResult,
    DeliveryRequest,
    DiffEvidence,
    EvidenceError,
    PolicyViolation,
    ReviewDecision,
    ReviewVerdict,
    Seat,
    SeatRequest,
    ToolPolicy,
    ValidationEvidence,
    WorkItem,
    WorkspaceRef,
)
from autobuild.enforcement import ApprovedValidator, PolicyConfig, PolicyGateway
from autobuild.testing import FakeCommandAdapter, FakeHarnessAdapter, FakeTrackerAdapter, FakeWorkspaceAdapter


def identity(name: str) -> AdapterIdentity:
    return AdapterIdentity(name, "1", frozenset({"test"}))


def policy(root: Path, *, allow_protected_merge: bool = False) -> PolicyConfig:
    return PolicyConfig(
        allowed_roots=(root,),
        approved_validators=(ApprovedValidator("tests", ("python", "-m", "pytest")),),
        allowed_tools=frozenset({"python"}),
        allow_protected_merge=allow_protected_merge,
    )


def close_evidence(root: Path, *, validation_revision: str = "revision") -> tuple[WorkspaceRef, CloseEvidence]:
    workspace = WorkspaceRef(root / "worktree", "autobuild/item", "base", "lease")
    changed = (ChangedPath(Path("src/example.py"), ChangeKind.MODIFIED, "digest"),)
    diff = DiffEvidence(workspace, "revision", changed, str(root / "patch.diff"))
    command = CommandResult("run:item:tests", 0, "stdout", "stderr", "start", "end")
    evidence = CloseEvidence(
        "item",
        "revision",
        diff,
        ValidationEvidence("tests", validation_revision, command, changed),
        ReviewVerdict("item", ReviewDecision.PASS, (), "review"),
        "trajectory",
    )
    return workspace, evidence


def test_command_rejects_an_out_of_root_cwd_before_dispatch(tmp_path: Path) -> None:
    fake = FakeCommandAdapter(identity("command"))
    port = PolicyGateway(policy(tmp_path / "allowed")).command(fake)

    with pytest.raises(PolicyViolation, match="outside the allowed roots"):
        port.run(CommandRequest("run:item:tests", ("python", "-m", "pytest"), tmp_path / "other"))

    assert fake.requests == []


def test_command_rejects_validator_argv_that_differs_from_approval(tmp_path: Path) -> None:
    fake = FakeCommandAdapter(identity("command"))
    port = PolicyGateway(policy(tmp_path)).command(fake)

    with pytest.raises(PolicyViolation, match="argv differs"):
        port.run(CommandRequest("run:item:tests", ("python", "-m", "unittest"), tmp_path))

    assert fake.requests == []


def test_harness_rejects_an_undeclared_tool_before_dispatch(tmp_path: Path) -> None:
    workspace = WorkspaceRef(tmp_path / "worktree", "autobuild/item", "base", "lease")
    request = SeatRequest(
        "run",
        "item",
        Seat.BUILDER,
        "builder",
        tmp_path / "brief.md",
        workspace,
        ToolPolicy(frozenset({"python", "curl"}), (workspace.root,)),
        "Build the item",
        "builder-report-v1",
        60,
    )
    fake = FakeHarnessAdapter(identity("harness"))
    port = PolicyGateway(policy(tmp_path)).harness(fake)

    with pytest.raises(PolicyViolation, match="undeclared tool"):
        port.invoke(request)

    assert fake.requests == []


def test_tracker_rejects_stale_validation_before_close(tmp_path: Path) -> None:
    workspace, evidence = close_evidence(tmp_path, validation_revision="old-revision")
    fake = FakeTrackerAdapter(identity("tracker"))
    port = PolicyGateway(policy(tmp_path)).tracker(fake)

    with pytest.raises(EvidenceError, match="stale"):
        port.close(evidence, "item-commit", workspace, "coordinator")

    assert fake.closed == []


def test_tracker_rejects_a_durable_claim_without_protected_branch_gate(tmp_path: Path) -> None:
    fake = FakeTrackerAdapter(identity("tracker"))
    port = PolicyGateway(policy(tmp_path)).tracker(fake)

    with pytest.raises(PolicyViolation, match="tracker claim"):
        port.claim(WorkItem("item", "title", "brief", ("accepted",)), "builder")

    assert fake.claims == []


def test_tracker_rejects_incomplete_close_record(tmp_path: Path) -> None:
    workspace, evidence = close_evidence(tmp_path)
    incomplete = CloseEvidence(
        evidence.item_id,
        evidence.workspace_revision,
        evidence.diff,
        evidence.validation,
        evidence.verdict,
        "",
    )
    fake = FakeTrackerAdapter(identity("tracker"))
    port = PolicyGateway(policy(tmp_path)).tracker(fake)

    with pytest.raises(EvidenceError, match="trajectory"):
        port.close(incomplete, "item-commit", workspace, "coordinator")

    assert fake.closed == []


def test_workspace_rejects_protected_delivery_without_human_gate(tmp_path: Path) -> None:
    workspace, _ = close_evidence(tmp_path)
    fake = FakeWorkspaceAdapter(identity("workspace"), workspace=workspace)
    port = PolicyGateway(policy(tmp_path)).workspace(fake)

    with pytest.raises(PolicyViolation, match="protected-branch delivery"):
        port.deliver(workspace, DeliveryRequest("item", "item-commit", "tracker-commit"))


def test_valid_close_and_explicitly_gated_delivery_reach_adapters(tmp_path: Path) -> None:
    workspace, evidence = close_evidence(tmp_path)
    tracker_fake = FakeTrackerAdapter(identity("tracker"))
    workspace_fake = FakeWorkspaceAdapter(identity("workspace"), workspace=workspace)
    gateway = PolicyGateway(policy(tmp_path, allow_protected_merge=True))

    gateway.tracker(tracker_fake).close(evidence, "item-commit", workspace, "coordinator")
    result = gateway.workspace(workspace_fake).deliver(
        workspace, DeliveryRequest("item", "item-commit", "tracker-commit")
    )

    assert tracker_fake.closed == [evidence]
    assert result.pushed is True
