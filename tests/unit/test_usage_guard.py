"""Usage meter levels, the per-seat gate and the launch preflight, against fakes."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from autobuild.adapters import FilesystemLaneStateAdapter
from autobuild.adapters.claude_usage import CLAUDE_SUBSCRIPTION_METER, ClaudeUsageMeterAdapter
from autobuild.application import CampaignRunner, ItemWorkflow, Lane, WorkflowPorts
from autobuild.application.item import (
    USAGE_RETRY_ATTEMPTS,
    USAGE_RETRY_SECONDS,
    USAGE_UNAVAILABLE_SIGNATURE,
    _LaneRouter,
)
from autobuild.domain import (
    AdapterIdentity,
    BuilderReport,
    CampaignRef,
    CampaignStopReason,
    CommandResult,
    DeliveryMode,
    DiffEvidence,
    ItemDisposition,
    ItemExecutionSpec,
    LaneSignal,
    LaneSignalKind,
    LanesExhausted,
    PreflightError,
    ReviewDecision,
    ReviewFinding,
    ReviewVerdict,
    SeatOutcome,
    SeatResult,
    SeatUsage,
    ToolPolicy,
    UsageLimit,
    UsageLimitKind,
    UsagePolicy,
    UsageProbeFailure,
    UsageReading,
    WorkItem,
    WorkspaceRef,
)
from autobuild.enforcement.preflight import UsageLane, run_usage_preflight
from autobuild.testing import (
    FakeCommandAdapter,
    FakeHarnessAdapter,
    FakeKnowledgeAdapter,
    FakeLaneStateAdapter,
    FakeRunRecordAdapter,
    FakeTrackerAdapter,
    FakeUsageMeter,
    FakeWorkspaceAdapter,
)

METER = "meter-a"
SESSION_RESET = "2026-10-22T00:40:00+00:00"
WEEKLY_RESET = "2026-10-26T06:00:00+00:00"
LAUNCH = datetime(2026, 10, 21, 20, 0, tzinfo=UTC)
POLICY = UsagePolicy(warn_percent=80, stop_percent=90, five_hour_stop_percent=95)


def identity(name: str) -> AdapterIdentity:
    return AdapterIdentity(name, "1", frozenset({"test"}))


def limit(
    kind: UsageLimitKind, percent: float, resets_at: str | None = WEEKLY_RESET, scope: str | None = None
) -> UsageLimit:
    return UsageLimit(kind, percent, resets_at, scope)


def weekly(percent: float, resets_at: str | None = WEEKLY_RESET) -> UsageLimit:
    return limit(UsageLimitKind.WEEKLY_ALL, percent, resets_at)


def session(percent: float, resets_at: str | None = SESSION_RESET) -> UsageLimit:
    return limit(UsageLimitKind.SESSION, percent, resets_at)


def scoped(percent: float, scope: str = "Fable") -> UsageLimit:
    return limit(UsageLimitKind.WEEKLY_SCOPED, percent, None, scope)


def reading(*limits: UsageLimit) -> UsageReading:
    return UsageReading(METER, True, tuple(limits), fetched_at="2026-10-21T20:00:00+00:00")


def failed() -> UsageReading:
    return UsageReading.failed(METER, "meter transport failed (exit 6)")


def builder(run: str = "builder") -> SeatResult:
    return SeatResult(
        run, SeatOutcome.SUCCEEDED, BuilderReport(f"report:{run}", "built"), f"raw:{run}",
        SeatUsage(source="fake"), "start", "end",
    )


def review(decision: ReviewDecision, suffix: str = "1") -> SeatResult:
    findings = ()
    if decision is not ReviewDecision.PASS:
        findings = (ReviewFinding("finding", "material consequence", f"finding:{suffix}", True),)
    return SeatResult(
        f"review-{suffix}", SeatOutcome.SUCCEEDED,
        ReviewVerdict("item-1", decision, findings, f"verdict:{suffix}"),
        f"raw:review:{suffix}", SeatUsage(source="fake"), "start", "end",
    )


def command(suffix: str = "1") -> CommandResult:
    return CommandResult(f"command:{suffix}", 0, f"stdout:{suffix}", f"stderr:{suffix}", "start", "end")


def work_item() -> WorkItem:
    return WorkItem("item-1", "test item", "plan", ("passes",))


def spec(max_corrections: int = 2) -> ItemExecutionSpec:
    return ItemExecutionSpec(
        work_item(),
        Path("brief.md"),
        "validator",
        ("python", "-m", "pytest"),
        ToolPolicy(frozenset({"python"}), (Path("/worktree"),)),
        "builder-class",
        "reviewer-class",
        "specialist-class",
        max_corrections=max_corrections,
        delivery_mode=DeliveryMode.PROTECTED_DEFAULT,
        delivery_target_branch="main",
        delivery_target_revision="base",
    )


def campaign() -> CampaignRef:
    return CampaignRef("campaign", Path("/repo"), max_items=1)


def metered(results: list[SeatResult], scopes: dict[str, frozenset[str]] | None = None) -> FakeHarnessAdapter:
    return FakeHarnessAdapter(
        identity("metered"), scripted_results=list(results), meter_id=METER,
        meter_scopes=dict(scopes or {}),
    )


def unmetered(results: list[SeatResult]) -> FakeHarnessAdapter:
    return FakeHarnessAdapter(identity("unmetered"), scripted_results=list(results))


def lane_of(name: str, harness: FakeHarnessAdapter) -> Lane:
    return Lane(
        name,
        harness,
        meter_id=harness.meter_id,
        meter_scope=harness.meter_scope_covers if harness.meter_id else None,
    )


def ports(
    lanes: list[tuple[str, FakeHarnessAdapter]],
    meter: FakeUsageMeter | None,
    *,
    policy: UsagePolicy | None = POLICY,
    lane_state=None,
    validations: int = 1,
    queue: bool = False,
    waits: list[float] | None = None,
) -> WorkflowPorts:
    workspace_ref = WorkspaceRef(Path("/worktree"), "run", "base", "lease")
    tracker = FakeTrackerAdapter(identity("tracker"), queue=[work_item()] if queue else [])
    workspace = FakeWorkspaceAdapter(
        identity("workspace"),
        workspace=workspace_ref,
        diffs=[DiffEvidence(workspace_ref, f"revision-{i}", (), f"patch:{i}") for i in range(validations)],
    )
    recorded = waits if waits is not None else []
    return WorkflowPorts(
        tracker,
        workspace,
        lanes[0][1],
        FakeCommandAdapter(identity("command"), scripted_results=[command(str(i)) for i in range(validations)]),
        FakeRunRecordAdapter(identity("records")),
        FakeKnowledgeAdapter(identity("knowledge")),
        lanes=tuple(lane_of(name, harness) for name, harness in lanes),
        lane_state=lane_state if lane_state is not None else FakeLaneStateAdapter(identity("lanes")),
        usage_meter=meter,
        usage_policy=policy if meter is not None else None,
        usage_retry_wait=recorded.append,
    )


def run_item(workflow_ports: WorkflowPorts, item_spec: ItemExecutionSpec | None = None):
    record = workflow_ports.records.create(campaign())
    return ItemWorkflow(workflow_ports).run(campaign(), item_spec or spec(), record)


def events(workflow_ports: WorkflowPorts, event_type: str):
    return [event for event in workflow_ports.records.events if event.event_type == event_type]


def meter_of(*script: UsageReading) -> FakeUsageMeter:
    return FakeUsageMeter(identity("meter"), readings={METER: list(script)})


# -- the level rule ----------------------------------------------------------


@pytest.mark.parametrize(
    ("limits", "stops"),
    [
        ((weekly(89),), False),
        ((weekly(90),), True),
        ((weekly(100),), True),
        ((session(94),), False),
        ((session(95),), True),
        ((session(100),), True),
        ((scoped(100),), False),
    ],
)
def test_an_unscoped_limit_stops_the_lane_at_and_above_its_own_stop_level(limits, stops) -> None:
    assert POLICY.assess(reading(*limits)).stops_lane is stops


def test_a_scoped_limit_at_the_stop_level_is_a_scoped_stop_only() -> None:
    assessment = POLICY.assess(reading(weekly(10), scoped(100)))

    assert assessment.lane_stops == ()
    assert [entry.scope for entry in assessment.scoped_stops] == ["Fable"]


def test_the_latest_reset_among_the_binding_limits_wins() -> None:
    assessment = POLICY.assess(reading(weekly(92), session(96)))

    assert assessment.reset_at == WEEKLY_RESET
    assert POLICY.assess(reading(session(96), weekly(92))).reset_at == WEEKLY_RESET
    assert POLICY.assess(reading(session(96), weekly(50))).reset_at == SESSION_RESET


def test_a_binding_limit_without_a_reset_is_a_meter_failure_not_a_stop() -> None:
    assessment = POLICY.assess(reading(weekly(95, resets_at=None)))

    assert assessment.failure
    assert assessment.stops_lane is False


def test_a_failed_reading_is_a_meter_failure() -> None:
    assessment = POLICY.assess(failed())

    assert assessment.failure == "meter transport failed (exit 6)"
    assert assessment.stops_lane is False


@pytest.mark.parametrize(
    "arguments",
    [
        {"warn_percent": 0},
        {"stop_percent": 101},
        {"five_hour_stop_percent": -1},
        {"warn_percent": 95, "stop_percent": 90},
    ],
)
def test_the_policy_refuses_levels_outside_the_rule(arguments) -> None:
    with pytest.raises(ValueError):
        UsagePolicy(**arguments)


# -- no [usage] --------------------------------------------------------------


def test_without_a_usage_policy_no_meter_is_read() -> None:
    meter = meter_of(reading(weekly(100)))
    workflow_ports = ports(
        [("metered", metered([builder(), review(ReviewDecision.PASS)]))], meter, policy=None
    )

    outcome = run_item(workflow_ports)

    assert outcome.disposition is ItemDisposition.ACCEPTED
    assert meter.reads == []


def test_an_unmetered_lane_is_never_read_or_cooled_by_the_meter() -> None:
    meter = meter_of(reading(weekly(100)))
    state = FakeLaneStateAdapter(identity("lanes"))
    workflow_ports = ports(
        [("unmetered", unmetered([builder(), review(ReviewDecision.PASS)]))], meter, lane_state=state
    )

    outcome = run_item(workflow_ports)

    assert outcome.disposition is ItemDisposition.ACCEPTED
    assert meter.reads == []
    assert state.cools == []


# -- launch preflight ----------------------------------------------------------


def preflight(lanes: tuple[UsageLane, ...], meter: FakeUsageMeter, state, policy: UsagePolicy = POLICY):
    return run_usage_preflight(policy, lanes, meter=meter, lane_state=state, campaign_id="campaign")


def test_preflight_refuses_when_every_lane_is_metered_and_at_the_stop_level() -> None:
    state = FakeLaneStateAdapter(identity("lanes"))

    with pytest.raises(PreflightError) as refused:
        preflight((UsageLane("claude-code", METER),), meter_of(reading(weekly(90))), state)

    message = str(refused.value)
    assert "weekly_all" in message
    assert "90%" in message
    assert WEEKLY_RESET in message
    assert state.cools == []


def test_preflight_proceeds_one_point_below_the_stop_level() -> None:
    state = FakeLaneStateAdapter(identity("lanes"))

    probe = preflight((UsageLane("claude-code", METER),), meter_of(reading(weekly(89))), state)

    assert probe.passed
    assert state.cools == []


def test_preflight_cools_only_the_metered_lane_until_the_exact_reset_and_the_seat_runs_unmetered(
    tmp_path: Path,
) -> None:
    state = FilesystemLaneStateAdapter(tmp_path / "lanes", cool_seconds=3600, now=lambda: LAUNCH)
    meter = meter_of(reading(weekly(90)))

    probe = preflight((UsageLane("codex", None), UsageLane("claude-code", METER)), meter, state)

    assert probe.passed
    cooled = state.active("claude-code")
    assert cooled is not None and cooled.cooled_until == WEEKLY_RESET
    assert cooled.signature == LaneSignalKind.USAGE.value
    assert state.active("codex") is None

    codex = unmetered([builder(), review(ReviewDecision.PASS)])
    claude = metered([])
    workflow_ports = ports(
        [("codex", codex), ("claude-code", claude)], meter, lane_state=state, queue=True
    )
    outcome = CampaignRunner(workflow_ports).run(campaign(), lambda item: spec())

    assert [o.disposition for o in outcome.items] == [ItemDisposition.ACCEPTED]
    assert [request.seat.value for request in codex.requests] == ["builder", "reviewer"]
    assert claude.requests == []
    assert meter.reads == [METER]


def test_preflight_with_only_unmetered_lanes_reads_nothing() -> None:
    meter = meter_of(reading(weekly(100)))

    probe = preflight((UsageLane("codex", None),), meter, FakeLaneStateAdapter(identity("lanes")))

    assert probe.passed
    assert meter.reads == []


def test_preflight_meter_failure_under_allow_proceeds_and_cools_nothing() -> None:
    state = FakeLaneStateAdapter(identity("lanes"))

    probe = preflight((UsageLane("claude-code", METER),), meter_of(failed()), state)

    assert probe.passed
    assert "unavailable" in probe.detail
    assert state.cools == []


def test_preflight_meter_failure_under_refuse_refuses_the_run() -> None:
    state = FakeLaneStateAdapter(identity("lanes"))
    refuse = UsagePolicy(on_probe_failure=UsageProbeFailure.REFUSE)

    with pytest.raises(PreflightError, match="unavailable"):
        preflight(
            (UsageLane("codex", None), UsageLane("claude-code", METER)), meter_of(failed()), state, refuse
        )
    assert state.cools == []


MALFORMED_BODY_LIMITS = {
    "kind-list": {"kind": ["weekly_all"], "percent": 95, "resets_at": WEEKLY_RESET},
    "kind-object": {"kind": {"name": "weekly_all"}, "percent": 95, "resets_at": WEEKLY_RESET},
    "reset-overflow": {"kind": "weekly_all", "percent": 95, "resets_at": "9999-12-31T23:59:59-23:00"},
    "percent-nan": {"kind": "weekly_all", "percent": float("nan"), "resets_at": WEEKLY_RESET},
    "scope-text": {"kind": "weekly_scoped", "percent": 100, "resets_at": None, "scope": "Fable"},
}


def real_meter(tmp_path: Path, limits: list[dict]) -> ClaudeUsageMeterAdapter:
    """The real Claude meter adapter over a scripted command that answers with
    ``limits``, fetched at LAUNCH."""

    tmp_path.mkdir(parents=True, exist_ok=True)
    out = tmp_path / "stdout.txt"
    err = tmp_path / "stderr.txt"
    out.write_text(json.dumps({"limits": limits}), encoding="utf-8")
    err.write_text("", encoding="utf-8")
    credential = tmp_path / "credentials.json"
    credential.write_text(json.dumps({"claudeAiOauth": {"accessToken": "token"}}), encoding="utf-8")
    command = FakeCommandAdapter(
        identity("command"),
        scripted_results=[CommandResult("usage", 0, str(out), str(err), "s", "e") for _ in range(8)],
    )
    return ClaudeUsageMeterAdapter(
        command, tmp_path / "usage", credentials_path=credential, now=lambda: LAUNCH
    )


@pytest.mark.parametrize("case", sorted(MALFORMED_BODY_LIMITS))
def test_a_malformed_meter_body_at_preflight_proceeds_with_a_warning_and_cools_nothing(
    tmp_path: Path, case: str
) -> None:
    state = FakeLaneStateAdapter(identity("lanes"))
    meter = real_meter(tmp_path, [MALFORMED_BODY_LIMITS[case]])

    probe = run_usage_preflight(
        POLICY,
        (UsageLane("codex", None), UsageLane("claude-code", CLAUDE_SUBSCRIPTION_METER)),
        meter=meter,
        lane_state=state,
        campaign_id="campaign",
    )

    assert probe.passed
    assert "unavailable" in probe.detail
    assert state.cools == []


def test_a_well_formed_meter_body_at_the_stop_level_cools_the_metered_lane(tmp_path: Path) -> None:
    state = FakeLaneStateAdapter(identity("lanes"))
    meter = real_meter(tmp_path, [{"kind": "weekly_all", "percent": 95, "resets_at": WEEKLY_RESET}])

    run_usage_preflight(
        POLICY,
        (UsageLane("codex", None), UsageLane("claude-code", CLAUDE_SUBSCRIPTION_METER)),
        meter=meter,
        lane_state=state,
        campaign_id="campaign",
    )

    assert [(lane, signal.reset_at) for lane, signal, _ in state.cools] == [("claude-code", WEEKLY_RESET)]


def test_a_malformed_meter_body_before_a_seat_proceeds_with_an_event_and_cools_nothing(
    tmp_path: Path,
) -> None:
    state = FakeLaneStateAdapter(identity("lanes"))
    claude = FakeHarnessAdapter(
        identity("metered"),
        scripted_results=[builder(), review(ReviewDecision.PASS)],
        meter_id=CLAUDE_SUBSCRIPTION_METER,
    )
    workflow_ports = ports([("claude-code", claude)], None, lane_state=state)
    workflow_ports = replace(
        workflow_ports,
        usage_meter=real_meter(tmp_path, [MALFORMED_BODY_LIMITS["kind-list"]]),
        usage_policy=POLICY,
    )

    outcome = run_item(workflow_ports)

    assert outcome.disposition is ItemDisposition.ACCEPTED
    assert [event.payload["action"] for event in events(workflow_ports, "usage.meter_failed")] == [
        "allow",
        "allow",
    ]
    assert state.cools == []


def test_a_meter_that_raises_is_a_meter_failure_not_a_crash() -> None:
    class Raising:
        def read(self, meter_id: str) -> UsageReading:
            raise RuntimeError("boom")

    state = FakeLaneStateAdapter(identity("lanes"))
    workflow_ports = ports(
        [("claude-code", metered([builder(), review(ReviewDecision.PASS)]))], None, lane_state=state
    )
    workflow_ports = replace(workflow_ports, usage_meter=Raising(), usage_policy=POLICY)

    outcome = run_item(workflow_ports)

    assert outcome.disposition is ItemDisposition.ACCEPTED
    failures = events(workflow_ports, "usage.meter_failed")
    assert failures and failures[0].payload["error"] == "meter read failed: RuntimeError"
    assert state.cools == []


# -- stale reset times ---------------------------------------------------------


def test_a_binding_limit_whose_reset_is_not_after_the_reading_is_a_meter_failure() -> None:
    stale = UsageReading(
        METER, True, (weekly(95, "2026-10-21T19:59:30+00:00"),), fetched_at="2026-10-21T20:00:00+00:00"
    )
    fresh = UsageReading(
        METER, True, (weekly(95, "2026-10-21T20:00:30+00:00"),), fetched_at="2026-10-21T20:00:00+00:00"
    )

    assert POLICY.assess(stale).failure and not POLICY.assess(stale).stops_lane
    assert POLICY.assess(fresh).stops_lane and POLICY.assess(fresh).failure == ""


def test_a_stale_stop_reading_at_preflight_never_cools_for_the_default_window(tmp_path: Path) -> None:
    state = FilesystemLaneStateAdapter(tmp_path / "lanes", cool_seconds=3600, now=lambda: LAUNCH)
    stale = UsageReading(
        METER, True, (weekly(95, "2026-10-21T19:59:30+00:00"),), fetched_at=LAUNCH.isoformat()
    )

    probe = preflight((UsageLane("codex", None), UsageLane("claude-code", METER)), meter_of(stale), state)

    assert probe.passed and "unavailable" in probe.detail
    assert state.snapshot() == ()


def test_a_stale_stop_reading_before_a_seat_is_handled_as_a_meter_failure() -> None:
    state = FakeLaneStateAdapter(identity("lanes"))
    stale = UsageReading(
        METER, True, (weekly(95, "2026-10-21T19:59:30+00:00"),), fetched_at=LAUNCH.isoformat()
    )
    workflow_ports = ports(
        [("claude-code", metered([builder(), review(ReviewDecision.PASS)]))], meter_of(stale), lane_state=state
    )

    outcome = run_item(workflow_ports)

    assert outcome.disposition is ItemDisposition.ACCEPTED
    assert events(workflow_ports, "usage.meter_failed")
    assert state.cools == []


# -- crossing the stop level before a seat -----------------------------------


def test_a_stop_crossed_before_seat_two_cools_the_lane_until_the_latest_reset_and_moves_the_seat(
    tmp_path: Path,
) -> None:
    state = FilesystemLaneStateAdapter(tmp_path / "lanes", cool_seconds=3600, now=lambda: LAUNCH)
    meter = meter_of(reading(weekly(50)), reading(session(96), weekly(92)))
    claude = metered([builder()])
    codex = unmetered([review(ReviewDecision.PASS)])
    workflow_ports = ports([("claude-code", claude), ("codex", codex)], meter, lane_state=state)

    outcome = run_item(workflow_ports)

    assert outcome.disposition is ItemDisposition.ACCEPTED
    assert [request.seat.value for request in claude.requests] == ["builder"]
    assert [request.seat.value for request in codex.requests] == ["reviewer"]
    cooled = state.active("claude-code")
    assert cooled is not None and cooled.cooled_until == WEEKLY_RESET
    assert state.active("codex") is None
    stop = events(workflow_ports, "usage.stop")
    assert len(stop) == 1 and stop[0].payload["reset_at"] == WEEKLY_RESET


def test_a_stop_crossed_on_the_only_lane_parks_the_item_and_stops_the_campaign() -> None:
    meter = meter_of(reading(weekly(50)), reading(weekly(91)))
    claude = metered([builder()])
    state = FakeLaneStateAdapter(identity("lanes"))
    workflow_ports = ports([("claude-code", claude)], meter, lane_state=state, queue=True)

    outcome = CampaignRunner(workflow_ports).run(campaign(), lambda item: spec())

    assert outcome.stop_reason is CampaignStopReason.LANES_EXHAUSTED
    assert outcome.items[0].disposition is ItemDisposition.PARKED
    assert outcome.items[0].lanes_exhausted
    assert workflow_ports.tracker.parked[0][1].startswith("claude-code:usage")
    assert [lane for lane, _signal, _campaign in state.cools] == ["claude-code"]
    assert state.cools[0][1].reset_at == WEEKLY_RESET


# -- model-scoped limits -------------------------------------------------------


def test_a_scoped_limit_moves_only_the_covered_seat_and_never_cools_the_lane() -> None:
    meter = meter_of(reading(weekly(10), scoped(100)))
    claude = metered([builder("b1"), builder("b2")], {"Fable": frozenset({"reviewer-class"})})
    codex = unmetered([review(ReviewDecision.CORRECT, "1"), review(ReviewDecision.PASS, "2")])
    state = FakeLaneStateAdapter(identity("lanes"))
    workflow_ports = ports(
        [("claude-code", claude), ("codex", codex)], meter, lane_state=state, validations=2
    )

    outcome = run_item(workflow_ports)

    assert outcome.disposition is ItemDisposition.ACCEPTED
    assert [request.seat.value for request in claude.requests] == ["builder", "builder"]
    assert [request.seat.value for request in codex.requests] == ["reviewer", "reviewer"]
    assert [observation.lane for observation in outcome.seats] == [
        "claude-code", "codex", "claude-code", "codex",
    ]
    assert state.cools == []
    assert state.active("claude-code") is None
    assert len(events(workflow_ports, "usage.seat_rerouted")) == 2


def test_a_scoped_limit_parks_the_covered_seat_when_no_lane_remains() -> None:
    meter = meter_of(reading(scoped(100)))
    claude = metered([builder()], {"Fable": frozenset({"reviewer-class"})})
    state = FakeLaneStateAdapter(identity("lanes"))
    workflow_ports = ports([("claude-code", claude)], meter, lane_state=state)

    outcome = run_item(workflow_ports)

    assert outcome.disposition is ItemDisposition.PARKED
    assert outcome.lanes_exhausted
    assert outcome.lane_signature == LaneSignalKind.USAGE.value
    assert [request.seat.value for request in claude.requests] == ["builder"]
    assert state.cools == []


def test_a_scoped_limit_that_covers_no_seat_model_changes_nothing() -> None:
    meter = meter_of(reading(scoped(100, "Other")))
    claude = metered([builder(), review(ReviewDecision.PASS)], {"Fable": frozenset({"reviewer-class"})})
    workflow_ports = ports([("claude-code", claude)], meter)

    outcome = run_item(workflow_ports)

    assert outcome.disposition is ItemDisposition.ACCEPTED
    assert events(workflow_ports, "usage.seat_rerouted") == []


def test_the_router_skip_never_moves_its_position() -> None:
    first = Lane("first", unmetered([]))
    second = Lane("second", unmetered([]))
    router = _LaneRouter((first, second), FakeLaneStateAdapter(identity("lanes")), "c", lambda *_: None)

    assert router.current_lane(frozenset({"first"})) is second
    assert router.current_lane() is first
    with pytest.raises(LanesExhausted) as exhausted:
        router.current_lane(frozenset({"first", "second"}))
    assert exhausted.value.signature == LaneSignalKind.USAGE.value


def test_cooling_a_lane_reached_past_a_skipped_lane_keeps_the_skipped_lane_for_later_seats() -> None:
    lanes = tuple(Lane(name, unmetered([])) for name in ("first", "second", "third"))
    router = _LaneRouter(lanes, FakeLaneStateAdapter(identity("lanes")), "c", lambda *_: None)

    second = router.current_lane(frozenset({"first"}))
    router.signal(second, LaneSignal(LaneSignalKind.USAGE, reset_at=WEEKLY_RESET))

    assert router.current_lane(frozenset({"first"})).name == "third"
    assert router.current_lane().name == "first"


# -- warnings and meter failure ------------------------------------------------


def test_the_warning_level_emits_one_event_per_crossing() -> None:
    meter = meter_of(reading(weekly(81)), reading(weekly(82)), reading(weekly(70)), reading(weekly(85)))
    claude = metered(
        [builder("b1"), review(ReviewDecision.CORRECT, "1"), builder("b2"), review(ReviewDecision.PASS, "2")]
    )
    workflow_ports = ports([("claude-code", claude)], meter, validations=2)

    outcome = run_item(workflow_ports)

    assert outcome.disposition is ItemDisposition.ACCEPTED
    warnings = events(workflow_ports, "usage.warning")
    assert [event.payload["percent"] for event in warnings] == [81, 85]
    assert all(event.payload["warn_percent"] == 80 for event in warnings)


def test_a_meter_failure_under_allow_proceeds_emits_an_event_and_cools_nothing() -> None:
    state = FakeLaneStateAdapter(identity("lanes"))
    waits: list[float] = []
    workflow_ports = ports(
        [("claude-code", metered([builder(), review(ReviewDecision.PASS)]))],
        meter_of(failed()),
        lane_state=state,
        waits=waits,
    )

    outcome = run_item(workflow_ports)

    assert outcome.disposition is ItemDisposition.ACCEPTED
    failures = events(workflow_ports, "usage.meter_failed")
    assert [event.payload["action"] for event in failures] == ["allow", "allow"]
    assert state.cools == []
    assert waits == []


def test_a_meter_failure_under_refuse_retries_the_seat_within_300_seconds_without_cooling() -> None:
    state = FakeLaneStateAdapter(identity("lanes"))
    waits: list[float] = []
    claude = metered([builder(), review(ReviewDecision.PASS)])
    workflow_ports = ports(
        [("claude-code", claude)],
        meter_of(failed(), reading(weekly(10))),
        policy=UsagePolicy(on_probe_failure=UsageProbeFailure.REFUSE),
        lane_state=state,
        waits=waits,
    )

    outcome = run_item(workflow_ports)

    assert outcome.disposition is ItemDisposition.ACCEPTED
    assert waits == [USAGE_RETRY_SECONDS]
    assert USAGE_RETRY_SECONDS <= 300
    assert [request.seat.value for request in claude.requests] == ["builder", "reviewer"]
    assert state.cools == []


def test_a_meter_that_stays_unavailable_under_refuse_ends_the_item_without_cooling() -> None:
    state = FakeLaneStateAdapter(identity("lanes"))
    waits: list[float] = []
    claude = metered([])
    workflow_ports = ports(
        [("claude-code", claude)],
        meter_of(failed()),
        policy=UsagePolicy(on_probe_failure=UsageProbeFailure.REFUSE),
        lane_state=state,
        waits=waits,
    )

    outcome = run_item(workflow_ports)

    assert outcome.disposition is ItemDisposition.PARKED
    assert outcome.lane_signature == USAGE_UNAVAILABLE_SIGNATURE
    assert waits == [USAGE_RETRY_SECONDS] * USAGE_RETRY_ATTEMPTS
    assert claude.requests == []
    assert state.cools == []
