"""Production composition root for one AutoBuild campaign."""

from __future__ import annotations

import os
import subprocess
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from autobuild import __version__
from autobuild.adapters import (
    BacklogTrackerAdapter,
    CommandHookProgressAdapter,
    CompositeProgressAdapter,
    FileProgressAdapter,
    FilesystemLaneStateAdapter,
    FilesystemLeaseAdapter,
    GitWorkspaceAdapter,
    KoineKnowledgeAdapter,
    LocalFilesystemProbe,
    LocalRunRecordAdapter,
    NoRefillKnowledgeAdapter,
    PinaxTrackerAdapter,
    PosixCommandAdapter,
    ProcessEnvironmentProbe,
    SocketNetworkProbe,
    StderrProgressAdapter,
    WindowsCommandAdapter,
)
from autobuild.application import CampaignRunner, Lane, WorkflowPorts
from autobuild.bootstrap.builtins import register_first_party_harnesses
from autobuild.bootstrap.environment import configure_scratch_environment
from autobuild.bootstrap.profile import ConfigurationError, RunSettings
from autobuild.bootstrap.registry import AdapterRegistry
from autobuild.domain import (
    AdapterIdentity,
    CampaignContext,
    CampaignRef,
    DeliveryMode,
    ItemExecutionSpec,
    LaneSignal,
    LaneSignalKind,
    LeaseSurface,
    PortKind,
    PreflightProbe,
    ProbeResult,
    RefillPlan,
    Seat,
    SeatRequest,
    SurfaceKind,
    ToolPolicy,
    WorkspaceRef,
)
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
    declared_item_class,
    run_preflight,
)


def _max_seat_timeout(settings: RunSettings) -> float:
    """The policy ceiling for a seat: the largest of the default cap and every
    declared item class, so a class deadline is never refused by the ceiling."""

    return max([settings.seat_timeout_seconds, *settings.item_classes.values()])


def _probe(name: str, adapter: object) -> tuple[str, str, str]:
    result = adapter.probe()
    if not isinstance(result, ProbeResult):
        raise TypeError(f"{name} adapter probe did not return ProbeResult")
    if not result.available or result.identity is None:
        detail = "; ".join(result.diagnostics) or "unavailable"
        raise RuntimeError(f"{name} preflight failed: {detail}")
    return name, result.identity.name, result.identity.version


_REQUIRED_HARNESS_CAPABILITIES = frozenset({"fresh-seat", "cancel", "typed-result", "usage"})


def _build_lanes(
    settings: RunSettings,
    command: object,
    scratch: Path,
    lane_state: FilesystemLaneStateAdapter,
    campaign_id: str,
):
    """Build one harness per configured lane in preference order.

    Each lane's adapter is probed at launch. A lane whose executable is missing,
    unauthenticated or lacking a required capability is cooled with the ``probe``
    signature so the router skips it; the first capable lane is the active lane
    for this launch. When no lane is capable the launch fails."""

    registry = AdapterRegistry()
    register_first_party_harnesses(registry)
    registry.load_entry_points()
    single = len(settings.lanes) == 1
    lanes: list[tuple[str, object]] = []
    lane_manifest: list[dict[str, object]] = []
    active: tuple[str, object, AdapterIdentity] | None = None
    diagnostics: list[str] = []
    for lane in settings.lanes:
        config: dict[str, object] = {
            "command_port": command,
            "output_root": scratch / "harness" / (lane.name or "lane"),
            "model_map": {
                "builder": lane.builder_model,
                "reviewer": lane.reviewer_model,
                "specialist": lane.specialist_model,
            },
        }
        if single and settings.harness_command is not None:
            config["command"] = settings.harness_command
        adapter = registry.create(PortKind.HARNESS, lane.name, config)
        probe = adapter.probe()
        capabilities = getattr(adapter, "capabilities", frozenset())
        missing = _REQUIRED_HARNESS_CAPABILITIES - capabilities
        capable = probe.available and not missing
        version = probe.identity.version if probe.identity is not None else "unavailable"
        if not capable:
            detail = "; ".join(probe.diagnostics) or (
                "missing capabilities: " + ", ".join(sorted(missing))
                if missing
                else "unavailable"
            )
            diagnostics.append(f"{lane.name}: {detail}")
            lane_state.cool(
                lane.name, LaneSignal(LaneSignalKind.PROBE, detail=detail), campaign_id
            )
        elif active is None:
            active = (lane.name, adapter, probe.identity)
        lanes.append((lane.name, adapter))
        lane_manifest.append(
            {"name": lane.name, "available": bool(capable), "version": version}
        )
    if active is None:
        raise RuntimeError("no capable harness lane: " + "; ".join(diagnostics))
    return lanes, active, lane_manifest


def _runtime(
    settings: RunSettings,
    scratch: Path,
    delivery_mode: DeliveryMode,
    push_current_branch: bool,
    campaign_id: str,
):
    command = (
        WindowsCommandAdapter(scratch / "commands")
        if os.name == "nt"
        else PosixCommandAdapter(scratch / "commands")
    )
    push_primary = (
        delivery_mode is DeliveryMode.PROTECTED_DEFAULT or push_current_branch
    )
    pinax = PinaxTrackerAdapter(settings.repository, push_primary=push_primary)
    backlog = BacklogTrackerAdapter(
        settings.repository, settings.backlog_path, push_primary=push_primary
    )
    if settings.tracker_kind == "pinax":
        tracker = pinax
    elif settings.tracker_kind == "backlog":
        tracker = backlog
    else:
        pinax_probe = pinax.probe()
        backlog_probe = backlog.probe()
        if pinax_probe.available:
            tracker = pinax
        elif backlog_probe.available:
            tracker = backlog
        else:
            diagnostics = "; ".join((*pinax_probe.diagnostics, *backlog_probe.diagnostics))
            raise RuntimeError(f"tracker preflight failed: {diagnostics}")
    tracker_paths = (
        (backlog.tracker_path,)
        if isinstance(tracker, BacklogTrackerAdapter)
        else (Path(".ergon"),)
    )
    tracker_surface = LeaseSurface(
        settings.backlog_path.parent
        if isinstance(tracker, BacklogTrackerAdapter)
        else settings.repository / ".ergon",
        SurfaceKind.TRACKER,
    )
    lease = FilesystemLeaseAdapter(scratch, stale_seconds=settings.lease_stale_seconds)
    workspace = GitWorkspaceAdapter(
        scratch / "workspace", tracker_paths=tracker_paths
    )
    if settings.refill_plan is not None:
        for proposal in settings.refill_plan.proposals:
            tracker.validate_proposal(proposal)
    if settings.refill_plan is not None and settings.refill_plan.fog:
        assert settings.fog_ledger is not None
        assert settings.knowledge_command is not None
        knowledge = KoineKnowledgeAdapter(
            settings.fog_ledger, settings.knowledge_command
        )
    else:
        knowledge = NoRefillKnowledgeAdapter()
    lane_state = FilesystemLaneStateAdapter(
        settings.lane_state_root or scratch, cool_seconds=settings.lane_cool_seconds
    )
    lanes_raw, active, lane_manifest = _build_lanes(
        settings, command, scratch, lane_state, campaign_id
    )
    active_name, active_adapter, active_identity = active
    manifest = [
        ("harness", active_identity.name, active_identity.version),
        _probe("tracker", tracker),
        _probe("workspace", workspace),
        _probe("command", command),
        _probe("knowledge", knowledge),
        _probe("lease", lease),
        _probe("lane_state", lane_state),
    ]
    record_probe = LocalRunRecordAdapter(scratch / "runs")
    manifest.append(_probe("run_record", record_probe))
    repository = workspace.identify(settings.repository)
    return (
        tracker,
        workspace,
        active_adapter,
        command,
        knowledge,
        lease,
        tracker_surface,
        manifest,
        repository,
        lanes_raw,
        lane_state,
        lane_manifest,
        active_name,
    )


def _selection_metadata(settings: RunSettings) -> dict[str, object]:
    selection = settings.selection
    return {
        "allow": list(selection.allow),
        "exclude": list(selection.exclude),
        "allow_source": selection.allow_source,
        "exclude_source": selection.exclude_source,
    }


def _lanes_metadata(
    settings: RunSettings,
    lane_manifest: list[dict[str, object]],
    lane_state,
    active_name: str,
) -> dict[str, object]:
    return {
        "order": [entry["name"] for entry in lane_manifest],
        "active": active_name,
        "cool_seconds": settings.lane_cool_seconds,
        "state": lane_manifest,
        "cooled": [
            {
                "lane": cooling.lane,
                "cooled_until": cooling.cooled_until,
                "signature": cooling.signature,
                "last_failure_at": cooling.last_failure_at,
                "campaign_id": cooling.campaign_id,
            }
            for cooling in lane_state.snapshot()
        ],
    }


def _runtime_metadata(
    settings: RunSettings,
    manifest: list[tuple[str, str, str]],
    preflight_probes: tuple[PreflightProbe, ...] = (),
    lanes: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "selection": _selection_metadata(settings),
        "preflight": [
            {"name": probe.name, "passed": probe.passed, "detail": probe.detail}
            for probe in preflight_probes
        ],
        "workflow_version": __version__,
        "adapters": [
            {"boundary": boundary, "name": name, "version": version}
            for boundary, name, version in manifest
        ],
        "harness": settings.harness,
        "lanes": lanes or {},
        "tracker": {
            "requested": settings.tracker_kind,
            "backlog_path": str(settings.backlog_path),
        },
        "models": {
            "builder": settings.builder_model,
            "reviewer": settings.reviewer_model,
            "specialist": settings.specialist_model,
        },
        "validator": {
            "id": settings.validator_id,
            "argv": list(settings.validator_argv),
        },
        "refill": {
            "enabled": settings.refill_plan is not None,
            "proposal_count": len(settings.refill_plan.proposals)
            if settings.refill_plan is not None
            else 0,
            "fog_count": len(settings.refill_plan.fog)
            if settings.refill_plan is not None
            else 0,
        },
    }


def _require_delivery_shape(
    delivery_mode: DeliveryMode,
    repository,
    allow_current_branch_default: bool,
) -> None:
    if (
        delivery_mode is DeliveryMode.CURRENT_BRANCH_PR
        and not repository.current_branch
    ):
        raise ConfigurationError(
            "current-branch-pr delivery requires a named invoking branch"
        )
    if (
        delivery_mode is DeliveryMode.CURRENT_BRANCH_PR
        and repository.current_branch == repository.default_branch
        and not allow_current_branch_default
    ):
        raise ConfigurationError(
            "current-branch-pr delivery refuses the detected default branch; "
            "pass --allow-current-branch-default after human approval"
        )


def _progress_port(settings: RunSettings) -> CompositeProgressAdapter:
    """Build the composite progress stream from the human-approved profile.

    The file sink opens ``progress.log`` under the run record; the stderr sink
    keeps a detached launch's lines; the command hook, when configured, forwards
    each line to an approved command. All three default sinks are optional."""

    adapters: list[object] = []
    if settings.progress_file:
        adapters.append(FileProgressAdapter())
    if settings.progress_stderr:
        adapters.append(StderrProgressAdapter())
    if settings.progress_command is not None:
        adapters.append(
            CommandHookProgressAdapter(
                settings.progress_command, settings.progress_command_timeout_seconds
            )
        )
    return CompositeProgressAdapter(tuple(adapters))


def _ports(
    settings: RunSettings,
    scratch: Path,
    delivery_mode: DeliveryMode,
    tracker,
    workspace,
    harness,
    command,
    records,
    knowledge,
    lease,
    lanes_raw,
    lane_state,
    active_name,
    progress,
) -> WorkflowPorts:
    policy = PolicyGateway(
        PolicyConfig(
            allowed_roots=(settings.repository, scratch, *settings.allowed_roots),
            approved_validators=(
                ApprovedValidator(settings.validator_id, settings.validator_argv),
            ),
            allowed_tools=settings.allowed_tools
            | frozenset({Path(settings.validator_argv[0]).name}),
            max_command_timeout_seconds=settings.command_timeout_seconds,
            max_seat_timeout_seconds=_max_seat_timeout(settings),
            allow_repository_mutation=True,
            allow_protected_merge=delivery_mode is DeliveryMode.PROTECTED_DEFAULT,
        )
    )
    wrapped_lanes = tuple(
        Lane(name, policy.harness(adapter)) for name, adapter in lanes_raw
    )
    active_harness = next(
        (lane.harness for lane in wrapped_lanes if lane.name == active_name),
        policy.harness(harness),
    )
    return WorkflowPorts(
        ScopedTrackerPort(policy.tracker(tracker), settings.selection),
        policy.workspace(workspace),
        active_harness,
        policy.command(command),
        policy.records(records),
        policy.knowledge(knowledge),
        progress=progress,
        lease=policy.lease(lease),
        lanes=wrapped_lanes,
        lane_state=lane_state,
    )


def _origin_https_target(repository: Path):
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    url = completed.stdout.strip()
    if completed.returncode != 0 or not url:
        return None
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    return TlsTarget(parsed.hostname, parsed.port or 443)


def _preflight(
    settings: RunSettings,
    scratch: Path,
    harness,
    command,
    tracker,
    lease,
    tracker_surface: LeaseSurface,
    campaign: CampaignRef,
    repository,
) -> tuple[PreflightProbe, ...]:
    targets: list[TlsTarget] = []
    seen: set[tuple[str, int]] = set()
    for entry in settings.tls_targets:
        host, _, port = entry.rpartition(":")
        target = TlsTarget(host, int(port))
        targets.append(target)
        seen.add((target.host, target.port))
    origin = _origin_https_target(repository.root)
    if origin is not None and (origin.host, origin.port) not in seen:
        targets.append(origin)

    telemetry = TelemetryCheck(
        settings.harness,
        tuple(harness.telemetry_environment),
        tuple(harness._scratch_environment()),
    )

    doctor_root = scratch / "harness" / "preflight"
    doctor_root.mkdir(parents=True, exist_ok=True)
    doctor_workspace = WorkspaceRef(doctor_root, "preflight", "base", "preflight")
    synthetic = SeatRequest(
        "preflight",
        "doctor",
        Seat.BUILDER,
        "builder",
        doctor_root / "brief.md",
        doctor_workspace,
        ToolPolicy(settings.allowed_tools, (doctor_root,)),
        "x" * 200_000,
        "builder-report-v1",
        60,
    )
    argv, _extra, stdin_ref = harness._invocation(synthetic, "preflight:doctor")
    max_argv_element = max((len(part.encode("utf-8")) for part in argv), default=0)
    transport = TransportCheck(settings.harness, max_argv_element, stdin_ref is not None)

    script_path = None
    if len(settings.validator_argv) >= 2:
        candidate = settings.validator_argv[1]
        if candidate.endswith(".py") and not Path(candidate).is_absolute():
            script_path = repository.root / candidate
    validator = ValidatorCheck(
        validator_id=settings.validator_id,
        argv=settings.validator_argv,
        repository=repository.root,
        command_timeout_seconds=settings.command_timeout_seconds,
        script_path=script_path,
        budget_seconds=settings.validator_budget_seconds,
    )

    briefs: tuple[BriefCheck, ...] = ()
    try:
        peeked = tracker.next_item(campaign)
    except Exception:
        peeked = None
    if peeked is not None:
        brief_path = Path(peeked.brief_ref).expanduser()
        if not brief_path.is_absolute():
            brief_path = repository.root / brief_path
        briefs = (BriefCheck(peeked.item_id, brief_path),)

    return run_preflight(
        PreflightRequest(
            scratch_root=scratch,
            tls_targets=tuple(targets),
            accepted_environment=settings.accepted_environment,
            telemetry=telemetry,
            transport=transport,
            validator=validator,
            briefs=briefs,
            tracker_surface=tracker_surface,
        ),
        network=SocketNetworkProbe(),
        environment=ProcessEnvironmentProbe(dict(os.environ)),
        filesystem=LocalFilesystemProbe(),
        command=command,
        lease=lease,
    )


def _read_brief_text(repository: Path, brief_path: Path) -> str:
    """Read the brief once, before the claim, so triage can classify the item.

    The brief is committed in the primary checkout at this point. An unreadable
    brief classifies as a repository item and the preflight brief probe reports
    the missing file separately."""

    resolved = brief_path if brief_path.is_absolute() else repository / brief_path
    try:
        return resolved.read_text(encoding="utf-8")
    except OSError:
        return ""


def _specification(
    settings: RunSettings,
    delivery_mode: DeliveryMode,
    target_branch: str,
    target_revision: str,
    push_current_branch: bool,
    allow_current_branch_default: bool,
):
    def for_item(item) -> ItemExecutionSpec:
        brief_path = Path(item.brief_ref).expanduser()
        brief_text = _read_brief_text(settings.repository, brief_path)
        item_class = declared_item_class(brief_text)
        seat_timeout = settings.item_classes.get(item_class, settings.seat_timeout_seconds)
        return ItemExecutionSpec(
            item=item,
            brief_path=brief_path,
            brief_text=brief_text,
            validator_id=settings.validator_id,
            validator_argv=settings.validator_argv,
            tool_policy=ToolPolicy(
                settings.allowed_tools,
                (settings.repository, *settings.allowed_roots),
            ),
            reviewer_tool_policy=ToolPolicy(
                frozenset({"read"}),
                (settings.repository, *settings.allowed_roots),
            ),
            builder_model_class="builder",
            reviewer_model_class="reviewer",
            specialist_model_class="specialist",
            seat_timeout_seconds=seat_timeout,
            seat_stall_seconds=settings.seat_stall_seconds,
            command_timeout_seconds=settings.command_timeout_seconds,
            max_corrections=2,
            delivery_mode=delivery_mode,
            delivery_target_branch=target_branch,
            delivery_target_revision=target_revision,
            push_current_branch=push_current_branch,
            allow_current_branch_default=allow_current_branch_default,
        )

    return for_item


def run_campaign(
    settings: RunSettings,
    *,
    campaign_id: str | None = None,
    allow_delivery: bool,
    delivery_mode: DeliveryMode = DeliveryMode.PROTECTED_DEFAULT,
    push_current_branch: bool = False,
    allow_current_branch_default: bool = False,
) -> dict[str, Any]:
    if not allow_delivery:
        raise ConfigurationError(
            "this workflow claims items and delivers accepted changes; pass --allow-delivery after human approval"
        )
    if delivery_mode is DeliveryMode.PROTECTED_DEFAULT and (
        push_current_branch or allow_current_branch_default
    ):
        raise ConfigurationError(
            "--push-current-branch and --allow-current-branch-default require "
            "--delivery-mode current-branch-pr"
        )
    scratch = configure_scratch_environment(settings.scratch_root)
    effective_campaign_id = campaign_id or datetime.now(UTC).strftime(
        "autobuild-%Y%m%dT%H%M%SZ"
    )
    (
        tracker,
        workspace,
        harness,
        command,
        knowledge,
        lease,
        tracker_surface,
        manifest,
        repository,
        lanes_raw,
        lane_state,
        lane_manifest,
        active_name,
    ) = _runtime(
        settings, scratch, delivery_mode, push_current_branch, effective_campaign_id
    )
    _require_delivery_shape(delivery_mode, repository, allow_current_branch_default)
    campaign = CampaignRef(
        effective_campaign_id,
        settings.repository,
        max_items=settings.max_items,
        refill_enabled=settings.refill_plan is not None,
        selection=settings.selection,
    )
    preflight_probes = _preflight(
        settings,
        scratch,
        harness,
        command,
        tracker,
        lease,
        tracker_surface,
        campaign,
        repository,
    )
    lanes_metadata = _lanes_metadata(settings, lane_manifest, lane_state, active_name)
    records = LocalRunRecordAdapter(
        scratch / "runs",
        metadata=_runtime_metadata(settings, manifest, preflight_probes, lanes_metadata),
    )
    ports = _ports(
        settings,
        scratch,
        delivery_mode,
        tracker,
        workspace,
        harness,
        command,
        records,
        knowledge,
        lease,
        lanes_raw,
        lane_state,
        active_name,
        _progress_port(settings),
    )
    refill = settings.refill_plan or RefillPlan()
    target_branch = repository.current_branch or repository.default_branch
    context = CampaignContext(
        harness=settings.harness,
        models={
            "builder": settings.builder_model,
            "reviewer": settings.reviewer_model,
            "specialist": settings.specialist_model,
        },
        delivery_mode=delivery_mode,
        validator_id=settings.validator_id,
        target_branch=target_branch,
        target_revision=repository.revision,
        push_current_branch=push_current_branch,
        allow_current_branch_default=allow_current_branch_default,
        tracker_surface=tracker_surface,
    )
    outcome = CampaignRunner(ports).run(
        campaign,
        _specification(
            settings,
            delivery_mode,
            target_branch,
            repository.revision,
            push_current_branch,
            allow_current_branch_default,
        ),
        refill,
        context=context,
    )
    return {
        "schema": "autobuild.campaign-result.v1",
        "version": __version__,
        "campaign_id": outcome.campaign_id,
        "repository": str(settings.repository),
        "scratch_root": str(scratch),
        "adapters": [
            {"boundary": boundary, "name": name, "version": version}
            for boundary, name, version in manifest
        ],
        "stop_reason": outcome.stop_reason.value,
        "selection": _selection_metadata(settings),
        "refill": {
            "enabled": settings.refill_plan is not None,
            "proposal_count": len(refill.proposals),
            "fog_count": len(refill.fog),
        },
        "report_ref": outcome.report_ref,
        "repository_report_ref": outcome.repository_report_ref,
        "progress_ref": outcome.progress_ref,
        "items": [
            {
                "item_id": item.item_id,
                "disposition": item.disposition.value,
                "states": [state.value for state in item.states],
                "reason": item.reason,
                "item_commit": item.finalise.item_commit if item.finalise else None,
                "tracker_commit": item.finalise.tracker_commit if item.finalise else None,
                "merged_commit": item.finalise.merged_commit if item.finalise else None,
                "pushed": item.finalise.pushed if item.finalise else False,
            }
            for item in outcome.items
        ],
    }
