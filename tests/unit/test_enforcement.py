from __future__ import annotations

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
    run_preflight,
)
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
    filesystem = FakeFilesystemProbe(files={str(tmp_path / "brief.md"): 10})
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
