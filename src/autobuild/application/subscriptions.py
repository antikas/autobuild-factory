"""Shared subscription admission for campaigns and independent read-only reviews."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace

from autobuild.domain import LaneSignal, LaneSignalKind, UsageLimitKind, UsageReading
from autobuild.ports.contracts import LastGoodReadingPort, SignInStatePort

_WITHHELD = "Local diagnostic withheld; see the repository-relative evidence reference."


def portable_text(value: str | None) -> str | None:
    """``value``, or a fixed marker for the whole of it when it contains
    path-like text."""

    # The evidence field owns file names. Free prose can come from any host
    # and can quote a path with arbitrary surrounding punctuation.
    if value is not None and ("/" in value or "\\" in value):
        return _WITHHELD
    return value


def portable_report(value):
    """``value`` with every text inside its mappings and sequences passed
    through ``portable_text``."""

    if isinstance(value, str):
        return portable_text(value)
    if isinstance(value, dict):
        return {key: portable_report(entry) for key, entry in value.items()}
    if isinstance(value, (list, tuple)):
        return [portable_report(entry) for entry in value]
    return value


def _name_stand_ins(decision, readings):
    """``decision`` with each lane judged on its last good reading saying so."""

    for row in decision["lanes"]:
        reading = readings[row["lane"]]
        if reading.ok and reading.error:
            row["reason"] += f"; {reading.error}; judged on the reading of {reading.fetched_at}"
    return decision


class SubscriptionRouting:
    def __init__(self, lanes, meter, policy, state, store, *, read_only=False):
        self.lanes = tuple(lanes)
        self.meter = meter
        self.policy = policy
        self.state = state
        self.store = store
        self.read_only = read_only

    def readings(self, skip=(), *, use_login_reading=False):
        readings = {}
        for lane in self.lanes:
            if lane.name in skip or not lane.meter_id:
                continue
            cooling = self.state.active(lane.name) if self.state is not None else None
            if cooling is not None and cooling.signature != LaneSignalKind.USAGE.value:
                continue
            try:
                reading = self._reading(lane, use_login_reading)
                # A cooling is cleared only by a reading of now, so a lane that
                # is cooling never takes the last good one.
                if cooling is None and isinstance(self.meter, LastGoodReadingPort):
                    reading = self.store.reading_or_last_good(
                        reading, self.meter.last_good(lane.meter_id), self.policy
                    )
                if cooling is not None:
                    assessment = self.policy.assess(reading)
                    if assessment.lane_stops:
                        # The allocator reports the provider window and rejudges its validity.
                        readings[lane.name] = reading
                        continue
                    if (
                        assessment.failure
                        or assessment.scoped_stops
                        or (not self.read_only and not self.state.clear(lane.name, cooling))
                    ):
                        continue
                readings[lane.name] = reading
            except Exception as exc:
                readings[lane.name] = UsageReading.failed(
                    lane.meter_id, type(exc).__name__
                )
        return readings

    def _reading(self, lane, use_login_reading):
        if isinstance(self.meter, SignInStatePort):
            expired = self.meter.sign_in_failure(lane.meter_id)
            if expired is not None:
                return expired
        initial = getattr(lane.harness, "login_reading", None) if use_login_reading else None
        return initial if initial is not None else self.meter.read(lane.meter_id)

    def for_role(self, readings, role):
        by_role = {}
        for lane in self.lanes:
            if lane.name not in readings:
                continue
            reading = readings[lane.name]
            # Unknown scopes remain binding. Only the provider can determine
            # that a named scope definitely does not cover the configured model.
            limits = tuple(
                w
                for w in reading.limits
                if w.kind is not UsageLimitKind.WEEKLY_SCOPED
                or not w.scope
                or lane.meter_scope is None
                or lane.meter_scope(w.scope, role)
            )
            by_role[lane.name] = replace(reading, limits=limits)
        return by_role

    def preview(self, readings, role):
        judged = self.for_role(readings, role)
        return _name_stand_ins(self.store.preview(judged, self.policy, role), judged)

    @contextmanager
    def reserve(self, role, timeout, skip=(), on_decision=None):
        readings = self.for_role(self.readings(skip), role)
        lease, decision = self.store.reserve_with_decision(readings, self.policy, role, timeout)
        decision = _name_stand_ins(decision, readings)
        lane = next(entry for entry in self.lanes if entry.name == lease.lane) if lease else None
        try:
            if self.state is not None:
                for row in decision["lanes"]:
                    reset = row.get("usage_reset_at")
                    if reset is not None:
                        self.state.cool(
                            row["lane"], LaneSignal(LaneSignalKind.USAGE, reset_at=reset),
                            "subscription-admission", only_if_clear=True,
                        )
            if on_decision is not None:
                on_decision(decision)
            yield lane
        finally:
            if lease is not None:
                try:
                    post = self.meter.read(lane.meter_id)
                except Exception:
                    post = None
                self.store.release(lease, post)
