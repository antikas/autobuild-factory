"""The Agent SDK lane: profile key, routing, credit stops, key isolation and the
probe-only preflight command, against fakes and a fake SDK client."""

from __future__ import annotations

import importlib.abc
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from autobuild.adapters.claude_sdk_harness import ClaudeSdkHarnessAdapter
from autobuild.application import CampaignRunner, ItemWorkflow, Lane, WorkflowPorts
from autobuild.bootstrap import composition
from autobuild.bootstrap.builtins import register_first_party_harnesses
from autobuild.bootstrap.profile import ConfigurationError, ProfileOverrides, load_settings
from autobuild.bootstrap.registry import AdapterRegistry
from autobuild.cli import main
from autobuild.domain import (
    AdapterIdentity,
    CampaignRef,
    CampaignStopReason,
    CommandResult,
    DeliveryMode,
    DiffEvidence,
    ItemDisposition,
    ItemExecutionSpec,
    LaneSignalKind,
    PortKind,
    ReviewDecision,
    SeatOutcome,
    SeatResult,
    SeatUsage,
    BuilderReport,
    ToolPolicy,
    UsageLimit,
    UsageLimitKind,
    UsagePolicy,
    UsageReading,
    WorkItem,
    WorkspaceRef,
)
from autobuild.enforcement.preflight import INTERCEPTION_VARIABLES
from autobuild.testing import (
    FakeAgentSdkClient,
    FakeCommandAdapter,
    FakeHarnessAdapter,
    FakeKnowledgeAdapter,
    FakeLaneStateAdapter,
    FakeLeaseAdapter,
    FakeRunRecordAdapter,
    FakeTrackerAdapter,
    FakeUsageMeter,
    FakeWorkspaceAdapter,
)

KEY_SENTINEL = "sk-ant-api03-U3-UNIT-SENTINEL-0000"
METER = "meter-a"
WEEKLY_RESET = "2026-10-26T06:00:00+00:00"
POLICY = UsagePolicy(warn_percent=80, stop_percent=90, five_hour_stop_percent=95)
CREDIT_EXHAUSTED = "Your credit balance is too low to access the Anthropic API."
SPEND_LIMIT = (
    "You have reached your specified workspace API usage limits. "
    "You will regain access on 2026-11-01 at 00:00 UTC."
)

PROFILE = """
[run]
lanes = ["claude-code", "claude-sdk"]

[lanes.claude-code]
builder = "claude-opus-5-5"
reviewer = "fable"

[lanes.claude-sdk]
builder = "claude-opus-5-5"
reviewer = "fable"
key_file = "secrets/sdk.key"

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


# -- profile -------------------------------------------------------------------


def test_a_lane_table_with_the_key_file_key_loads_and_resolves_against_the_profile(tmp_path: Path) -> None:
    settings = load(tmp_path, PROFILE)

    lanes = {lane.name: lane for lane in settings.lanes}
    assert lanes["claude-sdk"].key_file == (tmp_path / "project" / "secrets" / "sdk.key").resolve()
    assert lanes["claude-code"].key_file is None


def test_a_lane_table_with_an_unknown_key_is_still_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="lanes.claude-sdk contains unknown fields: api_key"):
        load(tmp_path, PROFILE.replace('key_file = "secrets/sdk.key"', 'key_file = "k"\napi_key = "x"'))


@pytest.mark.parametrize(
    "profile",
    [
        PROFILE.replace('key_file = "secrets/sdk.key"', 'key_file = ""'),
        PROFILE.replace('lanes = ["claude-code", "claude-sdk"]', 'lanes = ["claude-code"]').replace(
            'key_file = "secrets/sdk.key"', "key_file = 7"
        ),
    ],
)
def test_an_empty_or_non_string_key_file_is_refused_on_a_running_or_parked_lane(
    tmp_path: Path, profile: str
) -> None:
    with pytest.raises(ConfigurationError, match="lanes.claude-sdk.key_file must be a non-empty string"):
        load(tmp_path, profile)


# -- import isolation ------------------------------------------------------------


class _SdkImportBlocker(importlib.abc.MetaPathFinder):
    def __init__(self) -> None:
        self.attempts: list[str] = []

    def find_spec(self, name, path=None, target=None):
        if name == "claude_agent_sdk" or name.startswith("claude_agent_sdk."):
            self.attempts.append(name)
            raise ModuleNotFoundError(f"No module named {name!r}")
        return None


@pytest.fixture
def sdk_imports(monkeypatch: pytest.MonkeyPatch) -> _SdkImportBlocker:
    blocker = _SdkImportBlocker()
    for name in [name for name in sys.modules if name.startswith("claude_agent_sdk")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(sys, "meta_path", [blocker, *sys.meta_path])
    monkeypatch.setattr(AdapterRegistry, "load_entry_points", lambda self: None)
    return blocker


def _version_and_auth(tmp_path: Path) -> FakeCommandAdapter:
    version = tmp_path / "version.txt"
    auth = tmp_path / "auth.txt"
    version.write_text("2.1.301", encoding="utf-8")
    auth.write_text('{"loggedIn": true}', encoding="utf-8")
    return FakeCommandAdapter(
        identity("command"),
        scripted_results=[
            CommandResult("v", 0, str(version), str(version), "s", "e"),
            CommandResult("a", 0, str(auth), str(auth), "s", "e"),
        ],
    )


def test_a_profile_without_the_lane_imports_nothing_from_the_sdk(tmp_path: Path, sdk_imports) -> None:
    profile = PROFILE.replace('lanes = ["claude-code", "claude-sdk"]', 'lanes = ["claude-code"]')
    settings = load(tmp_path, profile + f'\n[harness]\ncommand = ["{Path(sys.executable).as_posix()}"]\n')

    _lanes, active, _manifest = composition._build_lanes(
        settings, _version_and_auth(tmp_path), tmp_path / "scratch", FakeLaneStateAdapter(identity("l")), "c"
    )

    assert active is not None and active[0] == "claude-code"
    assert sdk_imports.attempts == []
    assert not any(name.startswith("claude_agent_sdk") for name in sys.modules)


def test_a_profile_with_the_lane_reaches_for_the_sdk_and_cools_it_when_absent(tmp_path: Path, sdk_imports) -> None:
    settings = load(tmp_path, PROFILE.replace('lanes = ["claude-code", "claude-sdk"]', 'lanes = ["claude-sdk"]'))
    key = tmp_path / "project" / "secrets" / "sdk.key"
    key.parent.mkdir()
    key.write_text(KEY_SENTINEL, encoding="utf-8")
    state = FakeLaneStateAdapter(identity("l"))

    with pytest.raises(RuntimeError, match=r"autobuild-factory\[sdk\]"):
        composition._build_lanes(settings, FakeCommandAdapter(identity("c")), tmp_path / "scratch", state, "c")

    assert sdk_imports.attempts == ["claude_agent_sdk"]
    assert [(lane, signal.kind) for lane, signal, _campaign in state.cools] == [("claude-sdk", LaneSignalKind.PROBE)]


# -- routing, credit stops and key isolation through the item workflow -----------------


@dataclass
class RecordingWorkspace(FakeWorkspaceAdapter):
    commits: list[object] = field(default_factory=list)

    def commit_item(self, workspace, request) -> str:
        self.commits.append(request)
        return "item-commit"


def work_item() -> WorkItem:
    return WorkItem("item-1", "test item", "plan", ("passes",))


def spec() -> ItemExecutionSpec:
    return ItemExecutionSpec(
        work_item(),
        Path("brief.md"),
        "validator",
        ("python", "-m", "pytest"),
        ToolPolicy(frozenset({"read", "write", "python"}), (Path("/worktree"),)),
        "builder",
        "reviewer",
        "specialist",
        max_corrections=2,
        delivery_mode=DeliveryMode.PROTECTED_DEFAULT,
        delivery_target_branch="main",
        delivery_target_revision="base",
    )


def result_record(**fields) -> dict:
    record = {
        "message": "ResultMessage",
        "subtype": "success",
        "is_error": False,
        "num_turns": 1,
        "result": "done",
        "structured_output": None,
        "usage": {"input_tokens": 5, "output_tokens": 3},
        "total_cost_usd": 0.02,
        "api_error_status": None,
    }
    record.update(fields)
    return record


BUILT = result_record(structured_output={"summary": "built", "report_ref": "r"})
PASSED = result_record(structured_output={"decision": "pass", "findings": [], "evidence_ref": "v"})


def credit_stop(text: str) -> list[dict]:
    return [
        {"message": "AssistantMessage", "content": [], "error": "billing_error"},
        result_record(is_error=True, api_error_status=400, result=text),
    ]


def sdk_adapter(tmp_path: Path, client: FakeAgentSdkClient) -> ClaudeSdkHarnessAdapter:
    key = tmp_path / "secrets" / "sdk.key"
    key.parent.mkdir(parents=True, exist_ok=True)
    key.write_text(KEY_SENTINEL, encoding="utf-8")
    return ClaudeSdkHarnessAdapter(
        FakeCommandAdapter(identity("command")),
        tmp_path / "harness" / "claude-sdk",
        key,
        model_map={"builder": "claude-opus-5-5", "reviewer": "fable"},
        client=client,
    )


def fake_builder() -> SeatResult:
    return SeatResult(
        "fake-builder", SeatOutcome.SUCCEEDED, BuilderReport("report", "built"), "raw",
        SeatUsage(source="fake"), "start", "end",
    )


def fake_review() -> SeatResult:
    from autobuild.domain import ReviewVerdict

    return SeatResult(
        "fake-review", SeatOutcome.SUCCEEDED,
        ReviewVerdict("item-1", ReviewDecision.PASS, (), "verdict"), "raw",
        SeatUsage(source="fake"), "start", "end",
    )


def workflow_ports(lanes, *, meter=None, lane_state=None, queue=False) -> WorkflowPorts:
    workspace_ref = WorkspaceRef(Path("/worktree"), "run", "base", "lease")
    workspace = RecordingWorkspace(
        identity("workspace"),
        workspace=workspace_ref,
        diffs=[DiffEvidence(workspace_ref, "revision-0", (), "patch:0")],
    )
    command = tmp_command()
    return WorkflowPorts(
        FakeTrackerAdapter(identity("tracker"), queue=[work_item()] if queue else []),
        workspace,
        lanes[0][1],
        command,
        FakeRunRecordAdapter(identity("records")),
        FakeKnowledgeAdapter(identity("knowledge")),
        lanes=tuple(
            Lane(
                name,
                harness,
                meter_id=harness.meter_id,
                meter_scope=harness.meter_scope_covers if harness.meter_id else None,
            )
            for name, harness in lanes
        ),
        lane_state=lane_state if lane_state is not None else FakeLaneStateAdapter(identity("lanes")),
        usage_meter=meter,
        usage_policy=POLICY if meter is not None else None,
        usage_retry_wait=lambda seconds: None,
    )


def tmp_command() -> FakeCommandAdapter:
    return FakeCommandAdapter(
        identity("command"),
        scripted_results=[CommandResult("validate", 0, "stdout:v", "stderr:v", "start", "end")],
    )


def campaign() -> CampaignRef:
    return CampaignRef("campaign", Path("/repo"), max_items=1)


def test_with_the_subscription_lane_at_the_stop_level_the_seats_run_on_the_sdk_lane(tmp_path: Path) -> None:
    meter = FakeUsageMeter(
        identity("meter"),
        readings={METER: [UsageReading(METER, True, (UsageLimit(UsageLimitKind.WEEKLY_ALL, 90, WEEKLY_RESET),))]},
    )
    subscription = FakeHarnessAdapter(identity("claude-code"), meter_id=METER)
    client = FakeAgentSdkClient(scripts=[[BUILT], [PASSED]])
    state = FakeLaneStateAdapter(identity("lanes"))
    ports = workflow_ports(
        [("claude-code", subscription), ("claude-sdk", sdk_adapter(tmp_path, client))],
        meter=meter, lane_state=state,
    )

    outcome = ItemWorkflow(ports).run(campaign(), spec(), ports.records.create(campaign()))

    assert outcome.disposition is ItemDisposition.ACCEPTED
    assert subscription.requests == []
    assert len(client.specs) == 2
    assert [lane for lane, _signal, _campaign in state.cools] == ["claude-code"]
    assert meter.reads == [METER]


def test_with_both_lanes_cooled_the_item_parks_cleanly(tmp_path: Path) -> None:
    meter = FakeUsageMeter(
        identity("meter"),
        readings={METER: [UsageReading(METER, True, (UsageLimit(UsageLimitKind.WEEKLY_ALL, 95, WEEKLY_RESET),))]},
    )
    subscription = FakeHarnessAdapter(identity("claude-code"), meter_id=METER)
    client = FakeAgentSdkClient(scripts=[credit_stop(CREDIT_EXHAUSTED)])
    state = FakeLaneStateAdapter(identity("lanes"))
    ports = workflow_ports(
        [("claude-code", subscription), ("claude-sdk", sdk_adapter(tmp_path, client))],
        meter=meter, lane_state=state, queue=True,
    )

    outcome = CampaignRunner(ports).run(campaign(), lambda item: spec())

    # Exhausted lanes hold admissions; the item bound of one ends the campaign.
    assert outcome.stop_reason is CampaignStopReason.ITEM_BOUND
    held = [event for event in ports.records.events if event.event_type == "campaign.hold_started"]
    assert [event.payload["condition"] for event in held] == ["lanes_exhausted"]
    assert outcome.items[0].disposition is ItemDisposition.PARKED
    assert outcome.items[0].lanes_exhausted
    assert ports.tracker.parked[0][1].startswith("claude-sdk:quota")
    assert [lane for lane, _signal, _campaign in state.cools] == ["claude-code", "claude-sdk"]


@pytest.mark.parametrize(
    "text, reset_at",
    [(CREDIT_EXHAUSTED, None), (SPEND_LIMIT, "2026-11-01T00:00:00+00:00")],
)
def test_a_credit_or_spend_limit_stop_cools_the_sdk_lane_and_is_not_a_builder_failure(
    tmp_path: Path, text: str, reset_at: str | None
) -> None:
    client = FakeAgentSdkClient(scripts=[credit_stop(text)])
    fallback = FakeHarnessAdapter(identity("fallback"), scripted_results=[fake_builder(), fake_review()])
    state = FakeLaneStateAdapter(identity("lanes"))
    ports = workflow_ports(
        [("claude-sdk", sdk_adapter(tmp_path, client)), ("fallback", fallback)], lane_state=state
    )

    outcome = ItemWorkflow(ports).run(campaign(), spec(), ports.records.create(campaign()))

    assert outcome.disposition is ItemDisposition.ACCEPTED
    assert [request.seat.value for request in fallback.requests] == ["builder", "reviewer"]
    assert [(lane, signal.kind, signal.reset_at) for lane, signal, _c in state.cools] == [
        ("claude-sdk", LaneSignalKind.QUOTA, reset_at)
    ]


def test_the_key_appears_in_no_brief_log_run_record_or_commit(tmp_path: Path) -> None:
    client = FakeAgentSdkClient(scripts=[[BUILT], [PASSED]])
    adapter = sdk_adapter(tmp_path, client)
    ports = workflow_ports([("claude-sdk", adapter)], queue=True)

    outcome = CampaignRunner(ports).run(campaign(), lambda item: spec())

    assert outcome.items[0].disposition is ItemDisposition.ACCEPTED
    surfaces = [
        repr(ports.records.events),
        repr(ports.records.evidence),
        repr(ports.records.evidence_files),
        repr(ports.records.summaries),
        repr(ports.tracker.closed),
        repr(ports.workspace.commits),
        repr(client.specs),
    ]
    files = [path for path in (tmp_path / "harness").rglob("*") if path.is_file()]
    surfaces.extend(path.read_text(encoding="utf-8", errors="replace") for path in files)
    assert ports.workspace.commits and files
    assert all(KEY_SENTINEL not in surface for surface in surfaces)


# -- autobuild preflight --json ------------------------------------------------------


def _preflight_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, key: bool):
    repository = tmp_path / "project"
    repository.mkdir()
    profile = PROFILE.replace(
        'argv = ["python", "-m", "pytest"]', f'argv = ["{Path(sys.executable).as_posix()}", "-c", "pass"]'
    ) + '\n[tracker]\nkind = "pinax"\n'
    (repository / ".autobuild.toml").write_text(profile, encoding="utf-8")
    (repository / "brief.md").write_text("brief", encoding="utf-8")
    if key:
        (repository / "secrets").mkdir()
        (repository / "secrets" / "sdk.key").write_text(KEY_SENTINEL, encoding="utf-8")
    tracker = FakeTrackerAdapter(
        identity("tracker"), queue=[WorkItem("item-1", "ready item", "brief.md", ("passes",))]
    )
    tracker.commits_itself = False
    out = tmp_path / "out.txt"
    out.write_text("ok", encoding="utf-8")
    commands = FakeCommandAdapter(
        identity("command"),
        scripted_results=[CommandResult(f"c{i}", 0, str(out), str(out), "s", "e") for i in range(2)],
    )
    scratch = tmp_path / "scratch"
    client = FakeAgentSdkClient(sdk_version="0.2-fake")

    def register(registry) -> None:
        builtins = AdapterRegistry()
        register_first_party_harnesses(builtins)
        registry.register(
            PortKind.HARNESS,
            "claude-code",
            lambda config: FakeHarnessAdapter(
                identity("claude-code"), available=False, diagnostics=("claude executable was not found",)
            ),
        )
        registry.register(
            PortKind.HARNESS,
            "claude-sdk",
            lambda config: builtins.create(PortKind.HARNESS, "claude-sdk", config),
        )

    for name in INTERCEPTION_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("autobuild.adapters.claude_sdk_harness.ClaudeAgentSdkClient", lambda: client)
    monkeypatch.setattr(composition, "register_first_party_harnesses", register)
    monkeypatch.setattr(AdapterRegistry, "load_entry_points", lambda self: None)
    monkeypatch.setattr(composition, "configure_scratch_environment", lambda root: scratch)
    monkeypatch.setattr(composition, "_command_adapter", lambda root: commands)
    monkeypatch.setattr(composition, "PinaxTrackerAdapter", lambda *args, **kwargs: tracker)
    monkeypatch.setattr(
        composition, "GitWorkspaceAdapter", lambda *args, **kwargs: FakeWorkspaceAdapter(identity("workspace"))
    )
    monkeypatch.setattr(
        composition, "FilesystemLeaseAdapter", lambda *args, **kwargs: FakeLeaseAdapter(identity("lease"))
    )
    return repository, tracker, scratch


def test_preflight_json_reports_each_lane_capability_and_claims_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repository, tracker, scratch = _preflight_fixture(tmp_path, monkeypatch, key=True)

    code = main(["preflight", "--json", "--repository", str(repository)])

    report = json.loads(capsys.readouterr().out)
    assert code == 0, report
    assert report["ok"] is True
    assert report["active_lane"] == "claude-sdk"
    assert [(lane["name"], lane["capable"]) for lane in report["lanes"]] == [
        ("claude-code", False),
        ("claude-sdk", True),
    ]
    assert report["lanes"][0]["detail"] == "claude executable was not found"
    assert report["lanes"][1]["version"] == "0.2-fake"
    transport = next(probe for probe in report["preflight"] if probe["name"] == "transport")
    assert "runs seats in process" in transport["detail"]
    assert tracker.claims == []
    assert not (scratch / "lanes.json").exists()
    assert KEY_SENTINEL not in json.dumps(report)


def test_preflight_json_without_the_key_file_reports_no_capable_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repository, tracker, scratch = _preflight_fixture(tmp_path, monkeypatch, key=False)

    code = main(["preflight", "--json", "--repository", str(repository)])

    report = json.loads(capsys.readouterr().out)
    assert code == 1
    assert report["ok"] is False
    assert report["error"] == "no capable harness lane"
    assert "API key file is missing" in report["lanes"][1]["detail"]
    assert tracker.claims == []
    assert not (scratch / "lanes.json").exists()
