"""Plain-language progress lines rendered from the run events they mirror.

One run event is the single source for both its recorded payload and its owner
progress line, so the two can never drift. The rendering is pure and clock-free:
the timestamp it carries is the event's own ``occurred_at``, which the run-record
adapter stamps, so no wall clock is read here."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from autobuild.domain import RunEvent

# The stable opening of the campaign-completion line. The composite progress
# adapter reads it to append the swallowed hook-failure count once, so this token
# is the contract between the renderer and that adapter.
CAMPAIGN_COMPLETED_PREFIX = "campaign completed"


def render_progress_line(event: RunEvent) -> str:
    """Render one owner-facing progress line, or an empty string for an event
    that carries no owner-level meaning.

    The line is ASCII and carries no absolute filesystem path. When the event has
    an ``occurred_at`` it prefixes the line in the UTC ISO 8601 form the
    run-record adapter stamped."""

    body = _body(event.event_type, event.item_id or "", event.payload)
    if not body:
        return ""
    stamp = event.occurred_at.strip()
    return f"{stamp} {body}" if stamp else body


def _body(event_type: str, item_id: str, payload: Mapping[str, Any]) -> str:
    if event_type == "campaign.started":
        return (
            f"campaign started: harness {payload.get('harness', '')}, "
            f"models {_models(payload.get('models', {}))}, "
            f"up to {payload.get('item_bound', '?')} items"
        )
    if event_type == "item.claimed":
        return f"item {item_id} claimed: {payload.get('title', '')}"
    if event_type == "seat.completed":
        return (
            f"item {item_id} seat {payload.get('seat', '')} "
            f"{payload.get('outcome', '')}"
            f"{_minutes(payload.get('duration_seconds'))}"
            f"{_cost(payload.get('cost'))}"
        )
    if event_type == "validation.completed":
        exit_code = payload.get("exit_code")
        if exit_code == 0:
            return f"item {item_id} validation passed"
        return f"item {item_id} validation failed (exit {exit_code})"
    if event_type == "review.completed":
        count = len(payload.get("finding_codes", ()))
        return (
            f"item {item_id} review {payload.get('decision', '')} "
            f"with {count} finding(s)"
        )
    if event_type == "item.correcting":
        return f"item {item_id} correction round {payload.get('round', '?')}"
    if event_type == "item.parked":
        return f"item {item_id} parked: {payload.get('reason', '')}"
    if event_type == "item.finalised":
        return (
            f"item {item_id} delivered: item {_commit(payload.get('item_commit'))}, "
            f"tracker {_commit(payload.get('tracker_commit'))}, "
            f"pushed {bool(payload.get('pushed', False))}"
        )
    if event_type == "campaign.completed":
        return (
            f"{CAMPAIGN_COMPLETED_PREFIX}: shipped {payload.get('accepted', 0)}, "
            f"parked {payload.get('parked', 0)}, failed {payload.get('failed', 0)}; "
            f"report {payload.get('report', '')}; "
            f"stop {payload.get('stop_reason', '')}"
        )
    return ""


def _models(models: Mapping[str, Any]) -> str:
    return ", ".join(f"{name} {model}" for name, model in sorted(models.items()))


def _minutes(duration_seconds: Any) -> str:
    if duration_seconds is None:
        return ""
    return f" in {float(duration_seconds) / 60:.1f} min"


def _cost(cost: Any) -> str:
    return "" if cost is None else f", cost {cost}"


def _commit(value: Any) -> str:
    return "none" if not value else str(value)
