"""A rate-limited meter is left alone for longer than the sharing window, and
the two-provider meter answers a probe without reading a provider."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from autobuild.adapters.codex_subscription import CompositeUsageMeterAdapter
from autobuild.adapters.usage_cache import (
    CACHE_SECONDS,
    RATE_LIMIT_HOLD_SECONDS,
    SharedUsageMeterCache,
)
from autobuild.domain import AdapterIdentity, UsageLimit, UsageLimitKind, UsageReading
from autobuild.testing import FakeUsageMeter

METER = "meter-a"
FETCHED = datetime(2026, 11, 23, 8, 0, tzinfo=UTC)
RESET = "2026-11-30T06:00:00+00:00"


class Clock:
    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment


def good(fetched: datetime) -> UsageReading:
    return UsageReading(
        METER, True, (UsageLimit(UsageLimitKind.WEEKLY_ALL, 3, RESET),), "", False, fetched.isoformat()
    )


def failed(error: str) -> UsageReading:
    return UsageReading.failed(METER, error, FETCHED.isoformat())


def fake_meter(*script: UsageReading) -> FakeUsageMeter:
    return FakeUsageMeter(AdapterIdentity("meter", "1", frozenset({"test"})), readings={METER: list(script)})


def second_read(tmp_path: Path, first: UsageReading, age: float) -> tuple[FakeUsageMeter, UsageReading]:
    later = FETCHED + timedelta(seconds=age)
    inner = fake_meter(first, good(later))
    clock = Clock(FETCHED)
    SharedUsageMeterCache(inner, tmp_path, now=clock).read(METER)
    clock.moment = later
    return inner, SharedUsageMeterCache(inner, tmp_path, now=clock).read(METER)


def test_the_hold_is_longer_than_the_sharing_window() -> None:
    assert RATE_LIMIT_HOLD_SECONDS > CACHE_SECONDS


@pytest.mark.parametrize("age", [CACHE_SECONDS + 1, RATE_LIMIT_HOLD_SECONDS - 1])
def test_a_rate_limited_meter_is_not_asked_again_during_the_hold(tmp_path: Path, age: float) -> None:
    inner, reading = second_read(tmp_path, failed("meter returned error rate_limit_error"), age)

    assert inner.reads == [METER]
    assert not reading.ok and "rate_limit" in reading.error


def test_a_rate_limited_meter_is_asked_again_once_the_hold_has_passed(tmp_path: Path) -> None:
    inner, reading = second_read(
        tmp_path, failed("meter returned error rate_limit_error"), RATE_LIMIT_HOLD_SECONDS + 1
    )

    assert inner.reads == [METER, METER]
    assert reading.ok


def test_another_failure_keeps_the_ordinary_sharing_window(tmp_path: Path) -> None:
    inner, reading = second_read(tmp_path, failed("meter transport failed (exit 28)"), CACHE_SECONDS + 1)

    assert inner.reads == [METER, METER]
    assert reading.ok


class ProviderMeter:
    """A provider meter that counts its reads and has no probe of its own."""

    def __init__(self, meter_id: str) -> None:
        self.meter_ids = frozenset({meter_id})
        self.reads: list[str] = []

    def read(self, meter_id: str) -> UsageReading:
        self.reads.append(meter_id)
        return good(FETCHED)


def test_the_two_provider_meter_answers_a_probe_without_reading_a_provider(tmp_path: Path) -> None:
    first, second = ProviderMeter(METER), ProviderMeter("meter-b")
    composite = CompositeUsageMeterAdapter(first, second)

    probe = SharedUsageMeterCache(composite, tmp_path).probe()

    assert probe.available is True
    assert first.reads == [] and second.reads == []


def test_a_two_provider_meter_with_no_meter_is_unavailable() -> None:
    assert CompositeUsageMeterAdapter().probe().available is False
