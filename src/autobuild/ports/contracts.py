"""Protocols express intent; adapters own every execution mechanism."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from autobuild.domain import (
    CampaignRef,
    ClaimReceipt,
    CloseEvidence,
    CommandRequest,
    CommandResult,
    DeliveryRequest,
    DiffEvidence,
    DurableContext,
    FinaliseRequest,
    FinaliseResult,
    FogRecord,
    ProbeResult,
    Proposal,
    ProposalRef,
    RepositoryIdentity,
    RunEvent,
    RunRecordRef,
    SeatRequest,
    SeatResult,
    SeatUsage,
    WorkItem,
    WorkspaceRef,
)


class Probeable(Protocol):
    def probe(self) -> ProbeResult: ...


@runtime_checkable
class TrackerPort(Probeable, Protocol):
    def next_item(self, campaign: CampaignRef) -> WorkItem | None: ...
    def claim(self, item: WorkItem, actor: str) -> ClaimReceipt: ...
    def close(
        self, evidence: CloseEvidence, item_commit: str, workspace: WorkspaceRef, actor: str
    ) -> None: ...
    def park(
        self, item_id: str, reason: str, actor: str, workspace: WorkspaceRef | None = None
    ) -> None: ...
    def propose(self, proposal: Proposal, actor: str) -> ProposalRef: ...


@runtime_checkable
class WorkspacePort(Probeable, Protocol):
    def identify(self, root: Path) -> RepositoryIdentity: ...
    def create_isolated(self, campaign: CampaignRef, item: WorkItem) -> WorkspaceRef: ...
    def diff(self, workspace: WorkspaceRef) -> DiffEvidence: ...
    def commit_item(self, workspace: WorkspaceRef, request: FinaliseRequest) -> str: ...
    def commit_tracker(
        self, workspace: WorkspaceRef, item_id: str, item_commit: str | None
    ) -> str: ...
    def deliver(self, workspace: WorkspaceRef, request: DeliveryRequest) -> FinaliseResult: ...
    def release(self, workspace: WorkspaceRef) -> None: ...


@runtime_checkable
class HarnessPort(Probeable, Protocol):
    def invoke(self, request: SeatRequest) -> SeatResult: ...
    def cancel(self, run_ref: str) -> None: ...
    def collect_usage(self, run_ref: str) -> SeatUsage: ...


@runtime_checkable
class CommandPort(Probeable, Protocol):
    def run(self, request: CommandRequest) -> CommandResult: ...
    def cancel(self, command_id: str) -> None: ...


@runtime_checkable
class RunRecordPort(Probeable, Protocol):
    def create(self, campaign: CampaignRef) -> RunRecordRef: ...
    def append(self, record: RunRecordRef, event: RunEvent) -> str: ...
    def write_evidence(self, record: RunRecordRef, name: str, content: str) -> str: ...
    def complete(self, record: RunRecordRef, summary: str) -> str: ...


@runtime_checkable
class KnowledgePort(Probeable, Protocol):
    def retrieve(self, query: str) -> DurableContext: ...
    def record_fog(self, fog: FogRecord) -> str: ...
