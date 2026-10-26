"""Port bundle injected into the application layer by the composition root."""

import time
from collections.abc import Callable
from dataclasses import dataclass
from threading import Event

from autobuild.ports import (
    CommandPort,
    HarnessPort,
    HostResourcePort,
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


def _no_item_environment(item_id: str) -> tuple[tuple[str, str], ...]:
    return ()


def _wait_for_event(event: Event, seconds: float) -> bool:
    return event.wait(seconds)


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
    # The monotonic clock, in seconds, that ages a lane's shared meter reading.
    # Tests inject a fake clock.
    usage_clock: Callable[[], float] = time.monotonic
    # The environment one item's validator command runs with, so concurrent
    # items never share temporary directories. The production composition maps
    # each item to its own directories under the scratch root; the default adds
    # nothing.
    item_environment: Callable[[str], tuple[tuple[str, str], ...]] = _no_item_environment
    # The monotonic clock, in seconds, that times each item and the campaign's
    # makespan, and the wait the tracker lease heartbeat sleeps on between
    # renewals: it returns True once the event is set (the campaign is
    # ending) and False after ``seconds``. Tests inject a fake clock for both.
    schedule_clock: Callable[[], float] = time.monotonic
    heartbeat_wait: Callable[[Event, float], bool] = _wait_for_event
    # The host resource readings a campaign above one item at a time watches
    # for system-wide pressure, or None to run without the pressure monitor.
    # The monitor times its samples with ``schedule_clock`` and sleeps between
    # them on ``pressure_wait``, which behaves as ``heartbeat_wait`` does.
    host: HostResourcePort | None = None
    pressure_wait: Callable[[Event, float], bool] = _wait_for_event
    # The wait a hold on admissions sleeps on until its next probe, timed with
    # ``schedule_clock``; it behaves as ``heartbeat_wait`` does.
    hold_wait: Callable[[Event, float], bool] = _wait_for_event
