"""The per-lane meter reading cache shared by concurrent seats, on a fake clock."""

from __future__ import annotations

import threading

from autobuild.application import Lane
from autobuild.application.item import USAGE_READING_SECONDS, _UsageGate
from autobuild.domain import (
    AdapterIdentity,
    UsageLimit,
    UsageLimitKind,
    UsagePolicy,
    UsageReading,
)
from autobuild.testing import FakeClock, FakeHarnessAdapter, FakeUsageMeter

METER = "meter-a"
RESET = "2026-10-26T06:00:00+00:00"
POLICY = UsagePolicy(warn_percent=80, stop_percent=90)
WAIT_SECONDS = 5


def identity(name: str) -> AdapterIdentity:
    return AdapterIdentity(name, "1", frozenset({"test"}))


def weekly(percent: float) -> UsageReading:
    return UsageReading(
        METER, True, (UsageLimit(UsageLimitKind.WEEKLY_ALL, percent, RESET),),
        fetched_at="2026-10-25T20:00:00+00:00",
    )


def lane() -> Lane:
    return Lane("metered", FakeHarnessAdapter(identity("metered"), meter_id=METER), meter_id=METER)


def gate(meter: FakeUsageMeter, clock: FakeClock) -> _UsageGate:
    return _UsageGate(meter, POLICY, lambda seconds: None, clock)


def percent(assessment) -> float:
    return assessment.reading.limits[0].percent


def assess_together(usage: _UsageGate, seats: int) -> list:
    barrier = threading.Barrier(seats, timeout=WAIT_SECONDS)
    results: list = []
    errors: list[BaseException] = []

    def seat() -> None:
        try:
            barrier.wait()
            results.append(usage.assess(lane(), lambda *_: None))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=seat) for _ in range(seats)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=4 * WAIT_SECONDS)
    assert errors == []
    return results


def test_the_reading_lasts_sixty_seconds() -> None:
    assert USAGE_READING_SECONDS == 60


def test_five_concurrent_seats_share_one_read_within_sixty_seconds_and_read_again_at_sixty_one() -> None:
    meter = FakeUsageMeter(identity("meter"), readings={METER: [weekly(10), weekly(20)]})
    clock = FakeClock()
    usage = gate(meter, clock)

    first = assess_together(usage, 5)

    assert meter.reads == [METER]
    assert [percent(assessment) for assessment in first] == [10] * 5
    clock.set(59.9)
    assert percent(usage.assess(lane(), lambda *_: None)) == 10
    assert meter.reads == [METER]
    clock.set(61)
    assert percent(usage.assess(lane(), lambda *_: None)) == 20
    assert meter.reads == [METER, METER]


def test_the_stop_level_binds_on_every_seat_that_shares_a_reading() -> None:
    meter = FakeUsageMeter(identity("meter"), readings={METER: [weekly(95)]})

    assessments = assess_together(gate(meter, FakeClock()), 5)

    assert meter.reads == [METER]
    assert [assessment.stops_lane for assessment in assessments] == [True] * 5


def test_a_failed_reading_is_never_shared_and_the_next_seat_reads_again() -> None:
    failed = UsageReading.failed(METER, "meter transport failed (exit 6)")
    meter = FakeUsageMeter(identity("meter"), readings={METER: [failed, weekly(10)]})
    usage = gate(meter, FakeClock())

    assert usage.assess(lane(), lambda *_: None).failure
    assert percent(usage.assess(lane(), lambda *_: None)) == 10
    assert meter.reads == [METER, METER]


def test_a_seat_keeps_the_previous_reading_while_another_seat_refreshes_it() -> None:
    meter = FakeUsageMeter(identity("meter"), readings={METER: [weekly(10), weekly(20)]})
    clock = FakeClock()
    usage = gate(meter, clock)
    assert percent(usage.assess(lane(), lambda *_: None)) == 10
    clock.set(61)
    in_flight, release = threading.Event(), threading.Event()

    def hold_second_read(meter_id: str) -> None:
        if len(meter.reads) == 2:
            in_flight.set()
            release.wait(WAIT_SECONDS)

    meter.on_read = hold_second_read
    refreshed: list = []
    refresher = threading.Thread(target=lambda: refreshed.append(usage.assess(lane(), lambda *_: None)))
    refresher.start()
    assert in_flight.wait(WAIT_SECONDS)
    meanwhile: list = []
    other = threading.Thread(target=lambda: meanwhile.append(usage.assess(lane(), lambda *_: None)))
    other.start()
    other.join(timeout=WAIT_SECONDS / 2)

    finished_during_refresh = not other.is_alive()
    release.set()
    refresher.join(timeout=WAIT_SECONDS)
    other.join(timeout=WAIT_SECONDS)

    assert finished_during_refresh
    assert [percent(assessment) for assessment in meanwhile] == [10]
    assert [percent(assessment) for assessment in refreshed] == [20]
    assert meter.reads == [METER, METER]
    assert percent(usage.assess(lane(), lambda *_: None)) == 20
    assert meter.reads == [METER, METER]
