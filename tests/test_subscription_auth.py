"""Subscription routing must not inherit a paid or third-party dispatch path."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from autobuild.adapters.claude_harness import ClaudeCodeHarnessAdapter
from autobuild.adapters.codex_harness import CodexHarnessAdapter
from autobuild.adapters.codex_subscription import _reading, _subscription_removed_environment
from autobuild.domain import AdapterIdentity, CommandResult, Seat, SeatOutcome, SeatRequest, ToolPolicy, UsageReading, WorkspaceRef
from autobuild.testing import FakeCommandAdapter


@pytest.fixture(autouse=True)
def credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A credentials file with no stored expiry, in place of the host's own."""

    path = tmp_path / "injected-credentials.json"
    path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "synthetic-token"}}), encoding="utf-8")
    monkeypatch.setattr("autobuild.adapters.claude_usage.default_credentials_path", lambda: path)
    return path


def _result(tmp_path: Path, text: str) -> CommandResult:
    stdout = tmp_path / "stdout.txt"
    stderr = tmp_path / "stderr.txt"
    stdout.write_text(text, encoding="utf-8")
    stderr.write_text("", encoding="utf-8")
    return CommandResult("seat", 0, str(stdout), str(stderr), "start", "end")


def _request(tmp_path: Path, model: str) -> SeatRequest:
    root = tmp_path / "worktree"
    root.mkdir()
    (tmp_path / "brief.md").write_text("# Brief\n", encoding="utf-8")
    return SeatRequest(
        "run", "item", Seat.REVIEWER, model, tmp_path / "brief.md",
        WorkspaceRef(root, "branch", "base", "lease"), ToolPolicy(frozenset({"read"}), (root,)),
        "review", "review-verdict-v1", 30,
    )


def test_codex_subscription_child_ignores_user_config_and_removes_routing_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARBITRARY_PROVIDER", "third-party")
    monkeypatch.setenv("ARBITRARY_API_KEY", "secret")
    verdict = json.dumps({"decision": "pass", "findings": [], "evidence_ref": "ok", "inspected": [], "acceptance": []})
    commands = FakeCommandAdapter(
        AdapterIdentity("command", "1"),
        scripted_results=[_result(tmp_path, json.dumps({"type": "item.completed", "item": {"text": verdict}}))],
    )
    adapter = CodexHarnessAdapter(
        commands, tmp_path / "harness", (sys.executable,),
        {"reviewer": "gpt-5.6-sol"}, subscription_only=True,
    )

    result = adapter.invoke(_request(tmp_path, "reviewer"))

    assert result.outcome is SeatOutcome.SUCCEEDED
    dispatched = commands.requests[0]
    assert "--ignore-user-config" in dispatched.argv
    assert "ARBITRARY_PROVIDER" in dispatched.remove_environment
    assert "ARBITRARY_API_KEY" in dispatched.remove_environment
    assert "OPENAI_API_KEY" in dispatched.remove_environment


def test_subscription_model_tiers_are_positive_lists_and_non_subscription_mode_is_unchanged(tmp_path: Path) -> None:
    command = FakeCommandAdapter(AdapterIdentity("command", "1"))
    refused = CodexHarnessAdapter(
        command, tmp_path / "refused", (sys.executable,), {"reviewer": "paid-alternate"}, subscription_only=True,
    ).probe()
    ordinary = CodexHarnessAdapter(
        command, tmp_path / "ordinary", (sys.executable,), {"reviewer": "paid-alternate"}, subscription_only=False,
    )

    assert refused.available is False
    assert "non-included model tier" in refused.diagnostics[0]
    assert ordinary._model("reviewer") == "paid-alternate"


def test_claude_subscription_auth_does_not_fall_back_to_generic_cli_login(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    commands = FakeCommandAdapter(
        AdapterIdentity("command", "1"),
        scripted_results=[_result(tmp_path, json.dumps({
            "loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "max"
        }))],
    )
    adapter = ClaudeCodeHarnessAdapter(
        commands, tmp_path / "harness", (sys.executable,),
        {"reviewer": "claude-opus-5-5"}, subscription_only=True,
    )
    monkeypatch.setattr(
        "autobuild.adapters.claude_harness.ClaudeUsageMeterAdapter.read",
        lambda *_: UsageReading("claude-subscription", True, (), extra_usage_enabled=False),
    )

    authenticated, message = adapter._probe_authentication()

    assert authenticated is True
    assert message == "Claude included subscription is available"
    assert len(commands.requests) == 1
    assert commands.requests[0].argv[-3:] == ("auth", "status", "--json")
    assert "--setting-sources" in commands.requests[0].argv


def test_claude_subscription_refuses_unknown_or_enabled_extra_usage(tmp_path: Path) -> None:
    for extra in (None, True):
        body = {"limits": [], "extra_usage": {} if extra is None else {"is_enabled": extra}}
        commands = FakeCommandAdapter(AdapterIdentity("command", "1"), scripted_results=[_result(tmp_path, json.dumps(body))])
        adapter = ClaudeCodeHarnessAdapter(commands, tmp_path / str(extra), (sys.executable,), {"reviewer": "claude-opus-5-5"}, subscription_only=True)
        authenticated, _ = adapter._probe_authentication()
        assert authenticated is False
        assert len(commands.requests) == 1


def test_codex_windows_are_classified_by_duration_and_require_weekly() -> None:
    future = 1_900_000_000
    account = {"account": {"type": "chatgpt", "planType": "plus"}}
    limits = {"ordinaryUsageAllowed": True, "rateLimits": {
        "primary": {"usedPercent": 10, "resetsAt": future, "windowDurationMins": 300},
        "secondary": {"usedPercent": 20, "resetsAt": future, "windowDurationMins": 10080},
    }}
    reading = _reading(account, limits, "2026-01-01T00:00:00+00:00")
    assert reading.ok and [limit.kind.value for limit in reading.limits] == ["session", "weekly_all"]
    limits["rateLimits"]["secondary"] = {"usedPercent": 20, "resetsAt": future, "windowDurationMins": 300}
    assert _reading(account, limits, "x").ok is False


def test_codex_prolite_is_an_included_plan_but_usage_based_is_not() -> None:
    future = 1_900_000_000
    limits = {"ordinaryUsageAllowed": True, "rateLimits": {
        "primary": {"usedPercent": 10, "resetsAt": future, "windowDurationMins": 300},
        "secondary": {"usedPercent": 20, "resetsAt": future, "windowDurationMins": 10080},
    }}
    assert _reading({"account": {"type": "chatgpt", "planType": "prolite"}}, limits, "x").ok
    assert not _reading({"account": {"type": "chatgpt", "planType": "prolite_usage_based"}}, limits, "x").ok


def test_subscription_environment_scrubs_unknown_provider_key_and_helper_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CUSTOM_PROVIDER", "x")
    monkeypatch.setenv("CUSTOM_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("CUSTOM_KEY_HELPER", "/tmp/helper")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")

    removed = _subscription_removed_environment(())

    assert {"CUSTOM_PROVIDER", "CUSTOM_BASE_URL", "CUSTOM_KEY_HELPER", "CLAUDE_CODE_USE_BEDROCK"} <= set(removed)


def test_subscription_environment_keeps_runtime_paths_and_removes_unrelated_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/required/bin")
    monkeypatch.setenv("HOME", "/required/home")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/required/config")
    monkeypatch.setenv("GITHUB_TOKEN", "synthetic")
    monkeypatch.setenv("NPM_TOKEN", "synthetic")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "synthetic")
    monkeypatch.setenv("SSLKEYLOGFILE", "/synthetic/keylog")

    removed = set(_subscription_removed_environment(()))

    assert {"GITHUB_TOKEN", "NPM_TOKEN", "AWS_SECRET_ACCESS_KEY", "SSLKEYLOGFILE"} <= removed
    assert {"PATH", "HOME", "XDG_CONFIG_HOME"}.isdisjoint(removed)


def test_app_server_drains_multiple_buffered_frames_before_waiting_for_more():
    import subprocess
    import time
    from autobuild.adapters.codex_subscription import _await_response
    script = "import sys; print('\\n'.join(['{}','{\"id\":2}','{\"id\":3}']),flush=True); sys.stdin.readline()"
    with subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE) as process:
        try:
            buffer = bytearray()
            for expected in (2, 3):
                assert _await_response(process, expected, time.monotonic() + 2, buffer)["id"] == expected
        finally:
            process.stdin.close()


def test_subscription_status_preserves_shared_state_while_campaign_can_reserve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    from datetime import UTC, datetime, timedelta

    from autobuild.adapters import FilesystemLaneStateAdapter
    from autobuild.adapters.codex_subscription import CHATGPT_SUBSCRIPTION_METER, CodexSubscriptionMeterAdapter
    from autobuild.adapters.subscription_admission import SubscriptionAdmissionStore
    from autobuild.bootstrap.composition import subscription_context, subscription_status
    from autobuild.bootstrap.profile import load_settings
    from autobuild.domain import LaneSignal, LaneSignalKind, ProbeResult, UsageLimit, UsageLimitKind

    profile = tmp_path / "profile.toml"
    profile.write_text(
        """
[run]
lanes = ["claude-code", "codex"]
scratch_root = "scratch"
lane_state_root = "state"
[lanes.claude-code]
builder = "claude-sonnet-4-6"
reviewer = "claude-opus-5-5"
specialist = "claude-opus-5-5"
[lanes.codex]
builder = "gpt-5.6-terra"
reviewer = "gpt-5.6-sol"
specialist = "gpt-5.6-sol"
[validator]
id = "tests"
argv = ["python", "-m", "pytest"]
[subscription_routing]
enabled = true
[usage]
warn_percent = 94
stop_percent = 98
five_hour_stop_percent = 95
""",
        encoding="utf-8",
    )
    settings = load_settings(tmp_path, profile)
    now = datetime.now(UTC)
    reset = (now + timedelta(days=3)).isoformat()
    reading = UsageReading(
        CHATGPT_SUBSCRIPTION_METER, True, (UsageLimit(UsageLimitKind.WEEKLY_ALL, 15, reset),),
        extra_usage_enabled=False, fetched_at=now.isoformat(),
    )
    monkeypatch.setattr(os, "environ", os.environ.copy())
    monkeypatch.setattr(ClaudeCodeHarnessAdapter, "probe", lambda self: ProbeResult.unavailable("offline"))
    monkeypatch.setattr(CodexHarnessAdapter, "probe", lambda self: ProbeResult.ready(AdapterIdentity("codex", "1")))
    monkeypatch.setattr(CodexSubscriptionMeterAdapter, "read", lambda self, meter_id: reading)

    state = FilesystemLaneStateAdapter(settings.lane_state_root)
    state.cool("codex", LaneSignal(LaneSignalKind.USAGE, reset_at=reset), "prior-campaign")
    state_file = settings.lane_state_root / "lanes.json"
    ledger = SubscriptionAdmissionStore(settings.scratch_root)
    assert ledger.acquire({"codex": reading}, settings.usage, "reviewer", 300)
    before = state_file.read_bytes(), ledger.path.read_bytes()

    status = subscription_status(settings)

    assert {row["name"]: row["available"] for row in status["manifest"]} == {"codex": True, "claude-code": False}
    assert status["available"]["builder"] == {"lane": "codex"}
    assert status["lanes"][0]["windows"][0]["reserved_percent"] > 0
    assert (state_file.read_bytes(), ledger.path.read_bytes()) == before

    routing, _ = subscription_context(settings)
    with routing.reserve("builder", 300) as lane:
        assert lane.name == "codex"
        assert state.active("codex") is None


def test_claude_sign_in_probes_on_one_scratch_root_ask_the_meter_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credentials = tmp_path / "credentials.json"
    credentials.write_text(json.dumps({"claudeAiOauth": {"accessToken": "synthetic-token"}}), encoding="utf-8")
    monkeypatch.setattr("autobuild.adapters.claude_usage.default_credentials_path", lambda: credentials)
    meter_body = json.dumps({"limits": [], "extra_usage": {"is_enabled": False}})
    signed_in = json.dumps({
        "loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "max"
    })
    results = []
    for index, text in enumerate((meter_body, signed_in, signed_in)):
        folder = tmp_path / f"result-{index}"
        folder.mkdir()
        results.append(_result(folder, text))
    commands = FakeCommandAdapter(AdapterIdentity("command", "1"), scripted_results=results)
    shared = tmp_path / "scratch"

    outcomes = []
    for name in ("first", "second"):
        adapter = ClaudeCodeHarnessAdapter(
            commands, tmp_path / name, (sys.executable,), {"reviewer": "claude-opus-5-5"}, subscription_only=True,
        )
        adapter.shared_usage_root = shared
        outcomes.append(adapter._probe_authentication())

    assert [authenticated for authenticated, _ in outcomes] == [True, True]
    meter_requests = [request for request in commands.requests if "@-" in request.argv]
    assert len(meter_requests) == 1
    assert len(commands.requests) == 3


def test_subscription_commands_on_one_scratch_root_share_one_meter_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    from datetime import UTC, datetime, timedelta

    from autobuild.adapters.codex_subscription import CHATGPT_SUBSCRIPTION_METER, CodexSubscriptionMeterAdapter
    from autobuild.bootstrap.composition import subscription_context, subscription_status
    from autobuild.bootstrap.profile import load_settings
    from autobuild.domain import ProbeResult, UsageLimit, UsageLimitKind

    profile = tmp_path / "profile.toml"
    profile.write_text(
        """
[run]
lanes = ["claude-code", "codex"]
scratch_root = "scratch"
lane_state_root = "state"
[lanes.claude-code]
builder = "claude-sonnet-4-6"
reviewer = "claude-opus-5-5"
specialist = "claude-opus-5-5"
[lanes.codex]
builder = "gpt-5.6-terra"
reviewer = "gpt-5.6-sol"
specialist = "gpt-5.6-sol"
[validator]
id = "tests"
argv = ["python", "-m", "pytest"]
[subscription_routing]
enabled = true
[usage]
warn_percent = 94
stop_percent = 98
five_hour_stop_percent = 95
""",
        encoding="utf-8",
    )
    settings = load_settings(tmp_path, profile)
    now = datetime.now(UTC)
    reading = UsageReading(
        CHATGPT_SUBSCRIPTION_METER, True,
        (UsageLimit(UsageLimitKind.WEEKLY_ALL, 15, (now + timedelta(days=3)).isoformat()),),
        extra_usage_enabled=False, fetched_at=now.isoformat(),
    )
    reads: list[str] = []

    def counted(self, meter_id: str) -> UsageReading:
        reads.append(meter_id)
        return reading

    monkeypatch.setattr(os, "environ", os.environ.copy())
    monkeypatch.setattr(ClaudeCodeHarnessAdapter, "probe", lambda self: ProbeResult.unavailable("offline"))
    monkeypatch.setattr(CodexHarnessAdapter, "probe", lambda self: ProbeResult.ready(AdapterIdentity("codex", "1")))
    monkeypatch.setattr(CodexSubscriptionMeterAdapter, "read", counted)

    first = subscription_status(settings)
    second = subscription_status(settings)
    routing, _ = subscription_context(settings, read_only=True)
    routing.readings()

    assert first["available"]["builder"] == {"lane": "codex"}
    assert second["available"]["builder"] == {"lane": "codex"}
    assert reads == [CHATGPT_SUBSCRIPTION_METER]


def _subscription_settings(root: Path, lanes: tuple[str, ...] = ("claude-code", "codex")):
    from autobuild.bootstrap.profile import load_settings

    models = {"claude-code": "claude-opus-5-5", "codex": "gpt-5.6-sol"}
    tables = "".join(
        f'[lanes.{lane}]\nbuilder = "{models[lane]}"\nreviewer = "{models[lane]}"\nspecialist = "{models[lane]}"\n'
        for lane in lanes
    )
    profile = root / "profile.toml"
    profile.write_text(
        f"""
[run]
lanes = {json.dumps(lanes)}
scratch_root = "scratch"
lane_state_root = "state"
{tables}
[tracker]
kind = "backlog"
[validator]
id = "tests"
argv = ["python", "-m", "pytest"]
[subscription_routing]
enabled = true
[usage]
warn_percent = 94
stop_percent = 98
five_hour_stop_percent = 95
""",
        encoding="utf-8",
    )
    return load_settings(root, profile)


@pytest.mark.parametrize("carries_hook", (False, True), ids=("plain", "hook"))
def test_claude_subscription_seat_cannot_select_another_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, carries_hook: bool
) -> None:
    import os
    from dataclasses import replace

    from autobuild.adapters import FilesystemLaneStateAdapter
    from autobuild.bootstrap.composition import _build_lanes, _command_adapter
    from autobuild.domain import ProbeResult
    from autobuild.domain.models import ProcessMode, ProcessPolicy, TestCoverage, TestGuard

    redirecting = {
        "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
        "ANTHROPIC_CUSTOM_HEADERS", "ANTHROPIC_MODEL", "ANTHROPIC_UNIX_SOCKET",
        "ANTHROPIC_UNLISTED", "CLAUDE_CONFIG_DIR", "COMPANY_KEY_HELPER",
    }
    monkeypatch.setattr(os, "environ", os.environ.copy())
    for name in redirecting:
        os.environ[name] = "synthetic"
    os.environ["SEAT_TEST_CONTROL"] = "kept"
    record = tmp_path / "seat.json"
    stub = tmp_path / "seat.py"
    stub.write_text(
        "import json, os, sys\n"
        f"open({str(record)!r}, 'w').write(json.dumps([sys.argv[1:], sorted(os.environ)]))\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    settings = replace(
        _subscription_settings(tmp_path, ("claude-code",)), harness_command=(sys.executable, str(stub))
    )
    monkeypatch.setattr(ClaudeCodeHarnessAdapter, "probe", lambda self: ProbeResult.unavailable("offline"))
    scratch = tmp_path / "scratch"
    ((_, adapter),), _, _ = _build_lanes(
        settings, _command_adapter(scratch), scratch,
        FilesystemLaneStateAdapter(tmp_path / "state"), "seat", True, True,
    )
    request = _request(tmp_path, "builder")
    if carries_hook:
        policy = ProcessPolicy(
            ProcessMode.STRICT, ("uv", "run", "pytest"), (TestCoverage("src/**", tests=("tests/t.py",)),)
        )
        request = replace(
            request, seat=Seat.BUILDER, result_contract="builder-report-v1",
            test_guard=TestGuard(policy, policy.test_map),
        )

    adapter.invoke(request)

    argv, environment = json.loads(record.read_text(encoding="utf-8"))
    if carries_hook:
        assert argv[argv.index("--setting-sources") + 1] == ""
    else:
        assert "--safe-mode" in argv
    assert "SEAT_TEST_CONTROL" in environment
    assert redirecting.intersection(environment) == set()


def test_subscription_status_withholds_path_like_lane_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import os

    from autobuild import cli
    from autobuild.adapters.codex_subscription import CHATGPT_SUBSCRIPTION_METER, CodexSubscriptionMeterAdapter
    from autobuild.domain import ProbeResult

    settings = _subscription_settings(tmp_path)
    home = "/home/private-user/.codex/auth.json"
    drive = r"C:\Users\private-user\claude.json"
    marker = "Local diagnostic withheld"
    monkeypatch.setattr(os, "environ", os.environ.copy())
    monkeypatch.setattr(cli, "load_settings", lambda *args: settings)
    monkeypatch.setattr(
        ClaudeCodeHarnessAdapter, "probe", lambda self: ProbeResult.unavailable(f"cannot read {drive}")
    )
    monkeypatch.setattr(
        CodexHarnessAdapter, "probe",
        lambda self: ProbeResult.ready(
            AdapterIdentity("codex", f"1 {home}", self.capabilities), f"signed in through {home}"
        ),
    )
    monkeypatch.setattr(
        CodexSubscriptionMeterAdapter, "read",
        lambda self, meter_id: UsageReading.failed(CHATGPT_SUBSCRIPTION_METER, f"cannot open {home}"),
    )

    assert cli.main(["subscription-status", "--profile", str(tmp_path / "profile.toml")]) == 1
    text = capsys.readouterr().out
    assert cli.main(["subscription-status", "--profile", str(tmp_path / "profile.toml"), "--json"]) == 1
    printed = capsys.readouterr().out

    for output in (text, printed):
        assert "private-user" not in output
        assert marker in output
    result = json.loads(printed)
    rows = {row["name"]: row for row in result["manifest"]}
    assert rows["claude-code"]["detail"].startswith(marker)
    assert rows["codex"]["detail"].startswith(marker)
    assert rows["codex"]["version"].startswith(marker)
    assert result["lanes"][0]["reason"].startswith(marker)
    assert all(role["lanes"][0]["reason"].startswith(marker) for role in result["roles"].values())
    assert rows["claude-code"]["version"] == "unavailable"


@pytest.mark.parametrize("ledger_exists", (True, False), ids=("ledger", "no-ledger"))
@pytest.mark.parametrize("used", (15, 99), ids=("recovered", "stopped"))
def test_subscription_preflight_changes_neither_lane_state_nor_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ledger_exists: bool, used: int
) -> None:
    import os
    import subprocess
    from datetime import UTC, datetime, timedelta

    from autobuild.adapters import FilesystemLaneStateAdapter
    from autobuild.adapters.codex_subscription import CHATGPT_SUBSCRIPTION_METER, CodexSubscriptionMeterAdapter
    from autobuild.adapters.subscription_admission import SubscriptionAdmissionStore
    from autobuild.bootstrap import composition
    from autobuild.domain import LaneSignal, LaneSignalKind, ProbeResult, UsageLimit, UsageLimitKind

    repository = tmp_path / "repository"
    subprocess.run(("git", "init", "-q", "-b", "main", str(repository)), check=True)
    (repository / "BACKLOG.md").write_text("| Item | Title | Status | Brief |\n| --- | --- | --- | --- |\n", encoding="utf-8")
    for command in (
        ("remote", "add", "origin", "https://git.invalid/repository.git"),
        ("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"),
        ("add", "BACKLOG.md"),
        ("-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base"),
    ):
        subprocess.run(("git", "-C", str(repository), *command), check=True)
    settings = _subscription_settings(repository)
    now = datetime.now(UTC)
    reset = (now + timedelta(days=3)).isoformat()
    reading = UsageReading(
        CHATGPT_SUBSCRIPTION_METER, True, (UsageLimit(UsageLimitKind.WEEKLY_ALL, used, reset),),
        extra_usage_enabled=False, fetched_at=now.isoformat(),
    )
    monkeypatch.setattr(os, "environ", os.environ.copy())
    monkeypatch.setattr(ClaudeCodeHarnessAdapter, "probe", lambda self: ProbeResult.unavailable("offline"))
    monkeypatch.setattr(
        CodexHarnessAdapter, "probe",
        lambda self: ProbeResult.ready(AdapterIdentity("codex", "1", self.capabilities)),
    )
    monkeypatch.setattr(CodexSubscriptionMeterAdapter, "read", lambda self, meter_id: reading)
    monkeypatch.setattr(composition, "_preflight", lambda *args: ())
    state = FilesystemLaneStateAdapter(settings.lane_state_root)
    cooling = state.cool("codex", LaneSignal(LaneSignalKind.USAGE, reset_at=reset), "prior-campaign")
    state_file = settings.lane_state_root / "lanes.json"
    ledger = SubscriptionAdmissionStore(settings.scratch_root)
    if ledger_exists:
        held = UsageReading(
            CHATGPT_SUBSCRIPTION_METER, True, (UsageLimit(UsageLimitKind.WEEKLY_ALL, 15, reset),),
            extra_usage_enabled=False, fetched_at=now.isoformat(),
        )
        assert ledger.acquire({"codex": held}, settings.usage, "builder", 300)
    before = state_file.read_bytes(), ledger.path.read_bytes() if ledger_exists else None

    report = composition.preflight_report(settings)

    assert state_file.read_bytes() == before[0]
    if ledger_exists:
        assert ledger.path.read_bytes() == before[1]
    else:
        assert not ledger.path.parent.exists()
    assert report["lanes"][1]["cooled_until"] == cooling.cooled_until
    if used == 15:
        assert report["ok"], report["error"]
        assert report["active_lane"] == "codex"
        assert report["preflight"][-1]["detail"] == "subscription admission: codex"
    else:
        assert report["error"] == "no capable subscription has allowance for a builder"


# -- one shared reading, and the reason when the meter cannot be read -----------

CLAUDE = "claude-subscription"
CODEX = "chatgpt-subscription"


class _Meters:
    """Counted provider meters behind the real composition and sign-in probes."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import os
        from datetime import UTC, datetime

        from autobuild.adapters.claude_usage import ClaudeUsageMeterAdapter
        from autobuild.adapters.codex_subscription import CodexSubscriptionMeterAdapter
        from autobuild.adapters.harness_cli import CliHarnessAdapter
        from autobuild.testing import FakeUsageMeter

        self.fake = FakeUsageMeter(AdapterIdentity("meters", "1"))
        self.now = datetime.now(UTC)
        signed_in = json.dumps({
            "loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "max"
        })

        def probe_run(adapter, label, argv):
            folder = adapter._output_root / "probe" / label
            folder.mkdir(parents=True, exist_ok=True)
            return _result(folder, "1.0" if label == "version" else signed_in)

        monkeypatch.setattr(os, "environ", os.environ.copy())
        monkeypatch.setattr("autobuild.adapters.harness_cli.shutil.which", lambda name: sys.executable)
        monkeypatch.setattr(CliHarnessAdapter, "_probe_run", probe_run)
        monkeypatch.setattr(ClaudeUsageMeterAdapter, "read", lambda _self, meter_id: self.fake.read(meter_id))
        monkeypatch.setattr(CodexSubscriptionMeterAdapter, "read", lambda _self, meter_id: self.fake.read(meter_id))

    def at(self, seconds: float) -> str:
        from datetime import timedelta

        return (self.now + timedelta(seconds=seconds)).isoformat()

    def good(self, meter_id: str, percent: float = 3, age: float = 0) -> UsageReading:
        from autobuild.domain import UsageLimit, UsageLimitKind

        return UsageReading(
            meter_id, True, (UsageLimit(UsageLimitKind.WEEKLY_ALL, percent, self.at(3 * 86400)),),
            extra_usage_enabled=False, fetched_at=self.at(-age),
        )

    def then_rate_limited(self, percent: float, age: float) -> str:
        """Claude answers once, ``age`` seconds ago, and is rate-limited after that."""

        retry = self.at(241)
        self.fake.readings[CLAUDE] = self.fake.answers_then_rate_limited(
            self.good(CLAUDE, percent, age), self.at(0), retry
        )
        return retry


def _repository(root: Path) -> Path:
    import subprocess

    repository = root / "repository"
    subprocess.run(("git", "init", "-q", "-b", "main", str(repository)), check=True)
    (repository / "BACKLOG.md").write_text(
        "| Item | Title | Status | Brief |\n| --- | --- | --- | --- |\n", encoding="utf-8"
    )
    for command in (
        ("remote", "add", "origin", "https://git.invalid/repository.git"),
        ("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"),
        ("add", "BACKLOG.md"),
        ("-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base"),
    ):
        subprocess.run(("git", "-C", str(repository), *command), check=True)
    return repository


def _admission(settings):
    from autobuild.adapters import FilesystemLaneStateAdapter
    from autobuild.bootstrap import composition

    scratch = settings.scratch_root
    lanes = [
        ("claude-code", ClaudeCodeHarnessAdapter(None, scratch / "c", ("claude",), subscription_only=True)),
        ("codex", CodexHarnessAdapter(None, scratch / "x", ("codex",), subscription_only=True)),
    ]
    meter = composition._shared_meter(composition._usage_meter(settings, None, scratch, lanes), scratch)
    return composition._subscription_allocator(
        settings, lanes, meter, FilesystemLaneStateAdapter(settings.lane_state_root)
    )


@pytest.mark.parametrize(
    "entry", ("ordinary-admission", "admission", "preflight", "status", "review", "claude-probe", "codex-probe")
)
def test_ten_reads_through_each_entry_ask_each_provider_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    import subprocess

    from autobuild.bootstrap import composition
    from autobuild.bootstrap.subscription_review import subscription_review
    from autobuild.domain import ReviewDecision, ReviewVerdict, SeatResult, SeatUsage

    repository = _repository(tmp_path)
    settings = _subscription_settings(repository)
    meters = _Meters(monkeypatch)
    meters.fake.readings = {CLAUDE: [meters.good(CLAUDE)], CODEX: [meters.good(CODEX)]}
    monkeypatch.setattr(composition, "_preflight", lambda *args: ())

    def ordinary() -> None:
        lanes = [
            ("claude-code", ClaudeCodeHarnessAdapter(None, tmp_path / "c", ("claude",))),
        ]
        scratch = settings.scratch_root
        meter = composition._shared_meter(composition._usage_meter(settings, None, scratch, lanes), scratch)
        assert meter.read(CLAUDE).ok

    def admission() -> None:
        with _admission(settings).reserve("builder", 60) as lane:
            assert lane is not None

    def preflight() -> None:
        assert composition.preflight_report(settings)["ok"]

    def review() -> None:
        head = subprocess.check_output(("git", "-C", str(repository), "rev-parse", "HEAD"), text=True).strip()
        brief = tmp_path / "brief.md"
        brief.write_text("brief", encoding="utf-8")
        verdict = ReviewVerdict("independent-review", ReviewDecision.PASS, (), "ref")
        result = SeatResult("r", SeatOutcome.SUCCEEDED, verdict, "raw", SeatUsage(source="fake"), "s", "e")
        for kind in (ClaudeCodeHarnessAdapter, CodexHarnessAdapter):
            monkeypatch.setattr(kind, "invoke", lambda self, request: result)
        assert subscription_review(settings, head, brief)["verdict"] == "pass"

    def probe(kind) -> None:
        adapter = kind(None, tmp_path / "probe", (sys.executable,), subscription_only=True)
        adapter.shared_usage_root = settings.scratch_root
        assert adapter._probe_authentication()[0]

    call = {
        "ordinary-admission": ordinary,
        "admission": admission,
        "preflight": preflight,
        "status": lambda: composition.subscription_status(settings),
        "review": review,
        "claude-probe": lambda: probe(ClaudeCodeHarnessAdapter),
        "codex-probe": lambda: probe(CodexHarnessAdapter),
    }[entry]
    for _ in range(10):
        call()

    single = {"ordinary-admission": [CLAUDE], "claude-probe": [CLAUDE], "codex-probe": [CODEX]}
    assert sorted(meters.fake.reads) == single.get(entry, [CODEX, CLAUDE])


def test_a_launch_inside_the_stated_age_is_admitted_and_status_names_the_rate_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from autobuild import cli
    from autobuild.bootstrap import composition

    settings = _subscription_settings(_repository(tmp_path), ("claude-code",))
    meters = _Meters(monkeypatch)
    retry = meters.then_rate_limited(3, age=70)
    monkeypatch.setattr(composition, "_preflight", lambda *args: ())
    monkeypatch.setattr(cli, "load_settings", lambda *args: settings)
    _shared_reading(settings).read(CLAUDE)

    report = composition.preflight_report(settings)
    code = cli.main(["subscription-status", "--profile", "profile.toml"])
    text = capsys.readouterr().out

    assert report["ok"], report["error"]
    assert report["preflight"][-1]["detail"] == "subscription admission: claude-code"
    assert code == 0
    assert CLAUDE in text and "rate-limited" in text and retry[:16] in text
    assert meters.fake.reads == [CLAUDE, CLAUDE]


def _shared_reading(settings):
    from autobuild.bootstrap import composition

    lanes = [("claude-code", ClaudeCodeHarnessAdapter(None, settings.scratch_root / "c", ("claude",)))]
    return composition._shared_meter(
        composition._usage_meter(settings, None, settings.scratch_root, lanes), settings.scratch_root
    )


@pytest.mark.parametrize(("percent", "age"), ((3, 100), (94, 70)), ids=("too-old", "at-warning"))
def test_a_launch_refused_for_a_rate_limited_meter_says_so_and_when_to_try_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], percent: float, age: float
) -> None:
    from autobuild import cli
    from autobuild.adapters import FilesystemLaneStateAdapter

    settings = _subscription_settings(_repository(tmp_path), ("claude-code",))
    meters = _Meters(monkeypatch)
    retry = meters.then_rate_limited(percent, age)
    monkeypatch.setattr(cli, "load_settings", lambda *args: settings)
    _shared_reading(settings).read(CLAUDE)
    reason = f"no usage meter could be read: claude-code ({CLAUDE}): {CLAUDE} meter rate-limited, next read at "

    assert cli.main(["run", "--allow-delivery"]) == 2
    launch = capsys.readouterr().err
    assert cli.main(["preflight", "--json"]) == 1
    error = json.loads(capsys.readouterr().out)["error"]
    assert cli.main(["subscription-status"]) == 1
    status = capsys.readouterr().out

    for text in (launch, error):
        assert reason in text and "try again at " + retry in text
        assert "no capable harness lane" not in text
    assert "rate-limited" in status and CLAUDE in status and retry in status
    assert meters.fake.reads == [CLAUDE, CLAUDE]
    # A cooled lane would stay shut after the meter answers again.
    assert FilesystemLaneStateAdapter(settings.lane_state_root).snapshot() == ()


def test_a_transport_error_refusal_names_the_next_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from autobuild import cli

    settings = _subscription_settings(_repository(tmp_path), ("claude-code",))
    meters = _Meters(monkeypatch)
    meters.fake.readings[CLAUDE] = [meters.fake.transport_error(CLAUDE, meters.at(0))]
    monkeypatch.setattr(cli, "load_settings", lambda *args: settings)

    assert cli.main(["run", "--allow-delivery"]) == 2
    text = capsys.readouterr().err

    assert f"({CLAUDE}): meter transport failed (exit 28); try again at {meters.at(60)}" in text


@pytest.mark.parametrize("held", ("good", "rate-limited"))
def test_an_expired_sign_in_makes_no_request_overrides_the_held_reading_and_leaks_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    credentials: Path, held: str,
) -> None:
    from autobuild import cli
    from autobuild.bootstrap import composition

    settings = _subscription_settings(_repository(tmp_path), ("claude-code",))
    meters = _Meters(monkeypatch)
    meters.then_rate_limited(3, age=5 if held == "good" else 70)
    monkeypatch.setattr(cli, "load_settings", lambda *args: settings)
    meter = _shared_reading(settings)
    meter.read(CLAUDE)
    meter.read(CLAUDE)
    reads = list(meters.fake.reads)
    access, refresh = "fake-access-token-text", "fake-refresh-token-text"
    credentials.write_text(
        json.dumps({"claudeAiOauth": {"accessToken": access, "refreshToken": refresh, "expiresAt": 1795400000000 - 1e11}}),
        encoding="utf-8",
    )

    outputs = []
    for argv, code in (
        (["run", "--allow-delivery"], 2), (["preflight"], 1), (["preflight", "--json"], 1),
        (["subscription-status"], 1), (["subscription-status", "--json"], 1),
        (["usage", "--scratch-root", str(settings.scratch_root)], 1),
    ):
        assert cli.main(argv) == code
        captured = capsys.readouterr()
        outputs.append(captured.out + captured.err)
    with composition.subscription_context(settings)[0].reserve("builder", 60) as lane:
        assert lane is None

    for text in outputs:
        assert "sign-in expired: run Claude once in this venue to refresh it (expired at 2023-" in text
        assert "rate-limited" not in text
    assert meters.fake.reads == reads
    artefacts = [path for path in tmp_path.rglob("*") if path.is_file() and path != credentials]
    assert artefacts
    for text in (*outputs, *(path.read_text(encoding="utf-8", errors="replace") for path in artefacts)):
        assert access not in text and refresh not in text


def test_an_ordinary_launch_refused_for_an_unreadable_meter_says_when_to_try_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from autobuild.bootstrap import composition
    from autobuild.domain import PreflightError, UsageProbeFailure

    settings = _subscription_settings(_repository(tmp_path), ("claude-code",))
    settings = replace(
        settings, subscription_routing=False,
        usage=replace(settings.usage, on_probe_failure=UsageProbeFailure.REFUSE),
    )
    meters = _Meters(monkeypatch)
    meters.fake.readings[CLAUDE] = [meters.fake.transport_error(CLAUDE, meters.at(0))]
    lanes = [("claude-code", ClaudeCodeHarnessAdapter(None, tmp_path / "c", ("claude",)))]

    with pytest.raises(PreflightError) as refused:
        composition._usage_preflight(settings, lanes, _shared_reading(settings), None, "campaign")

    assert f"usage meter {CLAUDE} is unavailable: meter transport failed (exit 28)" in str(refused.value)
    assert f"try again at {meters.at(60)}" in str(refused.value)
