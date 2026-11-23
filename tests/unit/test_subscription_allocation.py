from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime, timedelta
import sqlite3

import pytest
from autobuild.adapters.subscription_admission import SubscriptionAdmissionStore
from autobuild.application import Lane
from autobuild.application.subscriptions import SubscriptionRouting
from autobuild.domain import (
    AdapterIdentity,
    LaneSignal,
    LaneSignalKind,
    UsagePolicy,
    UsageLimit,
    UsageLimitKind as Kind,
    UsageReading,
)
from autobuild.testing import FakeHarnessAdapter, FakeLaneStateAdapter, FakeUsageMeter


NOW = datetime(2026, 11, 22, 8, tzinfo=UTC)
POLICY = UsagePolicy(94, 98, 95)


def reading(used, hours=120, short=None):
    windows = [
        UsageLimit(Kind.WEEKLY_ALL, used, (NOW + timedelta(hours=hours)).isoformat())
    ]
    if short is not None:
        windows.append(
            UsageLimit(Kind.SESSION, short, (NOW + timedelta(hours=2)).isoformat())
        )
    return UsageReading(
        "subscription",
        True,
        tuple(windows),
        extra_usage_enabled=False,
        fetched_at=NOW.isoformat(),
    )


def acquire_once(path):
    s = SubscriptionAdmissionStore(path, now=lambda: NOW)
    return s.acquire({"codex": reading(96)}, POLICY, "builder", 3600) is not None


def test_concurrent_processes_cannot_both_spend_the_last_seat(tmp_path):
    with ProcessPoolExecutor(2) as pool:
        assert sorted(pool.map(acquire_once, [tmp_path, tmp_path])) == [False, True]


def test_recently_finished_seat_still_reserves_stale_meter_and_learns_cost(tmp_path):
    clock = [NOW]
    s = SubscriptionAdmissionStore(tmp_path, now=lambda: clock[0])
    lease = s.acquire({"codex": reading(90)}, POLICY, "builder", 300)
    assert s.release(lease, reading(94))
    assert not s.release(lease, reading(94))
    row = s.preview({"codex": reading(94)}, POLICY, "builder")["lanes"][0]
    assert row["windows"][0]["reserved_percent"] == 1
    assert row["windows"][0]["estimated_seat_percent"] == 4
    assert not row["eligible"]


def test_rank_uses_each_providers_own_reset_and_all_windows(tmp_path):
    s = SubscriptionAdmissionStore(tmp_path, now=lambda: NOW)
    assert s.preview({"a": reading(60, 24), "b": reading(60)}, POLICY, "reviewer")[
        "recommendation"
    ] == {"lane": "a"}
    assert s.preview({"a": reading(60, 24, 94), "b": reading(60)}, POLICY, "reviewer")[
        "recommendation"
    ] == {"lane": "b"}
    assert (
        s.preview({"a": reading(97), "b": reading(100)}, POLICY, "builder")[
            "recommendation"
        ]
        is None
    )


def test_read_only_preview_reads_existing_reservations_without_initialising_or_mutating(tmp_path):
    absent = SubscriptionAdmissionStore(tmp_path / "absent", now=lambda: NOW, read_only=True)
    absent.preview({"codex": reading(90)}, POLICY, "builder")
    assert not absent.path.parent.exists()

    writer = SubscriptionAdmissionStore(tmp_path, now=lambda: NOW)
    assert writer.acquire({"codex": reading(90)}, POLICY, "builder", 300) is not None
    before = writer.path.read_bytes()

    preview = SubscriptionAdmissionStore(tmp_path, now=lambda: NOW, read_only=True).preview(
        {"codex": reading(90)}, POLICY, "builder"
    )

    assert preview["lanes"][0]["windows"][0]["reserved_percent"] == 1
    assert writer.path.read_bytes() == before


def test_subscription_route_rejudges_only_usage_cooling(tmp_path):
    lane = Lane(
        "codex",
        FakeHarnessAdapter(AdapterIdentity("codex", "1"), meter_id="subscription"),
        meter_id="subscription",
    )
    state = FakeLaneStateAdapter(AdapterIdentity("state", "1"))
    meter = FakeUsageMeter(AdapterIdentity("meter", "1"), readings={"subscription": [reading(10)]})
    router = SubscriptionRouting((lane,), meter, POLICY, state, SubscriptionAdmissionStore(tmp_path, now=lambda: NOW))
    state.cool("codex", LaneSignal(LaneSignalKind.USAGE), "other")

    assert set(router.readings()) == {"codex"}
    assert meter.reads == ["subscription"]
    assert state.clears

    state.cool("codex", LaneSignal(LaneSignalKind.AUTH), "other")
    assert router.readings() == {}
    assert meter.reads == ["subscription"]

    state.cool("codex", LaneSignal(LaneSignalKind.USAGE), "other")
    observed = SubscriptionRouting((lane,), meter, POLICY, state, SubscriptionAdmissionStore(tmp_path, now=lambda: NOW, read_only=True), read_only=True)
    assert set(observed.readings()) == {"codex"}
    assert state.active("codex") is not None


def test_subscription_global_stop_cools_but_reservation_pressure_does_not(tmp_path):
    lane = Lane("codex", FakeHarnessAdapter(AdapterIdentity("codex", "1"), meter_id="subscription"), meter_id="subscription")
    state = FakeLaneStateAdapter(AdapterIdentity("state", "1"))
    stopped = FakeUsageMeter(AdapterIdentity("meter", "1"), readings={"subscription": [reading(99)]})
    routing = SubscriptionRouting((lane,), stopped, POLICY, state, SubscriptionAdmissionStore(tmp_path, now=lambda: NOW))

    with routing.reserve("builder", 300) as selected:
        assert selected is None
    cooling = state.active("codex")
    assert cooling is not None and cooling.signature == LaneSignalKind.USAGE.value

    state.cooled.clear()
    healthy = FakeUsageMeter(AdapterIdentity("meter", "1"), readings={"subscription": [reading(96)]})
    pressured = SubscriptionRouting((lane,), healthy, POLICY, state, SubscriptionAdmissionStore(tmp_path, now=lambda: NOW))
    assert pressured.store.acquire({"codex": reading(94)}, POLICY, "reviewer", 300)
    with pressured.reserve("builder", 300) as selected:
        assert selected is None
    assert state.active("codex") is None



def test_bad_or_stale_window_and_broken_ledger_fail_closed(tmp_path):
    s = SubscriptionAdmissionStore(tmp_path, now=lambda: NOW)
    for bad in (
        reading(1, 0),
        reading(-1),
        reading(float("nan")),
        reading(1, 180),
        UsageReading("subscription", True, (), fetched_at=NOW.isoformat()),
    ):
        assert s.acquire({"x": bad}, POLICY, "builder", 60) is None
    future = SubscriptionAdmissionStore(
        tmp_path, now=lambda: NOW + timedelta(minutes=5)
    )
    assert future.acquire({"x": reading(0)}, POLICY, "builder", 60) is None
    s.path.write_bytes(b"bad ledger")
    with pytest.raises(sqlite3.DatabaseError):
        s.acquire({"x": reading(1)}, POLICY, "builder", 60)


def test_status_reports_stopped_windows_and_bad_late_window_never_cools(tmp_path):
    from dataclasses import replace

    lane = Lane("codex", FakeHarnessAdapter(AdapterIdentity("codex", "1"), meter_id="subscription"), meter_id="subscription")
    state = FakeLaneStateAdapter(AdapterIdentity("state", "1"))
    stopped = reading(99)
    invalid = replace(stopped, limits=(*stopped.limits, *stopped.limits))
    meter = FakeUsageMeter(AdapterIdentity("meter", "1"), readings={"subscription": [invalid, stopped]})
    store = SubscriptionAdmissionStore(tmp_path, now=lambda: NOW)
    routing = SubscriptionRouting((lane,), meter, POLICY, state, store)
    with routing.reserve("builder", 300) as selected:
        assert selected is None
    assert state.active("codex") is None

    observer = SubscriptionRouting((lane,), meter, POLICY, state, store, read_only=True)
    row, = observer.preview(observer.readings(), "builder")["lanes"]
    assert not row["eligible"] and row["windows"][0]["used_percent"] == 99
    assert state.active("codex") is None


def _routed(tmp_path, age, failure=None, used=3, policy=POLICY):
    """Routing whose meter answered ``age`` seconds ago and then failed."""

    from dataclasses import replace
    from autobuild.adapters.usage_cache import SharedUsageMeterCache

    good = replace(reading(used), fetched_at=(NOW - timedelta(seconds=age)).isoformat())
    retry = (NOW + timedelta(seconds=241)).isoformat()
    script = FakeUsageMeter.answers_then_rate_limited(good, NOW.isoformat(), retry)
    if failure is not None:
        script[1] = failure
    inner = FakeUsageMeter(AdapterIdentity("meter", "1"), readings={"subscription": script})
    shared = SharedUsageMeterCache(inner, tmp_path, now=lambda: NOW)
    lane = Lane("codex", FakeHarnessAdapter(AdapterIdentity("codex", "1"), meter_id="subscription"), meter_id="subscription")
    state = FakeLaneStateAdapter(AdapterIdentity("state", "1"))
    store = SubscriptionAdmissionStore(tmp_path, now=lambda: NOW)
    shared.read("subscription")
    return SubscriptionRouting((lane,), shared, policy, state, store), state, inner


def _admitted(routing):
    decisions = []
    with routing.reserve("builder", 300, on_decision=decisions.append) as lane:
        return lane is not None, decisions[0]["lanes"][0]["reason"]


def test_the_stated_age_never_exceeds_the_time_a_released_reservation_is_counted():
    from autobuild.adapters.subscription_admission import LAST_GOOD_READING_SECONDS, RELEASED_RESERVATION_SECONDS

    assert 0 < LAST_GOOD_READING_SECONDS <= RELEASED_RESERVATION_SECONDS == 90


@pytest.mark.parametrize("age, admitted", [(70, True), (89, True), (90, False), (400, False)])
def test_a_rate_limited_read_is_judged_on_the_last_good_reading_only_inside_the_stated_age(tmp_path, age, admitted):
    routing, _state, inner = _routed(tmp_path, age)

    for _ in range(3):
        result, reason = _admitted(routing)
        assert result is admitted
        assert "subscription meter rate-limited, next read at 2026-11-22T08:04:01+00:00" in reason
    assert len(inner.reads) == 2


@pytest.mark.parametrize("used, admitted", [(93.9, True), (94, False), (97, False)])
def test_a_last_good_reading_at_the_warning_level_never_stands_in(tmp_path, used, admitted):
    assert _admitted(_routed(tmp_path, 70, used=used)[0])[0] is admitted


def test_a_transport_failure_may_use_the_last_good_reading_and_an_account_failure_may_not(tmp_path):
    lost = FakeUsageMeter.transport_error("subscription", NOW.isoformat())
    refused = UsageReading.failed("subscription", "usage is paid, stopped, or malformed", NOW.isoformat())

    assert _admitted(_routed(tmp_path / "lost", 70, lost)[0])[0] is True
    assert _admitted(_routed(tmp_path / "refused", 70, refused)[0])[0] is False


def test_an_expired_sign_in_overrides_a_young_good_reading(tmp_path):
    routing, _state, inner = _routed(tmp_path, 5)
    inner.expired["subscription"] = FakeUsageMeter.expired_sign_in("subscription", NOW.isoformat())

    assert _admitted(routing) == (False, "sign-in expired")
    assert inner.reads == ["subscription"]


def test_a_cooled_lane_is_never_reopened_by_the_last_good_reading(tmp_path):
    routing, state, _inner = _routed(tmp_path, 70)
    state.cool("codex", LaneSignal(LaneSignalKind.USAGE), "other")

    assert routing.readings() == {}
    assert state.active("codex") is not None
