"""Port bundle injected into the application layer by the composition root."""

import time
from collections.abc import Callable
from dataclasses import dataclass

from autobuild.ports import (
    CommandPort,
    HarnessPort,
    KnowledgePort,
    LaneStatePort,
    LeasePort,
    ProgressPort,
    RunRecordPort,
    TrackerPort,
    UsageMeterPort,
    WorkspacePort,
)
from autobuild.domain import RunRecordRef, UsagePolicy


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
    # The opaque usage meter id the lane's harness adapter declares, or None for
    # an unmetered lane, and the adapter's own decision whether a meter scope
    # label covers the model a seat of a given model class runs.
    meter_id: str | None = None
    meter_scope: Callable[[str, str], bool] | None = None

    def meter_covers(self, scope: str | None, model_class: str) -> bool:
        """A scoped limit without a label covers every seat on the lane."""

        if scope is None:
            return True
        return self.meter_scope is not None and self.meter_scope(scope, model_class)


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
    # The usage meter and the profile's levels. Both are None when the profile has
    # no ``[usage]`` table, and then no meter is ever read.
    usage_meter: UsageMeterPort | None = None
    usage_policy: UsagePolicy | None = None
    # How a seat waits before it re-reads a meter that failed under the refuse
    # policy. Tests inject a recorder so no test sleeps.
    usage_retry_wait: Callable[[float], None] = time.sleep
