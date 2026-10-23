"""The ``[usage]`` profile table, the launch wiring and the ``autobuild usage`` command."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from autobuild.bootstrap import composition
from autobuild.bootstrap.profile import ConfigurationError, ProfileOverrides, load_settings
from autobuild.cli import _parser, _usage, usage_payload
from autobuild.domain import (
    AdapterIdentity,
    LeaseSurface,
    PreflightError,
    RepositoryIdentity,
    SurfaceKind,
    UsageLimit,
    UsageLimitKind,
    UsagePolicy,
    UsageProbeFailure,
    UsageReading,
)
from autobuild.testing import (
    FakeCommandAdapter,
    FakeHarnessAdapter,
    FakeKnowledgeAdapter,
    FakeLaneStateAdapter,
    FakeLeaseAdapter,
    FakeTrackerAdapter,
    FakeUsageMeter,
    FakeWorkspaceAdapter,
)

WEEKLY_RESET = "2026-10-26T06:00:00+00:00"

BASE = """
[run]
lanes = ["claude-code"]

[lanes.claude-code]
builder = "claude-opus-5-5"
reviewer = "fable"

[validator]
id = "tests"
argv = ["python", "-m", "pytest"]
"""


def identity(name: str) -> AdapterIdentity:
    return AdapterIdentity(name, "1", frozenset({"test"}))


def load(tmp_path: Path, profile: str):
    repository = tmp_path / "project"
    repository.mkdir(exist_ok=True)
    (repository / ".autobuild.toml").write_text(profile, encoding="utf-8")
    return load_settings(repository, None, ProfileOverrides())


def test_a_profile_without_usage_has_no_usage_policy(tmp_path: Path) -> None:
    assert load(tmp_path, BASE).usage is None


def test_the_usage_table_reads_every_key(tmp_path: Path) -> None:
    settings = load(
        tmp_path,
        BASE
        + "\n[usage]\nwarn_percent = 70\nstop_percent = 85\nfive_hour_stop_percent = 92\n"
        'on_probe_failure = "refuse"\n',
    )

    assert settings.usage == UsagePolicy(70, 85, 92, UsageProbeFailure.REFUSE)


def test_an_empty_usage_table_takes_the_documented_defaults(tmp_path: Path) -> None:
    assert load(tmp_path, BASE + "\n[usage]\n").usage == UsagePolicy(
        80, 90, 95, UsageProbeFailure.ALLOW
    )


@pytest.mark.parametrize(
    ("table", "message"),
    [
        ('unknown = "x"', "usage contains unknown fields: unknown"),
        ('on_probe_failure = "maybe"', "usage.on_probe_failure must be one of: allow, refuse"),
        ("stop_percent = 101", "usage.stop_percent must be above 0 and at most 100"),
        ('stop_percent = "90"', "usage.stop_percent must be a number"),
        ("warn_percent = 95", "usage.warn_percent must not exceed usage.stop_percent"),
    ],
)
def test_an_invalid_usage_table_is_refused(tmp_path: Path, table: str, message: str) -> None:
    with pytest.raises(ConfigurationError, match=message):
        load(tmp_path, BASE + f"\n[usage]\n{table}\n")


def test_without_usage_the_launch_builds_no_meter_and_runs_no_usage_preflight(tmp_path: Path) -> None:
    settings = load(tmp_path, BASE)
    lanes_raw = [("claude-code", FakeHarnessAdapter(identity("h"), meter_id="claude-subscription"))]

    meter = composition._usage_meter(settings, FakeCommandAdapter(identity("c")), tmp_path, lanes_raw)

    assert meter is None
    assert composition._usage_preflight(settings, lanes_raw, meter, FakeLaneStateAdapter(identity("l")), "c") is None


def test_with_usage_the_launch_picks_the_meter_the_lanes_declare(tmp_path: Path) -> None:
    settings = load(tmp_path, BASE + "\n[usage]\n")
    command = FakeCommandAdapter(identity("c"))

    claude = composition._usage_meter(
        settings, command, tmp_path, [("claude-code", FakeHarnessAdapter(identity("h"), meter_id="claude-subscription"))]
    )
    other = composition._usage_meter(
        settings, command, tmp_path, [("lane", FakeHarnessAdapter(identity("h"), meter_id="unknown-meter"))]
    )

    assert type(claude).__name__ == "ClaudeUsageMeterAdapter"
    assert type(other).__name__ == "NullUsageMeterAdapter"


def test_the_composed_lane_carries_the_raw_adapter_meter_declaration() -> None:
    adapter = FakeHarnessAdapter(identity("h"), meter_id="m", meter_scopes={"Fable": frozenset({"reviewer"})})

    lane = composition._lane("claude-code", object(), adapter)
    bare = composition._lane("codex", object(), FakeHarnessAdapter(identity("h")))

    assert lane.meter_id == "m"
    assert lane.meter_covers("Fable", "reviewer") and not lane.meter_covers("Fable", "builder")
    assert bare.meter_id is None and bare.meter_scope is None


def _launch(tmp_path: Path, monkeypatch, profile: str, percent: float, *, patch_meter: bool = True):
    settings = load(tmp_path, profile)
    tracker = FakeTrackerAdapter(identity("tracker"))
    meter = FakeUsageMeter(
        identity("meter"),
        readings={
            "claude-subscription": [
                UsageReading(
                    "claude-subscription",
                    True,
                    (UsageLimit(UsageLimitKind.WEEKLY_ALL, percent, WEEKLY_RESET),),
                )
            ]
        },
    )
    harness = FakeHarnessAdapter(identity("harness"), meter_id="claude-subscription")
    lane_state = FakeLaneStateAdapter(identity("lanes"))
    scratch = tmp_path / "scratch"
    runtime = (
        tracker,
        FakeWorkspaceAdapter(identity("workspace")),
        harness,
        FakeCommandAdapter(identity("command")),
        FakeKnowledgeAdapter(identity("knowledge")),
        FakeLeaseAdapter(identity("lease")),
        LeaseSurface(settings.repository / ".ergon", SurfaceKind.TRACKER),
        [("harness", "fake", "1")],
        RepositoryIdentity(settings.repository, "main", "feature", "origin", "base"),
        [("claude-code", harness)],
        lane_state,
        [{"name": "claude-code", "available": True, "version": "1"}],
        "claude-code",
    )
    monkeypatch.setattr(composition, "configure_scratch_environment", lambda root: scratch)
    monkeypatch.setattr(composition, "_runtime", lambda *args: runtime)
    monkeypatch.setattr(composition, "_preflight", lambda *args: ())
    if patch_meter:
        monkeypatch.setattr(composition, "_usage_meter", lambda *args: meter)
    return settings, tracker, meter, lane_state


def test_the_launch_is_refused_before_any_claim_at_the_stop_level(tmp_path: Path, monkeypatch) -> None:
    settings, tracker, meter, lane_state = _launch(tmp_path, monkeypatch, BASE + "\n[usage]\n", 90)

    with pytest.raises(PreflightError) as refused:
        composition.run_campaign(settings, allow_delivery=True)

    message = str(refused.value)
    assert "weekly_all" in message and "90%" in message and WEEKLY_RESET in message
    assert tracker.claims == []
    assert meter.reads == ["claude-subscription"]
    assert lane_state.cools == []


def test_the_launch_proceeds_one_point_below_the_stop_level(tmp_path: Path, monkeypatch) -> None:
    settings, tracker, meter, lane_state = _launch(tmp_path, monkeypatch, BASE + "\n[usage]\n", 89)

    result = composition.run_campaign(settings, allow_delivery=True)

    assert result["stop_reason"] == "queue_dry"
    assert meter.reads == ["claude-subscription"]
    assert lane_state.cools == []


def test_the_launch_without_usage_never_reads_the_meter(tmp_path: Path, monkeypatch) -> None:
    settings, tracker, meter, lane_state = _launch(
        tmp_path, monkeypatch, BASE, 100, patch_meter=False
    )

    result = composition.run_campaign(settings, allow_delivery=True)

    assert result["stop_reason"] == "queue_dry"
    assert meter.reads == []


def test_the_usage_command_prints_the_normalised_reading_as_json(capsys) -> None:
    reading = UsageReading(
        "claude-subscription",
        True,
        (UsageLimit(UsageLimitKind.WEEKLY_SCOPED, 12.0, None, "Fable"),),
        extra_usage_enabled=False,
        fetched_at="2026-10-21T20:00:00+00:00",
    )
    args = _parser().parse_args(["usage", "--json", "--scratch-root", "scratch"])

    code = _usage(args, read=lambda scratch: reading)

    printed = json.loads(capsys.readouterr().out)
    assert code == 0
    assert printed == usage_payload(reading)
    assert printed["limits"] == [
        {"kind": "weekly_scoped", "scope": "Fable", "percent": 12.0, "resets_at": None}
    ]


def test_the_usage_command_exits_one_with_a_clean_error_when_reading_raises(capsys) -> None:
    def raising(scratch):
        raise OverflowError("date value out of range")

    code = _usage(argparse.Namespace(json=True, scratch_root=None), read=raising)

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert captured.err.strip() == "usage meter unavailable: OverflowError"


def test_the_usage_command_exits_one_when_the_meter_fails(capsys) -> None:
    args = argparse.Namespace(json=False, scratch_root=None)

    code = _usage(args, read=lambda scratch: UsageReading.failed("claude-subscription", "no token"))

    assert code == 1
    assert "unavailable: no token" in capsys.readouterr().out
