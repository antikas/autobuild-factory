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
            f"efforts {_efforts(payload.get('efforts', {}))}, "
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
    if event_type == "item.builder_continued":
        return (
            f"item {item_id} builder timed out; claim and worktree kept, a fresh builder "
            f"continues it (continuation {payload.get('continuation', '?')} of "
            f"{payload.get('limit', '?')})"
        )
    if event_type == "item.finalised":
        return (
            f"item {item_id} delivered: item {_commit(payload.get('item_commit'))}, "
            f"tracker {_commit(payload.get('tracker_commit'))}, "
            f"pushed {bool(payload.get('pushed', False))}"
        )
    if event_type == "tracker.unpublished":
        return (
            f"item {item_id} {payload.get('operation', '')} committed and not yet "
            "published; it goes out with the next push of its branch"
        )
    if event_type == "item.claim_taken":
        return f"item {item_id} is taken by an earlier claim; selecting the next item"
    if event_type == "item.tracker_failed":
        code = payload.get("exit_code")
        return (
            f"item {item_id} tracker write failed: {payload.get('kind', '')}"
            + (f" (tracker exit {code})" if code is not None else "")
            + "; the campaign goes on"
        )
    if event_type == "item.park_failed":
        return (
            f"item {item_id} park failed; the item is unfinished and its worktree is kept: "
            f"{payload.get('error', '')}"
        )
    if event_type == "item.follow_up_failed":
        return (
            f"item {item_id} is delivered; recording its follow-ups failed: "
            f"{payload.get('error', '')}"
        )
    if event_type == "campaign.hold_started":
        return (
            f"holding new admissions: {_HOLD_CONDITIONS.get(payload.get('condition', ''), payload.get('condition', ''))}; "
            f"next check in {_seconds(payload.get('retry_seconds'))}"
        )
    if event_type == "campaign.hold_retry":
        return (
            f"still holding ({payload.get('condition', '')}, check {payload.get('retry', '?')}); "
            f"next check in {_seconds(payload.get('retry_seconds'))}"
        )
    if event_type == "campaign.hold_released":
        return (
            f"hold released ({payload.get('condition', '')}) after "
            f"{_seconds(payload.get('seconds'))}; admissions resume"
        )
    if event_type == "campaign.hold_limit":
        return (
            f"hold ({payload.get('condition', '')}) reached its limit of "
            f"{_seconds(payload.get('limit_seconds'))}; the campaign ends"
        )
    if event_type == "item.skipped":
        return f"item {item_id} skipped: {payload.get('reason', '')}; it is not claimed"
    if event_type == "campaign.refill_failed":
        return f"refill failed: {payload.get('error', '')}"
    if event_type == "campaign.scope_fence_violation":
        return "the tracker offered an item outside the selection fence; it is not claimed"
    if event_type == "item.admitted":
        kind = "resumed" if payload.get("resume") else "admitted"
        return (
            f"item {item_id} {kind}: {payload.get('running', '?')} of "
            f"{payload.get('concurrency', '?')} running"
        )
    if event_type == "campaign.waiting":
        return (
            f"waiting to admit: {_WAIT_REASONS.get(payload.get('reason', ''), payload.get('reason', ''))}"
            f"{_items(payload.get('items', ()))}; "
            f"{payload.get('running', '?')} of {payload.get('concurrency', '?')} running"
        )
    if event_type == "campaign.draining":
        return (
            f"campaign stopping ({payload.get('stop_reason', '')}): no new admissions, "
            f"waiting for {payload.get('running', '?')} running item(s)"
        )
    if event_type == "campaign.pressure_changed":
        level = payload.get("level", "")
        return (
            f"host pressure {level} (was {payload.get('previous', '')}): "
            f"cpu {payload.get('cpu_busy_percent', '?')}% over the window, memory available "
            f"{payload.get('memory_available_gib', '?')} GiB; "
            f"{_PRESSURE_EFFECTS.get(level, '')}"
        )
    if event_type == "campaign.cap_changed":
        return (
            f"item cap {payload.get('previous', '?')} -> {payload.get('cap', '?')} of "
            f"{payload.get('capacity', '?')} at host pressure {payload.get('level', '')}"
        )
    if event_type == "campaign.pressure_unmeasured":
        fields = ", ".join(str(name) for name in payload.get("unmeasured", ()))
        return (
            f"host pressure is not measured ({fields}); the campaign keeps its "
            f"concurrency of {payload.get('concurrency', '?')}"
        )
    if event_type == "campaign.pressure_read_failed":
        return "host reading failed; pressure control keeps its last level and cap"
    if event_type == "campaign.pressure_fallback":
        return (
            f"host readings failed {payload.get('consecutive', '?')} times in a row; "
            f"keeping host pressure {payload.get('level', '')} with item cap "
            f"{payload.get('cap', '?')} until a reading succeeds"
        )
    if event_type == "campaign.lease_withheld":
        return (
            f"tracker lease renewal failed {payload.get('failures', '?')} times in a row; "
            "no new admissions until it renews"
        )
    if event_type == "campaign.lease_restored":
        return "tracker lease renewed again; admissions resume"
    if event_type == "validation.load_rerun":
        return (
            f"item {item_id} validation timed out under host pressure; "
            "running it once more alone"
        )
    if event_type == "delivery.revalidation":
        if payload.get("run"):
            return (
                f"item {item_id} delivery revalidates: the target changed "
                f"{_items(payload.get('overlapping', ())).strip()} beside the item's own paths"
            )
        return (
            f"item {item_id} delivery skips revalidation "
            f"({_REVALIDATION_SKIPS.get(payload.get('reason', ''), payload.get('reason', ''))})"
        )
    if event_type == "campaign.batch_validated":
        return (
            f"batch validation {payload.get('batch', '?')} passed on "
            f"{_commit(payload.get('revision'))} after {payload.get('deliveries', '?')} "
            "delivery(ies)"
        )
    if event_type == "campaign.batch_validation_failed":
        commits = ", ".join(str(commit) for commit in payload.get("commits", ()))
        return (
            f"batch validation {payload.get('batch', '?')} failed on "
            f"{_commit(payload.get('revision'))}; delivered since the last pass: "
            f"{commits or 'none'}; nothing is reverted"
        )
    if event_type == "item.delivery_waiting":
        return f"item {item_id} delivery waits behind {payload.get('ahead', '?')} delivery(ies)"
    if event_type == "campaign.completed":
        return (
            f"{CAMPAIGN_COMPLETED_PREFIX}: shipped {payload.get('accepted', 0)}, "
            f"parked {payload.get('parked', 0)}, failed {payload.get('failed', 0)}; "
            f"report {payload.get('report', '')}; "
            f"stop {payload.get('stop_reason', '')}"
        )
    return ""


_WAIT_REASONS = {
    "cap": "the concurrency cap is reached",
    "path_overlap": "every ready item declares a file an item in flight declares",
    "usage": "the usage meter is at its warning level",
    "pressure": "host pressure withholds new admissions",
    "lease": "the tracker lease is not renewing",
}

_HOLD_CONDITIONS = {
    "lanes_exhausted": "every lane is cooling",
    "tracker_pending": "a tracker write is not committed yet",
    "tracker_unreachable": "the tracker does not answer",
    "tracker_read_failed": "the ready queue could not be read",
    "lease_renew_failed": "the tracker lease cannot be renewed",
    "batch_validation_failed": "the validator failed on the target head",
}

_REVALIDATION_SKIPS = {
    "docs_only": "the target changed only tracker, docs or Markdown files",
    "no_overlap": "the target changed only other packages",
    "no_product_change": "the target changed no product file",
}

_PRESSURE_EFFECTS = {
    "normal": "admitting again",
    "recovering": "no new admissions until normal",
    "high": "no new admissions or heavy commands",
    "critical": "no new admissions; queued heavy commands wait",
}


def _items(items: Any) -> str:
    names = [str(name) for name in items or ()]
    return f" ({', '.join(names)})" if names else ""


def _models(models: Mapping[str, Any]) -> str:
    return ", ".join(f"{name} {model}" for name, model in sorted(models.items()))


def _efforts(efforts: Mapping[str, Any]) -> str:
    configured = [(seat, effort) for seat, effort in sorted(efforts.items()) if effort is not None]
    if not configured:
        return "none"
    return ", ".join(f"{seat} {effort}" for seat, effort in configured)


def _minutes(duration_seconds: Any) -> str:
    if duration_seconds is None:
        return ""
    return f" in {float(duration_seconds) / 60:.1f} min"


def _seconds(value: Any) -> str:
    try:
        return f"{float(value):.0f} s"
    except (TypeError, ValueError):
        return "? s"


def _cost(cost: Any) -> str:
    return "" if cost is None else f", cost {cost}"


def _commit(value: Any) -> str:
    return "none" if not value else str(value)
