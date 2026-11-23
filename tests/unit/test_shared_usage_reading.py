"""One shared usage reading per host: the cache file every meter reader uses,
on a fake clock, and the reading's age in the usage events and the command."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from autobuild.adapters.claude_usage import CLAUDE_SUBSCRIPTION_METER, ClaudeUsageMeterAdapter
from autobuild.adapters.usage_cache import (
    CACHE_SCHEMA,
    SOURCE_METER,
    SOURCE_SHARED_FILE,
    SharedUsageMeterCache,
)
from autobuild.application import ItemWorkflow, Lane, WorkflowPorts
from autobuild.application.progress import render_progress_line
from autobuild.cli import _parser, _usage
from autobuild.domain import (
    AdapterIdentity,
    BuilderReport,
    CampaignRef,
    CommandResult,
    DeliveryMode,
    DiffEvidence,
    ItemExecutionSpec,
    ReviewDecision,
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
FETCHED = datetime(2026, 11, 21, 7, 0, tzinfo=UTC)
RESET = "2026-11-26T06:00:00+00:00"
RATE_LIMITED = "meter returned error rate_limit_error"


def identity(name: str) -> AdapterIdentity:
    return AdapterIdentity(name, "1", frozenset({"test"}))


class Clock:
    """A settable wall clock for the cache and the run record."""

    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment


def good(percent: float = 40, fetched: datetime = FETCHED) -> UsageReading:
    return UsageReading(
        METER,
        True,
        (UsageLimit(UsageLimitKind.WEEKLY_ALL, percent, RESET),),
        "",
        False,
        fetched.isoformat(),
    )


def rate_limited(fetched: datetime = FETCHED) -> UsageReading:
    return UsageReading.failed(METER, RATE_LIMITED, fetched.isoformat())


def fake_meter(*script: UsageReading) -> FakeUsageMeter:
    return FakeUsageMeter(identity("meter"), readings={METER: list(script)})


def cache_path(root: Path, meter_id: str = METER) -> Path:
    return root / "usage-meter" / f"{meter_id}.json"


# -- the adapter --------------------------------------------------------------


def test_ten_readers_on_one_scratch_root_make_one_inner_read(tmp_path: Path) -> None:
    inner = fake_meter(good())
    clock = Clock(FETCHED + timedelta(seconds=1))

    readings = [SharedUsageMeterCache(inner, tmp_path, now=clock).read(METER) for _ in range(10)]

    assert inner.reads == [METER]
    assert all(reading == readings[0] for reading in readings)
    assert readings[0] == good()


@pytest.mark.parametrize(("age", "reads"), [(59, 1), (61, 2)])
def test_a_reading_is_refreshed_only_once_it_is_older_than_the_window(
    tmp_path: Path, age: int, reads: int
) -> None:
    inner = fake_meter(good(40), good(70, FETCHED + timedelta(seconds=age)))
    clock = Clock(FETCHED)
    SharedUsageMeterCache(inner, tmp_path, now=clock).read(METER)

    clock.moment = FETCHED + timedelta(seconds=age)
    reading = SharedUsageMeterCache(inner, tmp_path, now=clock).read(METER)

    assert len(inner.reads) == reads
    assert reading.limits[0].percent == (40 if reads == 1 else 70)
    assert reading.fetched_at == (FETCHED + timedelta(seconds=0 if reads == 1 else age)).isoformat()


def test_a_failed_reading_is_shared_within_the_window_and_stays_a_failure(tmp_path: Path) -> None:
    inner = fake_meter(rate_limited(), good())
    clock = Clock(FETCHED + timedelta(seconds=2))

    first = SharedUsageMeterCache(inner, tmp_path, now=clock).read(METER)
    second = SharedUsageMeterCache(inner, tmp_path, now=clock).read(METER)

    assert inner.reads == [METER]
    assert first == second
    assert not second.ok and second.error == RATE_LIMITED


def test_the_cache_file_keeps_the_reading_and_its_own_fetch_time(tmp_path: Path) -> None:
    inner = fake_meter(good())
    SharedUsageMeterCache(inner, tmp_path, now=Clock(FETCHED + timedelta(seconds=30))).read(METER)

    stored = json.loads(cache_path(tmp_path).read_text(encoding="utf-8"))

    assert stored["schema"] == CACHE_SCHEMA
    assert stored["meter_id"] == METER
    assert stored["fetched_at"] == FETCHED.isoformat()
    assert set(stored) == {
        "schema", "meter_id", "ok", "limits", "error", "extra_usage_enabled", "fetched_at"
    }


def test_a_reading_without_a_fetch_time_is_never_served_from_the_file(tmp_path: Path) -> None:
    inner = fake_meter(UsageReading.failed(METER, "no fetch time", ""))
    clock = Clock(FETCHED)

    for _ in range(3):
        SharedUsageMeterCache(inner, tmp_path, now=clock).read(METER)

    assert len(inner.reads) == 3


def _valid_file(meter_id: str = METER, schema: str = CACHE_SCHEMA) -> str:
    return json.dumps(
        {
            "schema": schema,
            "meter_id": meter_id,
            "ok": True,
            "limits": [{"kind": "weekly_all", "percent": 99, "resets_at": RESET, "scope": None}],
            "error": "",
            "extra_usage_enabled": False,
            "fetched_at": FETCHED.isoformat(),
        }
    )


@pytest.mark.parametrize(
    "content",
    [
        "",
        "{not json",
        '{"schema": "autobuild.usage-reading.v1", "meter_id": "meter-a", "ok": tr',
        _valid_file(meter_id="other-meter"),
        _valid_file(schema="autobuild.usage-reading.v0"),
        json.dumps({"schema": CACHE_SCHEMA, "meter_id": METER, "ok": "yes"}),
    ],
    ids=["empty", "invalid-json", "torn", "other-meter", "unknown-schema", "malformed"],
)
def test_an_unusable_cache_file_is_ignored_and_the_meter_is_read(tmp_path: Path, content: str) -> None:
    path = cache_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")
    inner = fake_meter(good(40))

    reading = SharedUsageMeterCache(inner, tmp_path, now=Clock(FETCHED)).read(METER)

    assert inner.reads == [METER]
    assert reading == good(40)


def test_a_valid_cache_file_is_served_without_an_inner_read(tmp_path: Path) -> None:
    path = cache_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(_valid_file(), encoding="utf-8")
    inner = fake_meter(good(40))

    sourced = SharedUsageMeterCache(inner, tmp_path, now=Clock(FETCHED)).read_sourced(METER)

    assert inner.reads == []
    assert sourced.source == SOURCE_SHARED_FILE and sourced.path == path
    assert sourced.reading.limits[0].percent == 99


def test_a_scratch_root_that_cannot_be_written_still_returns_the_inner_reading(tmp_path: Path) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_text("a file where the scratch directory would be", encoding="utf-8")
    inner = fake_meter(good(40))

    sourced = SharedUsageMeterCache(inner, blocked, now=Clock(FETCHED)).read_sourced(METER)

    assert inner.reads == [METER]
    assert sourced.reading == good(40)
    assert sourced.source == SOURCE_METER


def test_a_reader_that_cannot_take_the_lock_reads_the_meter_itself(tmp_path: Path) -> None:
    lock = cache_path(tmp_path).with_name(f"{METER}.json.lock")
    lock.parent.mkdir(parents=True)
    clock = Clock(FETCHED)
    lock.write_text(clock().isoformat(), encoding="utf-8")
    inner = fake_meter(good(40))

    reading = SharedUsageMeterCache(
        inner, tmp_path, now=clock, lock_timeout_seconds=0.05
    ).read(METER)

    assert inner.reads == [METER]
    assert reading == good(40)
    assert lock.exists()


def test_the_token_never_reaches_the_cache_file(tmp_path: Path) -> None:
    token = "test-access-token"
    out = tmp_path / "stdout.txt"
    err = tmp_path / "stderr.txt"
    out.write_text(
        json.dumps(
            {
                "limits": [{"kind": "weekly_all", "percent": 41, "resets_at": RESET}],
                "extra_usage": {"is_enabled": False},
            }
        ),
        encoding="utf-8",
    )
    err.write_text("", encoding="utf-8")
    credentials = tmp_path / "credentials.json"
    credentials.write_text(json.dumps({"claudeAiOauth": {"accessToken": token}}), encoding="utf-8")
    command = FakeCommandAdapter(
        identity("command"), scripted_results=[CommandResult("usage", 0, str(out), str(err), "s", "e")]
    )
    clock = Clock(FETCHED)
    inner = ClaudeUsageMeterAdapter(
        command, tmp_path / "usage", credentials_path=credentials, now=clock
    )
    scratch = tmp_path / "scratch"

    reading = SharedUsageMeterCache(inner, scratch, now=clock).read(CLAUDE_SUBSCRIPTION_METER)

    assert reading.ok and reading.limits[0].percent == 41
    text = cache_path(scratch, CLAUDE_SUBSCRIPTION_METER).read_text(encoding="utf-8")
    assert token not in text
    assert "41" in text


# -- the reading age in the usage events --------------------------------------


def _builder() -> SeatResult:
    return SeatResult(
        "builder", SeatOutcome.SUCCEEDED, BuilderReport("report:builder", "built"), "raw:builder",
        SeatUsage(source="fake"), "start", "end",
    )


def _review() -> SeatResult:
    return SeatResult(
        "review-1", SeatOutcome.SUCCEEDED,
        ReviewVerdict("item-1", ReviewDecision.PASS, (), "verdict:1"),
        "raw:review:1", SeatUsage(source="fake"), "start", "end",
    )


def _spec() -> ItemExecutionSpec:
    return ItemExecutionSpec(
        WorkItem("item-1", "test item", "plan", ("passes",)),
        Path("brief.md"),
        "validator",
        ("python", "-m", "pytest"),
        ToolPolicy(frozenset({"python"}), (Path("/worktree"),)),
        "builder-class",
        "reviewer-class",
        "specialist-class",
        max_corrections=2,
        delivery_mode=DeliveryMode.PROTECTED_DEFAULT,
        delivery_target_branch="main",
        delivery_target_revision="base",
    )


def _run(meter: FakeUsageMeter, policy: UsagePolicy, record_now: datetime) -> WorkflowPorts:
    workspace_ref = WorkspaceRef(Path("/worktree"), "run", "base", "lease")
    metered = FakeHarnessAdapter(
        identity("metered"), scripted_results=[_builder(), _review()], meter_id=METER
    )
    unmetered = FakeHarnessAdapter(identity("unmetered"), scripted_results=[_builder(), _review()])
    ports = WorkflowPorts(
        FakeTrackerAdapter(identity("tracker"), queue=[]),
        FakeWorkspaceAdapter(
            identity("workspace"),
            workspace=workspace_ref,
            diffs=[DiffEvidence(workspace_ref, "revision-0", (), "patch:0")],
        ),
        metered,
        FakeCommandAdapter(
            identity("command"),
            scripted_results=[CommandResult("command:0", 0, "stdout", "stderr", "start", "end")],
        ),
        FakeRunRecordAdapter(identity("records"), now=lambda: record_now),
        FakeKnowledgeAdapter(identity("knowledge")),
        lanes=(
            Lane("claude-code", metered, meter_id=METER, meter_scope=metered.meter_scope_covers),
            Lane("codex", unmetered),
        ),
        lane_state=FakeLaneStateAdapter(identity("lanes")),
        usage_meter=meter,
        usage_policy=policy,
        usage_retry_wait=lambda seconds: None,
        usage_clock=FakeClock(step=61.0),
    )
    campaign = CampaignRef("campaign", Path("/repo"), max_items=1)
    ItemWorkflow(ports).run(campaign, _spec(), ports.records.create(campaign))
    return ports


def _events(ports: WorkflowPorts, event_type: str):
    return [event for event in ports.records.events if event.event_type == event_type]


POLICY = UsagePolicy(warn_percent=80, stop_percent=90)


def test_a_usage_warning_carries_the_reading_age_from_the_run_record_clock() -> None:
    ports = _run(fake_meter(good(85)), POLICY, FETCHED + timedelta(seconds=12, milliseconds=400))

    warnings = _events(ports, "usage.warning")

    assert warnings and warnings[0].payload["reading_age_seconds"] == 12
    line = render_progress_line(warnings[0])
    assert line is not None and line.endswith("reading 12 s old")


def test_a_usage_stop_carries_the_reading_age_from_the_run_record_clock() -> None:
    ports = _run(fake_meter(good(95)), POLICY, FETCHED + timedelta(seconds=45))

    stops = _events(ports, "usage.stop")

    assert stops and stops[0].payload["reading_age_seconds"] == 45
    line = render_progress_line(stops[0])
    assert line is not None and line.endswith("reading 45 s old")


def test_a_meter_failure_carries_the_reading_age_or_null_without_a_fetch_time() -> None:
    allow = UsagePolicy(warn_percent=80, stop_percent=90, on_probe_failure=UsageProbeFailure.ALLOW)
    ports = _run(
        fake_meter(rate_limited(), UsageReading.failed(METER, RATE_LIMITED, "")),
        allow,
        FETCHED + timedelta(seconds=7),
    )

    failures = _events(ports, "usage.meter_failed")

    assert [event.payload["reading_age_seconds"] for event in failures] == [7, None]
    lines = [render_progress_line(event) for event in failures]
    assert lines[0] is not None and lines[0].endswith("reading 7 s old")
    assert lines[1] is not None and lines[1].endswith("reading age unknown")


# -- the age in the usage command ---------------------------------------------


def test_the_usage_command_names_the_age_and_the_shared_file_on_a_cached_reading(
    tmp_path: Path, capsys
) -> None:
    path = cache_path(tmp_path, CLAUDE_SUBSCRIPTION_METER)
    path.parent.mkdir(parents=True)
    path.write_text(
        _valid_file(meter_id=CLAUDE_SUBSCRIPTION_METER).replace('"percent": 99', '"percent": 33'),
        encoding="utf-8",
    )
    clock = Clock(FETCHED + timedelta(seconds=12))
    inner = FakeUsageMeter(identity("meter"))
    cache = SharedUsageMeterCache(inner, tmp_path, now=clock)
    args = _parser().parse_args(["usage", "--scratch-root", str(tmp_path)])

    code = _usage(args, read=lambda scratch: cache.read_sourced(CLAUDE_SUBSCRIPTION_METER), now=clock)

    out = capsys.readouterr().out
    assert code == 0
    assert inner.reads == []
    assert "weekly_all 33%" in out
    assert "reading 12 s old, from the shared subscription cache" in out
    assert str(path) not in out


@pytest.mark.parametrize("bad", [float("nan"), -1, 101])
def test_invalid_cached_percent_forces_a_fresh_meter_read(tmp_path: Path, bad: float) -> None:
    path = cache_path(tmp_path)
    path.parent.mkdir(parents=True)
    payload = json.loads(_valid_file())
    payload["limits"][0]["percent"] = bad
    path.write_text(json.dumps(payload), encoding="utf-8")
    inner = fake_meter(good(23))
    cache = SharedUsageMeterCache(inner, tmp_path, now=Clock(FETCHED + timedelta(seconds=12)))

    result = cache.read_sourced(METER)

    assert result.source == SOURCE_METER
    assert result.reading.limits[0].percent == 23
    assert inner.reads == [METER]


def test_the_usage_command_names_a_fresh_meter_reading_as_read_from_the_meter(capsys) -> None:
    args = argparse.Namespace(json=False, scratch_root=None)
    clock = Clock(FETCHED + timedelta(seconds=1))

    code = _usage(args, read=lambda scratch: good(), now=clock)

    out = capsys.readouterr().out
    assert code == 0
    assert "reading 1 s old, read from the meter" in out


def test_composition_wraps_the_first_party_meter_and_not_the_null_meter(tmp_path: Path) -> None:
    from autobuild.adapters.claude_usage import NullUsageMeterAdapter
    from autobuild.bootstrap import composition

    claude = ClaudeUsageMeterAdapter(FakeCommandAdapter(identity("c")), tmp_path / "usage")
    null = NullUsageMeterAdapter()

    wrapped = composition._shared_meter(claude, tmp_path)

    assert isinstance(wrapped, SharedUsageMeterCache)
    assert wrapped.path(CLAUDE_SUBSCRIPTION_METER) == cache_path(tmp_path.resolve(), CLAUDE_SUBSCRIPTION_METER)
    assert composition._shared_meter(null, tmp_path) is null
    assert composition._shared_meter(None, tmp_path) is None


# -- the meter's own retry time, and the last good reading ----------------------


def _after(seconds: float) -> datetime:
    return FETCHED + timedelta(seconds=seconds)


def _limited(at: float, retry: float) -> UsageReading:
    return UsageReading.rate_limited(METER, _after(retry).isoformat(), _after(at).isoformat())


def _read_at(inner: FakeUsageMeter, root: Path, seconds: float) -> UsageReading:
    return SharedUsageMeterCache(inner, root, now=Clock(_after(seconds))).read(METER)


@pytest.mark.parametrize("retry", [120.0, 900.0])
def test_a_rate_limited_meter_is_not_asked_before_its_own_retry_time(tmp_path: Path, retry: float) -> None:
    inner = fake_meter(good(), _limited(70, 70 + retry), good(5, _after(70 + retry)))
    _read_at(inner, tmp_path, 0)

    for seconds in (70, 71, 70 + retry - 1):
        reading = _read_at(inner, tmp_path, seconds)
        assert not reading.ok and "next read at " + _after(70 + retry).isoformat() in reading.error
        assert len(inner.reads) == 2

    assert _read_at(inner, tmp_path, 70 + retry).ok
    assert len(inner.reads) == 3


def test_a_failed_read_keeps_the_last_good_reading_but_never_serves_it(tmp_path: Path) -> None:
    lost = FakeUsageMeter.transport_error(METER, _after(140).isoformat())
    inner = fake_meter(good(3), _limited(70, 80), lost)
    cache = SharedUsageMeterCache(inner, tmp_path, now=Clock(FETCHED))

    _read_at(inner, tmp_path, 0)
    for seconds in (70, 140):
        assert not _read_at(inner, tmp_path, seconds).ok
        assert cache.last_good(METER) == good(3)


def test_a_failure_about_the_account_drops_the_last_good_reading(tmp_path: Path) -> None:
    refused = UsageReading.failed(METER, "ordinary included usage is unavailable", _after(70).isoformat())
    inner = fake_meter(good(3), refused, _limited(140, 400))
    cache = SharedUsageMeterCache(inner, tmp_path, now=Clock(FETCHED))

    _read_at(inner, tmp_path, 0)
    for seconds in (70, 140):
        _read_at(inner, tmp_path, seconds)
        assert cache.last_good(METER) is None


def test_an_expired_sign_in_replaces_a_held_failure_and_is_never_written(tmp_path: Path) -> None:
    inner = fake_meter(_limited(0, 400))
    _read_at(inner, tmp_path, 0)
    held = cache_path(tmp_path).read_text(encoding="utf-8")
    expired = FakeUsageMeter.expired_sign_in(METER, _after(1).isoformat())
    inner.expired[METER] = expired

    assert _read_at(inner, tmp_path, 1) == expired
    assert _read_at(inner, tmp_path, 500) == expired

    assert cache_path(tmp_path).read_text(encoding="utf-8") == held
    assert inner.reads == [METER]


@pytest.mark.parametrize("key, value", [("failure", "unknown"), ("retry_at", "soon"), ("retry_at", 5)])
def test_a_cache_file_with_a_malformed_failure_is_ignored(tmp_path: Path, key: str, value: object) -> None:
    path = cache_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({**json.loads(_valid_file()), key: value}), encoding="utf-8")
    inner = fake_meter(good(40))

    assert _read_at(inner, tmp_path, 1) == good(40)
    assert inner.reads == [METER]
