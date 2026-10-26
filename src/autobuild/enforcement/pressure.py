"""The host pressure rule: levels, thresholds and the effective item cap."""

from __future__ import annotations

from dataclasses import replace

from autobuild.domain.models import (
    GIB,
    HostReading,
    PressureLevel,
    PressurePolicy,
    PressureState,
)

_PRESSURED = frozenset({PressureLevel.HIGH, PressureLevel.CRITICAL})
# A seat's cap grows by every second it runs at ``high`` or ``critical``, up to
# this multiple of its class cap.
SEAT_STRETCH_FACTOR = 2.0
# A validator that starts at ``high`` or ``critical``, or within this many
# seconds after, runs in its patient form.
PATIENT_LOOKBACK_SECONDS = 60.0


def stretched_seat_cap(class_cap: float, pressured_seconds: float) -> float:
    """The seat's effective cap after ``pressured_seconds`` of its run at
    ``high`` or ``critical``: the class cap plus that time, at most
    ``SEAT_STRETCH_FACTOR`` times the class cap."""

    return min(class_cap + max(0.0, pressured_seconds), class_cap * SEAT_STRETCH_FACTOR)


def memory_lines(total_bytes: int, policy: PressurePolicy) -> tuple[float, float]:
    """The high and critical available-memory lines in bytes: the larger of a
    share of total memory and a floor."""

    high = max(total_bytes * policy.high_memory_percent / 100, policy.high_memory_gib * GIB)
    critical = max(
        total_bytes * policy.critical_memory_percent / 100, policy.critical_memory_gib * GIB
    )
    return high, critical


def initial_pressure(capacity: int) -> PressureState:
    """The state a campaign starts in: ``normal`` at its full capacity."""

    if capacity < 1:
        raise ValueError("capacity must be positive")
    return PressureState(PressureLevel.NORMAL, capacity, capacity)


def _inside(samples: tuple[tuple[float, float], ...], start: float) -> list[float]:
    return [busy for at, busy in samples if at >= start]


def assess_pressure(
    state: PressureState, now: float, reading: HostReading, policy: PressurePolicy
) -> PressureState:
    """The single owner of the pressure rule. Pure: the state after one reading
    taken at ``now``.

    Each window is inclusive: the window at t holds the samples timed in
    [t - window, t]. ``high`` is a mean CPU busy of at least the high share
    over a full window, available memory below the high memory line, or commit
    headroom below its share of the commit limit. ``critical`` is available
    memory below the critical line. ``normal`` needs every CPU sample of the
    calm window below the calm share and available memory at least the calm
    margin above the high line. Leaving ``high`` or ``critical`` without
    meeting ``normal`` gives ``recovering``.

    The cap falls by one for each further ``decrease_seconds`` spent at
    ``high`` or ``critical``, down to 1, and rises by one for each
    ``increase_seconds`` spent at ``normal``, up to the capacity. A reading
    without CPU busy or available memory leaves the state unchanged; the caller
    reports it."""

    busy = reading.cpu_busy_percent
    available = reading.memory_available_bytes
    total = reading.memory_total_bytes
    if busy is None or available is None or total is None:
        return state
    keep_from = now - max(policy.window_seconds, policy.calm_seconds)
    samples = tuple(
        sample for sample in (*state.samples, (now, float(busy))) if sample[0] >= keep_from
    )
    first_at = now if state.first_at is None else state.first_at
    window = _inside(samples, now - policy.window_seconds)
    cpu_mean = sum(window) / len(window)
    window_full = now - first_at >= policy.window_seconds
    high_line, critical_line = memory_lines(total, policy)
    commit_short = (
        reading.commit_available_bytes is not None
        and reading.commit_total_bytes is not None
        and reading.commit_available_bytes
        < reading.commit_total_bytes * policy.high_commit_percent / 100
    )
    high = (
        (window_full and cpu_mean >= policy.high_cpu_percent)
        or available < high_line
        or commit_short
    )
    calm = all(
        value < policy.calm_cpu_percent for value in _inside(samples, now - policy.calm_seconds)
    ) and available >= high_line + policy.calm_memory_gib * GIB

    previous = state.level
    if available < critical_line:
        level = PressureLevel.CRITICAL
    elif high:
        level = PressureLevel.HIGH
    elif previous is PressureLevel.NORMAL or calm:
        level = PressureLevel.NORMAL
    else:
        level = PressureLevel.RECOVERING

    cap, cap_since = state.cap, state.cap_since
    if level in _PRESSURED:
        if previous not in _PRESSURED:
            cap_since = now
        elif now - cap_since >= policy.decrease_seconds and cap > 1:
            cap, cap_since = cap - 1, now
    elif level is PressureLevel.NORMAL:
        if previous is not PressureLevel.NORMAL:
            cap_since = now
        elif cap < state.capacity and now - cap_since >= policy.increase_seconds:
            cap, cap_since = cap + 1, now

    return replace(
        state,
        level=level,
        cap=cap,
        samples=samples,
        first_at=first_at,
        cap_since=cap_since,
        episode=state.episode + (level is not previous),
        cpu_mean_percent=cpu_mean,
        memory_available_bytes=available,
        high_line_bytes=high_line,
    )
