"""Assess the subscription windows that apply to one capability and seat.

The Claude subscription meter uses an undocumented endpoint that may change
without notice. A missing or invalid reading excludes that provider.
"""

from __future__ import annotations

import math
from datetime import datetime, UTC
from autobuild.domain import UsageLimitKind, UsagePolicy, UsageReading


READING_MAX_AGE = 90
# Leave space for foreground work and in-flight turns even near reset. A
# provider can report rounded percentages; a single reported point is material.
MINIMUM_RESERVE = 1.0
WINDOW_HOURS = {
    UsageLimitKind.SESSION: 5,
    UsageLimitKind.WEEKLY_ALL: 168,
    UsageLimitKind.WEEKLY_SCOPED: 168,
}


def instant(text):
    value = datetime.fromisoformat(text)
    if value.tzinfo is None:
        raise ValueError("timestamp has no UTC offset")
    return value.astimezone(UTC)


def same_window(first, second):
    # Provider reset timestamps can vary by fractions of a second between reads.
    return abs((instant(first) - instant(second)).total_seconds()) < 60


def window_key(limit):
    return f"{limit.kind.value}:{limit.scope or ''}"


def assess_lane(
    lane: str,
    reading: UsageReading,
    policy: UsagePolicy,
    now: datetime,
    reservations: dict,
    estimates: dict,
) -> dict:
    """Return headroom after pending seats for each distinct applicable window."""
    result = dict(lane=lane, eligible=False, reason="", score=0.0, windows=[])

    def deny(reason):
        result["reason"] = reason
        return result

    if not reading.ok:
        return deny(reading.error or "subscription meter unavailable")
    if (
        reading.meter_id == "claude-subscription"
        and reading.extra_usage_enabled is not False
    ):
        return deny("extra usage is not explicitly disabled")
    if not any(w.kind is UsageLimitKind.WEEKLY_ALL for w in reading.limits):
        return deny("missing weekly subscription allowance")
    try:
        age = (now - instant(reading.fetched_at)).total_seconds()
        if not -5 <= age <= READING_MAX_AGE:
            return deny("stale subscription meter")
        pressure = []
        seen = set()
        shortage = ""
        for limit in reading.limits:
            key = window_key(limit)
            if key in seen:
                return deny("duplicate subscription window")
            seen.add(key)
            used = limit.percent
            if (
                isinstance(used, bool)
                or not math.isfinite(used)
                or not 0 <= used <= 100
            ):
                return deny("invalid usage percentage")
            remaining_hours = (instant(limit.resets_at) - now).total_seconds() / 3600
            if not 0 < remaining_hours <= WINDOW_HOURS[limit.kind] + 1:
                return deny("unusable window reset")
            cost = max(MINIMUM_RESERVE, estimates.get(key, MINIMUM_RESERVE))
            pending = sum(
                amount
                for (window, reset), amount in reservations.items()
                if window == key and same_window(reset, limit.resets_at)
            )
            stop = policy.stop_for(limit.kind)
            available = stop - used - pending
            window = dict(
                key=key,
                kind=limit.kind.value,
                used_percent=used,
                resets_at=limit.resets_at,
                stop_percent=stop,
                reserved_percent=pending,
                estimated_seat_percent=cost,
            )
            result["windows"].append(window)
            # Equal to the stop would leave no room for rounding error.
            if available <= cost and not shortage:
                shortage = f"{key} has insufficient unreserved allowance"
            # How many comparable seats per hour must this provider serve to
            # use its remaining allowance by its own reset? The tightest window
            # limits its pace; this avoids combining unlike resets and limits.
            pressure.append(available / cost / max(remaining_hours, 1 / 60))
    except (KeyError, TypeError, ValueError, OverflowError):
        return deny("malformed subscription reading")
    if shortage:
        # Only a complete, valid reading can set a shared provider cooldown.
        reset = policy.assess(reading).reset_at
        if reset is not None:
            result["usage_reset_at"] = reset
        return deny(shortage)
    result.update(
        eligible=True, reason="within all applicable windows", score=min(pressure)
    )
    return result
