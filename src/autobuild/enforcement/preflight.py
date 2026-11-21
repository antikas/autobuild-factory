"""Deterministic preflight doctor.

A launch that will fail on its environment fails here, before any tracker claim,
and the failure names its cause. The checks are deterministic and take the network,
environment and filesystem mechanisms behind small ports so the unit lane runs them
with fakes and without a network. Composition supplies the concrete adapters.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from autobuild.domain import (
    CommandRequest,
    ItemNature,
    LaneSignal,
    LaneSignalKind,
    LeaseSurface,
    PreflightError,
    PreflightProbe,
    UsagePolicy,
    UsageProbeFailure,
    validator_command_id,
)
from autobuild.ports import (
    CommandPort,
    EnvironmentProbePort,
    FilesystemProbePort,
    LaneStatePort,
    LeasePort,
    NetworkProbePort,
    UsageMeterPort,
)
from autobuild.enforcement.brief_git import MAX_QUOTED_LINE, git_staging_lines
from autobuild.enforcement.triage import declared_item_nature, out_of_fence_paths

# Environment names that redirect trust or interception on a child process. The
# child must not carry any of these unless the profile accepts the name.
INTERCEPTION_VARIABLES: tuple[str, ...] = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    "NODE_TLS_REJECT_UNAUTHORIZED",
    "GIT_SSL_CAINFO",
    "GIT_SSL_NO_VERIFY",
    "SSLKEYLOGFILE",
)


@dataclass(frozen=True, slots=True)
class TlsTarget:
    host: str
    port: int


@dataclass(frozen=True, slots=True)
class BriefCheck:
    item_id: str
    brief_path: Path


@dataclass(frozen=True, slots=True)
class TelemetryCheck:
    harness: str
    declared: tuple[tuple[str, str], ...]
    scratch_environment: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class TransportCheck:
    harness: str
    max_argv_element: int
    delivered_by_file: bool
    # A harness that runs the seat from inside the campaign process.
    in_process: bool = False


@dataclass(frozen=True, slots=True)
class ValidatorCheck:
    validator_id: str
    argv: tuple[str, ...]
    repository: Path
    command_timeout_seconds: float
    script_path: Path | None = None
    budget_seconds: float | None = None
    version_timeout_seconds: float = 20.0


@dataclass(frozen=True, slots=True)
class PreflightRequest:
    scratch_root: Path
    tls_targets: tuple[TlsTarget, ...]
    accepted_environment: frozenset[str]
    telemetry: TelemetryCheck
    transport: TransportCheck
    validator: ValidatorCheck
    briefs: tuple[BriefCheck, ...] = ()
    tracker_surface: LeaseSurface | None = None
    # Environment names the harness removes from every seat child.
    removed_environment: tuple[str, ...] = ()
    max_brief_bytes: int = 1_048_576
    max_argv_bytes: int = 8_192
    # The roots the claim-time triage receives. Without a repository root the
    # validator's repository stands in.
    repository_root: Path | None = None
    allowed_roots: tuple[Path, ...] = ()


def _fail(name: str, cause: str) -> PreflightError:
    return PreflightError(f"{name} preflight failed: {cause}")


def run_preflight(
    request: PreflightRequest,
    *,
    network: NetworkProbePort,
    environment: EnvironmentProbePort,
    filesystem: FilesystemProbePort,
    command: CommandPort,
    lease: LeasePort | None = None,
    now: Callable[[], float] = time.monotonic,
) -> tuple[PreflightProbe, ...]:
    """Run every probe in order. The first failure raises; a full pass returns
    the recorded probes for the run manifest."""

    probes: list[PreflightProbe] = []
    probes.append(_dns_tls(request, network))
    probes.append(_interception(request, environment))
    probes.append(_scratch(request, filesystem, lease))
    probes.append(_telemetry(request))
    probes.append(_removed_environment(request, environment))
    probes.append(_validator_runnable(request, environment, filesystem, command))
    probes.append(_validator_offline(request, network, command, now))
    probes.append(_validator_budget(request))
    probes.append(_transport(request))
    probes.append(_briefs(request, filesystem))
    return tuple(probes)


def _dns_tls(request: PreflightRequest, network: NetworkProbePort) -> PreflightProbe:
    name = "dns-tls"
    for target in request.tls_targets:
        try:
            network.resolve(target.host)
        except Exception as exc:
            raise _fail(name, f"cannot resolve {target.host}: {exc}") from exc
        try:
            network.tls_handshake(target.host, target.port)
        except Exception as exc:
            raise _fail(
                name, f"cannot complete a TLS handshake to {target.host}:{target.port}: {exc}"
            ) from exc
    if not request.tls_targets:
        return PreflightProbe(name, True, "no declared TLS targets")
    reached = ", ".join(f"{target.host}:{target.port}" for target in request.tls_targets)
    return PreflightProbe(name, True, f"reached {reached}")


def _interception(
    request: PreflightRequest, environment: EnvironmentProbePort
) -> PreflightProbe:
    name = "interception"
    presence: list[str] = []
    for variable in INTERCEPTION_VARIABLES:
        value = environment.value(variable)
        if value is None:
            continue
        if variable not in request.accepted_environment:
            raise _fail(
                name, f"{variable} is set but is not listed under accepted_environment"
            )
        presence.append(f"{variable}(accepted)")
    accepted = ", ".join(sorted(request.accepted_environment)) or "none"
    seen = ", ".join(presence) or "none present"
    return PreflightProbe(name, True, f"accepted={accepted}; present={seen}")


def _scratch(
    request: PreflightRequest,
    filesystem: FilesystemProbePort,
    lease: LeasePort | None = None,
) -> PreflightProbe:
    name = "scratch"
    root = request.scratch_root
    try:
        filesystem.ensure_directory(root)
    except Exception as exc:
        raise _fail(name, f"scratch root {root} cannot be created: {exc}") from exc
    try:
        filesystem.probe_write(root)
    except Exception as exc:
        raise _fail(name, f"scratch root {root} is not writable: {exc}") from exc
    locks = filesystem.foreign_locks(root)
    if locks:
        raise _fail(name, f"scratch root {root} holds another process lock: {locks[0]}")
    lease_detail = _tracker_lease(request, lease)
    return PreflightProbe(
        name, True, f"scratch root {root} is writable and unlocked; {lease_detail}"
    )


def _tracker_lease(request: PreflightRequest, lease: LeasePort | None) -> str:
    """Fail when a live campaign already holds the tracker-root surface.

    A stale or absent lease passes; a live one names the holder so a second
    campaign stops before any claim rather than write into a held surface."""

    surface = request.tracker_surface
    if lease is None or surface is None:
        return "tracker-root lease not checked"
    holder = lease.live_holder(surface)
    if holder is not None:
        raise _fail(
            "scratch",
            f"the tracker-root surface {surface.path} is held by campaign "
            f"{holder.campaign_id} (process {holder.process_id} on {holder.host})",
        )
    return f"tracker-root surface {surface.path} is free"


def _telemetry(request: PreflightRequest) -> PreflightProbe:
    name = "telemetry"
    scratch = dict(request.telemetry.scratch_environment)
    for variable, value in request.telemetry.declared:
        if scratch.get(variable) != value:
            raise _fail(
                name,
                f"{request.telemetry.harness} does not disable telemetry: "
                f"{variable} is not set to {value!r} in the child environment",
            )
    if not request.telemetry.declared:
        raise _fail(
            name, f"{request.telemetry.harness} declares no telemetry-disabling environment"
        )
    disabled = ", ".join(f"{key}={value}" for key, value in request.telemetry.declared)
    return PreflightProbe(name, True, f"{request.telemetry.harness} telemetry off: {disabled}")


def _removed_environment(
    request: PreflightRequest, environment: EnvironmentProbePort
) -> PreflightProbe:
    """Warn, by name only, when the parent carries a variable the seats lose.

    The value may be a credential, so the detail never includes it."""

    name = "removed-environment"
    present = [
        variable
        for variable in request.removed_environment
        if environment.value(variable) is not None
    ]
    if not present:
        listed = ", ".join(request.removed_environment) or "no names declared"
        return PreflightProbe(name, True, f"none set in the parent environment: {listed}")
    verb = "is" if len(present) == 1 else "are"
    return PreflightProbe(
        name,
        True,
        f"warning: {', '.join(present)} {verb} set in the parent environment "
        f"and {verb} removed from every seat child",
    )


def _validator_runnable(
    request: PreflightRequest,
    environment: EnvironmentProbePort,
    filesystem: FilesystemProbePort,
    command: CommandPort,
) -> PreflightProbe:
    name = "validator-runnable"
    validator = request.validator
    executable = validator.argv[0]
    resolved = environment.resolve_executable(executable)
    if resolved is None:
        raise _fail(name, f"validator executable was not found: {executable}")
    result = command.run(
        CommandRequest(
            command_id=validator_command_id(
                "preflight", "validator-version", validator=validator.validator_id
            ),
            argv=(executable, "--version"),
            cwd=validator.repository,
            timeout_seconds=validator.version_timeout_seconds,
        )
    )
    if result.timed_out:
        raise _fail(name, f"{executable} --version did not return within the probe window")
    if result.exit_code != 0:
        raise _fail(name, f"{executable} --version exited {result.exit_code}")
    if validator.script_path is not None and not filesystem.is_file(validator.script_path):
        raise _fail(
            name, f"validator script is missing from the repository: {validator.script_path}"
        )
    detail = f"{executable} resolves to {resolved} and reports its version"
    if validator.script_path is not None:
        detail += f"; script {validator.script_path} exists"
    return PreflightProbe(name, True, detail)


def _validator_offline(
    request: PreflightRequest,
    network: NetworkProbePort,
    command: CommandPort,
    now: Callable[[], float],
) -> PreflightProbe:
    name = "validator-offline"
    validator = request.validator
    port = network.closed_local_port()
    proxy = f"http://127.0.0.1:{port}"
    environment = (
        ("HTTP_PROXY", proxy),
        ("HTTPS_PROXY", proxy),
        ("ALL_PROXY", proxy),
        ("NO_PROXY", ""),
        ("UV_OFFLINE", "1"),
        ("PIP_NO_INDEX", "1"),
        ("UV_NO_SYNC", "1"),
    )
    started = now()
    result = command.run(
        CommandRequest(
            command_id=validator_command_id(
                "preflight", "validator-offline", validator=validator.validator_id
            ),
            argv=validator.argv,
            cwd=validator.repository,
            environment=environment,
            timeout_seconds=validator.command_timeout_seconds,
        )
    )
    duration = max(now() - started, 0.0)
    if result.timed_out:
        raise _fail(
            name,
            f"the validator did not finish offline within {validator.command_timeout_seconds}s",
        )
    if result.exit_code != 0:
        line = _last_output_line(result.stderr_ref) or _last_output_line(result.stdout_ref)
        raise _fail(
            name,
            f"the validator did not pass offline (exit {result.exit_code}): {line or 'no output'}",
        )
    if validator.budget_seconds is not None and duration > validator.budget_seconds:
        raise _fail(
            name,
            f"the offline validator took {duration:.1f}s, over the declared "
            f"budget of {validator.budget_seconds:.1f}s",
        )
    return PreflightProbe(name, True, f"validator passed offline in {duration:.1f}s")


def _validator_budget(request: PreflightRequest) -> PreflightProbe:
    name = "validator-budget"
    validator = request.validator
    if validator.budget_seconds is None:
        return PreflightProbe(name, True, "no budget declared")
    if validator.command_timeout_seconds < validator.budget_seconds:
        raise _fail(
            name,
            f"command_timeout_seconds ({validator.command_timeout_seconds:.0f}) is below "
            f"the declared validator budget ({validator.budget_seconds:.0f})",
        )
    return PreflightProbe(
        name,
        True,
        f"command_timeout_seconds {validator.command_timeout_seconds:.0f} honours "
        f"budget {validator.budget_seconds:.0f}",
    )


def _transport(request: PreflightRequest) -> PreflightProbe:
    name = "transport"
    transport = request.transport
    if not transport.delivered_by_file:
        raise _fail(
            name,
            f"{transport.harness} does not deliver instructions through a file or stdin",
        )
    if transport.max_argv_element > request.max_argv_bytes:
        raise _fail(
            name,
            f"{transport.harness} would place {transport.max_argv_element} bytes in one "
            f"argv element, over the {request.max_argv_bytes} byte limit",
        )
    where = "runs seats in process and streams" if transport.in_process else "streams"
    return PreflightProbe(
        name,
        True,
        f"{transport.harness} {where} instructions by file; largest argv element "
        f"is {transport.max_argv_element} bytes",
    )


def _briefs(request: PreflightRequest, filesystem: FilesystemProbePort) -> PreflightProbe:
    name = "briefs"
    warnings: list[str] = []
    for brief in request.briefs:
        if not filesystem.is_file(brief.brief_path):
            raise _fail(name, f"brief for {brief.item_id} is missing: {brief.brief_path}")
        if filesystem.size(brief.brief_path) > request.max_brief_bytes:
            raise _fail(
                name,
                f"brief for {brief.item_id} exceeds {request.max_brief_bytes} bytes: "
                f"{brief.brief_path}",
            )
        try:
            text = filesystem.read_text(brief.brief_path)
        except Exception as exc:
            raise _fail(
                name, f"brief for {brief.item_id} cannot be read as text: {exc}"
            ) from exc
        _brief_nature(request, brief, text)
        warning = _brief_git(brief, text)
        if warning is not None:
            warnings.append(warning)
    checked = ", ".join(brief.item_id for brief in request.briefs) or "none ready"
    if not warnings:
        return PreflightProbe(
            name,
            True,
            "brief files present and bounded, declared paths inside the fence, "
            f"no git staging instructions: {checked}",
        )
    return PreflightProbe(
        name,
        True,
        f"brief files present and bounded, declared paths inside the fence: {checked}; "
        + "; ".join(warnings),
    )


def _brief_nature(request: PreflightRequest, brief: BriefCheck, text: str) -> None:
    if declared_item_nature(text) is not ItemNature.REPOSITORY:
        return
    root = request.repository_root or request.validator.repository
    outside = out_of_fence_paths(
        text, repository_root=root, allowed_roots=request.allowed_roots
    )
    if not outside:
        return
    tokens = ", ".join(f"`{token}`" for token in outside)
    verb = "resolves" if len(outside) == 1 else "resolve"
    raise _fail(
        "briefs",
        f"brief for {brief.item_id} declares {tokens}, which {verb} outside the "
        "repository and the allowed roots, so the campaign would park the item as "
        f"`{ItemNature.CROSS_REPOSITORY.value}` at claim: {brief.brief_path}",
    )


def _brief_git(brief: BriefCheck, text: str) -> str | None:
    refused = git_staging_lines(text)
    if not refused:
        return None
    lines = "; ".join(
        f"line {line.line_number} `{'`, `'.join(line.invocations)}`: "
        f"{line.text[:MAX_QUOTED_LINE]}"
        for line in refused
    )
    return (
        f"warning: brief for {brief.item_id} tells the builder to stage, commit or "
        f"remove through git ({lines}); the item will be parked `brief:git-staging` "
        f"at claim and the launch goes on: {brief.brief_path}"
    )


@dataclass(frozen=True, slots=True)
class UsageLane:
    """One configured lane and the opaque meter id its harness declares."""

    name: str
    meter_id: str | None = None


def run_usage_preflight(
    policy: UsagePolicy,
    lanes: tuple[UsageLane, ...],
    *,
    meter: UsageMeterPort,
    lane_state: LaneStatePort,
    campaign_id: str,
) -> PreflightProbe:
    """Check every configured lane's usage meter before any claim.

    Each meter is read once. The launch is refused only when every configured
    lane is metered and at its stop level, or when a meter cannot be read under
    ``on_probe_failure = "refuse"``. Otherwise each metered lane at its stop level
    is cooled until the latest binding reset, an unmetered lane is never cooled,
    and the probe detail records warnings."""

    name = "usage"
    metered = [lane for lane in lanes if lane.meter_id]
    if not metered:
        return PreflightProbe(name, True, "no lane declares a usage meter")
    assessments = {
        meter_id: policy.assess_meter(meter.read, meter_id)
        for meter_id in dict.fromkeys(lane.meter_id for lane in metered if lane.meter_id)
    }
    notes: list[str] = []
    for meter_id, assessment in assessments.items():
        if not assessment.failure:
            continue
        if policy.on_probe_failure is UsageProbeFailure.REFUSE:
            raise _fail(name, f"usage meter {meter_id} is unavailable: {assessment.failure}")
        notes.append(f"warning: usage meter {meter_id} is unavailable ({assessment.failure})")
    stopped = [lane for lane in metered if assessments[lane.meter_id or ""].stops_lane]
    if stopped and len(stopped) == len(lanes):
        detail = "; ".join(
            f"{lane.name}: {assessments[lane.meter_id or ''].describe_stop(policy)}"
            for lane in stopped
        )
        raise _fail(name, f"every lane is at its usage stop level: {detail}")
    for lane in stopped:
        assessment = assessments[lane.meter_id or ""]
        detail = assessment.describe_stop(policy)
        lane_state.cool(
            lane.name,
            LaneSignal(LaneSignalKind.USAGE, reset_at=assessment.reset_at, detail=detail),
            campaign_id,
        )
        notes.append(f"{lane.name} cooled until {assessment.reset_at}: {detail}")
    for meter_id, assessment in assessments.items():
        for limit in assessment.warnings:
            notes.append(
                f"warning: usage meter {meter_id} {limit.describe()} is at or above "
                f"warn {policy.warn_percent:g}%"
            )
    return PreflightProbe(
        name, True, "; ".join(notes) or "every metered lane is below its usage stop levels"
    )


def _last_output_line(reference: str) -> str:
    path = Path(reference)
    if not path.is_file():
        return ""
    for line in reversed(path.read_text(encoding="utf-8", errors="replace").splitlines()):
        if line.strip():
            return line.strip()
    return ""
