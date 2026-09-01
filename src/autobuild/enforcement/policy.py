"""Deterministic policy wrappers around semantic ports.

The application chooses what should happen. These wrappers decide whether the
request is allowed and whether the evidence is internally consistent. Wrapped
adapters remain responsible for how the operation is performed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from autobuild.domain import (
    CampaignRef,
    CloseEvidence,
    CommandRequest,
    DeliveryMode,
    DeliveryRequest,
    EvidenceError,
    FinaliseRequest,
    PolicyViolation,
    Proposal,
    ReviewDecision,
    Seat,
    RunEvent,
    RunRecordRef,
    SeatRequest,
    WorkspaceRef,
)
from autobuild.ports import (
    CommandPort,
    HarnessPort,
    KnowledgePort,
    RunRecordPort,
    TrackerPort,
    WorkspacePort,
)


@dataclass(frozen=True, slots=True)
class ApprovedValidator:
    validator_id: str
    argv: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.validator_id.strip() or not self.argv:
            raise ValueError("approved validators require an id and argv")


@dataclass(frozen=True, slots=True)
class PolicyConfig:
    allowed_roots: tuple[Path, ...]
    approved_validators: tuple[ApprovedValidator, ...]
    allowed_tools: frozenset[str]
    max_command_timeout_seconds: float = 600.0
    max_seat_timeout_seconds: float = 900.0
    allow_destructive: bool = False
    allow_publication: bool = False
    allow_repository_mutation: bool = False
    allow_protected_merge: bool = False

    def __post_init__(self) -> None:
        if not self.allowed_roots:
            raise ValueError("policy requires at least one allowed root")
        if self.max_command_timeout_seconds <= 0 or self.max_seat_timeout_seconds <= 0:
            raise ValueError("policy timeouts must be positive")
        ids = [validator.validator_id for validator in self.approved_validators]
        if len(ids) != len(set(ids)):
            raise ValueError("approved validator ids must be unique")


def _is_within(path: Path, roots: tuple[Path, ...]) -> bool:
    resolved = path.resolve(strict=False)
    return any(
        resolved == root.resolve(strict=False)
        or root.resolve(strict=False) in resolved.parents
        for root in roots
    )


def _require_path(path: Path, config: PolicyConfig, label: str) -> None:
    if not _is_within(path, config.allowed_roots):
        raise PolicyViolation(f"{label} is outside the allowed roots: {path}")


def _require_workspace(workspace: WorkspaceRef, config: PolicyConfig) -> None:
    _require_path(workspace.root, config, "workspace")
    if not workspace.lease_id.strip() or not workspace.start_commit.strip():
        raise EvidenceError("workspace identity is incomplete")


def _require_close_evidence(
    evidence: CloseEvidence, workspace: WorkspaceRef, config: PolicyConfig
) -> None:
    if evidence.item_id != evidence.verdict.item_id:
        raise EvidenceError("close item and verdict item do not match")
    if evidence.verdict.decision is not ReviewDecision.PASS:
        raise EvidenceError("only a passing review can close an item")
    if evidence.diff.workspace != workspace:
        raise EvidenceError("close diff belongs to a different workspace")
    revisions = {
        evidence.workspace_revision,
        evidence.diff.workspace_revision,
        evidence.validation.workspace_revision,
    }
    if len(revisions) != 1:
        raise EvidenceError("validation or diff evidence is stale")
    if evidence.validation.changed_paths != evidence.diff.changed_paths:
        raise EvidenceError("validated paths do not match the closing diff")
    validator_ids = {validator.validator_id for validator in config.approved_validators}
    if evidence.validation.validator_id not in validator_ids:
        raise EvidenceError("closing validator is not approved")
    command = evidence.validation.command
    if not command.command_id.endswith(f":{evidence.validation.validator_id}"):
        raise EvidenceError("closing command does not identify the validator")
    if command.exit_code != 0 or command.timed_out or command.cancelled:
        raise EvidenceError("closing validator did not pass")
    if not command.stdout_ref.strip() or not command.stderr_ref.strip():
        raise EvidenceError("closing validator output references are incomplete")
    if not evidence.diff.patch_ref.strip() or not evidence.verdict.evidence_ref.strip():
        raise EvidenceError("diff or review evidence reference is missing")
    if not evidence.trajectory_ref.strip():
        raise EvidenceError("item trajectory is missing")
    for entry in evidence.diff.changed_paths:
        if entry.path.is_absolute() or not _is_within(workspace.root / entry.path, (workspace.root,)):
            raise EvidenceError(f"changed path escapes the workspace: {entry.path}")


class EnforcedCommandPort:
    def __init__(self, port: CommandPort, config: PolicyConfig) -> None:
        self._port = port
        self._config = config

    def probe(self):
        return self._port.probe()

    def run(self, request: CommandRequest):
        _require_path(request.cwd, self._config, "command cwd")
        if request.timeout_seconds > self._config.max_command_timeout_seconds:
            raise PolicyViolation("command timeout exceeds the policy ceiling")
        matches = [
            validator
            for validator in self._config.approved_validators
            if request.command_id.endswith(f":{validator.validator_id}")
        ]
        if len(matches) != 1:
            raise PolicyViolation("command does not identify one approved validator")
        if request.argv != matches[0].argv:
            raise PolicyViolation("validator argv differs from the approved item")
        executable = Path(request.argv[0]).name.casefold()
        allowed = {Path(tool).name.casefold() for tool in self._config.allowed_tools}
        if executable not in allowed:
            raise PolicyViolation(f"validator executable is not allowed: {request.argv[0]}")
        if request.shell is not None:
            shell = Path(request.shell).name.casefold()
            if shell not in allowed:
                raise PolicyViolation(f"requested shell is not allowed: {request.shell}")
        return self._port.run(request)

    def cancel(self, command_id: str) -> None:
        self._port.cancel(command_id)


class EnforcedHarnessPort:
    def __init__(self, port: HarnessPort, config: PolicyConfig) -> None:
        self._port = port
        self._config = config

    def probe(self):
        return self._port.probe()

    def invoke(self, request: SeatRequest):
        _require_workspace(request.workspace, self._config)
        _require_path(request.brief_path, self._config, "seat brief")
        if request.timeout_seconds > self._config.max_seat_timeout_seconds:
            raise PolicyViolation("seat timeout exceeds the policy ceiling")
        if not request.tool_policy.allowed_tools <= self._config.allowed_tools:
            raise PolicyViolation("seat requested an undeclared tool")
        if request.seat in {Seat.REVIEWER, Seat.SPECIALIST} and request.tool_policy.allowed_tools & {
            "write",
            "shell",
            "python",
            "git",
        }:
            raise PolicyViolation("review seats must remain read-only")
        for root in request.tool_policy.allowed_roots:
            if not _is_within(
                root, (request.workspace.root, *self._config.allowed_roots)
            ):
                raise PolicyViolation(f"seat tool root escapes the workspace: {root}")
        for reference in request.context_refs:
            path = Path(reference).expanduser()
            if path.is_absolute():
                _require_path(path, self._config, "seat evidence")
        if request.tool_policy.allow_destructive and not self._config.allow_destructive:
            raise PolicyViolation("destructive tool access has no human gate")
        if request.tool_policy.allow_publication and not self._config.allow_publication:
            raise PolicyViolation("publication access has no human gate")
        if request.tool_policy.allow_protected_merge and not self._config.allow_protected_merge:
            raise PolicyViolation("protected merge access has no human gate")
        return self._port.invoke(request)

    def cancel(self, run_ref: str) -> None:
        self._port.cancel(run_ref)

    def collect_usage(self, run_ref: str):
        return self._port.collect_usage(run_ref)


class EnforcedWorkspacePort:
    def __init__(self, port: WorkspacePort, config: PolicyConfig) -> None:
        self._port = port
        self._config = config

    def probe(self):
        return self._port.probe()

    def identify(self, root: Path):
        _require_path(root, self._config, "repository")
        result = self._port.identify(root)
        _require_path(result.root, self._config, "resolved repository")
        return result

    def create_isolated(self, campaign: CampaignRef, item):
        _require_path(campaign.repository, self._config, "campaign repository")
        workspace = self._port.create_isolated(campaign, item)
        _require_workspace(workspace, self._config)
        return workspace

    def diff(self, workspace: WorkspaceRef):
        _require_workspace(workspace, self._config)
        result = self._port.diff(workspace)
        if result.workspace != workspace:
            raise EvidenceError("diff adapter returned a different workspace")
        return result

    def commit_item(self, workspace: WorkspaceRef, request: FinaliseRequest) -> str:
        _require_workspace(workspace, self._config)
        if request.item_id != request.evidence.item_id:
            raise EvidenceError("commit item and close evidence do not match")
        if not request.commit_message.strip():
            raise EvidenceError("item commit message must not be empty")
        _require_close_evidence(request.evidence, workspace, self._config)
        return self._port.commit_item(workspace, request)

    def commit_tracker(
        self, workspace: WorkspaceRef, item_id: str, item_commit: str | None
    ) -> str:
        _require_workspace(workspace, self._config)
        if not item_id.strip():
            raise EvidenceError("tracker commit requires an item id")
        if item_commit is not None and not item_commit.strip():
            raise EvidenceError("item commit reference is empty")
        return self._port.commit_tracker(workspace, item_id, item_commit)

    def deliver(self, workspace: WorkspaceRef, request: DeliveryRequest):
        _require_workspace(workspace, self._config)
        if not self._config.allow_repository_mutation:
            raise PolicyViolation("repository delivery has no human gate")
        if (
            request.mode is DeliveryMode.PROTECTED_DEFAULT
            and not self._config.allow_protected_merge
        ):
            raise PolicyViolation("protected-branch delivery has no human gate")
        if not request.tracker_commit.strip():
            raise EvidenceError("delivery requires a tracker commit")
        return self._port.deliver(workspace, request)

    def release(self, workspace: WorkspaceRef) -> None:
        _require_workspace(workspace, self._config)
        self._port.release(workspace)


class EnforcedTrackerPort:
    def __init__(self, port: TrackerPort, config: PolicyConfig) -> None:
        self._port = port
        self._config = config

    def probe(self):
        return self._port.probe()

    def next_item(self, campaign: CampaignRef):
        _require_path(campaign.repository, self._config, "campaign repository")
        return self._port.next_item(campaign)

    def claim(self, item, actor: str):
        if not actor.strip():
            raise PolicyViolation("claim actor must not be empty")
        if not self._config.allow_repository_mutation:
            raise PolicyViolation("durable tracker claim has no repository-mutation gate")
        return self._port.claim(item, actor)

    def close(
        self,
        evidence: CloseEvidence,
        item_commit: str,
        workspace: WorkspaceRef,
        actor: str,
    ) -> None:
        _require_workspace(workspace, self._config)
        _require_close_evidence(evidence, workspace, self._config)
        if not item_commit.strip() or not actor.strip():
            raise EvidenceError("close requires item commit and actor")
        self._port.close(evidence, item_commit, workspace, actor)

    def park(
        self, item_id: str, reason: str, actor: str, workspace: WorkspaceRef | None = None
    ) -> None:
        if not item_id.strip() or not reason.strip() or not actor.strip():
            raise EvidenceError("park requires item, concrete reason, and actor")
        if workspace is not None:
            _require_workspace(workspace, self._config)
        elif not self._config.allow_repository_mutation:
            raise PolicyViolation("primary tracker park has no repository-mutation gate")
        self._port.park(item_id, reason, actor, workspace)

    def propose(self, proposal: Proposal, actor: str):
        if not actor.strip():
            raise PolicyViolation("proposal actor must not be empty")
        if not self._config.allow_repository_mutation:
            raise PolicyViolation("durable tracker proposal has no repository-mutation gate")
        result = self._port.propose(proposal, actor)
        if result.runnable:
            raise PolicyViolation("workflow proposals must remain non-runnable")
        return result


class EnforcedRunRecordPort:
    def __init__(self, port: RunRecordPort, config: PolicyConfig) -> None:
        self._port = port
        self._config = config

    def probe(self):
        return self._port.probe()

    def create(self, campaign: CampaignRef):
        _require_path(campaign.repository, self._config, "campaign repository")
        record = self._port.create(campaign)
        _require_path(record.root, self._config, "run record")
        return record

    def append(self, record: RunRecordRef, event: RunEvent):
        _require_path(record.root, self._config, "run record")
        return self._port.append(record, event)

    def write_evidence(self, record: RunRecordRef, name: str, content: str):
        _require_path(record.root, self._config, "run record")
        if not name.strip():
            raise EvidenceError("evidence name must not be empty")
        return self._port.write_evidence(record, name, content)

    def complete(self, record: RunRecordRef, summary: str):
        _require_path(record.root, self._config, "run record")
        return self._port.complete(record, summary)


class EnforcedKnowledgePort:
    def __init__(self, port: KnowledgePort) -> None:
        self._port = port

    def probe(self):
        return self._port.probe()

    def retrieve(self, query: str):
        return self._port.retrieve(query)

    def record_fog(self, fog):
        return self._port.record_fog(fog)


class PolicyGateway:
    """Construct policy-wrapped ports without importing application sequencing."""

    def __init__(self, config: PolicyConfig) -> None:
        self.config = config

    def tracker(self, port: TrackerPort) -> EnforcedTrackerPort:
        return EnforcedTrackerPort(port, self.config)

    def workspace(self, port: WorkspacePort) -> EnforcedWorkspacePort:
        return EnforcedWorkspacePort(port, self.config)

    def harness(self, port: HarnessPort) -> EnforcedHarnessPort:
        return EnforcedHarnessPort(port, self.config)

    def command(self, port: CommandPort) -> EnforcedCommandPort:
        return EnforcedCommandPort(port, self.config)

    def records(self, port: RunRecordPort) -> EnforcedRunRecordPort:
        return EnforcedRunRecordPort(port, self.config)

    def knowledge(self, port: KnowledgePort) -> EnforcedKnowledgePort:
        return EnforcedKnowledgePort(port)
