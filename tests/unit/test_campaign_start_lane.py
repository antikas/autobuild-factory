"""The ``campaign.started`` context names the lane the campaign starts on."""

from __future__ import annotations

from pathlib import Path

from autobuild.application import CampaignRunner
from autobuild.bootstrap import composition
from autobuild.bootstrap.profile import ProfileOverrides, load_settings
from autobuild.domain import (
    AdapterIdentity,
    EffortLevel,
    LaneCooling,
    LaneSignal,
    LaneSignalKind,
    LeaseSurface,
    RepositoryIdentity,
    SurfaceKind,
)
from autobuild.testing import (
    FakeCommandAdapter,
    FakeHarnessAdapter,
    FakeKnowledgeAdapter,
    FakeLaneStateAdapter,
    FakeLeaseAdapter,
    FakeTrackerAdapter,
    FakeWorkspaceAdapter,
)

ONE_LANE = """
[run]
lanes = ["claude-code"]

[lanes.claude-code]
builder = "first-builder"
reviewer = "first-reviewer"
specialist = "first-specialist"
builder_effort = "high"
specialist_effort = "max"

[validator]
id = "tests"
argv = ["python", "-m", "pytest"]
"""

TWO_LANES = ONE_LANE.replace(
    'lanes = ["claude-code"]', 'lanes = ["claude-code", "codex"]'
) + """
[lanes.codex]
builder = "second-builder"
reviewer = "second-reviewer"
specialist = "second-specialist"
builder_effort = "low"
reviewer_effort = "medium"
specialist_effort = "xhigh"
"""


def identity(name: str) -> AdapterIdentity:
    return AdapterIdentity(name, "1", frozenset({"test"}))


def _launch(
    tmp_path: Path,
    monkeypatch,
    profile: str,
    manifest: list[dict[str, object]],
    lane_state: FakeLaneStateAdapter,
):
    repository = tmp_path / "project"
    repository.mkdir()
    (repository / ".autobuild.toml").write_text(profile, encoding="utf-8")
    settings = load_settings(repository, None, ProfileOverrides())
    harness = FakeHarnessAdapter(identity("harness"))
    runtime = (
        FakeTrackerAdapter(identity("tracker")),
        FakeWorkspaceAdapter(identity("workspace")),
        harness,
        FakeCommandAdapter(identity("command")),
        FakeKnowledgeAdapter(identity("knowledge")),
        FakeLeaseAdapter(identity("lease")),
        LeaseSurface(settings.repository / ".ergon", SurfaceKind.TRACKER),
        [("harness", "fake", "1")],
        RepositoryIdentity(settings.repository, "main", "feature", "origin", "base"),
        [(str(entry["name"]), harness) for entry in manifest],
        lane_state,
        manifest,
        next((str(entry["name"]) for entry in manifest if entry["available"]), None),
    )
    contexts = []

    class _CapturingRunner(CampaignRunner):
        def run(self, *args, **kwargs):
            contexts.append(kwargs["context"])
            return super().run(*args, **kwargs)

    monkeypatch.setattr(composition, "configure_scratch_environment", lambda root: tmp_path / "scratch")
    monkeypatch.setattr(composition, "_runtime", lambda *args: runtime)
    monkeypatch.setattr(composition, "_preflight", lambda *args: ())
    monkeypatch.setattr(composition, "CampaignRunner", _CapturingRunner)
    composition.run_campaign(settings, allow_delivery=True)
    assert len(contexts) == 1
    return contexts[0]


def _manifest(first_capable: bool) -> list[dict[str, object]]:
    return [
        {"name": "claude-code", "available": first_capable, "version": "1", "detail": ""},
        {"name": "codex", "available": True, "version": "1", "detail": ""},
    ]


def _cooling(lane: str) -> LaneCooling:
    return LaneCooling(
        lane=lane,
        cooled_until="2999-01-01T00:00:00+00:00",
        signature="rate_limit",
        last_failure_at="2026-11-19T00:00:00+00:00",
        campaign_id="earlier",
    )


def _assert_second_lane(context) -> None:
    assert context.harness == "codex"
    assert dict(context.models) == {
        "builder": "second-builder",
        "reviewer": "second-reviewer",
        "specialist": "second-specialist",
    }
    assert dict(context.efforts) == {
        "builder": EffortLevel.LOW,
        "reviewer": EffortLevel.MEDIUM,
        "specialist": EffortLevel.XHIGH,
    }


def test_an_incapable_first_lane_starts_the_campaign_on_the_second(
    tmp_path: Path, monkeypatch
) -> None:
    context = _launch(
        tmp_path, monkeypatch, TWO_LANES, _manifest(False), FakeLaneStateAdapter(identity("lanes"))
    )

    _assert_second_lane(context)


def test_a_first_lane_cooling_from_an_earlier_campaign_starts_the_campaign_on_the_second(
    tmp_path: Path, monkeypatch
) -> None:
    lane_state = FakeLaneStateAdapter(
        identity("lanes"), cooled={"claude-code": _cooling("claude-code")}
    )

    context = _launch(tmp_path, monkeypatch, TWO_LANES, _manifest(True), lane_state)

    _assert_second_lane(context)


def test_a_first_lane_cooled_by_the_usage_preflight_starts_the_campaign_on_the_second(
    tmp_path: Path, monkeypatch
) -> None:
    def cooling_preflight(settings, lanes_raw, meter, lane_state, campaign_id):
        signal = LaneSignal(LaneSignalKind.USAGE, reset_at="2999-01-01T00:00:00+00:00")
        lane_state.cool("claude-code", signal, campaign_id)
        return None

    monkeypatch.setattr(composition, "_usage_preflight", cooling_preflight)

    context = _launch(
        tmp_path, monkeypatch, TWO_LANES, _manifest(True), FakeLaneStateAdapter(identity("lanes"))
    )

    _assert_second_lane(context)


def test_every_lane_cooling_starts_the_campaign_line_on_the_first_lane(tmp_path: Path) -> None:
    repository = tmp_path / "project"
    repository.mkdir()
    (repository / ".autobuild.toml").write_text(TWO_LANES, encoding="utf-8")
    settings = load_settings(repository, None, ProfileOverrides())
    lane_state = FakeLaneStateAdapter(
        identity("lanes"),
        cooled={"claude-code": _cooling("claude-code"), "codex": _cooling("codex")},
    )

    lane = composition._starting_lane(settings, _manifest(True), lane_state)

    assert lane.name == "claude-code"
    assert lane.builder_model == "first-builder"


def test_a_single_lane_profile_starts_on_its_lane(tmp_path: Path, monkeypatch) -> None:
    manifest = [{"name": "claude-code", "available": True, "version": "1", "detail": ""}]

    context = _launch(
        tmp_path, monkeypatch, ONE_LANE, manifest, FakeLaneStateAdapter(identity("lanes"))
    )

    assert context.harness == "claude-code"
    assert dict(context.models) == {
        "builder": "first-builder",
        "reviewer": "first-reviewer",
        "specialist": "first-specialist",
    }
    assert dict(context.efforts) == {
        "builder": EffortLevel.HIGH,
        "reviewer": None,
        "specialist": EffortLevel.MAX,
    }
