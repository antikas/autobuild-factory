"""Production composition root for one AutoBuild campaign."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from autobuild import __version__
from autobuild.adapters import (
    BacklogTrackerAdapter,
    GitWorkspaceAdapter,
    KoineKnowledgeAdapter,
    LocalRunRecordAdapter,
    NoRefillKnowledgeAdapter,
    PinaxTrackerAdapter,
    PosixCommandAdapter,
    WindowsCommandAdapter,
)
from autobuild.application import CampaignRunner, WorkflowPorts
from autobuild.bootstrap.builtins import register_first_party_harnesses
from autobuild.bootstrap.environment import configure_scratch_environment
from autobuild.bootstrap.profile import ConfigurationError, RunSettings
from autobuild.bootstrap.registry import AdapterRegistry, AdapterSelection
from autobuild.bootstrap.runtime import RuntimeResolver
from autobuild.domain import (
    CampaignRef,
    ItemExecutionSpec,
    PortKind,
    ProbeResult,
    RefillPlan,
    ToolPolicy,
)
from autobuild.enforcement import ApprovedValidator, PolicyConfig, PolicyGateway


def _probe(name: str, adapter: object) -> tuple[str, str, str]:
    result = adapter.probe()
    if not isinstance(result, ProbeResult):
        raise TypeError(f"{name} adapter probe did not return ProbeResult")
    if not result.available or result.identity is None:
        detail = "; ".join(result.diagnostics) or "unavailable"
        raise RuntimeError(f"{name} preflight failed: {detail}")
    return name, result.identity.name, result.identity.version


def _harness(settings: RunSettings, command: object, scratch: Path):
    registry = AdapterRegistry()
    register_first_party_harnesses(registry)
    registry.load_entry_points()
    config: dict[str, object] = {
        "command_port": command,
        "output_root": scratch / "harness",
        "model_map": {
            "builder": settings.builder_model,
            "reviewer": settings.reviewer_model,
            "specialist": settings.specialist_model,
        },
    }
    if settings.harness_command is not None:
        config["command"] = settings.harness_command
    binding = RuntimeResolver(registry).resolve(
        (PortKind.HARNESS,),
        (AdapterSelection(PortKind.HARNESS, settings.harness, config),),
        {
            PortKind.HARNESS: frozenset(
                {"fresh-seat", "cancel", "typed-result", "usage"}
            )
        },
    )
    return binding.get(PortKind.HARNESS), binding.bindings[0].identity


def _runtime(settings: RunSettings, scratch: Path):
    command = (
        WindowsCommandAdapter(scratch / "commands")
        if os.name == "nt"
        else PosixCommandAdapter(scratch / "commands")
    )
    pinax = PinaxTrackerAdapter(settings.repository)
    backlog = BacklogTrackerAdapter(settings.repository, settings.backlog_path)
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
    harness, harness_identity = _harness(settings, command, scratch)
    manifest = [
        ("harness", harness_identity.name, harness_identity.version),
        _probe("tracker", tracker),
        _probe("workspace", workspace),
        _probe("command", command),
        _probe("knowledge", knowledge),
    ]
    record_probe = LocalRunRecordAdapter(scratch / "runs")
    manifest.append(_probe("run_record", record_probe))
    records = LocalRunRecordAdapter(
        scratch / "runs", metadata=_runtime_metadata(settings, manifest)
    )
    workspace.identify(settings.repository)
    return tracker, workspace, harness, command, records, knowledge, manifest


def _runtime_metadata(
    settings: RunSettings, manifest: list[tuple[str, str, str]]
) -> dict[str, object]:
    return {
        "workflow_version": __version__,
        "adapters": [
            {"boundary": boundary, "name": name, "version": version}
            for boundary, name, version in manifest
        ],
        "harness": settings.harness,
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


def _ports(settings: RunSettings, scratch: Path):
    tracker, workspace, harness, command, records, knowledge, manifest = _runtime(
        settings, scratch
    )
    policy = PolicyGateway(
        PolicyConfig(
            allowed_roots=(settings.repository, scratch, *settings.allowed_roots),
            approved_validators=(
                ApprovedValidator(settings.validator_id, settings.validator_argv),
            ),
            allowed_tools=settings.allowed_tools
            | frozenset({Path(settings.validator_argv[0]).name}),
            max_command_timeout_seconds=settings.command_timeout_seconds,
            max_seat_timeout_seconds=settings.seat_timeout_seconds,
            allow_protected_merge=True,
        )
    )
    return (
        WorkflowPorts(
            policy.tracker(tracker),
            policy.workspace(workspace),
            policy.harness(harness),
            policy.command(command),
            policy.records(records),
            policy.knowledge(knowledge),
        ),
        manifest,
    )


def _specification(settings: RunSettings):
    def for_item(item) -> ItemExecutionSpec:
        return ItemExecutionSpec(
            item=item,
            brief_path=Path(item.brief_ref).expanduser(),
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
            seat_timeout_seconds=settings.seat_timeout_seconds,
            command_timeout_seconds=settings.command_timeout_seconds,
            max_corrections=2,
        )

    return for_item


def run_campaign(
    settings: RunSettings,
    *,
    campaign_id: str | None = None,
    allow_delivery: bool,
) -> dict[str, Any]:
    if not allow_delivery:
        raise ConfigurationError(
            "this workflow claims items and delivers accepted changes; pass --allow-delivery after human approval"
        )
    scratch = configure_scratch_environment(settings.scratch_root)
    ports, manifest = _ports(settings, scratch)
    effective_campaign_id = campaign_id or datetime.now(UTC).strftime(
        "autobuild-%Y%m%dT%H%M%SZ"
    )
    campaign = CampaignRef(
        effective_campaign_id,
        settings.repository,
        max_items=settings.max_items,
        refill_enabled=settings.refill_plan is not None,
    )
    refill = settings.refill_plan or RefillPlan()
    outcome = CampaignRunner(ports).run(campaign, _specification(settings), refill)
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
        "refill": {
            "enabled": settings.refill_plan is not None,
            "proposal_count": len(refill.proposals),
            "fog_count": len(refill.fog),
        },
        "report_ref": outcome.report_ref,
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
