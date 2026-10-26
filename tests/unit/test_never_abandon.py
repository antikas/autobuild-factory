"""No single error abandons a campaign: item errors end only their item, and a
condition that blocks all work holds admissions until it clears or until the
hold limit, on a fake clock."""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from test_parallel_scheduler import (
    campaign,
    context,
    events,
    finish,
    identity,
    incomplete_review,
    item_spec,
    keyed_ports,
    wait_until,
    work,
    workspace_of,
)

import test_workflows as wf

from autobuild.adapters.tracker_commits import only_tracker_changes
from autobuild.application import CampaignRunner, ItemWorkflow
from autobuild.domain import (
    AdapterError,
    CampaignStopReason,
    ItemDisposition,
    LaneCooling,
    ReviewDecision,
    TrackerStop,
    WorktreeStatus,
)
from autobuild.domain.models import HoldCondition, SkippedItem, TrackerRecovery
from autobuild.testing import (
    FakeClock,
    FakeLaneStateAdapter,
    FakeLeaseAdapter,
    FakeTrackerAdapter,
)


def run(ports, ref, hold_limit: float = 21600.0):
    box: dict[str, object] = {}
    runner = CampaignRunner(ports)

    def target() -> None:
        try:
            box["outcome"] = runner.run(
                ref,
                lambda item: item_spec(item, {}),
                context=replace(context(), hold_limit_seconds=hold_limit),
            )
        except BaseException as exc:  # surfaced by finish()
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, box, runner


def report_text(ports) -> str:
    return ports.workspace.reports[-1].content


def test_a_structural_failure_parks_that_item_and_the_others_deliver() -> None:
    ids = ("item-1", "item-2", "item-3")
    ports = keyed_ports(ids, reviews={"item-1": incomplete_review("item-1")})
    ports.tracker.ready.extend(work(item_id) for item_id in ids)

    thread, box, _ = run(ports, campaign(3, 4))
    outcome = finish(thread, box)

    by_id = {entry.item_id: entry for entry in outcome.items}
    assert outcome.stop_reason is CampaignStopReason.QUEUE_DRY
    assert by_id["item-1"].disposition is ItemDisposition.PARKED
    assert by_id["item-1"].structural_failure is True
    assert "ReviewVerdict" in (by_id["item-1"].reason or "")
    assert [item_id for item_id, _ in ports.tracker.parked] == ["item-1"]
    assert by_id["item-2"].disposition is ItemDisposition.ACCEPTED
    assert by_id["item-3"].disposition is ItemDisposition.ACCEPTED
    assert not events(ports, "campaign.draining")


class _ParkRefusingTracker(FakeTrackerAdapter):
    def park(self, item_id, reason, actor, workspace=None):
        if item_id == "item-1":
            raise TrackerStop("pinax park exit 5: push rejected", code=5, environment=False)
        return super().park(item_id, reason, actor, workspace)


def test_a_failed_park_leaves_the_item_unfinished_and_the_campaign_goes_on() -> None:
    ids = ("item-1", "item-2")
    ports = keyed_ports(ids, reviews={"item-1": incomplete_review("item-1")})
    tracker = _ParkRefusingTracker(identity("tracker"))
    tracker.ready.extend(work(item_id) for item_id in ids)
    ports = replace(ports, tracker=tracker)
    ports.workspace.on_deliver = lambda item_id: tracker.mark_done(item_id) if item_id else None

    thread, box, _ = run(ports, campaign(2, 3))
    outcome = finish(thread, box)

    by_id = {entry.item_id: entry for entry in outcome.items}
    assert outcome.stop_reason is CampaignStopReason.QUEUE_DRY
    assert by_id["item-1"].disposition is ItemDisposition.FAILED
    assert "exit 5" in (by_id["item-1"].reason or "")
    assert by_id["item-2"].disposition is ItemDisposition.ACCEPTED
    assert "lease-item-1" not in ports.workspace.released
    failed = [event for event in events(ports, "item.park_failed") if event.item_id == "item-1"]
    assert failed[0].payload["worktree_root"] == str(workspace_of("item-1").root)
    report = report_text(ports)
    assert "## Unfinished" in report
    assert f"- item-1: pinax park exit 5: push rejected (worktree {workspace_of('item-1').root})" in report
    # A refused park is a tracker failure: the campaign asked the tracker to
    # recover, found it clear and went on without a hold.
    assert tracker.recoveries == 1
    assert not events(ports, "campaign.hold_started")


def test_lanes_exhausted_holds_until_the_cooling_reset_then_continues() -> None:
    clock = FakeClock()
    ports = keyed_ports(("item-1",), clock=clock)
    reset = (datetime.now(UTC) + timedelta(seconds=30)).isoformat()
    state = FakeLaneStateAdapter(identity("lanes"))
    state.cooled["lane"] = LaneCooling("lane", reset, "rate_limit", "earlier", "other")
    ports = replace(ports, lane_state=state)
    ports.tracker.ready.append(work("item-1"))

    thread, box, runner = run(ports, campaign(2, 2))
    wait_until(lambda: events(ports, "campaign.hold_started"), "the lanes hold")
    started = events(ports, "campaign.hold_started")[0].payload
    assert started["condition"] == HoldCondition.LANES_EXHAUSTED.value
    assert 20 <= started["retry_seconds"] <= 30
    wait_until(runner.scheduler_idle, "the scheduler to hold")

    clock.advance(10)
    wait_until(runner.scheduler_idle, "the scheduler to stay held")
    assert ports.tracker.claims == []
    assert not events(ports, "campaign.hold_released")

    del state.cooled["lane"]
    clock.advance(started["retry_seconds"])
    outcome = finish(thread, box)

    assert outcome.stop_reason is CampaignStopReason.QUEUE_DRY
    assert [entry.disposition for entry in outcome.items] == [ItemDisposition.ACCEPTED]
    released = events(ports, "campaign.hold_released")[0].payload
    assert released["condition"] == HoldCondition.LANES_EXHAUSTED.value
    assert "- lanes_exhausted: " in report_text(ports)


def _tracker_failure_ports(clock: FakeClock, code: int, recoveries: list[TrackerRecovery]):
    ports = keyed_ports(("item-1", "item-2"), clock=clock)
    ports.tracker.queue.append(work("item-1"))
    ports.tracker.ready.append(work("item-2"))
    ports.tracker.claim_errors["item-1"] = TrackerStop(
        f"pinax claim exit {code}: tracker message", code=code, environment=code == 4
    )
    ports.tracker.recovery_results.extend(recoveries)
    return ports


def test_an_unreachable_tracker_holds_with_backoff_and_releases_when_it_answers() -> None:
    clock = FakeClock()
    unreachable = TrackerRecovery(False, HoldCondition.TRACKER_UNREACHABLE, "origin does not answer")
    ports = _tracker_failure_ports(clock, 4, [unreachable, unreachable])

    thread, box, runner = run(ports, campaign(1, 3))
    wait_until(lambda: events(ports, "campaign.hold_started"), "the tracker hold")
    started = events(ports, "campaign.hold_started")[0].payload
    assert started["condition"] == "tracker_unreachable"
    assert started["retry_seconds"] == 30
    wait_until(runner.scheduler_idle, "the scheduler to hold")

    clock.set(30)
    wait_until(lambda: events(ports, "campaign.hold_retry"), "the first retry")
    retry = events(ports, "campaign.hold_retry")[0].payload
    assert (retry["retry"], retry["retry_seconds"]) == (1, 60)
    wait_until(runner.scheduler_idle, "the scheduler to hold again")
    assert [item_id for item_id, _ in ports.tracker.claims] == []

    clock.set(90)
    outcome = finish(thread, box)

    assert outcome.stop_reason is CampaignStopReason.QUEUE_DRY
    assert ports.tracker.recoveries == 3
    released = events(ports, "campaign.hold_released")[0].payload
    assert (released["condition"], released["seconds"], released["retries"]) == (
        "tracker_unreachable", 90, 1,
    )
    by_id = {entry.item_id: entry for entry in outcome.items}
    assert by_id["item-1"].disposition is ItemDisposition.FAILED
    assert by_id["item-2"].disposition is ItemDisposition.ACCEPTED
    failed = events(ports, "item.tracker_failed")[0]
    assert failed.payload == {
        "kind": "tracker_environment", "exit_code": 4, "error": "pinax claim exit 4: tracker message",
    }


class _FlakyReadTracker(FakeTrackerAdapter):
    """Its ready listing raises ``read_failures`` times, then answers."""

    read_failures = 0

    def ready_items(self, campaign):
        if self.read_failures > 0:
            self.read_failures -= 1
            raise AdapterError("pinax ready exit 4: origin could not be reached")
        return super().ready_items(campaign)


def test_a_tracker_read_that_raises_twice_holds_and_then_the_campaign_runs_dry() -> None:
    clock = FakeClock()
    ports = keyed_ports(("item-1",), clock=clock)
    tracker = _FlakyReadTracker(identity("tracker"))
    tracker.read_failures = 2
    tracker.ready.append(work("item-1"))
    ports = replace(ports, tracker=tracker)
    ports.workspace.on_deliver = lambda item_id: tracker.mark_done(item_id) if item_id else None

    thread, box, runner = run(ports, campaign(2, 3))
    wait_until(lambda: events(ports, "campaign.hold_started"), "the read hold")
    started = events(ports, "campaign.hold_started")[0].payload
    assert (started["condition"], started["retry_seconds"]) == ("tracker_read_failed", 30)
    assert "origin could not be reached" in started["detail"]
    wait_until(runner.scheduler_idle, "the scheduler to hold")

    clock.set(30)
    wait_until(lambda: events(ports, "campaign.hold_retry"), "the failed retry")
    assert events(ports, "campaign.hold_retry")[0].payload["retry_seconds"] == 60
    wait_until(runner.scheduler_idle, "the scheduler to hold again")

    clock.set(90)
    outcome = finish(thread, box)

    assert outcome.stop_reason is CampaignStopReason.QUEUE_DRY
    assert [entry.disposition for entry in outcome.items] == [ItemDisposition.ACCEPTED]
    assert events(ports, "campaign.hold_released")[0].payload["condition"] == "tracker_read_failed"


class _RaisingRenewLease(FakeLeaseAdapter):
    """Its renewal raises ``renew_failures`` times, then succeeds."""

    renew_failures = 0

    def renew(self, grant):
        if self.renew_failures > 0:
            self.renew_failures -= 1
            raise OSError("lease file is locked")
        return super().renew(grant)


def test_a_raising_lease_renewal_withholds_admissions_and_releases_on_renewal() -> None:
    clock = FakeClock()
    lease = _RaisingRenewLease(identity("lease"))
    lease.renew_failures = 4
    ids = ("item-1", "item-2", "item-3", "item-4")
    ports = keyed_ports(ids, clock=clock, lease=lease)
    ports.tracker.queue.extend(work(item_id) for item_id in ids)

    thread, box, runner = run(ports, campaign(1, 5))
    wait_until(lambda: events(ports, "campaign.lease_withheld"), "the lease to be withheld")
    wait_until(runner.scheduler_idle, "the scheduler to hold for the lease")
    assert [item_id for item_id, _ in ports.tracker.claims] == ["item-1", "item-2", "item-3"]

    clock.advance(30)
    outcome = finish(thread, box)

    assert outcome.stop_reason is CampaignStopReason.QUEUE_DRY
    assert [entry.item_id for entry in outcome.items] == list(ids)
    assert len(events(ports, "campaign.lease_renew_failed")) == 4
    assert events(ports, "campaign.lease_restored")


class _BrieflessTracker(FakeTrackerAdapter):
    """Reports one ready item it passed over for want of a brief."""

    def skipped_items(self):
        return (SkippedItem("item-x", "no_brief"),)


def test_an_item_skipped_without_a_brief_is_recorded_once_and_reported() -> None:
    ports = keyed_ports(("item-1", "item-2"))
    tracker = _BrieflessTracker(identity("tracker"))
    tracker.ready.extend([work("item-1"), work("item-2")])
    ports = replace(ports, tracker=tracker)
    ports.workspace.on_deliver = lambda item_id: tracker.mark_done(item_id) if item_id else None

    thread, box, _ = run(ports, campaign(2, 3))
    outcome = finish(thread, box)

    assert outcome.stop_reason is CampaignStopReason.QUEUE_DRY
    skipped = events(ports, "item.skipped")
    assert [(event.item_id, event.payload) for event in skipped] == [
        ("item-x", {"reason": "no_brief"})
    ]
    report = report_text(ports)
    assert "## Skipped" in report and "- item-x: no_brief" in report


class _NeverRenewingLease(FakeLeaseAdapter):
    def renew(self, grant):
        raise RuntimeError("tracker lease renewal refused")


def test_a_lease_that_never_renews_ends_at_exactly_the_hold_limit() -> None:
    clock = FakeClock()
    ids = ("item-1", "item-2", "item-3", "item-4")
    ports = keyed_ports(ids, clock=clock, lease=_NeverRenewingLease(identity("lease")))
    ports.tracker.queue.extend(work(item_id) for item_id in ids)

    thread, box, runner = run(ports, campaign(1, 5), hold_limit=100)
    wait_until(lambda: events(ports, "campaign.hold_started"), "the lease hold")
    started = events(ports, "campaign.hold_started")[0].payload
    assert (started["condition"], started["retry_seconds"]) == ("lease_renew_failed", 30)
    assert "renewal refused" in started["detail"]
    wait_until(runner.scheduler_idle, "the scheduler to hold")

    clock.set(99)
    wait_until(lambda: events(ports, "campaign.hold_retry"), "a retry before the limit")
    wait_until(runner.scheduler_idle, "the scheduler to hold at 99")
    assert thread.is_alive()
    assert not events(ports, "campaign.hold_limit")

    clock.set(100)
    outcome = finish(thread, box)

    assert outcome.stop_reason is CampaignStopReason.HOLD_LIMIT
    limit = events(ports, "campaign.hold_limit")[0].payload
    assert (limit["condition"], limit["seconds"]) == ("lease_renew_failed", 100)
    assert [item_id for item_id, _ in ports.tracker.claims] == ["item-1", "item-2", "item-3"]


class _UnreadableLiveHolderLease(FakeLeaseAdapter):
    def live_holder(self, surface):
        raise OSError("lease file is unreadable")


def test_an_unreadable_resume_lease_is_no_signal_and_the_resume_goes_on() -> None:
    ports = wf.make_ports([wf.review(ReviewDecision.PASS)], [wf.command()], diff_count=1)
    ports = replace(ports, lease=_UnreadableLiveHolderLease(identity("lease")))
    ports.tracker.resumable = [wf.item()]
    ports.records.markers = {"item-1": wf._built_marker()}
    ports.workspace.worktrees = [WorktreeStatus(Path("/worktree"), "run", "base", "digest-built")]

    outcome = CampaignRunner(ports).run(wf.campaign(max_items=1), lambda selected: wf.spec(selected))

    assert [entry.disposition for entry in outcome.items] == [ItemDisposition.ACCEPTED]
    assert any(event.event_type == "item.resumed" for event in ports.records.events)


class _OnceUnreadableLanes(FakeLaneStateAdapter):
    reads = 0

    def active(self, lane):
        self.reads += 1
        if self.reads == 1:
            raise OSError("lane state file is locked")
        return super().active(lane)


def test_an_unreadable_lane_state_is_no_signal_and_admission_goes_on() -> None:
    ports = keyed_ports(("item-1",))
    ports = replace(ports, lane_state=_OnceUnreadableLanes(identity("lanes")))
    ports.tracker.ready.append(work("item-1"))

    thread, box, _ = run(ports, campaign(2, 2))
    outcome = finish(thread, box)

    assert outcome.stop_reason is CampaignStopReason.QUEUE_DRY
    assert [entry.disposition for entry in outcome.items] == [ItemDisposition.ACCEPTED]


class _UnreadableUsageWorkflow(ItemWorkflow):
    def usage_at_warning(self) -> bool:
        raise RuntimeError("usage reading is unreadable")


def test_an_unreadable_usage_level_is_no_signal_and_admission_goes_on() -> None:
    ports = keyed_ports(("item-1", "item-2"))
    ports.tracker.ready.extend([work("item-1"), work("item-2")])
    box: dict[str, object] = {}

    def target() -> None:
        try:
            box["outcome"] = CampaignRunner(ports, items=_UnreadableUsageWorkflow(ports)).run(
                campaign(2, 3), lambda item: item_spec(item, {}), context=context()
            )
        except BaseException as exc:  # surfaced by finish()
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    outcome = finish(thread, box)

    assert outcome.stop_reason is CampaignStopReason.QUEUE_DRY
    assert [entry.disposition for entry in outcome.items] == [ItemDisposition.ACCEPTED] * 2


def test_a_rename_counts_both_sides_when_checking_for_tracker_only_changes() -> None:
    def git_with(status: str):
        return lambda root, *args: status

    root = Path("/repo")
    assert only_tracker_changes(git_with("R  .ergon/a.md -> .ergon/b.md\n"), root, (".ergon",))
    assert not only_tracker_changes(git_with("R  .ergon/a.md -> src/b.py\n"), root, (".ergon",))
    assert not only_tracker_changes(git_with("R  src/a.py -> .ergon/b.md\n"), root, (".ergon",))


def test_a_hold_that_never_clears_ends_at_exactly_the_hold_limit() -> None:
    clock = FakeClock()
    pending = TrackerRecovery(False, HoldCondition.TRACKER_PENDING, "commit refused")
    ports = _tracker_failure_ports(clock, 7, [pending] * 10)

    thread, box, runner = run(ports, campaign(1, 2), hold_limit=150)
    wait_until(lambda: events(ports, "campaign.hold_started"), "the tracker hold")
    assert events(ports, "campaign.hold_started")[0].payload["retry_seconds"] == 60
    wait_until(runner.scheduler_idle, "the scheduler to hold")

    clock.set(149)
    wait_until(lambda: events(ports, "campaign.hold_retry"), "the retry at 149")
    wait_until(runner.scheduler_idle, "the scheduler to hold at 149")
    assert thread.is_alive()
    assert not events(ports, "campaign.hold_limit")

    clock.set(150)
    outcome = finish(thread, box)

    assert outcome.stop_reason is CampaignStopReason.HOLD_LIMIT
    limit = events(ports, "campaign.hold_limit")[0].payload
    assert (limit["condition"], limit["seconds"], limit["limit_seconds"]) == (
        "tracker_pending", 150, 150,
    )
    assert [item_id for item_id, _ in ports.tracker.claims] == []
    assert "- tracker_pending: 150.0 seconds, hold limit" in report_text(ports)
