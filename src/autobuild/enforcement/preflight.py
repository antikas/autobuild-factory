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

from autobuild.domain import CommandRequest, LeaseSurface, PreflightError, PreflightProbe
from autobuild.ports import (
    CommandPort,
    EnvironmentProbePort,
    FilesystemProbePort,
    LeasePort,
    NetworkProbePort,
)

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
    max_brief_bytes: int = 1_048_576
    max_argv_bytes: int = 8_192


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
            command_id=f"preflight:validator-version:{validator.validator_id}",
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
            command_id=f"preflight:validator-offline:{validator.validator_id}",
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
    return PreflightProbe(
        name,
        True,
        f"{transport.harness} streams instructions by file; largest argv element "
        f"is {transport.max_argv_element} bytes",
    )


def _briefs(request: PreflightRequest, filesystem: FilesystemProbePort) -> PreflightProbe:
    name = "briefs"
    for brief in request.briefs:
        if not filesystem.is_file(brief.brief_path):
            raise _fail(name, f"brief for {brief.item_id} is missing: {brief.brief_path}")
        if filesystem.size(brief.brief_path) > request.max_brief_bytes:
            raise _fail(
                name,
                f"brief for {brief.item_id} exceeds {request.max_brief_bytes} bytes: "
                f"{brief.brief_path}",
            )
    checked = ", ".join(brief.item_id for brief in request.briefs) or "none ready"
    return PreflightProbe(name, True, f"brief files present and bounded: {checked}")


def _last_output_line(reference: str) -> str:
    path = Path(reference)
    if not path.is_file():
        return ""
    for line in reversed(path.read_text(encoding="utf-8", errors="replace").splitlines()):
        if line.strip():
            return line.strip()
    return ""
