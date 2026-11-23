"""Plain-language progress lines rendered from the run events they mirror.

One run event is the single source for both its recorded payload and its owner
progress line, so the two can never drift. The rendering is pure and clock-free:
the timestamp it carries is the event's own ``occurred_at``, stamped from the
run-record port's clock, so no wall clock is read here."""

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

    The line is ASCII and carries no absolute filesystem path, except the
    validator output reference of a batch validation that failed at campaign
    end, which is the evidence the completion line must name. When the event has
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
    if event_type == "subscription.admission":
        reasons = ", ".join(f"{lane['lane']} {lane['reason']}" for lane in payload.get("lanes", []))
        selected = (payload.get("recommendation") or {}).get("lane", "on hold")
        return f"item {item_id} {payload.get('seat')} subscription {selected} ({reasons})"
    if event_type == "seat.started":
        lane = payload.get("lane")
        return (
            f"item {item_id} seat {payload.get('seat', '')} started: "
            f"{_seat_model(payload)}, effort {payload.get('effort') or 'default'}"
            + (f", lane {lane}" if lane else "")
        )
    if event_type == "seat.completed":
        return (
            f"item {item_id} seat {payload.get('seat', '')} "
            f"{payload.get('outcome', '')}"
            f"{_minutes(payload.get('duration_seconds'))}"
            f"{_cost(payload.get('cost'))}"
        )
    if event_type == "validation.started":
        kinds = []
        if payload.get("candidate") == "integrated":
            kinds.append("revalidation at delivery")
        if payload.get("rerun"):
            kinds.append("load rerun")
        return (
            f"item {item_id} validation started: validator {payload.get('validator_id', '')}"
            + (f" ({', '.join(kinds)})" if kinds else "")
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
    if event_type == "item.effort_raised":
        return (
            f"item {item_id} validator did not pass; a fresh builder retries at effort "
            f"{payload.get('effort', '?')} (was {payload.get('from_effort', '?')})"
        )
    if event_type == "item.builder_continued":
        return (
            f"item {item_id} builder timed out; claim and worktree kept, a fresh builder "
            f"continues it (continuation {payload.get('continuation', '?')} of "
            f"{payload.get('limit', '?')})"
        )
    if event_type == "delivery.started":
        return f"item {item_id} delivery started: target branch {payload.get('target_branch', '')}"
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
    if event_type == "campaign.launch_set_failed":
        return (
            f"the tracker could not list its items at launch: {payload.get('error', '')}; "
            "nothing is admitted and the campaign ends"
        )
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
    if event_type == "campaign.pause_requested":
        running = [str(name) for name in payload.get("running", ())]
        if running:
            return (
                f"pause requested: no new admissions; waiting for "
                f"{', '.join(running)} to finish, then the campaign stops"
            )
        return "pause requested: nothing is running, the campaign stops now"
    if event_type == "campaign.resumed":
        return (
            f"resuming paused run {payload.get('prior_run_id', '')}: "
            "its pause request is lifted"
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
    if event_type == "campaign.pause_check_failed":
        if payload.get("consecutive") != 1:
            return ""
        return (
            f"pause check failed: {payload.get('error', '')}; "
            "a pause request is seen at the next admission decision"
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
    if event_type == "campaign.batch_started":
        return (
            f"batch validation {payload.get('batch', '?')} started: validator "
            f"{payload.get('validator_id', '')} on {_commit(payload.get('revision'))}"
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
    if event_type == "usage.meter_failed":
        return _meter_failed(item_id, payload) + _reading_age(payload)
    if event_type == "usage.warning":
        return (
            f"{_seat_on_lane(item_id, payload)}: usage {_limit(payload)} is at or above "
            f"the warning level {_number(payload.get('warn_percent'))}%; admissions slow "
            "to one item at a time"
        ) + _reading_age(payload)
    if event_type == "usage.stop":
        return (
            f"{_seat_on_lane(item_id, payload)}: lane stopped at its usage level "
            f"({_one_ascii_line(payload.get('detail'))}); the seat moves to the next lane "
            "or the item waits"
        ) + _reading_age(payload)
    if event_type == "usage.seat_rerouted":
        limits = "; ".join(_limit(entry) for entry in payload.get("limits", ()) or ())
        return (
            f"{_seat_on_lane(item_id, payload)}: a scoped usage limit at its stop level "
            f"({limits}) covers the seat's model; the seat moves past the lane"
        )
    if event_type == "campaign.usage_stop":
        lanes = ", ".join(
            str(entry.get("lane", "")) for entry in payload.get("lanes", ()) or ()
        )
        return (
            f"every lane is stopped for usage ({lanes}): no new item is admitted; "
            "the items in flight finish and the campaign ends with usage_stop; "
            f"the earliest reset is {payload.get('resets_at') or 'unknown'}"
        )
    if event_type == "lane.usage_cooling_cleared":
        return (
            f"lane {payload.get('lane', '')} usage cooling until "
            f"{payload.get('cooled_until', '')} from campaign "
            f"{payload.get('campaign_id', '')} cleared: the meter reads "
            f"{_number(payload.get('percent'))}%, below this run's stop levels"
        )
    if event_type == "campaign.completed":
        return (
            f"{CAMPAIGN_COMPLETED_PREFIX}: shipped {payload.get('accepted', 0)}, "
            f"parked {payload.get('parked', 0)}, failed {payload.get('failed', 0)}; "
            f"{_batch_failure(payload)}"
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


def _reading_age(payload: Mapping[str, Any]) -> str:
    if "reading_age_seconds" not in payload:
        return ""
    age = payload.get("reading_age_seconds")
    if isinstance(age, bool) or not isinstance(age, (int, float)):
        return "; reading age unknown"
    return f"; reading {_number(age)} s old"


def _meter_failed(item_id: str, payload: Mapping[str, Any]) -> str:
    action = payload.get("action", "")
    head = (
        f"{_seat_on_lane(item_id, payload)}: usage meter reading failed "
        f"({_one_ascii_line(payload.get('error'))})"
    )
    last_good = payload.get("last_good")
    if isinstance(last_good, Mapping):
        percent = last_good.get("percent")
        stop = payload.get("stop_percent")
        if _at_or_above(percent, stop):
            level = f"stop level {_number(stop)}%"
        else:
            level = f"warning level {_number(payload.get('warn_percent'))}%"
        fetched = _one_ascii_line(last_good.get("fetched_at"))
        good = (
            f"last good reading {_limit(last_good)} reached the {level}"
            + (f" at {fetched}" if fetched else "")
        )
        if action == "stop":
            return (
                f"{head}; {good}; the hold reached its limit of "
                f"{_seconds(payload.get('hold_limit_seconds'))} and the item ends"
            )
        return (
            f"{head}; {good}; holding the seat until a reading succeeds, next read in "
            f"{_seconds(payload.get('retry_seconds'))}"
        )
    if action == "allow":
        return f"{head}; the profile allows a failed reading, so the seat starts"
    if action == "retry":
        return f"{head}; the seat waits and reads again in {_seconds(payload.get('retry_seconds'))}"
    if action == "stop":
        return f"{head}; the meter stayed unavailable and the item ends"
    return f"{head}; {_one_ascii_line(action)}"


def _seat_on_lane(item_id: str, payload: Mapping[str, Any]) -> str:
    seat = payload.get("seat") or "?"
    return f"item {item_id} seat {seat} on lane {payload.get('lane', '')}"


def _limit(limit: Any) -> str:
    if not isinstance(limit, Mapping):
        return ""
    scope = limit.get("scope")
    label = f"{limit.get('kind', '')}" + (f" [{_one_ascii_line(scope)}]" if scope else "")
    return f"{label} {_number(limit.get('percent'))}%"


def _number(value: Any) -> str:
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return "?"


def _at_or_above(value: Any, level: Any) -> bool:
    try:
        return float(value) >= float(level)
    except (TypeError, ValueError):
        return False


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


def _seat_model(payload: Mapping[str, Any]) -> str:
    model = payload.get("model")
    return f"model {model}" if model else f"model class {payload.get('model_class', '')}"


def _minutes(duration_seconds: Any) -> str:
    if duration_seconds is None:
        return ""
    return f" in {float(duration_seconds) / 60:.1f} min"


def _seconds(value: Any) -> str:
    try:
        return f"{float(value):.0f} s"
    except (TypeError, ValueError):
        return "? s"


def rounded_cost(cost: float) -> str:
    """The harness's cost estimate as an owner reads it, to two decimal places."""

    return f"{float(cost):.2f}"


def _cost(cost: Any) -> str:
    return "" if cost is None else f", estimated cost {rounded_cost(cost)}"


def _batch_failure(payload: Mapping[str, Any]) -> str:
    if payload.get("batch_validation") != "failed":
        return ""
    revision = payload.get("batch_revision") or "unknown"
    stdout_ref = _one_ascii_line(payload.get("batch_stdout_ref"))
    error = _one_ascii_line(payload.get("batch_error"))
    if stdout_ref:
        evidence = f"output {stdout_ref}"
    elif error:
        evidence = f"error {error}"
    else:
        evidence = "output none"
    return f"batch validation failed on {revision}; {evidence}; "


def _one_ascii_line(value: Any) -> str:
    if value is None:
        return ""
    flat = " ".join(str(value).split())
    escaped = "".join(
        f"\\x{ord(character):02x}"
        if ord(character) < 0x20 or ord(character) == 0x7F
        else character
        for character in flat
    )
    return escaped.encode("ascii", "backslashreplace").decode("ascii")


def _commit(value: Any) -> str:
    return "none" if not value else str(value)
