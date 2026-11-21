"""The launch set: a campaign selects only items that existed in the tracker
once it held its tracker lease. An item created during the run, its own
follow-ups included, waits for a later campaign."""

from __future__ import annotations

from dataclasses import replace

import pytest
from test_parallel_scheduler import (
    campaign,
    claimed,
    events,
    finish,
    identity,
    keyed_ports,
    run_in_background,
    work,
)

from autobuild.application.campaign import CampaignRunner
from autobuild.domain import (
    AdapterError,
    CampaignRef,
    CampaignSelection,
    CampaignStopReason,
    ItemDisposition,
    Proposal,
    ScopeFenceViolation,
    Seat,
)
from autobuild.domain.models import SkippedItem
from autobuild.enforcement.selection import ScopedTrackerPort
from autobuild.testing import FakeRunRecordAdapter, FakeTrackerAdapter

LAUNCH = ("item-1", "item-2", "item-3")
FOLLOW_UP = "follow-up"


def report_text(ports) -> str:
    return ports.workspace.reports[-1].content


@pytest.mark.parametrize("concurrency", [1, 2])
def test_a_follow_up_registered_mid_campaign_is_never_claimed(concurrency: int) -> None:
    holder: dict[str, object] = {}

    def register_follow_up(request) -> None:
        if request.seat is not Seat.BUILDER or request.item_id != "item-1":
            return
        tracker = holder["ports"].tracker
        # Ready, with a brief, at the head of both listings: only the launch
        # set can keep it out.
        with tracker._guard:
            tracker.queue.insert(0, work(FOLLOW_UP))
            tracker.ready.insert(0, work(FOLLOW_UP))

    ports = keyed_ports((*LAUNCH, FOLLOW_UP), on_invoke=register_follow_up)
    holder["ports"] = ports
    ports.tracker.queue.extend(work(item_id) for item_id in LAUNCH)
    ports.tracker.ready.extend(work(item_id) for item_id in LAUNCH)

    thread, box = run_in_background(ports, campaign(concurrency, 10))
    outcome = finish(thread, box)

    assert FOLLOW_UP not in claimed(ports)
    assert sorted(claimed(ports)) == sorted(LAUNCH)
    assert sorted(entry.item_id for entry in outcome.items) == sorted(LAUNCH)
    assert all(entry.disposition is ItemDisposition.ACCEPTED for entry in outcome.items)
    assert outcome.stop_reason is CampaignStopReason.QUEUE_DRY
    fixed = events(ports, "campaign.launch_set_fixed")
    assert [event.payload for event in fixed] == [{"items": len(LAUNCH)}]
    assert ports.tracker.known_reads == 1
    assert not events(ports, "campaign.scope_fence_violation")


def test_the_launch_set_is_fixed_after_the_lease_and_before_any_claim() -> None:
    ports = keyed_ports(("item-1",))
    ports.tracker.queue.append(work("item-1"))

    thread, box = run_in_background(ports, campaign(1, 5))
    finish(thread, box)

    order = [event.event_type for event in ports.records.events]
    assert order.index("campaign.lease_acquired") < order.index("campaign.launch_set_fixed")
    assert order.index("campaign.launch_set_fixed") < order.index("item.claimed")


class _LateBrieflessTracker(FakeTrackerAdapter):
    """Reports a launch-time item and a post-launch item passed over for want
    of a brief."""

    def skipped_items(self):
        return tuple(self.skipped)


def test_a_post_launch_item_without_a_brief_is_not_reported_as_skipped() -> None:
    holder: dict[str, object] = {}

    def register_late(request) -> None:
        if request.seat is not Seat.BUILDER or request.item_id != "item-1":
            return
        holder["tracker"].skipped.append(SkippedItem("late", "no_brief"))

    ports = keyed_ports(("item-1", "item-2"), on_invoke=register_late)
    tracker = _LateBrieflessTracker(
        identity("tracker"),
        existing={"early"},
        skipped=[SkippedItem("early", "no_brief")],
    )
    holder["tracker"] = tracker
    tracker.ready.extend([work("item-1"), work("item-2")])
    ports = replace(ports, tracker=tracker)
    ports.workspace.on_deliver = lambda item_id: tracker.mark_done(item_id) if item_id else None

    thread, box = run_in_background(ports, campaign(2, 5))
    finish(thread, box)

    assert [event.item_id for event in events(ports, "item.skipped")] == ["early"]
    report = report_text(ports)
    assert "- early: no_brief" in report
    assert "late" not in report


def test_a_launch_item_blocked_on_its_dependency_is_built_once_unblocked() -> None:
    ports = keyed_ports(("item-1", "item-2"))
    ports.tracker.ready.extend([work("item-1"), work("item-2")])
    ports.tracker.blocked_by["item-2"] = frozenset({"item-1"})

    thread, box = run_in_background(ports, campaign(2, 5))
    outcome = finish(thread, box)

    assert claimed(ports) == ["item-1", "item-2"]
    assert [entry.disposition for entry in outcome.items] == [ItemDisposition.ACCEPTED] * 2


class _LateHeadTracker(FakeTrackerAdapter):
    """Its next item is always one created after the campaign started."""

    def next_item(self, campaign):
        return work("late")

    def known_item_ids(self, campaign):
        return super().known_item_ids(campaign) - {"late"}


def test_a_post_launch_next_item_falls_back_to_the_first_launch_item_ready() -> None:
    ports = keyed_ports(("item-1", "late"))
    tracker = _LateHeadTracker(identity("tracker"))
    tracker.ready.extend([work("late"), work("item-1")])
    ports = replace(ports, tracker=tracker)
    ports.workspace.on_deliver = lambda item_id: tracker.mark_done(item_id) if item_id else None

    thread, box = run_in_background(ports, campaign(1, 5))
    outcome = finish(thread, box)

    assert claimed(ports) == ["item-1"]
    assert [entry.item_id for entry in outcome.items] == ["item-1"]
    assert outcome.stop_reason is CampaignStopReason.QUEUE_DRY
    assert not events(ports, "campaign.scope_fence_violation")


def test_an_unreadable_launch_set_stops_the_campaign_before_any_claim() -> None:
    ports = keyed_ports(("item-1",))
    ports.tracker.queue.append(work("item-1"))
    ports.tracker.ready.append(work("item-1"))
    ports.tracker.known_error = AdapterError("event log unreadable")

    thread, box = run_in_background(ports, campaign(1, 5))
    outcome = finish(thread, box)

    assert outcome.stop_reason is CampaignStopReason.LAUNCH_SET_UNREADABLE
    assert outcome.items == ()
    assert claimed(ports) == []
    assert ports.harness.requests == []
    failed = events(ports, "campaign.launch_set_failed")
    assert len(failed) == 1 and "event log unreadable" in failed[0].payload["error"]
    assert not events(ports, "campaign.launch_set_fixed")
    assert ports.lease.released
    assert ports.workspace.reports
    completed = events(ports, "campaign.completed")
    assert completed[0].payload["stop_reason"] == "launch_set_unreadable"


def test_the_scoped_tracker_refuses_to_claim_an_item_it_proposed() -> None:
    base = FakeTrackerAdapter(identity("tracker"))
    scoped = ScopedTrackerPort(base, CampaignSelection())

    proposed = scoped.propose(Proposal("follow-up", "question?", "reason", "brief"), "builder")

    with pytest.raises(ScopeFenceViolation):
        scoped.claim(work(proposed.proposal_id), "builder@autobuild")
    assert base.claims == []
    scoped.claim(work("item-1"), "builder@autobuild")
    assert base.claims == [("item-1", "builder@autobuild")]


def _allowed_late_campaign(max_items: int) -> CampaignRef:
    return replace(
        campaign(1, max_items), selection=CampaignSelection(allow=("late", "item-1"))
    )


def _late_head_ports(*item_ids: str):
    ports = keyed_ports(("item-1", "late", *item_ids))
    tracker = _LateHeadTracker(identity("tracker"))
    tracker.ready.extend(work(item_id) for item_id in ("late", "item-1", *item_ids))
    ports = replace(ports, tracker=tracker)
    ports.workspace.on_deliver = lambda item_id: tracker.mark_done(item_id) if item_id else None
    return ports


def test_an_allowed_ready_item_outside_the_launch_set_is_never_claimed() -> None:
    ports = _late_head_ports()

    thread, box = run_in_background(ports, _allowed_late_campaign(5))
    outcome = finish(thread, box)

    assert claimed(ports) == ["item-1"]
    assert [(entry.item_id, entry.disposition) for entry in outcome.items] == [
        ("item-1", ItemDisposition.ACCEPTED)
    ]
    assert outcome.stop_reason is CampaignStopReason.QUEUE_DRY
    assert not events(ports, "campaign.scope_fence_violation")
    assert "- late: outside the launch set" in report_text(ports)


def test_an_item_bound_stop_still_reports_a_post_launch_item_outside_the_launch_set() -> None:
    ports = _late_head_ports("item-2")

    thread, box = run_in_background(ports, _allowed_late_campaign(1))
    outcome = finish(thread, box)

    assert claimed(ports) == ["item-1"]
    assert outcome.stop_reason is CampaignStopReason.ITEM_BOUND
    assert "- late: outside the launch set" in report_text(ports)


def test_an_allowed_item_outside_the_launch_set_does_not_block_later_allowed_items() -> None:
    ports = _late_head_ports()
    ports.tracker.existing.add("item-2")
    ref = replace(
        campaign(1, 5), selection=CampaignSelection(allow=("late", "item-1", "item-2"))
    )

    thread, box = run_in_background(ports, ref)
    finish(thread, box)

    assert claimed(ports) == ["item-1"]
    report = report_text(ports)
    assert "- late: outside the launch set" in report
    assert "- item-2: not ready" in report
    assert "- item-2: blocked" not in report


def test_an_unreadable_launch_set_reports_no_allowed_item_outside_it() -> None:
    ports = keyed_ports(("item-1", "item-2"))
    ports.tracker.ready.extend([work("item-1"), work("item-2")])
    ports.tracker.known_error = AdapterError("event log unreadable")
    ref = replace(campaign(1, 5), selection=CampaignSelection(allow=("item-1", "item-2")))

    thread, box = run_in_background(ports, ref)
    outcome = finish(thread, box)

    assert outcome.stop_reason is CampaignStopReason.LAUNCH_SET_UNREADABLE
    report = report_text(ports)
    assert "- item-1: launch set unreadable" in report
    assert "- item-2: launch set unreadable" in report
    assert "outside the launch set" not in report


def test_an_unreadable_launch_set_still_reports_a_tracker_blocked_item_as_blocked() -> None:
    ports = keyed_ports(("item-1", "item-2"))
    ports.tracker.ready.append(work("item-1"))
    ports.tracker.blocked.add("item-2")
    ports.tracker.known_error = AdapterError("event log unreadable")
    ref = replace(campaign(1, 5), selection=CampaignSelection(allow=("item-1", "item-2")))

    thread, box = run_in_background(ports, ref)
    outcome = finish(thread, box)

    assert outcome.stop_reason is CampaignStopReason.LAUNCH_SET_UNREADABLE
    report = report_text(ports)
    assert "- item-1: launch set unreadable" in report
    assert "- item-2: blocked" in report


def test_a_selected_item_outside_the_launch_set_is_refused_after_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ports = _late_head_ports()
    monkeypatch.setattr(CampaignRunner, "_select_item", lambda self, *args, **kwargs: work("late"))

    thread, box = run_in_background(ports, campaign(1, 5))
    outcome = finish(thread, box)

    fence = events(ports, "campaign.scope_fence_violation")
    assert fence and all(event.item_id == "late" for event in fence)
    assert "late" not in claimed(ports)
    assert ports.harness.requests == []
    assert outcome.items == ()
    assert outcome.stop_reason is CampaignStopReason.QUEUE_DRY
    assert ports.lease.released


class _LoggingTracker(FakeTrackerAdapter):
    def __init__(self, *args, log: list[str], **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.log = log

    def known_item_ids(self, campaign):
        self.log.append("known_item_ids")
        return super().known_item_ids(campaign)

    def resumable_claims(self, campaign):
        self.log.append("resumable_claims")
        return super().resumable_claims(campaign)


class _LoggingRecords(FakeRunRecordAdapter):
    def __init__(self, *args, log: list[str], **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.log = log

    def clear_prior_pause(self, record):
        self.log.append("clear_prior_pause")
        return super().clear_prior_pause(record)


def test_the_launch_set_is_read_before_the_prior_pause_lift_and_the_resume_scan() -> None:
    log: list[str] = []
    ports = keyed_ports(("item-1", "resumed"))
    tracker = _LoggingTracker(identity("tracker"), log=log)
    tracker.ready.append(work("item-1"))
    tracker.resumable.append(work("resumed"))
    records = _LoggingRecords(identity("records"), log=log)
    records.prior_paused_run = "campaign-prior-run"
    ports = replace(ports, tracker=tracker, records=records)
    ports.workspace.on_deliver = lambda item_id: tracker.mark_done(item_id) if item_id else None

    thread, box = run_in_background(ports, campaign(1, 5))
    finish(thread, box)

    assert log.count("known_item_ids") == 1
    assert "clear_prior_pause" in log and "resumable_claims" in log
    known = log.index("known_item_ids")
    assert known < log.index("clear_prior_pause")
    assert known < log.index("resumable_claims")
    assert records.cleared_pauses == ["campaign-prior-run"]
