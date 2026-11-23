"""Usage meter levels, the per-seat gate and the launch preflight, against fakes."""

from __future__ import annotations

import json
import threading
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
    CampaignContext,
    CampaignRef,
    CampaignStopReason,
    CommandResult,
    DeliveryMode,
    DiffEvidence,
    ItemDisposition,
    ItemExecutionSpec,
    LaneCooling,
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
    FakeClock,
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
        # Every meter check sees a reading older than its 60 seconds, so each
        # seat reads the meter afresh as these tests script.
        usage_clock=FakeClock(step=61.0),
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


def test_a_stop_crossed_on_the_only_lane_parks_the_item_and_ends_the_campaign_usage_stop() -> None:
    meter = meter_of(reading(weekly(50)), reading(weekly(91)))
    claude = metered([builder()])
    state = FakeLaneStateAdapter(identity("lanes"))
    workflow_ports = ports([("claude-code", claude)], meter, lane_state=state, queue=True)

    outcome = CampaignRunner(workflow_ports).run(campaign(), lambda item: spec())

    # Every lane stopped for usage ends the campaign instead of holding.
    assert outcome.stop_reason is CampaignStopReason.USAGE_STOP
    assert events(workflow_ports, "campaign.hold_started") == []
    assert outcome.items[0].disposition is ItemDisposition.PARKED
    assert outcome.items[0].lanes_exhausted
    assert workflow_ports.tracker.parked[0][1].startswith("claude-code:usage")
    assert [lane for lane, _signal, _campaign in state.cools] == ["claude-code"]
    assert state.cools[0][1].reset_at == WEEKLY_RESET


# -- a campaign whose every lane is stopped for usage ---------------------------

OTHER_CAMPAIGN = "autobuild-other"
TWO_DAYS_AHEAD = "2026-10-23T20:00:00+00:00"


def campaign_item(item_id: str) -> WorkItem:
    return WorkItem(item_id, f"title {item_id}", "plan", ("passes",))


def campaign_spec(item: WorkItem) -> ItemExecutionSpec:
    return replace(spec(), item=item)


def campaign_builder(item_id: str) -> SeatResult:
    return SeatResult(
        f"{item_id}:build", SeatOutcome.SUCCEEDED, BuilderReport(f"report:{item_id}", "built"),
        f"raw:{item_id}:build", SeatUsage(source="fake"), "start", "end",
    )


def campaign_review(item_id: str) -> SeatResult:
    return SeatResult(
        f"{item_id}:review", SeatOutcome.SUCCEEDED,
        ReviewVerdict(item_id, ReviewDecision.PASS, (), f"verdict:{item_id}"),
        f"raw:{item_id}:review", SeatUsage(source="fake"), "start", "end",
    )


def campaign_harness(name: str, item_ids: tuple[str, ...], *, metered_lane: bool = True, on_invoke=None):
    return FakeHarnessAdapter(
        identity(name),
        results_by_item={
            item_id: [campaign_builder(item_id), campaign_review(item_id)] for item_id in item_ids
        },
        meter_id=METER if metered_lane else None,
        on_invoke=on_invoke,
    )


def campaign_ports(
    lanes: list[tuple[str, FakeHarnessAdapter]],
    meter: FakeUsageMeter,
    state: FakeLaneStateAdapter,
    item_ids: tuple[str, ...],
) -> WorkflowPorts:
    tracker = FakeTrackerAdapter(
        identity("tracker"),
        queue=[campaign_item(i) for i in item_ids],
        ready=[campaign_item(i) for i in item_ids],
    )

    def workspace_of(item_id: str) -> WorkspaceRef:
        return WorkspaceRef(Path(f"/worktrees/{item_id}"), f"autobuild/{item_id}", "base", f"lease-{item_id}")

    workspace = FakeWorkspaceAdapter(
        identity("workspace"),
        workspaces_by_item={item_id: workspace_of(item_id) for item_id in item_ids},
        diffs_by_item={
            item_id: [DiffEvidence(workspace_of(item_id), f"revision:{item_id}", (), f"patch:{item_id}")]
            for item_id in item_ids
        },
        on_deliver=lambda item_id: tracker.mark_done(item_id) if item_id else None,
    )
    commands = FakeCommandAdapter(
        identity("command"),
        results_by_item={item_id: [command(item_id)] for item_id in item_ids},
    )
    return WorkflowPorts(
        tracker,
        workspace,
        lanes[0][1],
        commands,
        FakeRunRecordAdapter(identity("records")),
        FakeKnowledgeAdapter(identity("knowledge")),
        lanes=tuple(lane_of(name, harness) for name, harness in lanes),
        lane_state=state,
        usage_meter=meter,
        usage_policy=POLICY,
        usage_retry_wait=lambda seconds: None,
        usage_clock=FakeClock(step=61.0),
        # Every schedule reading moves an hour on, so a hold outlasts the
        # small hold limit at its first decision instead of waiting for it.
        schedule_clock=FakeClock(step=3600.0),
    )


def run_campaign(workflow_ports: WorkflowPorts, *, concurrency: int = 1, max_items: int = 3):
    context = CampaignContext(
        harness="fake",
        models={"builder": "b", "reviewer": "r"},
        delivery_mode=DeliveryMode.PROTECTED_DEFAULT,
        validator_id="validator",
        target_branch="main",
        target_revision="base",
        hold_limit_seconds=60.0,
    )
    return CampaignRunner(workflow_ports).run(
        CampaignRef("campaign", Path("/repo"), max_items=max_items, concurrency=concurrency),
        campaign_spec,
        context=context,
    )


def usage_cooling(lane: str, campaign_id: str = OTHER_CAMPAIGN, until: str = TWO_DAYS_AHEAD) -> LaneCooling:
    return LaneCooling(lane, until, LaneSignalKind.USAGE.value, "2026-10-21T19:00:00+00:00", campaign_id)


def test_a_stop_before_item_two_ends_the_campaign_usage_stop_without_a_hold() -> None:
    ids = ("item-1", "item-2", "item-3")
    meter = meter_of(reading(weekly(50)), reading(weekly(50)), reading(weekly(91)))
    claude = campaign_harness("claude", ids)
    state = FakeLaneStateAdapter(identity("lanes"))
    workflow_ports = campaign_ports([("claude-code", claude)], meter, state, ids)

    outcome = run_campaign(workflow_ports)

    by_id = {entry.item_id: entry for entry in outcome.items}
    assert by_id["item-1"].disposition is ItemDisposition.ACCEPTED
    assert by_id["item-2"].disposition is ItemDisposition.PARKED
    assert by_id["item-2"].lane_signature == LaneSignalKind.USAGE.value
    assert "item-3" not in by_id
    assert [item_id for item_id, _actor in workflow_ports.tracker.claims] == ["item-1", "item-2"]
    assert outcome.stop_reason is CampaignStopReason.USAGE_STOP
    stopped = events(workflow_ports, "campaign.usage_stop")
    assert len(stopped) == 1
    assert stopped[0].payload["resets_at"] == WEEKLY_RESET
    assert stopped[0].payload["lanes"] == [
        {"lane": "claude-code", "cooled_until": WEEKLY_RESET, "signature": "usage"}
    ]
    assert events(workflow_ports, "campaign.hold_started") == []
    completed = events(workflow_ports, "campaign.completed")[0].payload
    assert completed["stop_reason"] == "usage_stop"
    report = workflow_ports.workspace.reports[-1].content
    next_section = report.split("## Next\n\n", 1)[1].split("\n", 1)[0]
    assert "usage_stop" in next_section and WEEKLY_RESET in next_section


def test_a_usage_cooling_beside_a_rate_limit_cooling_holds_lanes_exhausted_as_before() -> None:
    ids = ("item-1",)
    meter = meter_of(reading(weekly(91)))
    claude = campaign_harness("claude", ids)
    codex = campaign_harness("codex", ids, metered_lane=False)
    state = FakeLaneStateAdapter(identity("lanes"))
    state.cooled["claude-code"] = usage_cooling("claude-code")
    state.cooled["codex"] = LaneCooling(
        "codex", TWO_DAYS_AHEAD, LaneSignalKind.RATE_LIMIT.value, "earlier", OTHER_CAMPAIGN
    )
    workflow_ports = campaign_ports([("claude-code", claude), ("codex", codex)], meter, state, ids)

    outcome = run_campaign(workflow_ports, max_items=1)

    assert outcome.stop_reason is CampaignStopReason.HOLD_LIMIT
    assert [event.payload["condition"] for event in events(workflow_ports, "campaign.hold_started")] == [
        "lanes_exhausted"
    ]
    assert events(workflow_ports, "campaign.usage_stop") == []
    assert state.active("claude-code") == usage_cooling("claude-code")
    assert workflow_ports.tracker.claims == []


def test_both_lanes_stopped_for_usage_while_an_item_runs_drain_it_and_end_usage_stop() -> None:
    ids = ("item-1", "item-2", "item-3")
    second_started = threading.Event()
    release = threading.Event()

    def on_invoke(request) -> None:
        if request.seat.value != "builder":
            return
        if request.item_id == "item-2":
            second_started.set()
            assert release.wait(5.0), "item-2's builder was never released"
        elif request.item_id == "item-1":
            assert second_started.wait(5.0), "item-2's builder never started"

    first = campaign_harness("first", ids, on_invoke=on_invoke)
    second = campaign_harness("second", ids, on_invoke=on_invoke)
    # Both builders read below the stop; item-1's reviewer then reads both
    # lanes, one meter, at the stop.
    meter = meter_of(reading(weekly(50)), reading(weekly(50)), reading(weekly(91)))
    state = FakeLaneStateAdapter(identity("lanes"))
    workflow_ports = campaign_ports([("lane-a", first), ("lane-b", second)], meter, state, ids)
    box: dict[str, object] = {}

    def target() -> None:
        box["outcome"] = run_campaign(workflow_ports, concurrency=2)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    deadline = threading.Event()
    for _ in range(500):
        if events(workflow_ports, "campaign.draining"):
            break
        deadline.wait(0.01)
    release.set()
    thread.join(10.0)
    assert not thread.is_alive()

    outcome = box["outcome"]
    by_id = {entry.item_id: entry for entry in outcome.items}
    assert outcome.stop_reason is CampaignStopReason.USAGE_STOP
    assert by_id["item-1"].disposition is ItemDisposition.PARKED
    assert by_id["item-1"].lane_signature == LaneSignalKind.USAGE.value
    # item-2 was running when the stop was found: it ran to its own end and
    # its later seat was refused for usage, as any seat past the stop is.
    assert by_id["item-2"].disposition is ItemDisposition.PARKED
    assert "item-3" not in by_id
    assert [item_id for item_id, _actor in workflow_ports.tracker.claims] == ["item-1", "item-2"]
    draining = events(workflow_ports, "campaign.draining")
    assert [event.payload["stop_reason"] for event in draining] == ["usage_stop"]
    assert draining[0].payload["running"] == 1
    stopped = events(workflow_ports, "campaign.usage_stop")
    assert [entry["lane"] for entry in stopped[0].payload["lanes"]] == ["lane-a", "lane-b"]
    assert stopped[0].payload["running"] == ["item-2"]
    assert events(workflow_ports, "campaign.hold_started") == []


def test_a_stale_usage_cooling_from_another_campaign_is_cleared_below_this_runs_stop() -> None:
    ids = ("item-1",)
    meter = meter_of(reading(weekly(82)))
    claude = campaign_harness("claude", ids)
    state = FakeLaneStateAdapter(identity("lanes"))
    state.cooled["claude-code"] = usage_cooling("claude-code")
    workflow_ports = campaign_ports([("claude-code", claude)], meter, state, ids)

    outcome = run_campaign(workflow_ports, max_items=1)

    assert [entry.disposition for entry in outcome.items] == [ItemDisposition.ACCEPTED]
    assert [request.seat.value for request in claude.requests] == ["builder", "reviewer"]
    assert state.active("claude-code") is None
    cleared = events(workflow_ports, "lane.usage_cooling_cleared")
    assert len(cleared) == 1
    assert cleared[0].payload["lane"] == "claude-code"
    assert cleared[0].payload["campaign_id"] == OTHER_CAMPAIGN
    assert cleared[0].payload["cooled_until"] == TWO_DAYS_AHEAD
    assert cleared[0].payload["percent"] == 82
    assert events(workflow_ports, "campaign.usage_stop") == []


@pytest.mark.parametrize("script", [reading(weekly(91)), failed()], ids=["at_stop", "meter_failed"])
def test_a_usage_cooling_stays_while_its_reading_is_at_the_stop_or_unread(script) -> None:
    ids = ("item-1",)
    claude = campaign_harness("claude", ids)
    state = FakeLaneStateAdapter(identity("lanes"))
    state.cooled["claude-code"] = usage_cooling("claude-code")
    workflow_ports = campaign_ports([("claude-code", claude)], meter_of(script), state, ids)

    outcome = run_campaign(workflow_ports, max_items=1)

    assert state.active("claude-code") == usage_cooling("claude-code")
    assert events(workflow_ports, "lane.usage_cooling_cleared") == []
    assert claude.requests == []
    assert outcome.stop_reason is CampaignStopReason.USAGE_STOP
    assert events(workflow_ports, "campaign.usage_stop")[0].payload["resets_at"] == TWO_DAYS_AHEAD


def test_a_seat_meets_a_stale_usage_cooling_on_its_preferred_lane_and_clears_it() -> None:
    ids = ("item-1",)
    meter = meter_of(reading(weekly(82)))
    claude = campaign_harness("claude", ids)
    codex = campaign_harness("codex", ids, metered_lane=False)
    state = FakeLaneStateAdapter(identity("lanes"))
    state.cooled["claude-code"] = usage_cooling("claude-code")
    workflow_ports = campaign_ports([("claude-code", claude), ("codex", codex)], meter, state, ids)

    outcome = run_campaign(workflow_ports, max_items=1)

    assert [entry.disposition for entry in outcome.items] == [ItemDisposition.ACCEPTED]
    assert [request.seat.value for request in claude.requests] == ["builder", "reviewer"]
    assert codex.requests == []
    assert state.active("claude-code") is None
    cleared = events(workflow_ports, "lane.usage_cooling_cleared")
    assert len(cleared) == 1
    assert cleared[0].item_id == "item-1"
    assert cleared[0].payload["campaign_id"] == OTHER_CAMPAIGN


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


# -- a failed reading after the warning level --------------------------------

HOLD_POLICY = UsagePolicy(warn_percent=85, stop_percent=90, five_hour_stop_percent=95)
RATE_LIMITED = "meter returned error rate_limit_error"


def rate_limited() -> UsageReading:
    return UsageReading.failed(METER, RATE_LIMITED)


def hold_ports(
    harness: FakeHarnessAdapter,
    meter: FakeUsageMeter,
    *,
    policy: UsagePolicy = HOLD_POLICY,
    lane_state=None,
    waits: list[float] | None = None,
    hold_limit: float | None = None,
) -> WorkflowPorts:
    workflow_ports = ports(
        [("claude-code", harness)], meter, policy=policy, lane_state=lane_state, waits=waits
    )
    if hold_limit is not None:
        workflow_ports = replace(workflow_ports, usage_hold_limit_seconds=hold_limit)
    return workflow_ports


def actions(workflow_ports: WorkflowPorts) -> list[str]:
    return [event.payload["action"] for event in events(workflow_ports, "usage.meter_failed")]


def test_a_failed_reading_after_the_warning_level_holds_the_seat_until_a_reading_succeeds() -> None:
    waits: list[float] = []
    started_at_reads: list[int] = []
    meter = meter_of(reading(weekly(86)), rate_limited(), rate_limited(), reading(weekly(87)))
    claude = metered([builder(), review(ReviewDecision.PASS)])
    workflow_ports = hold_ports(claude, meter, waits=waits)
    claude.on_invoke = lambda _request: started_at_reads.append(len(meter.reads))

    outcome = run_item(workflow_ports)

    assert outcome.disposition is ItemDisposition.ACCEPTED
    # The reviewer starts only after the fourth read, the 87% reading.
    assert started_at_reads == [1, 4]
    assert actions(workflow_ports) == ["hold", "hold"]
    for event in events(workflow_ports, "usage.meter_failed"):
        assert event.payload["error"] == RATE_LIMITED
        assert event.payload["last_good"]["percent"] == 86
        assert event.payload["last_good"]["kind"] == "weekly_all"
        assert event.payload["last_good"]["fetched_at"] == "2026-10-21T20:00:00+00:00"
    assert waits == [USAGE_RETRY_SECONDS, USAGE_RETRY_SECONDS]
    warnings = events(workflow_ports, "usage.warning")
    assert [(event.payload["seat"], event.payload["percent"]) for event in warnings] == [
        ("builder", 86)
    ]


def test_a_held_seat_whose_next_good_reading_is_at_the_stop_stops_and_cools_the_lane() -> None:
    state = FakeLaneStateAdapter(identity("lanes"))
    meter = meter_of(reading(weekly(86)), rate_limited(), rate_limited(), reading(weekly(91)))
    claude = metered([builder()])
    workflow_ports = hold_ports(claude, meter, lane_state=state)

    outcome = run_item(workflow_ports)

    assert outcome.disposition is ItemDisposition.PARKED
    assert outcome.lanes_exhausted
    assert [request.seat.value for request in claude.requests] == ["builder"]
    assert actions(workflow_ports) == ["hold", "hold"]
    assert len(events(workflow_ports, "usage.stop")) == 1
    assert [lane for lane, _signal, _campaign in state.cools] == ["claude-code"]
    assert state.cools[0][1].reset_at == WEEKLY_RESET


def test_a_meter_that_has_only_ever_failed_still_allows_every_seat() -> None:
    waits: list[float] = []
    claude = metered([builder(), review(ReviewDecision.PASS)])
    workflow_ports = hold_ports(claude, meter_of(rate_limited()), waits=waits)

    outcome = run_item(workflow_ports)

    assert outcome.disposition is ItemDisposition.ACCEPTED
    assert actions(workflow_ports) == ["allow", "allow"]
    assert all("last_good" not in event.payload for event in events(workflow_ports, "usage.meter_failed"))
    assert waits == []


def test_a_failed_reading_after_good_readings_below_the_warning_level_still_allows() -> None:
    waits: list[float] = []
    claude = metered([builder(), review(ReviewDecision.PASS)])
    workflow_ports = hold_ports(
        claude, meter_of(reading(weekly(84)), rate_limited()), waits=waits
    )

    outcome = run_item(workflow_ports)

    assert outcome.disposition is ItemDisposition.ACCEPTED
    assert actions(workflow_ports) == ["allow"]
    assert [request.seat.value for request in claude.requests] == ["builder", "reviewer"]
    assert waits == []


def test_under_refuse_a_failed_reading_after_the_warning_level_holds_past_the_retries() -> None:
    waits: list[float] = []
    state = FakeLaneStateAdapter(identity("lanes"))
    claude = metered([builder()])
    workflow_ports = hold_ports(
        claude,
        meter_of(reading(weekly(86)), rate_limited()),
        policy=replace(HOLD_POLICY, on_probe_failure=UsageProbeFailure.REFUSE),
        lane_state=state,
        waits=waits,
    )

    outcome = run_item(workflow_ports)

    assert outcome.disposition is ItemDisposition.PARKED
    assert outcome.lane_signature == USAGE_UNAVAILABLE_SIGNATURE
    held = actions(workflow_ports)
    assert held[-1] == "stop"
    assert set(held[:-1]) == {"hold"} and len(held) - 1 > USAGE_RETRY_ATTEMPTS
    assert len(waits) == len(held) - 1
    # The fake clock moves 61 s per call, so the default 21600 s hold limit is
    # reached only after far more reads than the refuse path's retries.
    assert len(waits) > 100
    assert [request.seat.value for request in claude.requests] == ["builder"]
    assert state.cools == []


def test_a_hold_on_failed_readings_ends_the_item_at_the_hold_limit_without_cooling() -> None:
    waits: list[float] = []
    state = FakeLaneStateAdapter(identity("lanes"))
    claude = metered([builder()])
    workflow_ports = hold_ports(
        claude,
        meter_of(reading(weekly(86)), rate_limited()),
        lane_state=state,
        waits=waits,
        hold_limit=200.0,
    )

    outcome = run_item(workflow_ports)

    assert outcome.disposition is ItemDisposition.PARKED
    assert outcome.lane_signature == USAGE_UNAVAILABLE_SIGNATURE
    failures = events(workflow_ports, "usage.meter_failed")
    assert failures[-1].payload["action"] == "stop"
    assert failures[-1].payload["last_good"]["percent"] == 86
    assert {event.payload["action"] for event in failures[:-1]} == {"hold"}
    assert [request.seat.value for request in claude.requests] == ["builder"]
    assert state.cools == []
    assert state.active("claude-code") is None


def test_a_stop_reading_without_a_reset_counts_as_seen_and_its_failure_holds() -> None:
    waits: list[float] = []
    meter = meter_of(reading(weekly(50)), reading(weekly(95, resets_at=None)), reading(weekly(87)))
    claude = metered([builder(), review(ReviewDecision.PASS)])
    workflow_ports = hold_ports(claude, meter, waits=waits)

    outcome = run_item(workflow_ports)

    assert outcome.disposition is ItemDisposition.ACCEPTED
    assert actions(workflow_ports) == ["hold"]
    assert events(workflow_ports, "usage.meter_failed")[0].payload["last_good"]["percent"] == 95
    assert waits == [USAGE_RETRY_SECONDS]


def test_subscription_campaign_routes_each_real_workflow_seat_and_records_its_lane(tmp_path):
    from autobuild.adapters.subscription_admission import SubscriptionAdmissionStore
    from autobuild.application.subscriptions import SubscriptionRouting

    capped = metered([])
    healthy = metered([builder(), review(ReviewDecision.PASS)])
    healthy.meter_id = "meter-b"
    meter = FakeUsageMeter(identity("meters"), readings={
        METER: [replace(reading(weekly(96)), extra_usage_enabled=False)],
        "meter-b": [replace(reading(weekly(10)), meter_id="meter-b", extra_usage_enabled=False)],
    })
    workflow = ports([("capped", capped), ("healthy", healthy)], meter)
    allocator = SubscriptionRouting(workflow.lanes, meter, POLICY, workflow.lane_state,
                                    SubscriptionAdmissionStore(tmp_path, now=lambda: LAUNCH))
    workflow = replace(workflow, subscription_admission=allocator)
    assert run_item(workflow).disposition is ItemDisposition.ACCEPTED
    assert [event.payload["lane"] for event in events(workflow, "item.claimed")] == ["healthy"]
    assert [event.payload["lane"] for event in events(workflow, "seat.started")] == ["healthy", "healthy"]


def test_subscription_holds_before_tracker_claim_when_no_builder_fits(tmp_path):
    from autobuild.adapters.subscription_admission import SubscriptionAdmissionStore
    from autobuild.application.subscriptions import SubscriptionRouting

    meter = meter_of(replace(reading(weekly(96)), extra_usage_enabled=False))
    workflow = ports([("capped", metered([]))], meter)
    allocator = SubscriptionRouting(workflow.lanes, meter, POLICY, workflow.lane_state,
                                    SubscriptionAdmissionStore(tmp_path, now=lambda: LAUNCH))
    result = run_item(replace(workflow, subscription_admission=allocator))
    assert result.lanes_exhausted
    assert not events(workflow, "item.claimed")
    assert not events(workflow, "seat.started")
    assert events(workflow, "subscription.admission")[-1].payload["recommendation"] is None


def test_subscription_reservation_blocks_a_concurrent_claim_until_the_builder_finishes(tmp_path):
    from autobuild.adapters.subscription_admission import SubscriptionAdmissionStore
    from autobuild.application.subscriptions import SubscriptionRouting

    started = threading.Event()
    release = threading.Event()

    def hold_first_builder(request) -> None:
        if request.seat.value == "builder":
            started.set()
            assert release.wait(5.0)

    first = ports([("codex", metered([builder(), review(ReviewDecision.PASS)]))], meter_of(reading(weekly(88))))
    second = ports([("codex", metered([builder(), review(ReviewDecision.PASS)]))], meter_of(reading(weekly(88))))
    first.lanes[0].harness.on_invoke = hold_first_builder
    shared_tracker = FakeTrackerAdapter(identity("tracker"))
    store = SubscriptionAdmissionStore(tmp_path, now=lambda: LAUNCH)
    first = replace(
        first,
        tracker=shared_tracker,
        subscription_admission=SubscriptionRouting(first.lanes, first.usage_meter, POLICY, first.lane_state, store),
    )
    second = replace(
        second,
        tracker=shared_tracker,
        subscription_admission=SubscriptionRouting(second.lanes, second.usage_meter, POLICY, second.lane_state, store),
    )
    first_spec = replace(spec(), item=WorkItem("item-first", "first", "plan", ("passes",)))
    second_spec = replace(spec(), item=WorkItem("item-second", "second", "plan", ("passes",)))
    outcomes: dict[str, object] = {}

    thread = threading.Thread(
        target=lambda: outcomes.setdefault("first", run_item(first, first_spec)), daemon=True
    )
    thread.start()
    assert started.wait(5.0)
    outcomes["second"] = run_item(second, second_spec)
    release.set()
    thread.join(5.0)

    assert not thread.is_alive()
    assert outcomes["second"].lanes_exhausted
    assert [item_id for item_id, _actor in shared_tracker.claims] == ["item-first"]


def test_subscription_releases_reservation_after_harness_error_and_skips_failed_peer(tmp_path):
    from autobuild.adapters.subscription_admission import SubscriptionAdmissionStore
    from autobuild.application.subscriptions import SubscriptionRouting

    a, b = metered([]), metered([])
    b.meter_id = "meter-b"
    meter = FakeUsageMeter(identity("meters"), readings={
        METER: [failed()],
        "meter-b": [replace(reading(weekly(10)), meter_id="meter-b", extra_usage_enabled=False)],
    })
    workflow = ports([("offline", a), ("healthy", b)], meter)
    store = SubscriptionAdmissionStore(tmp_path, now=lambda: LAUNCH)
    allocator = SubscriptionRouting(workflow.lanes, meter, POLICY, workflow.lane_state, store)
    with pytest.raises(RuntimeError, match="seat fault"):
        with allocator.reserve("builder", 60) as lane:
            assert lane.name == "healthy"
            raise RuntimeError("seat fault")
    import sqlite3
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT lane, released FROM admissions").fetchall() == [("healthy", 1)]


def test_subscription_campaign_drains_at_the_actual_global_stop(tmp_path):
    from dataclasses import replace
    from autobuild.adapters.subscription_admission import SubscriptionAdmissionStore
    from autobuild.application.subscriptions import SubscriptionRouting

    meter = meter_of(reading(weekly(50)), reading(weekly(91), session(98)))
    state = FakeLaneStateAdapter(identity("lanes"))
    ports_for_campaign = ports([("claude-code", metered([builder()]))], meter, lane_state=state, queue=True)
    routing = SubscriptionRouting(
        ports_for_campaign.lanes, meter, POLICY, state, SubscriptionAdmissionStore(tmp_path, now=lambda: LAUNCH)
    )

    outcome = CampaignRunner(replace(ports_for_campaign, subscription_admission=routing)).run(
        campaign(), lambda item: spec()
    )

    assert outcome.stop_reason is CampaignStopReason.USAGE_STOP
    assert events(ports_for_campaign, "campaign.usage_stop")[0].payload["resets_at"] == WEEKLY_RESET
    assert events(ports_for_campaign, "campaign.hold_started") == []
