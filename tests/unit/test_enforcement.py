from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

from autobuild.domain import (
    AdapterIdentity,
    CampaignRef,
    CampaignReport,
    CampaignSelection,
    ChangedPath,
    ChangeKind,
    CloseEvidence,
    CommandRequest,
    CommandResult,
    DeliveryMode,
    DeliveryRequest,
    DiffEvidence,
    EvidenceError,
    FinaliseResult,
    ItemNature,
    LeaseRecord,
    LeaseSurface,
    PolicyViolation,
    SurfaceKind,
    ReviewDecision,
    ReviewFinding,
    ReviewVerdict,
    ScopeFenceViolation,
    Seat,
    SeatOutcome,
    SeatRequest,
    SeatResult,
    SeatUsage,
    ToolPolicy,
    ValidationEvidence,
    WorkItem,
    WorkspaceRef,
)
from autobuild.domain import PreflightError
from autobuild.enforcement import (
    ApprovedValidator,
    BriefCheck,
    PolicyConfig,
    PolicyGateway,
    PreflightRequest,
    ScopedTrackerPort,
    TelemetryCheck,
    TlsTarget,
    TransportCheck,
    ValidatorCheck,
    classify_item_nature,
    declared_item_class,
    git_staging_lines,
    out_of_fence_paths,
    run_preflight,
)
from autobuild.enforcement.triage import declared_path_tokens, exact_declared_paths
from autobuild.testing import (
    FakeCommandAdapter,
    FakeEnvironmentProbe,
    FakeFilesystemProbe,
    FakeHarnessAdapter,
    FakeLeaseAdapter,
    FakeNetworkProbe,
    FakeTrackerAdapter,
    FakeWorkspaceAdapter,
)
from test_brief_git_fixtures import EXCERPT_C


def identity(name: str) -> AdapterIdentity:
    return AdapterIdentity(name, "1", frozenset({"test"}))


def policy(
    root: Path,
    *,
    allow_repository_mutation: bool = False,
    allow_protected_merge: bool = False,
) -> PolicyConfig:
    return PolicyConfig(
        allowed_roots=(root,),
        approved_validators=(ApprovedValidator("tests", ("python", "-m", "pytest")),),
        allowed_tools=frozenset({"python"}),
        allow_repository_mutation=allow_repository_mutation,
        allow_protected_merge=allow_protected_merge,
    )


def _review_policy(root: Path) -> PolicyConfig:
    return PolicyConfig(
        allowed_roots=(root,),
        approved_validators=(ApprovedValidator("tests", ("python", "-m", "pytest")),),
        allowed_tools=frozenset({"read"}),
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


def test_command_admits_the_approved_patient_argv_and_nothing_else(tmp_path: Path) -> None:
    patient = ("python", "-m", "pytest", "-p", "serial")
    config = PolicyConfig(
        allowed_roots=(tmp_path,),
        approved_validators=(
            ApprovedValidator("tests", ("python", "-m", "pytest"), patient_argv=patient),
        ),
        allowed_tools=frozenset({"python"}),
    )
    result = CommandResult("run:item:tests", 0, "stdout", "stderr", "start", "end")
    fake = FakeCommandAdapter(identity("command"), scripted_results=[result])
    port = PolicyGateway(config).command(fake)

    assert port.run(CommandRequest("run:item:tests", patient, tmp_path)) == result
    with pytest.raises(PolicyViolation, match="argv differs"):
        port.run(CommandRequest("run:item:tests", ("python", "-m", "unittest"), tmp_path))
    assert [request.argv for request in fake.requests] == [patient]


def test_command_rejects_undeclared_validator_stdin(tmp_path: Path) -> None:
    fake = FakeCommandAdapter(identity("command"))
    port = PolicyGateway(policy(tmp_path)).command(fake)

    with pytest.raises(PolicyViolation, match="validator stdin"):
        port.run(
            CommandRequest(
                "run:item:tests",
                ("python", "-m", "pytest"),
                tmp_path,
                stdin_ref=str(tmp_path / "unexpected.txt"),
            )
        )

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


def test_harness_accepts_configured_external_roots(tmp_path: Path) -> None:
    workspace = WorkspaceRef(tmp_path / "worktree", "autobuild/item", "base", "lease")
    external = tmp_path.parent / f"{tmp_path.name}-shared-briefs"
    external.mkdir()
    request = SeatRequest(
        "run",
        "item",
        Seat.BUILDER,
        "builder",
        workspace.root / "brief.md",
        workspace,
        ToolPolicy(frozenset({"python"}), (workspace.root, external)),
        "Build the item",
        "builder-report-v1",
        60,
    )
    result = SeatResult(
        "run-ref",
        SeatOutcome.SUCCEEDED,
        None,
        "raw-output",
        SeatUsage(source="fake"),
        "start",
        "end",
    )
    fake = FakeHarnessAdapter(identity("harness"), scripted_results=[result])
    config = PolicyConfig(
        allowed_roots=(workspace.root, external),
        approved_validators=(ApprovedValidator("tests", ("python", "-m", "pytest")),),
        allowed_tools=frozenset({"python"}),
    )
    port = PolicyGateway(config).harness(fake)

    assert port.invoke(request) == result
    assert fake.requests == [request]


def test_harness_rejects_unconfigured_external_roots(tmp_path: Path) -> None:
    workspace = WorkspaceRef(tmp_path / "worktree", "autobuild/item", "base", "lease")
    outside = tmp_path.parent / f"{tmp_path.name}-outside-shared-briefs"
    request = SeatRequest(
        "run",
        "item",
        Seat.BUILDER,
        "builder",
        tmp_path / "brief.md",
        workspace,
        ToolPolicy(frozenset({"python"}), (workspace.root, outside)),
        "Build the item",
        "builder-report-v1",
        60,
    )
    fake = FakeHarnessAdapter(identity("harness"))
    port = PolicyGateway(policy(tmp_path)).harness(fake)

    with pytest.raises(PolicyViolation, match="seat tool root escapes"):
        port.invoke(request)

    assert fake.requests == []


def test_harness_rejects_a_pass_verdict_that_carries_a_blocking_finding(tmp_path: Path) -> None:
    workspace = WorkspaceRef(tmp_path / "worktree", "autobuild/item", "base", "lease")
    request = SeatRequest(
        "run",
        "item",
        Seat.REVIEWER,
        "reviewer",
        tmp_path / "brief.md",
        workspace,
        ToolPolicy(frozenset({"read"}), (workspace.root,)),
        "Review the item",
        "review-verdict-v1",
        60,
    )
    verdict = ReviewVerdict(
        "item",
        ReviewDecision.PASS,
        (ReviewFinding("BUG", "wrong output", "file:line", blocking=True),),
        "review",
    )
    result = SeatResult(
        "run-ref", SeatOutcome.SUCCEEDED, verdict, "raw", SeatUsage(source="fake"), "start", "end"
    )
    fake = FakeHarnessAdapter(identity("harness"), scripted_results=[result])
    port = PolicyGateway(_review_policy(tmp_path)).harness(fake)

    with pytest.raises(EvidenceError, match="blocking"):
        port.invoke(request)


def test_harness_passes_a_pass_verdict_with_non_blocking_findings(tmp_path: Path) -> None:
    workspace = WorkspaceRef(tmp_path / "worktree", "autobuild/item", "base", "lease")
    request = SeatRequest(
        "run",
        "item",
        Seat.REVIEWER,
        "reviewer",
        tmp_path / "brief.md",
        workspace,
        ToolPolicy(frozenset({"read"}), (workspace.root,)),
        "Review the item",
        "review-verdict-v1",
        60,
    )
    verdict = ReviewVerdict(
        "item",
        ReviewDecision.PASS,
        (ReviewFinding("STYLE", "rename for clarity", "file:line", blocking=False),),
        "review",
    )
    result = SeatResult(
        "run-ref", SeatOutcome.SUCCEEDED, verdict, "raw", SeatUsage(source="fake"), "start", "end"
    )
    fake = FakeHarnessAdapter(identity("harness"), scripted_results=[result])
    port = PolicyGateway(_review_policy(tmp_path)).harness(fake)

    assert port.invoke(request) == result


def test_tracker_rejects_stale_validation_before_close(tmp_path: Path) -> None:
    workspace, evidence = close_evidence(tmp_path, validation_revision="old-revision")
    fake = FakeTrackerAdapter(identity("tracker"))
    port = PolicyGateway(policy(tmp_path)).tracker(fake)

    with pytest.raises(EvidenceError, match="stale"):
        port.close(evidence, "item-commit", workspace, "coordinator")

    assert fake.closed == []


def _round_limit_evidence(
    root: Path,
    *,
    mark: bool,
    exit_code: int = 0,
    validation_revision: str = "revision",
) -> tuple[WorkspaceRef, CloseEvidence]:
    workspace, evidence = close_evidence(root, validation_revision=validation_revision)
    command = CommandResult("run:item:tests", exit_code, "stdout", "stderr", "start", "end")
    verdict = ReviewVerdict(
        "item",
        ReviewDecision.CORRECT,
        (ReviewFinding("BUG", "wrong output", "finding:bug", blocking=True),),
        "review",
    )
    return workspace, replace(
        evidence,
        validation=replace(evidence.validation, command=command),
        verdict=verdict,
        accepted_at_round_limit=mark,
    )


@pytest.mark.parametrize(
    "mark,exit_code,validation_revision,refusal",
    [
        (False, 0, "revision", "only a passing review"),
        (True, 1, "revision", "did not pass"),
        (True, 0, "old-revision", "stale"),
    ],
)
def test_close_refuses_a_correct_verdict_without_a_green_round_limit(
    tmp_path: Path, mark: bool, exit_code: int, validation_revision: str, refusal: str
) -> None:
    workspace, evidence = _round_limit_evidence(
        tmp_path, mark=mark, exit_code=exit_code, validation_revision=validation_revision
    )
    fake = FakeTrackerAdapter(identity("tracker"))
    port = PolicyGateway(policy(tmp_path)).tracker(fake)

    with pytest.raises(EvidenceError, match=refusal):
        port.close(evidence, "item-commit", workspace, "coordinator")

    assert fake.closed == []


def test_close_accepts_a_correct_verdict_at_the_round_limit_on_green_validation(
    tmp_path: Path,
) -> None:
    workspace, evidence = _round_limit_evidence(tmp_path, mark=True)
    fake = FakeTrackerAdapter(identity("tracker"))
    port = PolicyGateway(policy(tmp_path)).tracker(fake)

    port.close(evidence, "item-commit", workspace, "coordinator")

    assert fake.closed == [evidence]


@pytest.mark.parametrize("decision", [ReviewDecision.PARK, ReviewDecision.ESCALATE])
def test_close_refuses_other_non_pass_verdicts_even_with_the_mark(
    tmp_path: Path, decision: ReviewDecision
) -> None:
    workspace, evidence = _round_limit_evidence(tmp_path, mark=True)
    evidence = replace(evidence, verdict=replace(evidence.verdict, decision=decision))
    fake = FakeTrackerAdapter(identity("tracker"))
    port = PolicyGateway(policy(tmp_path)).tracker(fake)

    with pytest.raises(EvidenceError, match="only a passing review"):
        port.close(evidence, "item-commit", workspace, "coordinator")


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
    port = PolicyGateway(
        policy(tmp_path, allow_repository_mutation=True)
    ).workspace(fake)

    with pytest.raises(PolicyViolation, match="protected-branch delivery"):
        port.deliver(
            workspace,
            DeliveryRequest(
                "item", "item-commit", "tracker-commit", DeliveryMode.PROTECTED_DEFAULT, "main", "base"
            ),
        )


def test_workspace_accepts_current_branch_delivery_with_local_mutation_gate(tmp_path: Path) -> None:
    workspace, _ = close_evidence(tmp_path)
    fake = FakeWorkspaceAdapter(identity("workspace"), workspace=workspace)
    port = PolicyGateway(
        policy(tmp_path, allow_repository_mutation=True)
    ).workspace(fake)

    result = port.deliver(
        workspace,
        DeliveryRequest(
            "item",
            "item-commit",
            "tracker-commit",
            DeliveryMode.CURRENT_BRANCH_PR,
            "feature/local-pr",
            "base",
        ),
    )

    assert result.pushed is True


def test_report_delivery_requires_the_protected_merge_gate(tmp_path: Path) -> None:
    fake = FakeWorkspaceAdapter(identity("workspace"))
    port = PolicyGateway(
        policy(tmp_path, allow_repository_mutation=True)
    ).workspace(fake)

    with pytest.raises(PolicyViolation, match="protected-branch report delivery"):
        port.deliver_report(
            CampaignReport(
                "campaign",
                tmp_path,
                "docs/campaigns/campaign.md",
                "# report\n",
                DeliveryMode.PROTECTED_DEFAULT,
                "main",
                "base",
            )
        )

    assert fake.reports == []


def test_gated_report_delivery_reaches_the_adapter(tmp_path: Path) -> None:
    fake = FakeWorkspaceAdapter(identity("workspace"))
    port = PolicyGateway(
        policy(tmp_path, allow_repository_mutation=True, allow_protected_merge=True)
    ).workspace(fake)

    result = port.deliver_report(
        CampaignReport(
            "campaign",
            tmp_path,
            "docs/campaigns/campaign.md",
            "# report\n",
            DeliveryMode.PROTECTED_DEFAULT,
            "main",
            "base",
        )
    )

    assert result.pushed is True
    assert len(fake.reports) == 1


def test_valid_close_and_explicitly_gated_delivery_reach_adapters(tmp_path: Path) -> None:
    workspace, evidence = close_evidence(tmp_path)
    tracker_fake = FakeTrackerAdapter(identity("tracker"))
    workspace_fake = FakeWorkspaceAdapter(identity("workspace"), workspace=workspace)
    gateway = PolicyGateway(
        PolicyConfig(
            allowed_roots=(tmp_path,),
            approved_validators=(ApprovedValidator("tests", ("python", "-m", "pytest")),),
            allowed_tools=frozenset({"python"}),
            allow_repository_mutation=True,
            allow_protected_merge=True,
        )
    )

    gateway.tracker(tracker_fake).close(evidence, "item-commit", workspace, "coordinator")
    result = gateway.workspace(workspace_fake).deliver(
        workspace,
        DeliveryRequest(
            "item", "item-commit", "tracker-commit", DeliveryMode.PROTECTED_DEFAULT, "main", "base"
        ),
    )

    assert tracker_fake.closed == [evidence]
    assert result.pushed is True


def test_workspace_confirm_delivery_checks_the_workspace_and_delegates(tmp_path: Path) -> None:
    workspace, _ = close_evidence(tmp_path)
    fake = FakeWorkspaceAdapter(identity("workspace"), workspace=workspace)
    port = PolicyGateway(policy(tmp_path)).workspace(fake)

    port.confirm_delivery(
        workspace, FinaliseResult("item-commit", "tracker-commit", "merged", True), "main"
    )

    assert fake.confirmed == [(workspace.lease_id, "main")]


def test_workspace_confirm_delivery_rejects_an_out_of_root_workspace(tmp_path: Path) -> None:
    outside = WorkspaceRef(tmp_path.parent / "elsewhere", "autobuild/item", "base", "lease")
    fake = FakeWorkspaceAdapter(identity("workspace"), workspace=outside)
    port = PolicyGateway(policy(tmp_path / "allowed")).workspace(fake)

    with pytest.raises(PolicyViolation, match="outside the allowed roots"):
        port.confirm_delivery(outside, FinaliseResult(None, "t", "m", True), "main")

    assert fake.confirmed == []


def test_a_brief_or_evidence_outside_the_roots_is_refused_without_its_absolute_path(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "allowed" / "worktree"
    request = SeatRequest(
        "run", "item", Seat.BUILDER, "model", tmp_path / "private" / "brief.md",
        WorkspaceRef(worktree, "branch", "base", "lease"),
        ToolPolicy(frozenset(), (worktree,)), "Build it", "builder-report-v1", 60,
    )
    port = PolicyGateway(policy(tmp_path / "allowed")).harness(FakeHarnessAdapter(identity("h")))

    with pytest.raises(PolicyViolation) as refused:
        port.invoke(request)
    assert str(refused.value) == "seat brief is outside the allowed roots: brief.md"

    with pytest.raises(PolicyViolation) as refused:
        port.invoke(replace(request, brief_ref="../private/brief.md"))
    assert str(refused.value) == "seat brief is outside the allowed roots: ../private/brief.md"

    with pytest.raises(PolicyViolation) as refused:
        port.invoke(
            replace(
                request,
                brief_path=worktree / "brief.md",
                context_refs=(str(tmp_path / "private" / "change.patch"),),
            )
        )
    assert str(refused.value) == "seat evidence is outside the allowed roots: change.patch"


# --- Selection fence -----------------------------------------------------------


def _work_item(item_id: str) -> WorkItem:
    return WorkItem(item_id, "title", "brief", ("accepted",))


def test_scoped_tracker_refuses_a_next_item_outside_the_allow_list() -> None:
    base = FakeTrackerAdapter(identity("tracker"), queue=[_work_item("c")])
    scoped = ScopedTrackerPort(base, CampaignSelection(allow=("a",)))

    with pytest.raises(ScopeFenceViolation, match="c"):
        scoped.next_item(CampaignRef("campaign", Path("/repo")))


def test_scoped_tracker_refuses_a_claim_of_an_excluded_item() -> None:
    base = FakeTrackerAdapter(identity("tracker"))
    scoped = ScopedTrackerPort(base, CampaignSelection(exclude=("x",)))

    with pytest.raises(ScopeFenceViolation, match="x"):
        scoped.claim(_work_item("x"), "builder")

    assert base.claims == []


def test_scoped_tracker_passes_ready_items_and_in_fence_calls_through() -> None:
    allowed = _work_item("a")
    base = FakeTrackerAdapter(identity("tracker"), queue=[allowed], ready=[allowed])
    scoped = ScopedTrackerPort(base, CampaignSelection(allow=("a",)))
    campaign = CampaignRef("campaign", Path("/repo"))

    assert scoped.ready_items(campaign) == (allowed,)
    assert scoped.next_item(campaign) == allowed
    assert scoped.claim(allowed, "builder").item_id == "a"


def test_blocked_item_ids_pass_through_both_tracker_wrappers(tmp_path: Path) -> None:
    base = FakeTrackerAdapter(identity("tracker"), blocked={"b"})
    enforced = PolicyGateway(policy(tmp_path)).tracker(base)
    scoped = ScopedTrackerPort(enforced, CampaignSelection(allow=("a", "b")))

    assert scoped.blocked_item_ids(CampaignRef("campaign", tmp_path)) == frozenset({"b"})


def test_blocked_item_ids_refuses_a_campaign_outside_the_allowed_roots(tmp_path: Path) -> None:
    base = FakeTrackerAdapter(identity("tracker"), blocked={"b"})
    enforced = PolicyGateway(policy(tmp_path / "allowed")).tracker(base)

    with pytest.raises(PolicyViolation, match="outside the allowed roots"):
        enforced.blocked_item_ids(CampaignRef("campaign", tmp_path / "other"))


# --- Item-nature triage --------------------------------------------------------


REPO = Path("/repo")


@pytest.mark.parametrize(
    "line,expected",
    [
        ("Item nature: repository", ItemNature.REPOSITORY),
        ("Item nature: machine", ItemNature.MACHINE),
        ("Item nature: cross-repository", ItemNature.CROSS_REPOSITORY),
        ("Item nature: owner-gated", ItemNature.OWNER_GATED),
    ],
)
def test_each_class_parses_from_the_brief_line(line: str, expected: ItemNature) -> None:
    brief = f"# Title\n\n{line}.\nItem class: default.\n"
    assert classify_item_nature(brief, repository_root=REPO) is expected


def test_a_brief_without_the_line_is_repository() -> None:
    brief = "# Title\n\n## Outcome\n\nBuild the thing.\n"
    assert classify_item_nature(brief, repository_root=REPO) is ItemNature.REPOSITORY


def test_an_unrecognised_nature_value_falls_back_to_repository() -> None:
    brief = "Item nature: interpretive-dance\n"
    assert classify_item_nature(brief, repository_root=REPO) is ItemNature.REPOSITORY


def test_a_declared_path_outside_the_repository_forces_cross_repository() -> None:
    brief = (
        "Item nature: repository.\n\n"
        "## Declared paths\n\n"
        "`src/inside.py` (edit), `../other-repo/outside.py` (new).\n"
    )
    assert (
        classify_item_nature(brief, repository_root=REPO)
        is ItemNature.CROSS_REPOSITORY
    )


def test_an_absolute_declared_path_outside_the_roots_forces_cross_repository() -> None:
    outside = Path("/etc/systemd/system/thing.service")
    brief = f"## Declared paths\n\n`{outside.as_posix()}`\n"
    assert (
        classify_item_nature(brief, repository_root=REPO)
        is ItemNature.CROSS_REPOSITORY
    )


def test_a_declared_path_inside_an_allowed_root_stays_repository() -> None:
    shared = Path("/shared/briefs")
    brief = (
        "## Declared paths\n\n"
        f"`src/inside.py`, `{(shared / 'note.md').as_posix()}`\n"
    )
    assert (
        classify_item_nature(brief, repository_root=REPO, allowed_roots=(shared,))
        is ItemNature.REPOSITORY
    )


def test_declared_paths_only_apply_under_their_heading() -> None:
    brief = (
        "## Outcome\n\n"
        "The edit touches `../elsewhere/file.py` in prose only.\n\n"
        "## Declared paths\n\n`src/inside.py`\n"
    )
    assert classify_item_nature(brief, repository_root=REPO) is ItemNature.REPOSITORY


def test_an_explicit_machine_class_wins_over_a_path_check() -> None:
    brief = (
        "Item nature: machine.\n\n"
        "## Declared paths\n\n`../other-repo/outside.py`\n"
    )
    assert classify_item_nature(brief, repository_root=REPO) is ItemNature.MACHINE


@pytest.mark.parametrize(
    "tokens",
    [("src/app.py", "/account/home"), ("/account/home",)],
)
def test_a_missing_extensionless_route_token_is_not_a_declared_path(
    tokens: tuple[str, ...],
) -> None:
    listed = ", ".join(f"`{token}`" for token in tokens)
    brief = f"Item nature: repository.\n\n## Declared paths\n\n{listed}\n"
    assert classify_item_nature(brief, repository_root=REPO) is ItemNature.REPOSITORY


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="a temporary directory has no leading-slash form on this platform",
)
def test_an_existing_leading_slash_directory_outside_the_repository_is_cross_repository(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    repository = tmp_path / "repo"
    repository.mkdir()
    brief = f"## Declared paths\n\n`{outside.as_posix()}`\n"
    assert (
        classify_item_nature(brief, repository_root=repository)
        is ItemNature.CROSS_REPOSITORY
    )


def test_a_missing_leading_slash_token_with_an_extension_is_cross_repository() -> None:
    brief = "## Declared paths\n\n`/elsewhere/notes.md`\n"
    assert (
        classify_item_nature(brief, repository_root=REPO)
        is ItemNature.CROSS_REPOSITORY
    )


def test_route_tokens_are_left_out_of_the_declared_path_sets() -> None:
    brief = "## Declared paths\n\n`src/app.py`, `/account/home`\n"
    assert exact_declared_paths(brief) == frozenset({"src/app.py"})
    assert declared_path_tokens(brief) == ("src/app.py",)


# --- Item class and the seat-timeout ceiling -----------------------------------


def test_declared_item_class_reads_the_brief_line() -> None:
    assert declared_item_class("# Title\n\nItem class: large\n") == "large"
    assert declared_item_class("Item class: default.\n") == "default"
    assert declared_item_class("Item class: `large`\n") == "large"
    assert declared_item_class("# Title\n\nno class line here\n") == "default"


def _seat_request(root: Path, timeout: float) -> SeatRequest:
    workspace = WorkspaceRef(root / "worktree", "autobuild/item", "base", "lease")
    return SeatRequest(
        "run",
        "item",
        Seat.BUILDER,
        "builder",
        root / "brief.md",
        workspace,
        ToolPolicy(frozenset({"python"}), (workspace.root,)),
        "Build the item",
        "builder-report-v1",
        timeout,
    )


def test_policy_ceiling_admits_a_class_deadline_and_refuses_one_above_it(tmp_path: Path) -> None:
    succeeded = SeatResult(
        "run-ref", SeatOutcome.SUCCEEDED, None, "raw", SeatUsage(source="fake"), "start", "end"
    )
    accepting = PolicyConfig(
        allowed_roots=(tmp_path,),
        approved_validators=(ApprovedValidator("tests", ("python", "-m", "pytest")),),
        allowed_tools=frozenset({"python"}),
        max_seat_timeout_seconds=7200.0,
    )
    fake = FakeHarnessAdapter(identity("harness"), scripted_results=[succeeded])

    result = PolicyGateway(accepting).harness(fake).invoke(_seat_request(tmp_path, 7200.0))
    assert result.outcome is SeatOutcome.SUCCEEDED

    refusing = PolicyGateway(policy(tmp_path)).harness(FakeHarnessAdapter(identity("harness")))
    with pytest.raises(PolicyViolation, match="seat timeout exceeds"):
        refusing.invoke(_seat_request(tmp_path, 7200.0))


# --- Preflight doctor ----------------------------------------------------------


def _out(tmp_path: Path, name: str, content: str = "") -> str:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return str(path)


def _cmd(tmp_path: Path, name: str, exit_code: int = 0, stdout: str = "", stderr: str = "") -> CommandResult:
    return CommandResult(
        name,
        exit_code,
        _out(tmp_path, f"{name}.out.txt", stdout),
        _out(tmp_path, f"{name}.err.txt", stderr),
        "start",
        "end",
    )


def _clock(*values: float):
    stream = iter(values)
    return lambda: next(stream)


def preflight_request(tmp_path: Path, **overrides) -> PreflightRequest:
    base = dict(
        scratch_root=tmp_path / "scratch",
        tls_targets=(TlsTarget("example.test", 443),),
        accepted_environment=frozenset(),
        telemetry=TelemetryCheck(
            "claude-code",
            (("DO_NOT_TRACK", "1"),),
            (("DO_NOT_TRACK", "1"), ("TMPDIR", "t")),
        ),
        transport=TransportCheck("claude-code", 512, True),
        validator=ValidatorCheck("tests", ("python", "-m", "pytest"), tmp_path, 600.0),
        briefs=(BriefCheck("APP-1", tmp_path / "brief.md"),),
    )
    base.update(overrides)
    return PreflightRequest(**base)


def passing_probes(tmp_path: Path):
    network = FakeNetworkProbe()
    environment = FakeEnvironmentProbe(executables={"python": "/bin/python"})
    filesystem = FakeFilesystemProbe(
        files={str(tmp_path / "brief.md"): 10}, texts={str(tmp_path / "brief.md"): "# Build it\n"}
    )
    command = FakeCommandAdapter(
        identity("command"),
        scripted_results=[_cmd(tmp_path, "version"), _cmd(tmp_path, "offline")],
    )
    return network, environment, filesystem, command


def run(request, network, environment, filesystem, command):
    return run_preflight(
        request,
        network=network,
        environment=environment,
        filesystem=filesystem,
        command=command,
        now=_clock(0.0, 0.2),
    )


def test_preflight_records_every_probe_when_the_environment_is_fit(tmp_path: Path) -> None:
    network, environment, filesystem, command = passing_probes(tmp_path)

    probes = run(preflight_request(tmp_path), network, environment, filesystem, command)

    assert [probe.name for probe in probes] == [
        "dns-tls",
        "interception",
        "scratch",
        "telemetry",
        "removed-environment",
        "validator-runnable",
        "validator-offline",
        "validator-budget",
        "transport",
        "briefs",
    ]
    assert all(probe.passed for probe in probes)


def test_preflight_stops_on_unresolvable_tls_target(tmp_path: Path) -> None:
    network, environment, filesystem, command = passing_probes(tmp_path)
    network.unresolvable = frozenset({"example.test"})

    with pytest.raises(PreflightError, match="dns-tls.*example.test"):
        run(preflight_request(tmp_path), network, environment, filesystem, command)


def test_preflight_stops_on_failed_tls_handshake(tmp_path: Path) -> None:
    network, environment, filesystem, command = passing_probes(tmp_path)
    network.unreachable = frozenset({("example.test", 443)})

    with pytest.raises(PreflightError, match="dns-tls.*example.test:443"):
        run(preflight_request(tmp_path), network, environment, filesystem, command)


def test_preflight_names_an_unlisted_interception_variable(tmp_path: Path) -> None:
    network, environment, filesystem, command = passing_probes(tmp_path)
    environment.values = {"HTTPS_PROXY": "http://127.0.0.1:3128"}

    with pytest.raises(PreflightError, match="interception.*HTTPS_PROXY"):
        run(preflight_request(tmp_path), network, environment, filesystem, command)


def test_preflight_accepts_a_listed_interception_variable(tmp_path: Path) -> None:
    network, environment, filesystem, command = passing_probes(tmp_path)
    environment.values = {"NODE_EXTRA_CA_CERTS": "/etc/ca.pem"}

    probes = run(
        preflight_request(tmp_path, accepted_environment=frozenset({"NODE_EXTRA_CA_CERTS"})),
        network,
        environment,
        filesystem,
        command,
    )

    interception = next(probe for probe in probes if probe.name == "interception")
    assert "NODE_EXTRA_CA_CERTS(accepted)" in interception.detail


def test_preflight_stops_when_the_scratch_root_is_read_only(tmp_path: Path) -> None:
    network, environment, filesystem, command = passing_probes(tmp_path)
    filesystem.writable = False

    with pytest.raises(PreflightError, match="scratch.*not writable"):
        run(preflight_request(tmp_path), network, environment, filesystem, command)


def test_preflight_stops_on_a_foreign_scratch_lock(tmp_path: Path) -> None:
    network, environment, filesystem, command = passing_probes(tmp_path)
    filesystem.locks = {str(tmp_path / "scratch"): ("busy.lock",)}

    with pytest.raises(PreflightError, match="scratch.*lock"):
        run(preflight_request(tmp_path), network, environment, filesystem, command)


def test_preflight_scratch_probe_stops_on_a_live_tracker_root_lease(tmp_path: Path) -> None:
    network, environment, filesystem, command = passing_probes(tmp_path)
    surface = LeaseSurface(tmp_path / "repo" / ".ergon", SurfaceKind.TRACKER)
    lease = FakeLeaseAdapter(identity("lease"))
    lease.live_holders[str(surface.path)] = LeaseRecord(
        "other-campaign", 4242, "other-host", "s", "b", surface.path, SurfaceKind.TRACKER
    )

    with pytest.raises(PreflightError, match="scratch.*held by campaign other-campaign"):
        run_preflight(
            preflight_request(tmp_path, tracker_surface=surface),
            network=network,
            environment=environment,
            filesystem=filesystem,
            command=command,
            lease=lease,
            now=_clock(0.0, 0.2),
        )


def test_preflight_scratch_probe_reports_a_free_tracker_root_lease(tmp_path: Path) -> None:
    network, environment, filesystem, command = passing_probes(tmp_path)
    surface = LeaseSurface(tmp_path / "repo" / ".ergon", SurfaceKind.TRACKER)
    lease = FakeLeaseAdapter(identity("lease"))

    probes = run_preflight(
        preflight_request(tmp_path, tracker_surface=surface),
        network=network,
        environment=environment,
        filesystem=filesystem,
        command=command,
        lease=lease,
        now=_clock(0.0, 0.2),
    )

    scratch = next(probe for probe in probes if probe.name == "scratch")
    assert scratch.passed
    assert "is free" in scratch.detail


def test_preflight_stops_when_the_harness_leaves_telemetry_on(tmp_path: Path) -> None:
    network, environment, filesystem, command = passing_probes(tmp_path)
    telemetry = TelemetryCheck("claude-code", (("DISABLE_TELEMETRY", "1"),), (("DO_NOT_TRACK", "1"),))

    with pytest.raises(PreflightError, match="telemetry.*DISABLE_TELEMETRY"):
        run(
            preflight_request(tmp_path, telemetry=telemetry),
            network,
            environment,
            filesystem,
            command,
        )


REMOVED = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def _removed_probe(tmp_path: Path, values: dict[str, str]):
    network, environment, filesystem, command = passing_probes(tmp_path)
    environment.values = values
    probes = run(
        preflight_request(tmp_path, removed_environment=REMOVED),
        network,
        environment,
        filesystem,
        command,
    )
    return next(probe for probe in probes if probe.name == "removed-environment")


def test_preflight_warns_by_name_when_a_removed_variable_is_set_in_the_parent(
    tmp_path: Path,
) -> None:
    sentinel = "sentinel-billing-value-7f3a"

    probe = _removed_probe(tmp_path, {"ANTHROPIC_API_KEY": sentinel})

    assert probe.passed is True
    assert probe.detail == (
        "warning: ANTHROPIC_API_KEY is set in the parent environment "
        "and is removed from every seat child"
    )
    assert sentinel not in probe.detail


def test_preflight_names_every_removed_variable_that_is_set(tmp_path: Path) -> None:
    sentinel = "sentinel-billing-value-7f3a"

    probe = _removed_probe(
        tmp_path, {"ANTHROPIC_API_KEY": sentinel, "ANTHROPIC_AUTH_TOKEN": sentinel}
    )

    assert probe.detail == (
        "warning: ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN are set in the parent "
        "environment and are removed from every seat child"
    )
    assert sentinel not in probe.detail


def test_preflight_reports_no_warning_when_no_removed_variable_is_set(tmp_path: Path) -> None:
    probe = _removed_probe(tmp_path, {})

    assert probe.passed is True
    assert probe.detail == (
        "none set in the parent environment: ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN"
    )
    assert "warning" not in probe.detail


def test_preflight_stops_when_the_validator_executable_is_missing(tmp_path: Path) -> None:
    network, environment, filesystem, command = passing_probes(tmp_path)
    environment.executables = {}

    with pytest.raises(PreflightError, match="validator-runnable.*python"):
        run(preflight_request(tmp_path), network, environment, filesystem, command)


def test_preflight_stops_when_the_validator_script_is_absent(tmp_path: Path) -> None:
    network, environment, filesystem, _ = passing_probes(tmp_path)
    command = FakeCommandAdapter(identity("command"), scripted_results=[_cmd(tmp_path, "version")])
    validator = ValidatorCheck(
        "tests",
        ("python", "scripts/validate.py"),
        tmp_path,
        600.0,
        script_path=tmp_path / "scripts" / "validate.py",
    )

    with pytest.raises(PreflightError, match="validator-runnable.*validate.py"):
        run(preflight_request(tmp_path, validator=validator), network, environment, filesystem, command)


def test_preflight_names_the_failing_offline_validator_line(tmp_path: Path) -> None:
    network, environment, filesystem, _ = passing_probes(tmp_path)
    command = FakeCommandAdapter(
        identity("command"),
        scripted_results=[
            _cmd(tmp_path, "version"),
            _cmd(tmp_path, "offline", exit_code=1, stderr="ModuleNotFoundError: no network for pip\n"),
        ],
    )

    with pytest.raises(PreflightError, match="validator-offline.*ModuleNotFoundError"):
        run(preflight_request(tmp_path), network, environment, filesystem, command)


def test_preflight_refuses_a_command_timeout_below_the_validator_budget(tmp_path: Path) -> None:
    network, environment, filesystem, command = passing_probes(tmp_path)
    validator = ValidatorCheck("tests", ("python", "-m", "pytest"), tmp_path, 600.0, budget_seconds=1200.0)

    with pytest.raises(PreflightError, match="validator-budget.*budget"):
        run(preflight_request(tmp_path, validator=validator), network, environment, filesystem, command)


def test_preflight_stops_when_an_adapter_puts_instructions_on_argv(tmp_path: Path) -> None:
    network, environment, filesystem, command = passing_probes(tmp_path)
    transport = TransportCheck("github-copilot", 200_000, False)

    with pytest.raises(PreflightError, match="transport.*github-copilot"):
        run(preflight_request(tmp_path, transport=transport), network, environment, filesystem, command)


def test_preflight_stops_when_an_argv_element_is_too_large(tmp_path: Path) -> None:
    network, environment, filesystem, command = passing_probes(tmp_path)
    transport = TransportCheck("codex", 9_000, True)

    with pytest.raises(PreflightError, match="transport.*9000"):
        run(preflight_request(tmp_path, transport=transport), network, environment, filesystem, command)


def test_preflight_stops_when_a_ready_brief_is_missing(tmp_path: Path) -> None:
    network, environment, filesystem, command = passing_probes(tmp_path)
    filesystem.files = {}

    with pytest.raises(PreflightError, match="briefs.*APP-1"):
        run(preflight_request(tmp_path), network, environment, filesystem, command)


def test_preflight_stops_when_a_ready_brief_is_too_large(tmp_path: Path) -> None:
    network, environment, filesystem, command = passing_probes(tmp_path)
    filesystem.files = {str(tmp_path / "brief.md"): 2_000_000}

    with pytest.raises(PreflightError, match="briefs.*APP-1"):
        run(preflight_request(tmp_path), network, environment, filesystem, command)


# --- Brief faults refused at preflight -----------------------------------------

OUTSIDE_BRIEF = (
    "# Title\n\nItem nature: {nature}\n\n## Outcome\n\nBuild it.\n\n"
    "## Declared paths\n\n- `src/app.py`\n- `../outside/notes.md`\n"
)


def _brief_probes(tmp_path: Path, text: str):
    network, environment, filesystem, command = passing_probes(tmp_path)
    filesystem.texts = {str(tmp_path / "brief.md"): text}
    return network, environment, filesystem, command


def _fenced_request(tmp_path: Path, **overrides) -> PreflightRequest:
    repository = tmp_path / "repo"
    base = dict(repository_root=repository, allowed_roots=(repository,))
    base.update(overrides)
    return preflight_request(tmp_path, **base)


def test_preflight_refuses_a_repository_brief_whose_declared_path_leaves_the_fence(
    tmp_path: Path,
) -> None:
    probes = _brief_probes(tmp_path, OUTSIDE_BRIEF.format(nature="repository"))

    with pytest.raises(PreflightError) as caught:
        run(_fenced_request(tmp_path), *probes)

    message = str(caught.value)
    assert message.startswith("briefs preflight failed")
    assert "APP-1" in message
    assert "../outside/notes.md" in message
    assert "cross-repository" in message
    assert "src/app.py" not in message


def test_preflight_passes_the_same_paths_under_an_explicit_cross_repository_nature(
    tmp_path: Path,
) -> None:
    probes = _brief_probes(tmp_path, OUTSIDE_BRIEF.format(nature="cross-repository"))

    probes_run = run(_fenced_request(tmp_path), *probes)

    assert probes_run[-1].name == "briefs" and probes_run[-1].passed


def test_preflight_passes_a_route_token_in_the_declared_paths(tmp_path: Path) -> None:
    text = "Item nature: repository\n\n## Declared paths\n\n- `//test:run`\n- `src/app.py`\n"
    probes = _brief_probes(tmp_path, text)

    assert run(_fenced_request(tmp_path), *probes)[-1].passed


def test_preflight_passes_a_declared_path_inside_a_requested_allowed_root(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    probes = _brief_probes(tmp_path, OUTSIDE_BRIEF.format(nature="repository"))
    request = _fenced_request(
        tmp_path, allowed_roots=(repository, tmp_path / "outside")
    )

    assert run(request, *probes)[-1].passed


def test_out_of_fence_paths_returns_the_outside_tokens_in_brief_order() -> None:
    brief = (
        "## Declared paths\n\n"
        "- `../first/a.md`\n- `src/inside.py`\n- `/elsewhere/b.py`\n"
        "- `/account/home`\n- `../second/c.md`\n"
    )

    assert out_of_fence_paths(brief, repository_root=REPO) == (
        "../first/a.md",
        "/elsewhere/b.py",
        "../second/c.md",
    )
    assert out_of_fence_paths(
        brief, repository_root=REPO, allowed_roots=(Path("/elsewhere"),)
    ) == ("../first/a.md", "../second/c.md")


STAGING_BRIEF_LINES = (
    "It applies cleanly to main `1a2b3c4d` (`git apply --index`), and "
    "`src/components/dashboard/__tests__/` then passes (4 files, 27 tests).",
    "7. Remove with `git rm` the forked tracker, its local re-export and "
    "the `components/legacy/*` duplicates once nothing imports them, or name in "
    "the work record the importer that keeps each one [R-3].",
)


def test_preflight_warns_of_a_brief_that_tells_the_builder_to_stage_through_git(
    tmp_path: Path,
) -> None:
    lines = ["# Title", "", "Item nature: repository", "", "## Scope", "", "", "", ""]
    lines += [STAGING_BRIEF_LINES[0], "", STAGING_BRIEF_LINES[1], ""]
    probes = _brief_probes(tmp_path, "\n".join(lines))

    briefs = run(_fenced_request(tmp_path), *probes)[-1]

    assert briefs.name == "briefs" and briefs.passed
    assert "warning: brief for APP-1" in briefs.detail
    assert "line 12 `git rm`" in briefs.detail
    assert "line 10" not in briefs.detail
    assert "parked `brief:git-staging` at claim" in briefs.detail
    assert "no git staging instructions" not in briefs.detail


def test_preflight_passes_the_launch_and_names_only_the_refused_brief(
    tmp_path: Path,
) -> None:
    network, environment, filesystem, command = passing_probes(tmp_path)
    first, second = tmp_path / "first.md", tmp_path / "second.md"
    filesystem.files = {str(first): 10, str(second): 10}
    filesystem.texts = {str(first): EXCERPT_C, str(second): "# Clean\n\nBuild it.\n"}
    request = preflight_request(
        tmp_path, briefs=(BriefCheck("APP-1", first), BriefCheck("APP-2", second))
    )

    briefs = run(request, network, environment, filesystem, command)[-1]

    assert briefs.passed
    warning = briefs.detail.split("; ", 1)[1]
    assert "brief for APP-1" in warning
    assert "line 2 `git rm`" in warning
    assert "APP-2" not in warning


def test_preflight_quotes_at_most_160_characters_of_a_refused_line(tmp_path: Path) -> None:
    line = "Run `git add -A` " + "x" * 300
    probes = _brief_probes(tmp_path, line)

    detail = run(_fenced_request(tmp_path), *probes)[-1].detail

    assert line[:160] in detail
    assert line[:161] not in detail


@pytest.mark.parametrize("text", ["", " \n\t\n"], ids=["zero-byte", "whitespace"])
def test_preflight_stops_when_a_ready_brief_is_empty(tmp_path: Path, text: str) -> None:
    probes = _brief_probes(tmp_path, text)

    with pytest.raises(PreflightError) as caught:
        run(_fenced_request(tmp_path), *probes)

    assert str(caught.value) == (
        f"briefs preflight failed: brief for APP-1 is empty: {tmp_path / 'brief.md'}"
    )


def test_preflight_keeps_the_messages_of_an_unusable_brief(tmp_path: Path) -> None:
    path = tmp_path / "brief.md"

    def message(prepare) -> str:
        network, environment, filesystem, command = passing_probes(tmp_path)
        prepare(filesystem)
        with pytest.raises(PreflightError) as caught:
            run(preflight_request(tmp_path), network, environment, filesystem, command)
        return str(caught.value)

    assert message(lambda fs: fs.files.clear()) == (
        f"briefs preflight failed: brief for APP-1 is missing: {path}"
    )
    assert message(lambda fs: fs.files.update({str(path): 1_048_577})) == (
        f"briefs preflight failed: brief for APP-1 exceeds 1048576 bytes: {path}"
    )
    assert message(lambda fs: fs.read_errors.update({str(path): OSError("not text")})) == (
        "briefs preflight failed: brief for APP-1 cannot be read as text: not text"
    )


@pytest.mark.parametrize(
    "line",
    [
        "- Scope: remove the superseded modules with `git rm src/old.py`.",
        "Commit the change with git commit and a short message",
        "```\ngit add -A\n```",
        "Run `git stash push` to set the work aside.",
        "Then run `git reset --hard`.",
        "Then run `git reset HEAD~1`.",
        "Apply the carry with `git apply --cached carry.patch`.",
        "`git apply -3 carry.patch`",
        "git -C <worktree> add -A",
        "Do not edit the lock file; run `git add -A`.",
        "Run `git add -A`, not the validator.",
        "When done, stage with `git add -A`.",
        "Then, `git add -A`.",
        "Build it, then `git add -A` and finish.",
        "The builder must run `git add -A`.",
        "Run git -C ../repo add -A",
        "`git apply --3way x.patch`",
        "`git --git-dir=.git add -A`",
        "`git --work-tree=. add -A`",
        "`git -c user.name=x commit -m y`",
        "`git mv a b`",
        "`git apply --index x.patch`",
        "```\ngit apply -3 x.patch\n```",
        "The old files are removed with `git rm`.",
        "The patch is applied using `git apply --index x.patch`.",
        "Make sure you run `git add -A`.",
        "Remember to run `git add -A`.",
        "Be sure to commit with `git commit -m x`.",
        "Afterwards `git add -A`.",
        "- Run `git add -A`.",
        "You have to run `git add -A`.",
        "Afterward `git add -A`.",
        "The patch gets applied with `git apply --index x.patch`.",
        "The old file is removed (`git rm`).",
        "Old files: removed with `git rm`.",
        "Retire the old module with `git rm`.",
        "The builder removes them with `git rm`.",
        "The builder runs `git add -A` at the end.",
        "Do not edit main: run `git add -A` instead.",
        "Required: git commit -m x",
        "Build it, then `git reset --hard` so nothing stays.",
        "Afterwards `git add -A` as well.",
        "Build it, then `git add -A` plus the docs.",
        "Be sure to run `git add -A`.",
        "Let the old file be removed with `git rm`.",
        "The old file is removed with `cd x && git rm a`.",
    ],
)
def test_the_git_reader_refuses_a_staging_instruction(line: str) -> None:
    assert git_staging_lines(line)


@pytest.mark.parametrize(
    "line",
    [
        "Never run `git commit`, `git checkout`, `git stash` or `git clean` in the worktree.",
        "Delete files only with plain `rm`, never `git rm`, and stage nothing.",
        "Remove with plain `rm` (never `git rm`; nothing staged) the forked tracker.",
        "Apply the carry with `git apply -3 carry.patch`, then `git reset -q`.",
        "Check the carry with `git apply --check carry.patch`.",
        "Apply the carry with `git apply carry.patch`.",
        "Compare with `git -C <worktree> diff --cached`.",
        "> Remove with `git rm` the forked tracker and run `git add -A`.",
        "Do not run `git add -A`.",
        "Never stage, e.g. `git add -A`.",
        "The earlier `git apply -3` run failed on `AGENTS.md`.",
        "It applies cleanly (`git apply --index`), and the tests pass.",
        "A fake seat that runs `git commit --allow-empty` makes the close raise.",
        "The deletions were staged by `git rm` in the first build.",
        "Apply with `git apply -3`; the earlier build cleared the index with `git reset -q`.",
        "Five test files still commit by hand after such a command, so `git commit` "
        "finds nothing to commit and the fixture helper raises.",
        "Leave changes uncommitted. Forbidden: git commit, checkout, stash, clean, "
        "branch, rebase, reset, amend, force flags.",
        "The test commits by hand, and `git commit` finds nothing.",
        "Never: `git add -A`.",
        "The patch got applied with `git apply --index x.patch`.",
        "Cause: the patch applied with `git apply --index`.",
        "The builder ran `git add -A` at the end.",
        "The builder's notes quote `git add -A`.",
        "The builder was shown `git add -A` output.",
        "The builder has staged them with `git add -A` already.",
        "The lock is held with a lease while the earlier `git commit` finishes.",
        "The old file removed with `git rm` is still listed.",
    ],
)
def test_the_git_reader_passes_a_line_that_does_not_stage(line: str) -> None:
    assert git_staging_lines(line) == ()


@pytest.mark.parametrize(
    "line",
    [
        "When done, `git add -A` and finish.",
    ],
)
def test_the_git_reader_does_not_see_an_order_without_a_verb_modal_or_bare_command(
    line: str,
) -> None:
    # Known gap: this orders the builder to stage, but carries no cue the reader reads.
    assert git_staging_lines(line) == ()


@pytest.mark.parametrize(
    ("line", "invocation"),
    [
        ("Run git -C ../repo add -A", "git -C ../repo add -A"),
        ("`git --git-dir=.git add -A`", "git --git-dir=.git add -A"),
        ("`git --work-tree=. add -A`", "git --work-tree=. add -A"),
        ("`git -c user.name=x commit -m y`", "git -c user.name=x commit -m y"),
        ("`git apply --3way x.patch`", "git apply --3way x.patch"),
    ],
)
def test_the_git_reader_names_the_whole_invocation_past_global_options(
    line: str, invocation: str
) -> None:
    (refused,) = git_staging_lines(line)
    assert refused.invocations == (invocation,)


def test_the_git_reader_names_the_line_number_and_the_invocation() -> None:
    text = "# Title\n\nKeep it.\nApply with `git apply --index carry.patch` and `git rm a.py`.\n"

    (refused,) = git_staging_lines(text)

    assert refused.line_number == 4
    assert refused.invocations == ("git apply --index carry.patch", "git rm a.py")


def test_the_git_reader_ignores_negation_words_inside_a_fenced_block() -> None:
    text = "Never stage anything.\n\n```sh\n# never do this\ngit commit -m never\n```\n"

    (refused,) = git_staging_lines(text)

    assert refused.line_number == 5


def test_the_launch_preflight_passes_the_claim_time_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autobuild.bootstrap import composition
    from autobuild.bootstrap.profile import ProfileOverrides, load_settings
    from autobuild.domain.models import SeatTransport

    repository = tmp_path / "project"
    repository.mkdir()
    shared = tmp_path / "shared"
    (repository / ".autobuild.toml").write_text(
        '[run]\nlanes = ["claude-code"]\n\n[lanes.claude-code]\nbuilder = "b"\n'
        'reviewer = "r"\n\n[validator]\nid = "tests"\nargv = ["python", "-m", "pytest"]\n\n'
        f"[policy]\nallowed_roots = [{str(shared)!r}]\n",
        encoding="utf-8",
    )
    settings = load_settings(repository, None, ProfileOverrides())
    captured: list[PreflightRequest] = []
    monkeypatch.setattr(
        composition, "run_preflight", lambda request, **_: captured.append(request) or ()
    )
    monkeypatch.setattr(
        composition,
        "_seat_transport",
        lambda *_: SeatTransport(10, True, ()),
    )
    monkeypatch.setattr(composition, "_origin_https_target", lambda _: None)

    composition._preflight(
        settings,
        tmp_path / "scratch",
        FakeHarnessAdapter(identity("h")),
        FakeCommandAdapter(identity("c")),
        FakeTrackerAdapter(identity("t")),
        None,
        LeaseSurface(tmp_path / "tracker", SurfaceKind.TRACKER),
        CampaignRef("c", settings.repository),
        type("Repository", (), {"root": settings.repository})(),
    )

    (request,) = captured
    assert request.repository_root == settings.repository
    assert request.allowed_roots == (settings.repository, *settings.allowed_roots)
    assert settings.allowed_roots and request.allowed_roots[0] == settings.repository
