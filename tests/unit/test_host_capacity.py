"""Capacity rule, concurrency profile settings and the ``autobuild host`` command."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autobuild.adapters import BacklogTrackerAdapter
from autobuild.bootstrap.composition import (
    _launch_items,
    campaign_concurrency,
    host_capacity,
    item_concurrency,
)
from autobuild.bootstrap.profile import (
    ConfigurationError,
    ProfileOverrides,
    load_settings,
    read_capacity_policy,
)
from autobuild.cli import _host, _parser, host_payload
from autobuild.domain import (
    GIB,
    AdapterIdentity,
    CampaignRef,
    CampaignSelection,
    CapacityPolicy,
    HostCapacity,
    HostReading,
    WorkItem,
)
from autobuild.domain.models import PressurePolicy
from autobuild.enforcement.capacity import derive_capacity
from autobuild.ports import HostResourcePort
from autobuild.testing.fakes import FakeHostResources, FakeTrackerAdapter, NullHostResources


def host(logical: int | None, available_gib: float | None) -> HostReading:
    available = None if available_gib is None else int(available_gib * GIB)
    return HostReading(logical, 35.6, 64 * GIB, available, None, None, 1.0)


# --- capacity table: exact expected values from the acceptance contract -----


@pytest.mark.parametrize(
    ("logical", "available_gib", "policy", "items", "heavy"),
    [
        (24, 27.7, CapacityPolicy(), 6, 2),
        (4, 3.0, CapacityPolicy(), 1, 1),
        (64, 200.0, CapacityPolicy(), 8, 5),
        (24, None, CapacityPolicy(), 6, 2),
        (24, 27.7, CapacityPolicy(concurrency=2), 2, 2),
    ],
    ids=["workstation", "small-host", "ceiling", "memory-unmeasured", "profile-two"],
)
def test_the_capacity_table(logical, available_gib, policy, items, heavy) -> None:
    capacity = derive_capacity(host(logical, available_gib), policy)
    assert (capacity.items, capacity.heavy) == (items, heavy)


def test_the_memory_term_binds_when_it_is_smaller_than_the_cpu_term() -> None:
    # cpu 24 // 4 = 6; memory floor((14 - 8) / 3) = 2.
    assert derive_capacity(host(24, 14.0), CapacityPolicy()) == HostCapacity(
        2, 2, "auto", "auto", 6, 2
    )


def test_the_cpu_term_binds_when_it_is_smaller_than_the_memory_term() -> None:
    # cpu 12 // 4 = 3; memory floor((60 - 8) / 3) = 17; heavy 12 // 12 = 1.
    assert derive_capacity(host(12, 60.0), CapacityPolicy()) == HostCapacity(
        3, 1, "auto", "auto", 3, 17
    )


def test_the_capacity_terms_come_from_the_profile() -> None:
    # memory floor((27.7 - 4) / 2) = 11; cpu 6; max 4.
    policy = CapacityPolicy(reserve_gib=4, per_item_gib=2, max_concurrency=4)
    assert derive_capacity(host(24, 27.7), policy) == HostCapacity(4, 2, "auto", "auto", 6, 11)


def test_automatic_heavy_never_exceeds_automatic_items() -> None:
    # memory floor((9 - 8) / 3) = 0, so items clamp to 1; heavy 64 // 12 = 5
    # would exceed it without the cap.
    assert derive_capacity(host(64, 9.0), CapacityPolicy()) == HostCapacity(
        1, 1, "auto", "auto", 16, 0
    )


def test_an_unmeasured_cpu_count_gives_one_item_and_one_heavy_command() -> None:
    assert derive_capacity(host(None, 200.0), CapacityPolicy()) == HostCapacity(
        1, 1, "auto", "auto", None, 64
    )


def test_a_fully_unmeasured_host_gives_one() -> None:
    reading = NullHostResources(AdapterIdentity("null", "1", frozenset())).read(1.0)
    assert derive_capacity(reading, CapacityPolicy()) == HostCapacity(
        1, 1, "auto", "auto", None, None
    )


def test_an_explicit_heavy_concurrency_overrides_auto_and_never_exceeds_items() -> None:
    assert derive_capacity(host(24, 27.7), CapacityPolicy(heavy_concurrency=3)).heavy == 3
    capped = derive_capacity(host(24, 27.7), CapacityPolicy(concurrency=2, heavy_concurrency=5))
    assert (capped.items, capped.heavy, capped.heavy_source) == (2, 2, "profile")


def test_an_explicit_concurrency_is_not_bounded_by_the_automatic_ceiling() -> None:
    capacity = derive_capacity(host(4, 3.0), CapacityPolicy(concurrency=12))
    assert (capacity.items, capacity.items_source) == (12, "profile")


# --- profile -------------------------------------------------------------

BASE = """
[run]
harness = "codex"
{run}

[models]
builder = "m"
reviewer = "m"

[validator]
id = "tests"
argv = ["python", "-m", "pytest"]
{extra}
"""


def write_profile(tmp_path: Path, run: str = "", extra: str = "") -> Path:
    repository = tmp_path / "project"
    repository.mkdir(exist_ok=True)
    (repository / ".autobuild.toml").write_text(
        BASE.format(run=run, extra=extra), encoding="utf-8"
    )
    return repository


def test_a_profile_without_concurrency_settings_resolves_to_auto_with_the_default_terms(
    tmp_path: Path,
) -> None:
    settings = load_settings(write_profile(tmp_path), None, ProfileOverrides())
    assert settings.capacity == CapacityPolicy(None, None, 8.0, 3.0, 8)
    assert settings.pressure == PressurePolicy()


def test_concurrency_one_is_the_serial_setting(tmp_path: Path) -> None:
    repository = write_profile(tmp_path, run="concurrency = 1")
    settings = load_settings(repository, None, ProfileOverrides())
    assert settings.capacity.concurrency == 1
    assert campaign_concurrency(settings, object()) == (1, "profile", 1)


def test_the_profile_reads_every_pressure_key(tmp_path: Path) -> None:
    repository = write_profile(
        tmp_path,
        extra=(
            "[pressure]\nsample_seconds = 5\nread_seconds = 0.5\nwindow_seconds = 30\n"
            "calm_seconds = 90\nhigh_cpu_percent = 85\ncalm_cpu_percent = 70\n"
            "high_memory_percent = 12\nhigh_memory_gb = 6\ncritical_memory_percent = 6\n"
            "critical_memory_gb = 3\nhigh_commit_percent = 8\ncalm_memory_gb = 1.5\n"
            "decrease_seconds = 240\nincrease_seconds = 30\n"
        ),
    )
    assert load_settings(repository, None, ProfileOverrides()).pressure == PressurePolicy(
        5, 0.5, 30, 90, 85, 70, 12, 6, 6, 3, 8, 1.5, 240, 30
    )


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ("[pressure]\nspare = 1\n", "pressure contains unknown fields: spare"),
        ('[pressure]\nhigh_cpu_percent = "90"\n', "pressure.high_cpu_percent must be a number"),
        ("[pressure]\nhigh_cpu_percent = 101\n", "pressure.high_cpu_percent must be between"),
        ("[pressure]\nsample_seconds = 0\n", "pressure.sample_seconds must be a positive"),
        ("[pressure]\nread_seconds = 20\n", "pressure.read_seconds must not exceed"),
        ("[pressure]\ncalm_cpu_percent = 95\n", "pressure.calm_cpu_percent must not exceed"),
        ("[pressure]\nhigh_memory_gb = -1\n", "pressure.high_memory_gb must not be negative"),
    ],
)
def test_an_invalid_pressure_setting_is_refused(tmp_path: Path, extra: str, message: str) -> None:
    repository = write_profile(tmp_path, extra=extra)
    with pytest.raises(ConfigurationError, match=message):
        load_settings(repository, None, ProfileOverrides())


def test_an_explicit_auto_concurrency_is_accepted(tmp_path: Path) -> None:
    repository = write_profile(tmp_path, run='concurrency = "auto"')
    assert load_settings(repository, None, ProfileOverrides()).capacity.concurrency is None


@pytest.mark.parametrize(("value", "accepted"), [(64, True), (65, False), (1000, False)])
def test_an_explicit_concurrency_above_64_is_refused_at_profile_load(
    tmp_path: Path, value: int, accepted: bool
) -> None:
    repository = write_profile(tmp_path, run=f"concurrency = {value}")
    if accepted:
        assert load_settings(repository, None, ProfileOverrides()).capacity.concurrency == value
        return
    message = f"run.concurrency must be at most 64; {value} was named"
    with pytest.raises(ConfigurationError, match=message):
        load_settings(repository, None, ProfileOverrides())
    with pytest.raises(ConfigurationError, match=message):
        read_capacity_policy(repository, None)


def test_the_profile_reads_every_concurrency_key(tmp_path: Path) -> None:
    repository = write_profile(
        tmp_path,
        run="concurrency = 2\nheavy_concurrency = \"auto\"",
        extra="[capacity]\nreserve_gb = 6.5\nper_item_gb = 2\nmax_concurrency = 4\n",
    )
    expected = CapacityPolicy(2, None, 6.5, 2.0, 4)
    assert load_settings(repository, None, ProfileOverrides()).capacity == expected
    assert read_capacity_policy(repository, None) == expected


@pytest.mark.parametrize(
    ("run", "extra", "message"),
    [
        ("", "[capacity]\nreserve_gb = 8\nspare = 1\n", "capacity contains unknown fields: spare"),
        ("concurrency = 0", "", 'run.concurrency must be "auto" or a positive integer'),
        ('concurrency = "many"', "", 'run.concurrency must be "auto" or a positive integer'),
        ("heavy_concurrency = true", "", 'run.heavy_concurrency must be "auto" or a positive'),
        ("concurrency = 2.5", "", 'run.concurrency must be "auto" or a positive integer'),
        ("", "[capacity]\nper_item_gb = 0\n", "capacity.per_item_gb must be a positive number"),
        ("", "[capacity]\nreserve_gb = -1\n", "capacity.reserve_gb must not be negative"),
        ("", '[capacity]\nreserve_gb = "8"\n', "capacity.reserve_gb must be a number of GiB"),
        ("", "[capacity]\nmax_concurrency = 0\n", "capacity.max_concurrency must be a positive"),
        ("", "[capacity]\nmax_concurrency = 2.0\n", "capacity.max_concurrency must be a positive"),
    ],
)
def test_an_invalid_concurrency_setting_is_refused(
    tmp_path: Path, run: str, extra: str, message: str
) -> None:
    repository = write_profile(tmp_path, run=run, extra=extra)
    with pytest.raises(ConfigurationError, match=message):
        load_settings(repository, None, ProfileOverrides())
    with pytest.raises(ConfigurationError, match=message):
        read_capacity_policy(repository, None)


def test_the_host_command_reads_capacity_from_a_profile_without_models(tmp_path: Path) -> None:
    repository = tmp_path / "bare"
    repository.mkdir()
    (repository / ".autobuild.toml").write_text("[run]\nconcurrency = 3\n", encoding="utf-8")
    assert read_capacity_policy(repository, None) == CapacityPolicy(concurrency=3)
    assert read_capacity_policy(tmp_path / "no-profile-here", None) == CapacityPolicy()


# --- composition and command ---------------------------------------------


def fake_host(*readings: HostReading) -> FakeHostResources:
    return FakeHostResources(AdapterIdentity("fake-host", "1", frozenset()), readings=list(readings))


def test_host_capacity_applies_the_profile_to_the_reading(tmp_path: Path) -> None:
    repository = write_profile(tmp_path, run="concurrency = 2")
    adapter = fake_host(host(24, 27.7))

    reading, policy, capacity = host_capacity(repository, None, 1.5, adapter=adapter)

    assert isinstance(adapter, HostResourcePort)
    assert adapter.samples == [1.5]
    assert reading == host(24, 27.7)
    assert policy == CapacityPolicy(concurrency=2)
    assert capacity == HostCapacity(2, 2, "profile", "auto", 6, 6)


def run_host(argv: list[str], read) -> int:
    return _host(_parser().parse_args(["host", *argv]), read=read)


def test_the_host_command_prints_the_reading_and_capacity_in_gib(capsys) -> None:
    reading = HostReading(24, 35.64, 63 * GIB + GIB // 2, int(27.7 * GIB), 105 * GIB, 54 * GIB, 1.0)
    policy = CapacityPolicy()

    def read(repository, profile, sample_seconds):
        assert (repository, profile, sample_seconds) == (".", "p.toml", 2.0)
        return reading, policy, derive_capacity(reading, policy)

    assert run_host(["--json", "--profile", "p.toml", "--sample-seconds", "2"], read) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload == {
        "schema": "autobuild.host.v1",
        "reading": {
            "logical_cpus": 24,
            "cpu_busy_percent": 35.6,
            "sample_seconds": 1.0,
            "memory_total_gib": 63.5,
            "memory_available_gib": 27.7,
            "commit_total_gib": 105.0,
            "commit_available_gib": 54.0,
            "measured": dict.fromkeys(HostReading.MEASURED_FIELDS, True),
            "diagnostics": [],
        },
        "profile": {
            "concurrency": "auto",
            "heavy_concurrency": "auto",
            "reserve_gb": 8.0,
            "per_item_gb": 3.0,
            "max_concurrency": 8,
        },
        "capacity": {
            "items": 6,
            "heavy": 2,
            "items_source": "auto",
            "heavy_source": "auto",
            "cpu_term": 6,
            "memory_term": 6,
        },
    }


def test_the_host_payload_shows_unmeasured_fields_as_null() -> None:
    reading = HostReading.unmeasured("no reader", logical_cpus=24)
    payload = host_payload(reading, CapacityPolicy(), derive_capacity(reading, CapacityPolicy()))
    assert payload["reading"]["memory_available_gib"] is None
    assert payload["reading"]["measured"]["memory_available_bytes"] is False
    assert payload["reading"]["diagnostics"] == ["no reader"]
    assert (payload["capacity"]["items"], payload["capacity"]["heavy"]) == (6, 2)


def test_the_host_command_text_names_unmeasured_fields(capsys) -> None:
    reading = HostReading.unmeasured("no reader", logical_cpus=24)

    def read(_repository, _profile, _sample):
        return reading, CapacityPolicy(), derive_capacity(reading, CapacityPolicy())

    assert run_host([], read) == 0
    out = capsys.readouterr().out
    assert "memory available: unmeasured of unmeasured" in out
    assert "cpu busy: unmeasured\n" in out
    assert " over " not in out
    assert "note: no reader" in out
    assert "concurrency: items 6 (auto), heavy 2 (auto)" in out
    assert "run.concurrency is auto, the default; set it to 1 to build one item at a time" in out


def test_the_host_command_refuses_a_bad_profile_or_window(tmp_path: Path, capsys) -> None:
    repository = write_profile(tmp_path, extra="[capacity]\nspare = 1\n")

    def read(repository_arg, profile, sample):
        return host_capacity(repository_arg, profile, sample, adapter=fake_host(host(24, 27.7)))

    assert run_host(["--repository", str(repository), "--json"], read) == 2
    assert "capacity contains unknown fields: spare" in capsys.readouterr().err
    for window in ("0", "-1", "nan", "inf"):
        assert run_host([f"--sample-seconds={window}"], read) == 2
        assert "--sample-seconds must be a positive finite number" in capsys.readouterr().err


# --- campaign item concurrency ---------------------------------------------


def _settings(tmp_path: Path, name: str, run: str = ""):
    (tmp_path / name).mkdir()
    return load_settings(write_profile(tmp_path / name, run=run), None, ProfileOverrides())


def test_the_campaign_concurrency_comes_from_the_profile_the_tracker_or_the_host(
    tmp_path: Path,
) -> None:
    pinax_like = object()
    backlog = BacklogTrackerAdapter(tmp_path / "backlog-repo")
    reader = fake_host(host(24, 27.7))

    assert item_concurrency(_settings(tmp_path, "default"), backlog, host=reader) == (1, "backlog")
    assert item_concurrency(_settings(tmp_path, "named", "concurrency = 4"), backlog) == (
        4,
        "profile",
    )
    automatic = _settings(tmp_path, "auto", 'concurrency = "auto"')
    assert item_concurrency(automatic, backlog, host=reader) == (1, "backlog")
    assert reader.samples == []
    assert item_concurrency(automatic, pinax_like, host=reader) == (6, "auto")
    assert reader.samples == [0.5]


def test_the_campaign_heavy_limit_comes_from_the_capacity_rule(tmp_path: Path) -> None:
    pinax_like = object()
    reader = fake_host(host(24, 27.7))
    # One reading gives both: items 6 and heavy 24 // 12 = 2.
    assert campaign_concurrency(_settings(tmp_path, "auto"), pinax_like, host=reader) == (
        6,
        "auto",
        2,
    )
    assert reader.samples == [0.5]
    named = _settings(tmp_path, "named", "concurrency = 3\nheavy_concurrency = 5")
    assert campaign_concurrency(named, pinax_like, host=reader) == (3, "profile", 3)
    assert reader.samples == [0.5]
    profile_items = _settings(tmp_path, "items", "concurrency = 4")
    assert campaign_concurrency(profile_items, pinax_like, host=reader) == (4, "profile", 2)
    assert reader.samples == [0.5, 0.5]


def test_the_launch_preflight_checks_every_ready_item_only_when_more_than_one_runs() -> None:
    tracker = FakeTrackerAdapter(AdapterIdentity("tracker", "1", frozenset()))
    first, second, third = (
        WorkItem(f"item-{n}", f"title {n}", f"docs/brief-{n}.md", ("passes",)) for n in (1, 2, 3)
    )
    tracker.ready.extend([first, second, third])
    fence = CampaignSelection(exclude=("item-2",))

    serial = CampaignRef("campaign", Path("/repo"), selection=fence)
    tracker.queue.append(first)
    assert _launch_items(tracker, serial) == (first,)

    parallel = CampaignRef("campaign", Path("/repo"), selection=fence, concurrency=3)
    assert _launch_items(tracker, parallel) == (first, third)
