"""Validated project profile for one production AutoBuild campaign."""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from autobuild.domain import FogRecord, Proposal, RefillPlan


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
    allowed_tools: frozenset[str]
    allowed_roots: tuple[Path, ...]
    max_items: int
    seat_timeout_seconds: float
    command_timeout_seconds: float
    scratch_root: Path | None
    tracker_kind: str
    backlog_path: Path
    refill_plan: RefillPlan | None
    knowledge_command: tuple[str, ...] | None
    fog_ledger: Path | None


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
    refill = _table(document, "refill")
    knowledge = _table(document, "knowledge")

    harness_name, builder, reviewer, specialist, validator_id = _required_settings(
        run, models, validator, overrides
    )
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
    command_timeout = float(run.get("command_timeout_seconds", 600))
    if max_items < 1 or seat_timeout <= 0 or command_timeout <= 0:
        raise ConfigurationError("run bounds and timeouts must be positive")

    scratch_value = overrides.scratch_root or _optional_string(
        run.get("scratch_root"), "run.scratch_root"
    )
    scratch_root = _resolve_path(scratch_value, base) if scratch_value else None
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
    return RunSettings(
        repository=resolved_repository,
        harness=harness_name,
        harness_command=harness_command,
        builder_model=builder,
        reviewer_model=reviewer,
        specialist_model=specialist,
        validator_id=validator_id,
        validator_argv=_validator_argv(validator, overrides),
        allowed_tools=_allowed_tools(policy, overrides),
        allowed_roots=_allowed_roots(policy, overrides, base),
        max_items=max_items,
        seat_timeout_seconds=seat_timeout,
        command_timeout_seconds=command_timeout,
        scratch_root=scratch_root,
        tracker_kind=tracker_kind,
        backlog_path=backlog_path,
        refill_plan=refill_plan,
        knowledge_command=knowledge_command,
        fog_ledger=fog_ledger,
    )
