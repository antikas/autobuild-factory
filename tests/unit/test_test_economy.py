"""Test economy: focused builder tests, delivery revalidation on overlap only,
batch validation of the target head, a patient validator under pressure and
seat time limits that stretch under pressure.

Every case runs on fakes with a fake clock; no test sleeps or reads wall time."""

from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path

import pytest
from test_workflows import builder, campaign, command, item, make_ports, review, spec

from autobuild.application import CampaignRunner, ItemWorkflow
from autobuild.application.item import HeavySlots
from autobuild.application.prompts import render_seat_instructions
from autobuild.domain import (
    CampaignContext,
    CommandResult,
    DeliveryMode,
    FinaliseResult,
    ItemDisposition,
    ItemOutcome,
    ItemState,
    ReviewDecision,
    Seat,
    SeatRequest,
    WorkItem,
)
from autobuild.domain.models import BatchValidationPolicy, HoldCondition
from autobuild.enforcement.integration import package_key, revalidation_decision
from autobuild.enforcement.policy import ApprovedValidator, EnforcedCommandPort, PolicyConfig
from autobuild.enforcement.pressure import stretched_seat_cap
from autobuild.testing import FakeClock

# --- focused builder tests --------------------------------------------------------


def test_the_builder_prompt_carries_the_focused_test_constraint() -> None:
    work = spec()
    instructions = render_seat_instructions(work, Seat.BUILDER, ())

    assert "Constraint (focused tests):" in instructions
    assert "run only the tests that cover the files you changed" in instructions
    assert "Never run the repository's full test suite or the validator command" in instructions
    assert " ".join(work.validator_argv) not in instructions
    assert "pytest" not in instructions


def test_the_reviewer_prompt_does_not_carry_the_builder_constraint() -> None:
    instructions = render_seat_instructions(spec(), Seat.REVIEWER, ())

    assert "Constraint (focused tests):" not in instructions


# --- the revalidation rule ----------------------------------------------------------


@pytest.mark.parametrize(
    "path, key",
    [
        ("apps/web/src/page.ts", "apps/web"),
        ("packages/ui/button.tsx", "packages/ui"),
        ("src/autobuild/application/item.py", "src"),
        ("tests/unit/test_x.py", "tests"),
        ("lib.txt", ""),
        ("apps\\api\\main.py", "apps/api"),
        ("./src/x.py", "src"),
        ("tests/**", "tests"),
        ("apps/*/src/**", "apps"),
    ],
)
def test_the_package_key_of_a_path(path: str, key: str) -> None:
    assert package_key(path) == key


def test_an_incoming_change_in_the_same_package_revalidates() -> None:
    decision = revalidation_decision(
        incoming=("apps/web/other.ts",), own=("apps/web/feature.ts",), declared=()
    )

    assert decision.run is True
    assert decision.reason == "overlap"
    assert decision.overlapping == ("apps/web/other.ts",)


def test_an_incoming_change_in_another_package_does_not_revalidate() -> None:
    decision = revalidation_decision(
        incoming=("apps/api/other.ts",), own=("apps/web/feature.ts",), declared=()
    )

    assert decision.run is False
    assert decision.reason == "no_overlap"
    assert decision.incoming == ("apps/api/other.ts",)
    assert decision.overlapping == ()


def test_root_files_share_the_root_package() -> None:
    assert revalidation_decision(("lib.txt",), ("feature.txt",), ()).run is True


def test_a_declared_path_counts_as_the_items_own() -> None:
    decision = revalidation_decision(("src/b.py",), ("apps/web/x.ts",), ("src/a.py",))

    assert decision.run is True
    assert decision.overlapping == ("src/b.py",)


@pytest.mark.parametrize(
    "incoming, own, declared",
    [
        (("apps/api/b.ts",), ("apps/web/a.ts",), ("**/*.ts",)),
        (("src/b.py",), ("tests/test_a.py",), ("*.py",)),
        (("src/b.py",), ("tests/a.py",), ("src",)),
        (("src/deep/b.py",), ("tests/a.py",), ("src/",)),
        (("apps/api/src/b.ts",), ("apps/web/a.ts",), ("apps/*/src/**",)),
    ],
)
def test_a_declared_glob_or_directory_covers_the_paths_it_matches(incoming, own, declared) -> None:
    decision = revalidation_decision(incoming, own, declared)

    assert (decision.run, decision.reason) == (True, "overlap")
    assert decision.overlapping == incoming


def test_a_declared_glob_does_not_cover_what_it_does_not_match() -> None:
    decision = revalidation_decision(("lib/b.go",), ("tests/a.py",), ("src/**/*.py", "src"))

    assert decision.run is False


@pytest.mark.parametrize(
    "root_file, own",
    [
        ("pyproject.toml", "src/autobuild/x.py"),
        ("uv.lock", "src/autobuild/x.py"),
        ("package.json", "apps/web/src/a.ts"),
        ("pnpm-lock.yaml", "packages/ui/a.ts"),
        ("conftest.py", "tests/unit/test_a.py"),
        ("tsconfig.base.json", "packages/ui/a.ts"),
    ],
)
def test_a_root_manifest_or_configuration_change_overlaps_every_scope(root_file, own) -> None:
    decision = revalidation_decision((root_file,), (own,), ())

    assert (decision.run, decision.reason) == (True, "overlap")
    assert decision.overlapping == (root_file,)


def test_a_root_markdown_change_still_never_revalidates() -> None:
    assert revalidation_decision(("CHANGELOG.md",), ("src/a.py",), ()).run is False


def test_a_declared_glob_counts_by_its_package() -> None:
    assert revalidation_decision(("tests/unit/t.py",), ("src/a.py",), ("tests/**",)).run is True


@pytest.mark.parametrize(
    "incoming",
    [(".ergon/events.jsonl",), ("docs/guide.txt",), ("src/README.md",), ("docs/a.md", "x.md")],
)
def test_tracker_docs_and_markdown_never_revalidate(incoming: tuple[str, ...]) -> None:
    decision = revalidation_decision(incoming, ("src/a.py", "x.md"), ("docs/**",))

    assert decision.run is False
    assert decision.reason == "docs_only"


def test_no_incoming_change_never_revalidates() -> None:
    decision = revalidation_decision((), ("src/a.py",), ())

    assert (decision.run, decision.reason) == (False, "no_product_change")


def test_an_item_with_no_known_paths_revalidates_on_any_product_change() -> None:
    decision = revalidation_decision(("src/a.py",), (), ())

    assert (decision.run, decision.reason) == (True, "unknown_scope")


# --- seat stretch ------------------------------------------------------------------


@pytest.mark.parametrize(
    "pressured, cap",
    [(0.0, 3600.0), (1800.0, 5400.0), (3600.0, 7200.0), (9000.0, 7200.0), (-5.0, 3600.0)],
)
def test_the_stretched_seat_cap(pressured: float, cap: float) -> None:
    assert stretched_seat_cap(3600.0, pressured) == cap


def test_heavy_slots_count_the_pressured_seconds_inside_a_span() -> None:
    slots = HeavySlots()
    slots.withhold(True, at=100.0)
    slots.withhold(False, at=400.0)
    slots.withhold(True, at=1000.0)

    assert slots.pressured_seconds(0.0, 50.0) == 0.0
    assert slots.pressured_seconds(0.0, 500.0) == 300.0
    assert slots.pressured_seconds(200.0, 1100.0) == 300.0
    assert slots.pressured_within(460.0, 60.0) is True
    assert slots.pressured_within(461.0, 60.0) is False
    assert slots.pressured_within(1001.0, 60.0) is True


def _stretch_case(script) -> tuple[list[float], list[dict]]:
    """Run one item whose builder seat follows ``script`` on the fake clock and
    returns the effective caps the seat read and the seat payloads."""

    clock = FakeClock()
    ports = make_ports([builder(), review(ReviewDecision.PASS)], [command()])
    ports = replace(ports, schedule_clock=clock)
    slots = HeavySlots()
    items = ItemWorkflow(ports)
    items.use_heavy_slots(slots)
    caps: list[float] = []

    def on_invoke(request: SeatRequest) -> None:
        if request.seat is Seat.BUILDER:
            script(clock, slots, request, caps)

    ports.harness.on_invoke = on_invoke
    work = replace(spec(), seat_timeout_seconds=3600.0)
    outcome = items.run(campaign(), work, ports.records.create(campaign()))
    assert outcome.disposition is ItemDisposition.ACCEPTED, outcome.reason
    payloads = [
        event.payload for event in ports.records.events if event.event_type == "seat.completed"
    ]
    return caps, payloads


def test_a_seat_running_1800_seconds_at_high_gets_5400_and_is_not_timed_out_at_5000() -> None:
    def script(clock, slots, request, caps) -> None:
        start = clock.current
        slots.withhold(True, at=start)
        clock.set(start + 1800.0)
        slots.withhold(False, at=start + 1800.0)
        clock.set(start + 5000.0)
        caps.append(request.current_timeout_seconds())

    caps, payloads = _stretch_case(script)

    assert caps == [5400.0]
    assert not 5000.0 > caps[0]
    assert payloads[0]["timeout_extension_seconds"] == 1800.0
    assert "timeout_extension_seconds" not in payloads[1]


def test_a_seat_under_pressure_for_its_whole_run_stops_at_7200() -> None:
    def script(clock, slots, request, caps) -> None:
        start = clock.current
        slots.withhold(True, at=start)
        clock.set(start + 5000.0)
        caps.append(request.current_timeout_seconds())
        clock.set(start + 7300.0)
        caps.append(request.current_timeout_seconds())
        # The span stays open; only new heavy commands are let start again, so
        # the item's validator can run after the seat.
        slots._withheld = False  # noqa: SLF001

    caps, payloads = _stretch_case(script)

    assert caps == [7200.0, 7200.0]
    assert payloads[0]["timeout_extension_seconds"] == 3600.0


def test_a_seat_without_pressure_keeps_its_class_cap() -> None:
    def script(clock, slots, request, caps) -> None:
        clock.set(clock.current + 5000.0)
        caps.append(request.current_timeout_seconds())

    caps, payloads = _stretch_case(script)

    assert caps == [3600.0]
    assert all("timeout_extension_seconds" not in payload for payload in payloads)


# --- patient validator ---------------------------------------------------------------


def _validator_case(pressure: str, patient_argv: tuple[str, ...] = ()):
    clock = FakeClock(current=1000.0)
    ports = make_ports([builder(), review(ReviewDecision.PASS)], [command()])
    ports = replace(ports, schedule_clock=clock)
    slots = HeavySlots()
    if pressure == "high":
        # The monitor published high and the validator starts while it holds;
        # the fake slots let it start so the level at its start is high.
        slots.withhold(True, at=990.0)
        slots._withheld = False  # noqa: SLF001 - the level stays high on record
    elif pressure == "recent":
        slots.withhold(True, at=900.0)
        slots.withhold(False, at=950.0)
    elif pressure == "old":
        slots.withhold(True, at=800.0)
        slots.withhold(False, at=939.0)
    items = ItemWorkflow(ports)
    items.use_heavy_slots(slots)
    work = replace(spec(), validator_patient_argv=patient_argv)
    outcome = items.run(campaign(), work, ports.records.create(campaign()))
    assert outcome.disposition is ItemDisposition.ACCEPTED, outcome.reason
    request = ports.command.requests[0]
    completed = next(
        event for event in ports.records.events if event.event_type == "validation.completed"
    )
    return request, completed.payload, work


def test_at_high_the_validator_receives_the_patient_argv() -> None:
    request, payload, _work = _validator_case("high", ("python", "-m", "pytest", "-p", "serial"))

    assert request.argv == ("python", "-m", "pytest", "-p", "serial")
    assert ("AUTOBUILD_HOST_PRESSURE", "1") not in request.environment
    assert payload["patient"] is True


def test_at_high_without_patient_argv_the_validator_gets_the_pressure_variable() -> None:
    request, payload, work = _validator_case("high")

    assert request.argv == work.validator_argv
    assert ("AUTOBUILD_HOST_PRESSURE", "1") in request.environment
    assert payload["patient"] is True


def test_high_in_the_previous_60_seconds_makes_the_validator_patient() -> None:
    request, _payload, _work = _validator_case("recent")

    assert ("AUTOBUILD_HOST_PRESSURE", "1") in request.environment


@pytest.mark.parametrize("pressure", ["normal", "old"])
def test_at_normal_neither_patient_form_applies(pressure: str) -> None:
    request, payload, work = _validator_case(pressure, ("python", "-m", "pytest", "-p", "serial"))

    assert request.argv == work.validator_argv
    assert ("AUTOBUILD_HOST_PRESSURE", "1") not in request.environment
    assert "patient" not in payload


def test_the_monitor_stamps_the_level_with_its_clock() -> None:
    from autobuild.application.campaign import _PressureMonitor
    from autobuild.domain import HostReading
    from autobuild.domain.models import GIB, PressurePolicy

    clock = FakeClock(current=500.0)
    slots = HeavySlots()
    monitor = _PressureMonitor(
        None, PressurePolicy(), 4, clock, lambda event, seconds: True, slots,
        lambda event: None, lambda: None,
    )
    monitor._observe(
        HostReading(
            logical_cpus=8,
            cpu_busy_percent=10.0,
            memory_total_bytes=32 * GIB,
            memory_available_bytes=1 * GIB,
            commit_total_bytes=None,
            commit_available_bytes=None,
        )
    )

    assert slots.withheld() is True
    assert slots.pressured_within(530.0, 60.0) is True
    assert slots.pressured_seconds(500.0, 530.0) == 30.0


# --- batch validation ------------------------------------------------------------------


class _Deliveries:
    """An item workflow that delivers every item at once with its own merge
    commit. ``before`` runs first with the item id."""

    def __init__(self, before=None) -> None:
        self.ran: list[str] = []
        self._before = before

    def run(self, campaign_ref, work, record) -> ItemOutcome:
        if self._before is not None:
            self._before(work.item.item_id)
        self.ran.append(work.item.item_id)
        number = len(self.ran)
        return ItemOutcome(
            work.item.item_id,
            ItemDisposition.ACCEPTED,
            (ItemState.RELEASED,),
            finalise=FinaliseResult(f"item-{number}", f"tracker-{number}", f"merge-{number}", True),
            title=work.item.title,
        )


def _batch_context(every: int = 5, **changes) -> CampaignContext:
    return CampaignContext(
        harness="",
        models={},
        delivery_mode=DeliveryMode.PROTECTED_DEFAULT,
        validator_id="tests",
        target_branch="main",
        target_revision="base",
        batch=BatchValidationPolicy(
            every=every, argv=("python", "-m", "pytest"), validator_id="tests"
        ),
        **changes,
    )


def _batch_ports(count: int, results: list[CommandResult]):
    ports = make_ports([], results, diff_count=0)
    ports.tracker.queue.extend(
        WorkItem(f"item-{n}", f"item {n}", "plan", ("passes",)) for n in range(1, count + 1)
    )
    return ports


def _batch_events(ports, event_type: str) -> list[dict]:
    return [event.payload for event in ports.records.events if event.event_type == event_type]


def test_six_deliveries_validate_the_target_head_after_the_fifth_and_at_the_end() -> None:
    ports = _batch_ports(6, [command(suffix="b1"), command(suffix="b2")])
    ports.workspace.validation_revisions = ["head-5", "head-6"]

    outcome = CampaignRunner(ports, items=_Deliveries()).run(
        campaign(max_items=6), lambda selected: spec(selected), context=_batch_context()
    )

    assert [entry.disposition for entry in outcome.items] == [ItemDisposition.ACCEPTED] * 6
    roots = [checkout.root for checkout in ports.workspace.validation_checkouts]
    assert [request.cwd for request in ports.command.requests] == roots
    assert len(ports.command.requests) == 2
    assert all(request.argv == ("python", "-m", "pytest") for request in ports.command.requests)
    assert ports.workspace.closed_checkouts == roots
    validated = _batch_events(ports, "campaign.batch_validated")
    assert [(entry["revision"], entry["deliveries"], entry["final"]) for entry in validated] == [
        ("head-5", 5, False),
        ("head-6", 6, True),
    ]
    assert validated[0]["commits"] == [f"merge-{n}" for n in range(1, 6)]
    assert validated[1]["commits"] == ["merge-6"]


def test_five_deliveries_validate_the_head_once_and_not_again_at_the_end() -> None:
    ports = _batch_ports(5, [command(suffix="b1")])

    CampaignRunner(ports, items=_Deliveries()).run(
        campaign(max_items=5), lambda selected: spec(selected), context=_batch_context()
    )

    assert len(ports.command.requests) == 1


def test_a_batch_validation_passes_the_enforced_command_port() -> None:
    # The live port is the policy-wrapped one; it admits a validator command
    # only when the command id ends with the approved validator id.
    ports = _batch_ports(5, [command(suffix="b1")])
    fake = ports.command
    config = PolicyConfig(
        allowed_roots=(Path("/fake/validation"),),
        approved_validators=(ApprovedValidator("tests", ("python", "-m", "pytest")),),
        allowed_tools=frozenset({"python"}),
    )
    ports = replace(ports, command=EnforcedCommandPort(fake, config))

    CampaignRunner(ports, items=_Deliveries()).run(
        campaign(max_items=5), lambda selected: spec(selected), context=_batch_context()
    )

    assert len(fake.requests) == 1
    assert fake.requests[0].command_id.endswith(":tests")
    assert ":batch:1:" in fake.requests[0].command_id
    assert _batch_events(ports, "campaign.batch_validation_failed") == []
    assert [entry["revision"] for entry in _batch_events(ports, "campaign.batch_validated")] == [
        "head"
    ]


def test_a_batch_policy_without_a_validator_id_is_refused_at_construction() -> None:
    with pytest.raises(TypeError, match="validator_id"):
        BatchValidationPolicy(every=1, argv=("python", "-m", "pytest"))  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="validator id"):
        BatchValidationPolicy(every=1, argv=("python", "-m", "pytest"), validator_id=" ")


def test_without_a_batch_policy_no_batch_runs() -> None:
    ports = _batch_ports(6, [])

    CampaignRunner(ports, items=_Deliveries()).run(
        campaign(max_items=6), lambda selected: spec(selected)
    )

    assert ports.command.requests == []
    assert ports.workspace.validation_checkouts == []


def _wait_for(predicate, what: str) -> None:
    deadline = threading.Event()
    for _ in range(500):
        if predicate():
            return
        deadline.wait(0.01)
    raise AssertionError(f"timed out waiting for {what}")


def test_a_fix_pushed_to_origin_releases_the_hold_with_nothing_in_flight() -> None:
    clock = FakeClock(current=0.0)

    def hold_wait(stop: threading.Event, seconds: float) -> bool:
        clock.advance(seconds)
        return stop.is_set()

    holder: dict[str, object] = {}
    seen_at_run: dict[str, list[str]] = {}

    def before(item_id: str) -> None:
        ports = holder["ports"]
        if item_id == "item-3":
            _wait_for(
                lambda: any(
                    event.event_type == "campaign.hold_started"
                    for event in list(ports.records.events)
                ),
                "the batch-failure hold",
            )
        seen_at_run[item_id] = [event.event_type for event in list(ports.records.events)]

    ports = _batch_ports(
        4, [command(1, suffix="b1"), command(0, suffix="b2"), command(0, suffix="b3")]
    )
    ports = replace(ports, schedule_clock=clock, hold_wait=hold_wait)
    holder["ports"] = ports
    # The local head never moves; only the remote gains the operator's fix.
    ports.workspace.validation_revisions = ["head-2"]
    items = _Deliveries(before)

    def deliver_then_fix(campaign_ref, work, record):
        outcome = _Deliveries.run(items, campaign_ref, work, record)
        if work.item.item_id == "item-3":
            ports.workspace.remote_head = "head-fixed"
        return outcome

    items.run = deliver_then_fix
    CampaignRunner(ports, items=items).run(
        campaign(max_items=4),
        lambda selected: spec(selected),
        context=_batch_context(every=2, hold_limit_seconds=1e9),
    )

    assert [entry["condition"] for entry in _batch_events(ports, "campaign.hold_released")] == [
        "batch_validation_failed"
    ]
    assert "campaign.hold_released" in seen_at_run["item-4"]
    assert _batch_events(ports, "campaign.batch_validated")[0]["revision"] == "head-fixed"
    # Triggered batches read the local head; only the hold's probe refreshes.
    assert ports.workspace.refreshes[0] is False
    assert True in ports.workspace.refreshes[1:]


def test_a_failing_batch_records_the_commits_holds_admissions_and_reverts_nothing() -> None:
    clock = FakeClock(current=0.0)

    def hold_wait(stop: threading.Event, seconds: float) -> bool:
        clock.advance(seconds)
        return stop.is_set()

    third_started = threading.Event()
    seen_at_run: dict[str, list[str]] = {}
    holder: dict[str, object] = {}

    def before(item_id: str) -> None:
        ports = holder["ports"]
        if item_id == "item-3":
            third_started.set()
            # The failing batch result is recorded while this item still runs.
            _wait_for(
                lambda: any(
                    event.event_type == "campaign.batch_validation_failed"
                    for event in list(ports.records.events)
                ),
                "the batch failure",
            )
        seen_at_run[item_id] = [event.event_type for event in list(ports.records.events)]

    ports = _batch_ports(
        4, [command(1, suffix="b1"), command(0, suffix="b2"), command(0, suffix="b3")]
    )
    ports = replace(ports, schedule_clock=clock, hold_wait=hold_wait)
    holder["ports"] = ports

    def on_run(request) -> None:
        if len(ports.command.requests) == 1:
            assert third_started.wait(5)

    ports.command.on_run = on_run
    # The failing head stays until an operator lands a fix, which moves it.
    ports.workspace.validation_revisions = ["head-2", "head-2", "head-fixed", "head-4"]
    items = _Deliveries(before)

    outcome = CampaignRunner(ports, items=items).run(
        campaign(max_items=4),
        lambda selected: spec(selected),
        context=_batch_context(every=2, hold_limit_seconds=1e9),
    )

    failed = _batch_events(ports, "campaign.batch_validation_failed")
    assert len(failed) == 1
    assert failed[0]["revision"] == "head-2"
    assert failed[0]["commits"] == ["merge-1", "merge-2"]
    assert failed[0]["exit_code"] == 1
    held = _batch_events(ports, "campaign.hold_started")
    assert [entry["condition"] for entry in held] == [HoldCondition.BATCH_VALIDATION_FAILED.value]
    released = _batch_events(ports, "campaign.hold_released")
    assert [entry["condition"] for entry in released] == ["batch_validation_failed"]
    # Admissions stayed held until a later batch passed.
    assert "campaign.hold_released" in seen_at_run["item-4"]
    assert items.ran == ["item-1", "item-2", "item-3", "item-4"]
    # The failing head is checked once by the hold and never validated again;
    # the moved head is validated and passes.
    assert [checkout.revision for checkout in ports.workspace.validation_checkouts] == [
        "head-2",
        "head-2",
        "head-fixed",
        "head-4",
    ]
    assert [request.cwd for request in ports.command.requests] == [
        ports.workspace.validation_checkouts[index].root for index in (0, 2, 3)
    ]
    passed = _batch_events(ports, "campaign.batch_validated")
    assert passed[0]["revision"] == "head-fixed"
    # The moved head may be validated before or after item 3 is delivered.
    assert passed[0]["commits"][:2] == ["merge-1", "merge-2"]
    assert "merge-4" in passed[-1]["commits"]
    # Nothing is reverted and the campaign is not ended by the failure.
    assert outcome.stop_reason.value == "item_bound"
    assert [entry.disposition for entry in outcome.items] == [ItemDisposition.ACCEPTED] * 4
    assert ports.workspace.delivered == []

