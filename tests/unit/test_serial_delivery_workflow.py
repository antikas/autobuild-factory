"""The item workflow's side of serialised delivery, against the port fakes.

The real Git and Pinax behaviour is proved in
``tests/integration/test_serial_delivery.py``; these tests pin how the
workflow reacts: a delivery park parks from the primary checkout and keeps the
worktree, and a moved base runs the same validator once more."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_workflows import builder, campaign, command, item, make_ports, review, spec

from autobuild.application import CampaignRunner, ItemWorkflow
from autobuild.application.item import _ParkGuard, _PhaseMarker
from autobuild.application.state_machine import ItemStateMachine
from autobuild.domain import (
    ItemDisposition,
    ItemState,
    PhaseMarker,
    ReviewDecision,
    WorktreeStatus,
)
from autobuild.domain.models import DeliveryParked, DeliveryParkReason


def _run(ports):
    workflow = ItemWorkflow(ports)
    return workflow.run(campaign(), spec(), ports.records.create(campaign()))


def _validations(ports) -> list[dict]:
    return [
        event.payload for event in ports.records.events if event.event_type == "validation.completed"
    ]


def test_a_delivery_park_parks_from_the_primary_and_keeps_the_worktree() -> None:
    ports = make_ports([builder(), review(ReviewDecision.PASS)], [command()])
    ports.workspace.delivery_parks.append(
        DeliveryParked(DeliveryParkReason.CONFLICT, "integrating origin/main conflicted in a.py")
    )

    outcome = _run(ports)

    assert outcome.disposition is ItemDisposition.PARKED
    assert outcome.reason == "delivery_conflict: integrating origin/main conflicted in a.py"
    assert outcome.finalise is None
    assert ports.tracker.parked == [
        ("item-1", f"{outcome.reason} [snapshot=evidence/item-1-park]")
    ]
    # The park went to the primary checkout: nothing was committed or delivered
    # in the worktree, and the worktree is snapshotted and kept as evidence.
    assert ports.workspace.released == []
    assert ports.workspace.confirmed == []
    assert ports.workspace.snapshotted == ["lease"]
    parked = next(event for event in ports.records.events if event.event_type == "item.parked")
    assert parked.payload["reason"] == outcome.reason
    assert parked.payload["snapshot_path"] == "evidence/item-1-park"


def test_an_unchanged_base_runs_the_validator_once() -> None:
    ports = make_ports([builder(), review(ReviewDecision.PASS)], [command()])

    outcome = _run(ports)

    assert outcome.disposition is ItemDisposition.ACCEPTED
    assert len(ports.command.requests) == 1
    assert ports.workspace.revalidations == []


def test_a_moved_base_runs_the_same_validator_once_more_in_the_worktree() -> None:
    ports = make_ports(
        [builder(), review(ReviewDecision.PASS)], [command(), command(suffix="integrated")]
    )
    ports.workspace.base_moved = True

    outcome = _run(ports)

    assert outcome.disposition is ItemDisposition.ACCEPTED
    assert ports.workspace.revalidations == [True]
    first, second = ports.command.requests
    assert second.argv == first.argv
    assert second.cwd == first.cwd
    assert second.environment == first.environment
    payloads = _validations(ports)
    assert [payload.get("candidate") for payload in payloads] == [None, "integrated"]


def test_a_failed_revalidation_parks_delivery_revalidation_failed() -> None:
    ports = make_ports(
        [builder(), review(ReviewDecision.PASS)],
        [command(), command(exit_code=1, suffix="integrated")],
    )
    ports.workspace.base_moved = True

    outcome = _run(ports)

    assert outcome.disposition is ItemDisposition.PARKED
    assert outcome.reason.startswith("delivery_revalidation_failed")
    assert ports.workspace.revalidations == [False]
    assert [entry[0] for entry in ports.tracker.parked] == ["item-1"]
    assert ports.tracker.parked[0][1].startswith(outcome.reason)
    assert ports.workspace.released == []


def _finalised_marker(head: str) -> PhaseMarker:
    return PhaseMarker("item-1", "finalised", Path("/worktree"), "run", head, "digest", 0, "run-old")


def test_a_finalised_marker_resumes_over_an_integration_merge_head() -> None:
    ports = make_ports([], [], diff_count=0)
    ports.tracker.resumable = [item()]
    ports.records.markers = {"item-1": _finalised_marker("tracker-commit")}
    ports.workspace.worktrees = [
        WorktreeStatus(Path("/worktree"), "run", "integration-merge", "digest", "tracker-commit")
    ]

    outcome = CampaignRunner(ports).run(campaign(max_items=5), lambda selected: spec(selected))

    assert outcome.items[0].disposition is ItemDisposition.ACCEPTED
    assert any(event.event_type == "item.resume_selected" for event in ports.records.events)


def test_a_head_whose_base_is_not_the_recorded_commit_still_parks_head_moved() -> None:
    ports = make_ports([], [], diff_count=0)
    ports.tracker.resumable = [item()]
    ports.records.markers = {"item-1": _finalised_marker("tracker-commit")}
    ports.workspace.worktrees = [
        WorktreeStatus(Path("/worktree"), "run", "integration-merge", "digest", "other-commit")
    ]

    outcome = CampaignRunner(ports).run(campaign(max_items=5), lambda selected: spec(selected))

    assert outcome.items[0].disposition is ItemDisposition.PARKED
    assert outcome.items[0].reason == "resume:head-moved"


def test_a_resumed_finalised_delivery_park_keeps_the_worktree() -> None:
    ports = make_ports([], [], diff_count=0)
    ports.tracker.resumable = [item()]
    ports.records.markers = {"item-1": _finalised_marker("tracker-commit")}
    ports.workspace.worktrees = [
        WorktreeStatus(Path("/worktree"), "run", "tracker-commit", "digest", "tracker-commit")
    ]
    ports.workspace.delivery_parks.append(
        DeliveryParked(DeliveryParkReason.CONTENDED, "the push was rejected")
    )

    outcome = CampaignRunner(ports).run(campaign(max_items=5), lambda selected: spec(selected))

    assert outcome.items[0].disposition is ItemDisposition.PARKED
    assert outcome.items[0].reason == "delivery_contended: the push was rejected"
    assert ports.workspace.released == []


def _bare_park(ports, machine: ItemStateMachine, *, delivery_park: bool):
    workflow = ItemWorkflow(ports)
    record = ports.records.create(campaign())
    return workflow._park(
        spec(),
        machine,
        _PhaseMarker(ports.records, record, "item-1"),
        record,
        None,
        [],
        _ParkGuard(),
        "reason",
        delivery_park=delivery_park,
    )


@pytest.mark.parametrize("state", (ItemState.VERIFIED, ItemState.FINALISED))
def test_a_park_from_a_state_with_no_park_transition_still_raises(state: ItemState) -> None:
    ports = make_ports([], [], diff_count=0)
    machine = ItemStateMachine.resume_at(ItemState.FINALISED)
    if state is ItemState.VERIFIED:
        machine = ItemStateMachine()
        machine.transition(ItemState.VERIFIED)

    with pytest.raises(ValueError, match="invalid item transition"):
        _bare_park(ports, machine, delivery_park=False)


def test_only_a_delivery_park_of_a_finalised_item_skips_the_transition() -> None:
    ports = make_ports([], [], diff_count=0)
    machine = ItemStateMachine.resume_at(ItemState.FINALISED)

    outcome = _bare_park(ports, machine, delivery_park=True)

    assert outcome.disposition is ItemDisposition.PARKED
    assert machine.current is ItemState.FINALISED


def test_composition_hands_one_primary_lock_to_both_trackers_and_the_workspace(
    tmp_path: Path,
) -> None:
    from types import SimpleNamespace

    from autobuild.bootstrap.composition import _repository_adapters
    from autobuild.domain import DeliveryMode

    for kind in ("pinax", "backlog"):
        settings = SimpleNamespace(
            repository=tmp_path, backlog_path=tmp_path / "BACKLOG.md", tracker_kind=kind
        )
        pinax, backlog, tracker, workspace, _ = _repository_adapters(
            settings, tmp_path / "scratch", DeliveryMode.PROTECTED_DEFAULT, False
        )

        assert pinax._primary_lock is backlog._primary_lock is workspace._primary_lock
        assert (workspace._projection is pinax) is (kind == "pinax")
        assert tracker is (pinax if kind == "pinax" else backlog)
    first = _repository_adapters(settings, tmp_path / "s1", DeliveryMode.PROTECTED_DEFAULT, False)
    second = _repository_adapters(settings, tmp_path / "s2", DeliveryMode.PROTECTED_DEFAULT, False)
    assert first[3]._primary_lock is not second[3]._primary_lock
