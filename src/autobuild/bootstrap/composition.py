"""Production composition root for one AutoBuild campaign."""

from __future__ import annotations

import os
import subprocess
import threading
import urllib.parse
from dataclasses import replace
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
from autobuild.adapters.harness_cli import item_scratch_environment
from autobuild.adapters.claude_usage import (
    CLAUDE_SUBSCRIPTION_METER,
    ClaudeUsageMeterAdapter,
    NullUsageMeterAdapter,
)
from autobuild.application import CampaignRunner, Lane, WorkflowPorts
from autobuild.bootstrap.builtins import register_first_party_harnesses
from autobuild.bootstrap.environment import configure_scratch_environment
from autobuild.bootstrap.profile import ConfigurationError, LaneProfile, RunSettings
from autobuild.bootstrap.registry import AdapterRegistry
from autobuild.domain import (
    AdapterIdentity,
    CampaignContext,
    CampaignRef,
    DeliveryMode,
    EffortLevel,
    ItemExecutionSpec,
    LaneSignal,
    LaneSignalKind,
    LeaseSurface,
    PortKind,
    PreflightError,
    PreflightProbe,
    ProbeResult,
    RefillPlan,
    Seat,
    SeatRequest,
    SurfaceKind,
    ToolPolicy,
    UsageReading,
    WorkspaceRef,
)
from autobuild.domain.models import BatchValidationPolicy, HostReading, SeatTransport
from autobuild.enforcement.preflight import UsageLane, run_usage_preflight
from autobuild.ports.contracts import SeatTransportPort, TrackerProjectionPort
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


def primary_checkout_lock() -> threading.Lock:
    """The one primary-checkout lock of a campaign.

    Composition creates it once and hands the same instance to the tracker
    adapter and the workspace adapter, so every local mutation of the primary
    checkout (a claim, park or proposal, a worktree add or remove, the delivery
    catch-up before its push and fast-forward after it) runs one at a time.
    Each adapter holds it only for that local mutation: never across its own
    fetch or push, a validator run, or a merge in an item worktree other than
    the delivery's tracker-only catch-up merge."""

    return threading.Lock()


def _probe(name: str, adapter: object) -> tuple[str, str, str]:
    result = adapter.probe()
    if not isinstance(result, ProbeResult):
        raise TypeError(f"{name} adapter probe did not return ProbeResult")
    if not result.available or result.identity is None:
        detail = "; ".join(result.diagnostics) or "unavailable"
        raise RuntimeError(f"{name} preflight failed: {detail}")
    return name, result.identity.name, result.identity.version


_REQUIRED_HARNESS_CAPABILITIES = frozenset({"fresh-seat", "cancel", "typed-result", "usage"})


def _require_lane_efforts(lane: LaneProfile, adapter: object) -> None:
    """Refuse the launch when a lane sets a seat effort its adapter cannot pass.

    An adapter states the levels it can pass in ``effort_levels``; one that
    states none cannot run any seat with an effort."""

    accepted = getattr(adapter, "effort_levels", frozenset())
    for seat, effort in lane.efforts().items():
        if effort not in accepted:
            levels = ", ".join(level.value for level in EffortLevel if level in accepted)
            raise ConfigurationError(
                f"lane {lane.name} sets {seat.value} effort {effort.value}, which its "
                f"harness adapter cannot pass (accepted: {levels or 'none'})"
            )


def _build_lanes(
    settings: RunSettings,
    command: object,
    scratch: Path,
    lane_state: FilesystemLaneStateAdapter,
    campaign_id: str,
    probe_only: bool = False,
):
    """Build one harness per configured lane in preference order.

    Every lane's seat efforts are checked against its adapter before any lane is
    probed, so a refused effort stops the launch with no probe run and no lane
    cooled. Each lane's adapter is then probed. A lane whose executable is
    missing, unauthenticated or lacking a required capability is cooled with the
    ``probe`` signature so the router skips it; the first capable lane is the
    active lane for this launch. When no lane is capable the launch fails.

    With ``probe_only`` nothing is cooled and no capable lane is not an error:
    the active lane is then None and the manifest carries each lane's result."""

    registry = AdapterRegistry()
    register_first_party_harnesses(registry)
    registry.load_entry_points()
    single = len(settings.lanes) == 1
    created: list[tuple[LaneProfile, object]] = []
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
        if lane.key_file is not None:
            config["key_file"] = lane.key_file
        adapter = registry.create(PortKind.HARNESS, lane.name, config)
        _require_lane_efforts(lane, adapter)
        created.append((lane, adapter))
    lanes: list[tuple[str, object]] = []
    lane_manifest: list[dict[str, object]] = []
    active: tuple[str, object, AdapterIdentity] | None = None
    diagnostics: list[str] = []
    for lane, adapter in created:
        probe = adapter.probe()
        capabilities = getattr(adapter, "capabilities", frozenset())
        missing = _REQUIRED_HARNESS_CAPABILITIES - capabilities
        capable = probe.available and not missing
        version = probe.identity.version if probe.identity is not None else "unavailable"
        detail = "; ".join(probe.diagnostics)
        if not capable:
            detail = detail or (
                "missing capabilities: " + ", ".join(sorted(missing))
                if missing
                else "unavailable"
            )
            diagnostics.append(f"{lane.name}: {detail}")
            if not probe_only:
                lane_state.cool(
                    lane.name, LaneSignal(LaneSignalKind.PROBE, detail=detail), campaign_id
                )
        elif active is None:
            active = (lane.name, adapter, probe.identity)
        lanes.append((lane.name, adapter))
        lane_manifest.append(
            {"name": lane.name, "available": bool(capable), "version": version, "detail": detail}
        )
    if active is None and not probe_only:
        raise RuntimeError("no capable harness lane: " + "; ".join(diagnostics))
    return lanes, active, lane_manifest


def _command_adapter(scratch: Path):
    return (
        WindowsCommandAdapter(scratch / "commands")
        if os.name == "nt"
        else PosixCommandAdapter(scratch / "commands")
    )


def _usage_meter(settings: RunSettings, command: object, scratch: Path, lanes_raw):
    """The usage meter for this run, or None when the profile has no ``[usage]``.

    The meter is chosen by the meter ids the lane adapters declare; a declared id
    no first-party meter serves gets the null meter, whose readings fail."""

    if settings.usage is None:
        return None
    declared = {getattr(adapter, "meter_id", None) for _name, adapter in lanes_raw}
    if declared & ClaudeUsageMeterAdapter.meter_ids:
        return ClaudeUsageMeterAdapter(command, scratch / "usage")
    return NullUsageMeterAdapter()


def _usage_preflight(
    settings: RunSettings, lanes_raw, meter, lane_state, campaign_id: str
) -> PreflightProbe | None:
    """Read every configured lane's meter before any claim; None without ``[usage]``."""

    if settings.usage is None or meter is None:
        return None
    return run_usage_preflight(
        settings.usage,
        tuple(
            UsageLane(name, getattr(adapter, "meter_id", None)) for name, adapter in lanes_raw
        ),
        meter=meter,
        lane_state=lane_state,
        campaign_id=campaign_id,
    )


def read_usage(
    scratch_root: Path | None = None, meter_id: str = CLAUDE_SUBSCRIPTION_METER
) -> UsageReading:
    """One read-only meter reading for the ``autobuild usage`` command."""

    scratch = configure_scratch_environment(scratch_root)
    meter = ClaudeUsageMeterAdapter(_command_adapter(scratch), scratch / "usage")
    return meter.read(meter_id)


def _repository_adapters(
    settings: RunSettings,
    scratch: Path,
    delivery_mode: DeliveryMode,
    push_current_branch: bool,
):
    """The tracker and workspace adapters, sharing one primary-checkout lock.

    Returns ``(pinax, backlog, tracker, workspace, tracker_paths)``; ``tracker``
    is whichever of the two the profile or the probes chose."""

    push_primary = (
        delivery_mode is DeliveryMode.PROTECTED_DEFAULT or push_current_branch
    )
    primary_lock = primary_checkout_lock()
    pinax = PinaxTrackerAdapter(
        settings.repository,
        accept_unpublished_claims=delivery_mode is DeliveryMode.CURRENT_BRANCH_PR,
        primary_lock=primary_lock,
    )
    backlog = BacklogTrackerAdapter(
        settings.repository,
        settings.backlog_path,
        push_primary=push_primary,
        primary_lock=primary_lock,
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
    workspace = GitWorkspaceAdapter(
        scratch / "workspace",
        tracker_paths=tracker_paths,
        tracker_commits_itself=tracker.commits_itself,
        primary_lock=primary_lock,
        projection=tracker if isinstance(tracker, TrackerProjectionPort) else None,
        tracker_recovery=tracker.recover if tracker is pinax else None,
    )
    return pinax, backlog, tracker, workspace, tracker_paths


def _runtime(
    settings: RunSettings,
    scratch: Path,
    delivery_mode: DeliveryMode,
    push_current_branch: bool,
    campaign_id: str,
    probe_only: bool = False,
):
    command = _command_adapter(scratch)
    tracker, workspace = _repository_adapters(
        settings, scratch, delivery_mode, push_current_branch
    )[2:4]
    tracker_surface = LeaseSurface(
        settings.backlog_path.parent
        if isinstance(tracker, BacklogTrackerAdapter)
        else settings.repository / ".ergon",
        SurfaceKind.TRACKER,
    )
    lease = FilesystemLeaseAdapter(scratch, stale_seconds=settings.lease_stale_seconds)
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
        settings, command, scratch, lane_state, campaign_id, probe_only
    )
    if active is None:
        active_name, active_adapter, harness_entry = "", None, ("harness", "none", "unavailable")
    else:
        active_name, active_adapter, active_identity = active
        harness_entry = ("harness", active_identity.name, active_identity.version)
    manifest = [
        harness_entry,
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
    usage_meter=None,
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
    wrapped_lanes = tuple(_lane(name, policy.harness(adapter), adapter) for name, adapter in lanes_raw)
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
        usage_meter=usage_meter,
        usage_policy=settings.usage if usage_meter is not None else None,
        item_environment=_item_environment(scratch),
        host=host_resource_adapter(),
    )


def _item_environment(scratch: Path):
    """Map each item to its own temporary directories under ``<scratch>/items``."""

    root = scratch / "items"
    return lambda item_id: item_scratch_environment(root, item_id)


def _lane(name: str, harness, adapter: object) -> Lane:
    """Bind a lane to its policy-wrapped harness and the raw adapter's meter
    declaration, which the policy wrapper does not carry."""

    meter_id = getattr(adapter, "meter_id", None)
    return Lane(
        name,
        harness,
        meter_id=meter_id,
        meter_scope=getattr(adapter, "meter_scope_covers", None) if meter_id else None,
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


def _seat_transport(name: str, harness, request: SeatRequest) -> SeatTransport:
    """How the active harness delivers a seat, for the preflight doctor.

    A harness that declares its transport answers for itself; this is required
    of an in-process harness. A command-line harness without the declaration is
    read from the argument vector it would run."""

    if isinstance(harness, SeatTransportPort):
        return harness.seat_transport(request)
    invocation = getattr(harness, "_invocation", None)
    environment = getattr(harness, "_scratch_environment", None)
    if invocation is None or environment is None:
        raise ConfigurationError(f"harness {name} declares no seat transport for the preflight")
    argv, _extra, stdin_ref = invocation(request, "preflight:doctor")
    return SeatTransport(
        max((len(part.encode("utf-8")) for part in argv), default=0),
        stdin_ref is not None,
        tuple(environment()),
    )


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
    seat_transport = _seat_transport(settings.harness, harness, synthetic)
    telemetry = TelemetryCheck(
        settings.harness,
        tuple(getattr(harness, "telemetry_environment", ())),
        seat_transport.child_environment,
    )
    transport = TransportCheck(
        settings.harness,
        seat_transport.max_argv_element,
        seat_transport.delivered_by_file,
        seat_transport.in_process,
    )

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

    briefs: list[BriefCheck] = []
    for launch_item in _launch_items(tracker, campaign):
        brief_path = Path(launch_item.brief_ref).expanduser()
        if not brief_path.is_absolute():
            brief_path = repository.root / brief_path
        briefs.append(BriefCheck(launch_item.item_id, brief_path))

    return run_preflight(
        PreflightRequest(
            scratch_root=scratch,
            tls_targets=tuple(targets),
            accepted_environment=settings.accepted_environment,
            telemetry=telemetry,
            transport=transport,
            validator=validator,
            briefs=tuple(briefs),
            tracker_surface=tracker_surface,
            removed_environment=tuple(getattr(harness, "removed_environment", ())),
        ),
        network=SocketNetworkProbe(),
        environment=ProcessEnvironmentProbe(dict(os.environ)),
        filesystem=LocalFilesystemProbe(),
        command=command,
        lease=lease,
    )


def _launch_items(tracker, campaign: CampaignRef) -> tuple:
    """The items whose briefs the launch preflight checks.

    A serial campaign checks the item it would build first, as it always has.
    A campaign that builds more than one item at a time checks every ready
    item inside the selection fence, because any of them can be admitted at
    launch. A tracker that cannot be read here yields none; the campaign then
    meets the same tracker error at its first selection."""

    try:
        if campaign.concurrency == 1:
            peeked = tracker.next_item(campaign)
            return (peeked,) if peeked is not None else ()
        return tuple(
            item
            for item in tracker.ready_items(campaign)
            if campaign.selection.permits(item.item_id)
        )
    except Exception:
        return ()


# The host sample, in seconds, an automatic item concurrency is derived from.
AUTO_CONCURRENCY_SAMPLE_SECONDS = 0.5


def item_concurrency(settings: RunSettings, tracker, host=None) -> tuple[int, str]:
    """How many items this campaign builds at once, and where the value came from.

    An explicit ``run.concurrency`` wins (``profile``); ``1`` is the serial
    setting. A ``BACKLOG.md`` tracker cannot express dependencies, so it builds
    one item at a time unless the profile names an integer (``backlog``).
    Otherwise ``auto``, the default, reads the host once and applies the
    capacity rule (``auto``)."""

    items, source, _reading = _items_and_reading(settings, tracker, host)
    return items, source


def _items_and_reading(settings: RunSettings, tracker, host=None):
    from autobuild.enforcement.capacity import derive_capacity

    policy = settings.capacity
    if policy.concurrency is not None:
        return policy.concurrency, "profile", None
    if isinstance(tracker, BacklogTrackerAdapter):
        return 1, "backlog", None
    reading = (host or host_resource_adapter()).read(AUTO_CONCURRENCY_SAMPLE_SECONDS)
    return derive_capacity(reading, policy).items, "auto", reading


def campaign_concurrency(settings: RunSettings, tracker, host=None) -> tuple[int, str, int]:
    """The item concurrency, its source and the heavy-command limit.

    The heavy limit comes from the capacity rule with the item value fixed, so
    it never exceeds it. A serial campaign has a limit of 1 and reads nothing
    for it; an explicit ``run.heavy_concurrency`` needs no host reading."""

    from autobuild.enforcement.capacity import derive_capacity

    items, source, reading = _items_and_reading(settings, tracker, host)
    if items == 1:
        return items, source, 1
    policy = replace(settings.capacity, concurrency=items)
    if reading is None and policy.heavy_concurrency is None:
        reading = (host or host_resource_adapter()).read(AUTO_CONCURRENCY_SAMPLE_SECONDS)
    if reading is None:
        reading = HostReading.unmeasured("heavy concurrency is set by the profile")
    return items, source, derive_capacity(reading, policy).heavy


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
    lane_efforts: tuple[tuple[str, Seat, EffortLevel], ...] | None = None,
):
    resolved_lane_efforts = lane_efforts if lane_efforts is not None else _lane_efforts(settings)

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
            validator_patient_argv=settings.validator_patient_argv,
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
            lane_efforts=resolved_lane_efforts,
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


def _lane_efforts(settings: RunSettings) -> tuple[tuple[str, Seat, EffortLevel], ...]:
    return tuple(
        (lane.name, seat, effort)
        for lane in settings.lanes
        for seat, effort in lane.efforts().items()
    )


def _starting_lane_efforts(
    settings: RunSettings, lane_efforts: tuple[tuple[str, Seat, EffortLevel], ...]
) -> dict[str, EffortLevel | None]:
    starting_lane = settings.lanes[0].name
    return {
        seat.value: next(
            (
                effort
                for lane, configured_seat, effort in lane_efforts
                if lane == starting_lane and configured_seat is seat
            ),
            None,
        )
        for seat in Seat
    }


def preflight_report(
    settings: RunSettings, *, campaign_id: str | None = None
) -> dict[str, Any]:
    """Probe every lane and run the launch preflight, claiming nothing.

    Lane probes cool nothing. The shared lane state is read for each lane's
    current cooling and never written: a usage stop is assessed against a lane
    state private to this command."""

    scratch = configure_scratch_environment(settings.scratch_root)
    effective_id = campaign_id or datetime.now(UTC).strftime("preflight-%Y%m%dT%H%M%SZ")
    (
        tracker,
        _workspace,
        harness,
        command,
        _knowledge,
        lease,
        tracker_surface,
        _manifest,
        repository,
        lanes_raw,
        lane_state,
        lane_manifest,
        active_name,
    ) = _runtime(
        settings, scratch, DeliveryMode.PROTECTED_DEFAULT, False, effective_id, True
    )
    lanes: list[dict[str, object]] = []
    for entry in lane_manifest:
        cooling = lane_state.active(str(entry["name"]))
        lanes.append(
            {
                "name": entry["name"],
                "capable": entry["available"],
                "version": entry["version"],
                "detail": entry["detail"],
                "cooled_until": cooling.cooled_until if cooling is not None else None,
                "cooled_signature": cooling.signature if cooling is not None else None,
            }
        )
    probes: list[PreflightProbe] = []
    error: str | None = None
    if harness is None:
        error = "no capable harness lane"
    else:
        campaign = CampaignRef(
            effective_id,
            settings.repository,
            max_items=settings.max_items,
            refill_enabled=settings.refill_plan is not None,
            selection=settings.selection,
            concurrency=item_concurrency(settings, tracker)[0],
        )
        try:
            probes.extend(
                _preflight(
                    settings, scratch, harness, command, tracker, lease,
                    tracker_surface, campaign, repository,
                )
            )
            meter = _usage_meter(settings, command, scratch, lanes_raw)
            private_state = FilesystemLaneStateAdapter(
                scratch / "preflight" / effective_id, cool_seconds=settings.lane_cool_seconds
            )
            usage_probe = _usage_preflight(settings, lanes_raw, meter, private_state, effective_id)
            if usage_probe is not None:
                probes.append(usage_probe)
        except PreflightError as exc:
            error = str(exc)
    return {
        "schema": "autobuild.preflight.v1",
        "version": __version__,
        "repository": str(settings.repository),
        "scratch_root": str(scratch),
        "ok": error is None,
        "error": error,
        "active_lane": active_name or None,
        "lanes": lanes,
        "preflight": [
            {"name": probe.name, "passed": probe.passed, "detail": probe.detail}
            for probe in probes
        ],
    }


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
    concurrency, concurrency_source, heavy = campaign_concurrency(settings, tracker)
    campaign = CampaignRef(
        effective_campaign_id,
        settings.repository,
        max_items=settings.max_items,
        refill_enabled=settings.refill_plan is not None,
        selection=settings.selection,
        concurrency=concurrency,
        heavy_concurrency=heavy,
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
    usage_meter = _usage_meter(settings, command, scratch, lanes_raw)
    usage_probe = _usage_preflight(
        settings, lanes_raw, usage_meter, lane_state, effective_campaign_id
    )
    if usage_probe is not None:
        preflight_probes = (*preflight_probes, usage_probe)
    lanes_metadata = _lanes_metadata(settings, lane_manifest, lane_state, active_name)
    metadata = _runtime_metadata(settings, manifest, preflight_probes, lanes_metadata)
    metadata["concurrency"] = {
        "items": concurrency,
        "source": concurrency_source,
        "heavy": heavy,
    }
    records = LocalRunRecordAdapter(scratch / "runs", metadata=metadata)
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
        usage_meter,
    )
    refill = settings.refill_plan or RefillPlan()
    target_branch = repository.current_branch or repository.default_branch
    lane_efforts = _lane_efforts(settings)
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
        efforts=_starting_lane_efforts(settings, lane_efforts),
        push_current_branch=push_current_branch,
        allow_current_branch_default=allow_current_branch_default,
        tracker_surface=tracker_surface,
        lease_heartbeat_seconds=settings.lease_stale_seconds / 3,
        pressure=settings.pressure,
        hold_limit_seconds=settings.hold_limit_seconds,
        batch=BatchValidationPolicy(
            every=settings.batch_validate_every,
            argv=settings.validator_argv,
            patient_argv=settings.validator_patient_argv,
            timeout_seconds=settings.command_timeout_seconds,
            validator_id=settings.validator_id,
        ),
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
            lane_efforts,
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
        "schedule": {
            "concurrency": concurrency,
            "concurrency_source": concurrency_source,
            "heavy_concurrency": heavy,
            "peak_concurrency": outcome.peak_concurrency,
            "makespan_seconds": round(outcome.makespan_seconds, 3),
            "item_seconds": round(outcome.item_seconds, 3),
        },
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


def host_resource_adapter():
    """The host resource adapter for the current Python host, chosen as the
    command adapter is chosen."""

    import sys

    from autobuild.adapters.host_resources import (
        Kernel32HostResourceAdapter,
        PortableHostResourceAdapter,
        ProcHostResourceAdapter,
    )

    if os.name == "nt":
        return Kernel32HostResourceAdapter()
    if sys.platform.startswith("linux"):
        return ProcHostResourceAdapter()
    return PortableHostResourceAdapter()


def host_capacity(
    repository: str | Path,
    profile_path: str | Path | None,
    sample_seconds: float,
    adapter=None,
):
    """One host reading and the capacity it gives with the profile applied,
    for the ``autobuild host`` command. Returns the reading, the profile's
    capacity policy and the derived capacity."""

    from autobuild.bootstrap.profile import read_capacity_policy
    from autobuild.enforcement.capacity import derive_capacity

    policy = read_capacity_policy(repository, profile_path)
    reading = (adapter or host_resource_adapter()).read(sample_seconds)
    return reading, policy, derive_capacity(reading, policy)
