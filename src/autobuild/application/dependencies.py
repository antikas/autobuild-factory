"""Port bundle injected into the application layer by the composition root."""

from dataclasses import dataclass, field

from autobuild.ports import (
    CommandPort,
    HarnessPort,
    KnowledgePort,
    LaneStatePort,
    LeasePort,
    ProgressPort,
    RunRecordPort,
    TrackerPort,
    WorkspacePort,
)
from autobuild.domain import RunRecordRef


class _NoOpProgressPort:
    """The default progress port: it records the run and drops every line, so a
    construction site that does not wire a real progress stream keeps working."""

    def begin(self, record: RunRecordRef) -> None:
        return None

    def emit(self, line: str) -> None:
        return None


_NO_OP_PROGRESS: ProgressPort = _NoOpProgressPort()


@dataclass(frozen=True, slots=True)
class Lane:
    """One harness lane: an opaque name and the harness that serves it.

    The name is a configuration label the application records on tracker and run
    events; it names no provider to the application layer, which treats it as an
    ordered, comparable string."""

    name: str
    harness: HarnessPort


@dataclass(frozen=True, slots=True)
class WorkflowPorts:
    tracker: TrackerPort
    workspace: WorkspacePort
    harness: HarnessPort
    command: CommandPort
    records: RunRecordPort
    knowledge: KnowledgePort
    # The owner-facing progress stream. It defaults to a no-op port so every
    # existing construction site keeps working; the production composition supplies
    # the real composite that fans lines out to the file, stderr and command sinks.
    progress: ProgressPort = _NO_OP_PROGRESS
    # The single-writer lease mechanism. It is optional so a caller that does not
    # guard a surface (for example a narrow harness proof) can omit it; the
    # production composition always supplies one.
    lease: LeasePort | None = None
    # The ordered harness lanes in preference order and the machine-local lane
    # cooling state. When ``lanes`` is empty the single ``harness`` above is the
    # only lane and no failover is possible; the production composition always
    # supplies at least one named lane.
    lanes: tuple[Lane, ...] = ()
    lane_state: LaneStatePort | None = None
