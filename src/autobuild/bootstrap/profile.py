"""Validated project profile for one production AutoBuild campaign."""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from autobuild.domain import CampaignSelection, FogRecord, Proposal, RefillPlan


class ConfigurationError(ValueError):
    """A run profile is absent, incomplete or internally inconsistent."""


@dataclass(frozen=True, slots=True)
class ProfileOverrides:
    harness: str | None = None
    harness_command: tuple[str, ...] = ()
    builder_model: str | None = None
    reviewer_model: str | None = None
    specialist_model: str | None = None
    validator_id: str | None = None
    validator_argv_json: str | None = None
    allowed_tools: tuple[str, ...] = ()
    allowed_roots: tuple[str, ...] = ()
    max_items: int | None = None
    scratch_root: str | None = None
    refill_plan: str | None = None
    tracker_kind: str | None = None
    backlog_path: str | None = None
    allow_items: tuple[str, ...] = ()
    exclude_items: tuple[str, ...] = ()
    lane_state_root: str | None = None


@dataclass(frozen=True, slots=True)
class LaneProfile:
    """One harness lane's name and its per-seat model names."""

    name: str
    builder_model: str
    reviewer_model: str
    specialist_model: str


@dataclass(frozen=True, slots=True)
class RunSettings:
    repository: Path
    harness: str
    harness_command: tuple[str, ...] | None
    builder_model: str
    reviewer_model: str
    specialist_model: str
    validator_id: str
    validator_argv: tuple[str, ...]
    validator_budget_seconds: float | None
    allowed_tools: frozenset[str]
    allowed_roots: tuple[Path, ...]
    max_items: int
    seat_timeout_seconds: float
    seat_stall_seconds: float
    lease_stale_seconds: float
    item_classes: Mapping[str, float]
    command_timeout_seconds: float
    scratch_root: Path | None
    lanes: tuple[LaneProfile, ...]
    lane_state_root: Path | None
    lane_cool_seconds: float
    tracker_kind: str
    backlog_path: Path
    selection: CampaignSelection
    tls_targets: tuple[str, ...]
    accepted_environment: frozenset[str]
    refill_plan: RefillPlan | None
    knowledge_command: tuple[str, ...] | None
    fog_ledger: Path | None
    progress_command: tuple[str, ...] | None
    progress_file: bool
    progress_stderr: bool
    progress_command_timeout_seconds: float


def _table(document: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = document.get(key, {})
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"[{key}] must be a TOML table")
    return value


def _optional_string(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{label} must be a non-empty string")
    return value.strip()


def _string_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(
        isinstance(part, str) and part for part in value
    ):
        raise ConfigurationError(f"{label} must be a non-empty string array")
    return tuple(value)


def _resolve_path(value: str | Path, base: Path) -> Path:
    path = Path(value).expanduser()
    return (base / path).resolve(strict=False) if not path.is_absolute() else path.resolve(strict=False)


def _load_document(profile_path: Path | None) -> tuple[Mapping[str, Any], Path | None]:
    if profile_path is None:
        return {}, None
    resolved = profile_path.expanduser().resolve(strict=False)
    if not resolved.is_file():
        raise ConfigurationError(f"run profile does not exist: {resolved}")
    try:
        return tomllib.loads(resolved.read_text(encoding="utf-8")), resolved
    except tomllib.TOMLDecodeError as exc:
        raise ConfigurationError(f"run profile is invalid TOML: {exc}") from exc


def _record(value: object, label: str, fields: tuple[str, ...]) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{label} must be an object")
    unknown = set(value) - set(fields)
    if unknown:
        raise ConfigurationError(
            f"{label} contains unknown fields: " + ", ".join(sorted(unknown))
        )
    missing = [field for field in fields if field not in value]
    if missing:
        raise ConfigurationError(
            f"{label} is missing fields: " + ", ".join(missing)
        )
    result: dict[str, str] = {}
    for field in fields:
        text = value[field]
        if not isinstance(text, str) or not text.strip():
            raise ConfigurationError(f"{label}.{field} must be a non-empty string")
        result[field] = text.strip()
    return result


def _load_refill_plan(path: Path) -> RefillPlan:
    if not path.is_file():
        raise ConfigurationError(f"refill plan does not exist: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"refill plan is invalid JSON: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ConfigurationError("refill plan must be a JSON object")
    if payload.get("schema") != "autobuild.refill-plan.v1":
        raise ConfigurationError("refill plan schema must be autobuild.refill-plan.v1")
    unknown = set(payload) - {"schema", "proposals", "fog"}
    if unknown:
        raise ConfigurationError(
            "refill plan contains unknown fields: " + ", ".join(sorted(unknown))
        )
    proposals_value = payload.get("proposals", [])
    fog_value = payload.get("fog", [])
    if not isinstance(proposals_value, list) or not isinstance(fog_value, list):
        raise ConfigurationError("refill plan proposals and fog must be arrays")
    proposals = tuple(
        Proposal(**_record(value, f"proposals[{index}]", ("title", "question", "rationale", "brief_ref")))
        for index, value in enumerate(proposals_value)
    )
    fog = tuple(
        FogRecord(**_record(value, f"fog[{index}]", ("direction", "blocking_question", "surface_when")))
        for index, value in enumerate(fog_value)
    )
    if not proposals and not fog:
        raise ConfigurationError("refill plan must contain at least one proposal or fog record")
    return RefillPlan(proposals, fog)


def _required_settings(
    run: Mapping[str, Any],
    models: Mapping[str, Any],
    validator: Mapping[str, Any],
    overrides: ProfileOverrides,
) -> tuple[str, str, str, str, str]:
    harness = _optional_string(overrides.harness or run.get("harness"), "run.harness")
    builder = _optional_string(
        overrides.builder_model or models.get("builder"), "models.builder"
    )
    reviewer = _optional_string(
        overrides.reviewer_model or models.get("reviewer"), "models.reviewer"
    )
    specialist = _optional_string(
        overrides.specialist_model or models.get("specialist"), "models.specialist"
    )
    validator_id = _optional_string(
        overrides.validator_id or validator.get("id"), "validator.id"
    )
    missing = [
        label
        for label, value in (
            ("run.harness", harness),
            ("models.builder", builder),
            ("models.reviewer", reviewer),
            ("validator.id", validator_id),
        )
        if value is None
    ]
    if missing:
        raise ConfigurationError(
            "missing required run configuration: " + ", ".join(missing)
        )
    assert harness is not None and builder is not None and reviewer is not None
    assert validator_id is not None
    return harness, builder, reviewer, specialist or reviewer, validator_id


def _lane_profile(lanes_table: Mapping[str, Any], name: str) -> LaneProfile:
    table = lanes_table.get(name)
    if not isinstance(table, Mapping):
        raise ConfigurationError(f"[lanes.{name}] must be a TOML table")
    builder = _optional_string(table.get("builder"), f"lanes.{name}.builder")
    reviewer = _optional_string(table.get("reviewer"), f"lanes.{name}.reviewer")
    specialist = _optional_string(table.get("specialist"), f"lanes.{name}.specialist")
    missing = [
        label
        for label, value in (
            (f"lanes.{name}.builder", builder),
            (f"lanes.{name}.reviewer", reviewer),
        )
        if value is None
    ]
    if missing:
        raise ConfigurationError("missing lane configuration: " + ", ".join(missing))
    assert builder is not None and reviewer is not None
    return LaneProfile(name, builder, reviewer, specialist or reviewer)


def _lanes_and_validator(
    document: Mapping[str, Any],
    run: Mapping[str, Any],
    models: Mapping[str, Any],
    validator: Mapping[str, Any],
    overrides: ProfileOverrides,
) -> tuple[tuple[LaneProfile, ...], str]:
    """Build the ordered tier map and the validator id.

    A profile with ``run.lanes`` and ``[lanes.<harness>]`` tables is the tier-map
    form; the single-lane ``run.harness`` plus ``[models]`` form stays valid and
    means one lane. ``--harness`` reorders the listed lanes so the named lane
    starts this launch."""

    validator_id = _optional_string(overrides.validator_id or validator.get("id"), "validator.id")
    run_lanes = run.get("lanes")
    if run_lanes is not None:
        order = list(_string_list(run_lanes, "run.lanes"))
        selected = _optional_string(overrides.harness, "--harness")
        if selected is not None:
            if selected not in order:
                raise ConfigurationError(
                    f"--harness {selected!r} is not one of run.lanes: " + ", ".join(order)
                )
            order = [selected, *[name for name in order if name != selected]]
        lanes_table = _table(document, "lanes")
        lanes = tuple(_lane_profile(lanes_table, name) for name in order)
        if validator_id is None:
            raise ConfigurationError("missing required run configuration: validator.id")
        return lanes, validator_id
    harness, builder, reviewer, specialist, validator_id = _required_settings(
        run, models, validator, overrides
    )
    return (LaneProfile(harness, builder, reviewer, specialist),), validator_id


def _validator_argv(
    validator: Mapping[str, Any], overrides: ProfileOverrides
) -> tuple[str, ...]:
    if overrides.validator_argv_json:
        try:
            value = json.loads(overrides.validator_argv_json)
        except json.JSONDecodeError as exc:
            raise ConfigurationError("--validator-argv-json is not valid JSON") from exc
    else:
        value = validator.get("argv")
    return _string_list(value, "validator.argv")


def _allowed_tools(
    policy: Mapping[str, Any], overrides: ProfileOverrides
) -> frozenset[str]:
    value = list(overrides.allowed_tools) or policy.get(
        "allowed_tools", ["read", "write", "shell", "python", "git"]
    )
    tools = frozenset(_string_list(value, "policy.allowed_tools"))
    unknown = tools - {"read", "write", "shell", "python", "git"}
    if unknown:
        raise ConfigurationError(
            "policy.allowed_tools contains unknown semantic tools: "
            + ", ".join(sorted(unknown))
        )
    return tools


def _allowed_roots(
    policy: Mapping[str, Any], overrides: ProfileOverrides, base: Path
) -> tuple[Path, ...]:
    profile_roots = policy.get("allowed_roots", [])
    if not isinstance(profile_roots, list):
        raise ConfigurationError("policy.allowed_roots must be a string array")
    values = profile_roots + list(overrides.allowed_roots)
    if not all(isinstance(value, str) and value for value in values):
        raise ConfigurationError("policy.allowed_roots must be a string array")
    return tuple(_resolve_path(value, base) for value in values)


def _tls_targets(preflight: Mapping[str, Any]) -> tuple[str, ...]:
    value = preflight.get("tls_targets", [])
    if not isinstance(value, list) or not all(isinstance(part, str) for part in value):
        raise ConfigurationError("preflight.tls_targets must be a string array")
    targets: list[str] = []
    for entry in value:
        text = entry.strip()
        host, separator, port = text.rpartition(":")
        if not separator or not host or not port.isdigit():
            raise ConfigurationError(
                f"preflight.tls_targets entry must be host:port, got {entry!r}"
            )
        targets.append(f"{host}:{int(port)}")
    return tuple(targets)


def _accepted_environment(preflight: Mapping[str, Any]) -> frozenset[str]:
    value = preflight.get("accepted_environment", [])
    if not isinstance(value, list) or not all(
        isinstance(part, str) and part.strip() for part in value
    ):
        raise ConfigurationError("preflight.accepted_environment must be a string array")
    return frozenset(part.strip() for part in value)


def _selection_list(
    profile_value: object,
    cli_values: tuple[str, ...],
    label: str,
    profile_path: Path | None,
) -> tuple[tuple[str, ...], str]:
    profile_items: tuple[str, ...] = ()
    if profile_value is not None:
        if not isinstance(profile_value, list) or not all(
            isinstance(part, str) and part.strip() for part in profile_value
        ):
            raise ConfigurationError(f"{label} must be an array of non-empty strings")
        profile_items = tuple(part.strip() for part in profile_value)
    cli_items = tuple(value.strip() for value in cli_values if value.strip())
    merged: list[str] = []
    for value in profile_items + cli_items:
        if value not in merged:
            merged.append(value)
    sources: list[str] = []
    if profile_items:
        sources.append(str(profile_path) if profile_path is not None else "profile")
    if cli_items:
        sources.append("command line")
    return tuple(merged), ", ".join(sources)


def _selection(
    table: Mapping[str, Any],
    overrides: ProfileOverrides,
    profile_path: Path | None,
) -> CampaignSelection:
    allow, allow_source = _selection_list(
        table.get("allow"), overrides.allow_items, "selection.allow", profile_path
    )
    exclude, exclude_source = _selection_list(
        table.get("exclude"), overrides.exclude_items, "selection.exclude", profile_path
    )
    return CampaignSelection(allow, exclude, allow_source, exclude_source)


def _item_classes(run: Mapping[str, Any]) -> dict[str, float]:
    value = run.get("item_classes", {})
    if not isinstance(value, Mapping):
        raise ConfigurationError("[run.item_classes] must be a TOML table")
    classes: dict[str, float] = {}
    for name, cap in value.items():
        if not isinstance(cap, (int, float)) or isinstance(cap, bool) or cap <= 0:
            raise ConfigurationError(
                f"run.item_classes.{name} must be a positive number of seconds"
            )
        classes[name] = float(cap)
    return classes


def _progress_boolean(value: object, default: bool, label: str) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ConfigurationError(f"{label} must be a boolean")
    return value


def _progress(table: Mapping[str, Any]) -> tuple[tuple[str, ...] | None, bool, bool, float]:
    """Validate the human-approved ``[progress]`` table.

    ``command`` is an optional non-empty string array run once per line with the
    line on stdin; ``file`` and ``stderr`` are booleans defaulting to true;
    ``command_timeout_seconds`` is a positive per-call ceiling defaulting to 5."""

    command_value = table.get("command")
    command = (
        None if command_value is None else _string_list(command_value, "progress.command")
    )
    file_enabled = _progress_boolean(table.get("file"), True, "progress.file")
    stderr_enabled = _progress_boolean(table.get("stderr"), True, "progress.stderr")
    timeout = table.get("command_timeout_seconds", 5)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise ConfigurationError(
            "progress.command_timeout_seconds must be a positive number of seconds"
        )
    return command, file_enabled, stderr_enabled, float(timeout)


def _budget_seconds(validator: Mapping[str, Any]) -> float | None:
    if "budget_seconds" not in validator:
        return None
    value = validator["budget_seconds"]
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise ConfigurationError("validator.budget_seconds must be a positive number")
    return float(value)


def read_run_scratch_root(
    repository: str | Path,
    profile_path: str | Path | None,
    override: str | None,
) -> Path | None:
    """Resolve the run scratch root from ``--scratch-root`` then ``[run]
    scratch_root``, reading no other profile field.

    This never loads models, the validator or lane tables, so a watcher can find
    a run's scratch root from a profile that would not pass full validation. An
    explicit override is resolved against the working directory and short-circuits
    the profile entirely. A profile ``[run] scratch_root`` is resolved against the
    profile's own directory, matching ``load_settings``. Returns ``None`` when
    neither source sets it, leaving the default to the caller."""

    if override:
        return Path(override).expanduser().resolve(strict=False)
    resolved_repository = Path(repository).expanduser().resolve(strict=False)
    default_profile = resolved_repository / ".autobuild.toml"
    selected_profile = (
        Path(profile_path).expanduser()
        if profile_path is not None
        else default_profile
        if default_profile.is_file()
        else None
    )
    document, resolved_profile = _load_document(selected_profile)
    base = resolved_profile.parent if resolved_profile is not None else resolved_repository
    run = _table(document, "run")
    scratch_value = _optional_string(run.get("scratch_root"), "run.scratch_root")
    return _resolve_path(scratch_value, base) if scratch_value else None


def load_settings(
    repository: str | Path,
    profile_path: str | Path | None = None,
    overrides: ProfileOverrides = ProfileOverrides(),
) -> RunSettings:
    resolved_repository = Path(repository).expanduser().resolve(strict=True)
    if not resolved_repository.is_dir():
        raise ConfigurationError(f"repository is not a directory: {resolved_repository}")
    default_profile = resolved_repository / ".autobuild.toml"
    selected_profile = (
        Path(profile_path).expanduser()
        if profile_path is not None
        else default_profile
        if default_profile.is_file()
        else None
    )
    document, resolved_profile = _load_document(selected_profile)
    base = resolved_profile.parent if resolved_profile is not None else resolved_repository
    run = _table(document, "run")
    models = _table(document, "models")
    validator = _table(document, "validator")
    harness = _table(document, "harness")
    policy = _table(document, "policy")
    tracker = _table(document, "tracker")
    preflight = _table(document, "preflight")
    refill = _table(document, "refill")
    knowledge = _table(document, "knowledge")
    selection = _table(document, "selection")
    progress = _table(document, "progress")

    lanes, validator_id = _lanes_and_validator(
        document, run, models, validator, overrides
    )
    primary = lanes[0]
    harness_name = primary.name
    builder = primary.builder_model
    reviewer = primary.reviewer_model
    specialist = primary.specialist_model
    raw_harness_command = harness.get("command")
    if overrides.harness_command:
        harness_command = overrides.harness_command
    elif raw_harness_command is not None:
        harness_command = _string_list(raw_harness_command, "harness.command")
    else:
        harness_command = None
    max_items = (
        overrides.max_items
        if overrides.max_items is not None
        else int(run.get("max_items", 20))
    )
    seat_timeout = float(run.get("seat_timeout_seconds", 900))
    seat_stall = float(run.get("seat_stall_seconds", 900))
    lease_stale = float(run.get("lease_stale_seconds", 1800))
    command_timeout = float(run.get("command_timeout_seconds", 600))
    if (
        max_items < 1
        or seat_timeout <= 0
        or seat_stall <= 0
        or lease_stale <= 0
        or command_timeout <= 0
    ):
        raise ConfigurationError("run bounds and timeouts must be positive")
    item_classes = _item_classes(run)

    scratch_value = overrides.scratch_root or _optional_string(
        run.get("scratch_root"), "run.scratch_root"
    )
    scratch_root = _resolve_path(scratch_value, base) if scratch_value else None
    lane_state_value = overrides.lane_state_root or _optional_string(
        run.get("lane_state_root"), "run.lane_state_root"
    )
    lane_state_root = _resolve_path(lane_state_value, base) if lane_state_value else None
    lane_cool_seconds = float(run.get("lane_cool_seconds", 3600))
    if lane_cool_seconds <= 0:
        raise ConfigurationError("run.lane_cool_seconds must be a positive number of seconds")
    tracker_kind = (
        _optional_string(overrides.tracker_kind or tracker.get("kind"), "tracker.kind")
        or "auto"
    ).casefold()
    if tracker_kind not in {"auto", "pinax", "backlog"}:
        raise ConfigurationError("tracker.kind must be auto, pinax or backlog")
    backlog_value = overrides.backlog_path or _optional_string(
        tracker.get("path"), "tracker.path"
    )
    if backlog_value:
        backlog_path = _resolve_path(backlog_value, resolved_repository)
    elif (resolved_repository / "BACKLOG.md").is_file():
        backlog_path = resolved_repository / "BACKLOG.md"
    elif (resolved_repository / "docs" / "BACKLOG.md").is_file():
        backlog_path = resolved_repository / "docs" / "BACKLOG.md"
    else:
        backlog_path = resolved_repository / "BACKLOG.md"
    refill_value = overrides.refill_plan or _optional_string(
        refill.get("plan"), "refill.plan"
    )
    refill_path = _resolve_path(refill_value, base) if refill_value else None
    refill_plan = _load_refill_plan(refill_path) if refill_path else None
    raw_knowledge_command = knowledge.get("command")
    knowledge_command = (
        _string_list(raw_knowledge_command, "knowledge.command")
        if raw_knowledge_command is not None
        else None
    )
    tls_targets = _tls_targets(preflight)
    accepted_environment = _accepted_environment(preflight)
    validator_budget_seconds = _budget_seconds(validator)
    fog_value = _optional_string(knowledge.get("fog_ledger"), "knowledge.fog_ledger")
    fog_ledger = _resolve_path(fog_value, base) if fog_value else None
    if (knowledge_command is None) != (fog_ledger is None):
        raise ConfigurationError(
            "knowledge.command and knowledge.fog_ledger must be supplied together"
        )
    if refill_plan is not None and refill_plan.fog and fog_ledger is None:
        raise ConfigurationError(
            "a refill plan containing fog requires knowledge.command and knowledge.fog_ledger"
        )
    progress_command, progress_file, progress_stderr, progress_timeout = _progress(progress)
    return RunSettings(
        repository=resolved_repository,
        harness=harness_name,
        harness_command=harness_command,
        builder_model=builder,
        reviewer_model=reviewer,
        specialist_model=specialist,
        validator_id=validator_id,
        validator_argv=_validator_argv(validator, overrides),
        validator_budget_seconds=validator_budget_seconds,
        allowed_tools=_allowed_tools(policy, overrides),
        allowed_roots=_allowed_roots(policy, overrides, base),
        max_items=max_items,
        seat_timeout_seconds=seat_timeout,
        seat_stall_seconds=seat_stall,
        lease_stale_seconds=lease_stale,
        item_classes=item_classes,
        command_timeout_seconds=command_timeout,
        scratch_root=scratch_root,
        lanes=lanes,
        lane_state_root=lane_state_root,
        lane_cool_seconds=lane_cool_seconds,
        tracker_kind=tracker_kind,
        backlog_path=backlog_path,
        selection=_selection(selection, overrides, resolved_profile),
        tls_targets=tls_targets,
        accepted_environment=accepted_environment,
        refill_plan=refill_plan,
        knowledge_command=knowledge_command,
        fog_ledger=fog_ledger,
        progress_command=progress_command,
        progress_file=progress_file,
        progress_stderr=progress_stderr,
        progress_command_timeout_seconds=progress_timeout,
    )
