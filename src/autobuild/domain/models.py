"""Immutable semantic requests, results, and evidence for AutoBuild."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class PortKind(str, Enum):
    TRACKER = "tracker"
    WORKSPACE = "workspace"
    HARNESS = "harness"
    COMMAND = "command"
    RUN_RECORD = "run_record"
    KNOWLEDGE = "knowledge"


class Seat(str, Enum):
    BUILDER = "builder"
    REVIEWER = "reviewer"
    SPECIALIST = "specialist"


class SeatOutcome(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    UNAVAILABLE = "unavailable"


class ReviewDecision(str, Enum):
    PASS = "pass"
    CORRECT = "correct"
    ESCALATE = "escalate"
    PARK = "park"


class ChangeKind(str, Enum):
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    SYMLINK = "symlink"


class ItemState(str, Enum):
    READY = "ready"
    VERIFIED = "verified"
    CLAIMED = "claimed"
    ISOLATED = "isolated"
    BUILT = "built"
    VALIDATED = "validated"
    REVIEWED = "reviewed"
    CORRECTING = "correcting"
    ESCALATED = "escalated"
    FINALISED = "finalised"
    PARKED = "parked"
    RELEASED = "released"


class ItemDisposition(str, Enum):
    ACCEPTED = "accepted"
    PARKED = "parked"
    FAILED = "failed"


class CampaignStopReason(str, Enum):
    QUEUE_DRY = "queue_dry"
    ITEM_BOUND = "item_bound"
    STRUCTURAL_FAILURE = "structural_failure"


@dataclass(frozen=True, slots=True)
class AdapterIdentity:
    name: str
    version: str
    capabilities: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("adapter name must not be empty")
        if not self.version.strip():
            raise ValueError("adapter version must not be empty")


@dataclass(frozen=True, slots=True)
class ProbeResult:
    available: bool
    identity: AdapterIdentity | None = None
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.available and self.identity is None:
            raise ValueError("an available adapter must provide its identity")
        if not self.available and self.identity is not None:
            raise ValueError("an unavailable adapter must not provide an identity")

    @classmethod
    def ready(cls, identity: AdapterIdentity, *diagnostics: str) -> ProbeResult:
        return cls(True, identity, tuple(diagnostics))

    @classmethod
    def unavailable(cls, *diagnostics: str) -> ProbeResult:
        return cls(False, None, tuple(diagnostics))


@dataclass(frozen=True, slots=True)
class CampaignRef:
    campaign_id: str
    repository: Path
    max_items: int = 1
    refill_enabled: bool = False

    def __post_init__(self) -> None:
        if not self.campaign_id.strip():
            raise ValueError("campaign_id must not be empty")
        if self.max_items < 1:
            raise ValueError("max_items must be positive")


@dataclass(frozen=True, slots=True)
class WorkItem:
    item_id: str
    title: str
    brief_ref: str
    acceptance: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.item_id.strip() or not self.title.strip():
            raise ValueError("work item id and title must not be empty")
        if not self.brief_ref.strip():
            raise ValueError("work item brief_ref must not be empty")
        if not self.acceptance:
            raise ValueError("work item must declare measurable acceptance")


@dataclass(frozen=True, slots=True)
class ClaimReceipt:
    item_id: str
    actor: str
    claimed_at: str


@dataclass(frozen=True, slots=True)
class RepositoryIdentity:
    root: Path
    default_branch: str
    remote: str
    revision: str


@dataclass(frozen=True, slots=True)
class WorkspaceRef:
    root: Path
    branch: str
    start_commit: str
    lease_id: str


@dataclass(frozen=True, slots=True)
class ChangedPath:
    path: Path
    kind: ChangeKind
    digest: str | None = None


@dataclass(frozen=True, slots=True)
class DiffEvidence:
    workspace: WorkspaceRef
    workspace_revision: str
    changed_paths: tuple[ChangedPath, ...]
    patch_ref: str


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    allowed_tools: frozenset[str]
    allowed_roots: tuple[Path, ...]
    allow_destructive: bool = False
    allow_publication: bool = False
    allow_protected_merge: bool = False


@dataclass(frozen=True, slots=True)
class ItemExecutionSpec:
    item: WorkItem
    brief_path: Path
    validator_id: str
    validator_argv: tuple[str, ...]
    tool_policy: ToolPolicy
    builder_model_class: str
    reviewer_model_class: str
    specialist_model_class: str
    seat_timeout_seconds: float = 900.0
    command_timeout_seconds: float = 600.0
    max_corrections: int = 2
    reviewer_tool_policy: ToolPolicy | None = None

    def __post_init__(self) -> None:
        if not self.validator_id.strip() or not self.validator_argv:
            raise ValueError("item execution requires an approved validator")
        if self.seat_timeout_seconds <= 0 or self.command_timeout_seconds <= 0:
            raise ValueError("execution timeouts must be positive")
        if self.max_corrections < 0:
            raise ValueError("max_corrections must not be negative")


@dataclass(frozen=True, slots=True)
class SeatRequest:
    run_id: str
    item_id: str
    seat: Seat
    model_class: str
    brief_path: Path
    workspace: WorkspaceRef
    tool_policy: ToolPolicy
    instructions: str
    result_contract: str
    timeout_seconds: float
    context_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("seat timeout must be positive")
        if not self.instructions.strip():
            raise ValueError("seat instructions must not be empty")


@dataclass(frozen=True, slots=True)
class SeatUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost: float | None = None
    source: str = "unavailable"


@dataclass(frozen=True, slots=True)
class BuilderReport:
    report_ref: str
    summary: str


@dataclass(frozen=True, slots=True)
class SeatResult:
    run_ref: str
    outcome: SeatOutcome
    payload: BuilderReport | ReviewVerdict | None
    raw_output_ref: str
    usage: SeatUsage
    started_at: str
    ended_at: str
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CommandRequest:
    command_id: str
    argv: tuple[str, ...]
    cwd: Path
    environment: tuple[tuple[str, str], ...] = ()
    timeout_seconds: float = 600.0
    shell: Path | None = None

    def __post_init__(self) -> None:
        if not self.command_id.strip() or not self.argv:
            raise ValueError("command id and argv must not be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("command timeout must be positive")


@dataclass(frozen=True, slots=True)
class CommandResult:
    command_id: str
    exit_code: int | None
    stdout_ref: str
    stderr_ref: str
    started_at: str
    ended_at: str
    timed_out: bool = False
    cancelled: bool = False


@dataclass(frozen=True, slots=True)
class ValidationEvidence:
    validator_id: str
    workspace_revision: str
    command: CommandResult
    changed_paths: tuple[ChangedPath, ...]


@dataclass(frozen=True, slots=True)
class ReviewFinding:
    code: str
    consequence: str
    evidence_ref: str
    specialist_boundary: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewVerdict:
    item_id: str
    decision: ReviewDecision
    findings: tuple[ReviewFinding, ...]
    evidence_ref: str


@dataclass(frozen=True, slots=True)
class CloseEvidence:
    item_id: str
    workspace_revision: str
    diff: DiffEvidence
    validation: ValidationEvidence
    verdict: ReviewVerdict
    trajectory_ref: str


@dataclass(frozen=True, slots=True)
class FinaliseRequest:
    item_id: str
    evidence: CloseEvidence
    commit_message: str


@dataclass(frozen=True, slots=True)
class DeliveryRequest:
    item_id: str
    item_commit: str | None
    tracker_commit: str
    merge_to_default: bool = True


@dataclass(frozen=True, slots=True)
class FinaliseResult:
    item_commit: str | None
    tracker_commit: str | None
    merged_commit: str | None
    pushed: bool
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Proposal:
    title: str
    question: str
    rationale: str
    brief_ref: str

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (self.title, self.question, self.rationale, self.brief_ref)
        ):
            raise ValueError("proposal fields must not be empty")


@dataclass(frozen=True, slots=True)
class ProposalRef:
    proposal_id: str
    runnable: bool = False

    def __post_init__(self) -> None:
        if self.runnable:
            raise ValueError("workflow-created proposals must remain non-runnable")


@dataclass(frozen=True, slots=True)
class FogRecord:
    direction: str
    blocking_question: str
    surface_when: str

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (self.direction, self.blocking_question, self.surface_when)
        ):
            raise ValueError("fog fields must not be empty")


@dataclass(frozen=True, slots=True)
class DurableContext:
    query: str
    references: tuple[str, ...]
    excerpts: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RunRecordRef:
    run_id: str
    root: Path


@dataclass(frozen=True, slots=True)
class RunEvent:
    event_type: str
    occurred_at: str
    item_id: str | None = None
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RefillPlan:
    proposals: tuple[Proposal, ...] = ()
    fog: tuple[FogRecord, ...] = ()


@dataclass(frozen=True, slots=True)
class ItemOutcome:
    item_id: str
    disposition: ItemDisposition
    states: tuple[ItemState, ...]
    reason: str | None = None
    finalise: FinaliseResult | None = None
    structural_failure: bool = False


@dataclass(frozen=True, slots=True)
class CampaignOutcome:
    campaign_id: str
    items: tuple[ItemOutcome, ...]
    stop_reason: CampaignStopReason
    report_ref: str
