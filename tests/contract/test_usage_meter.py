"""The Claude usage meter adapter's result contract and its secret handling."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from autobuild.adapters import ClaudeCodeHarnessAdapter, CodexHarnessAdapter, CopilotCliHarnessAdapter
from autobuild.adapters.claude_usage import (
    CLAUDE_SUBSCRIPTION_METER,
    USAGE_ENDPOINT,
    ClaudeUsageMeterAdapter,
    NullUsageMeterAdapter,
    normalise_usage,
)
from autobuild.domain import AdapterIdentity, CommandResult, UsageLimitKind
from autobuild.ports import UsageMeterPort
from autobuild.testing import FakeCommandAdapter

SENTINEL = "sk-ant-oat01-SENTINEL-TOKEN-0123456789"
NOW = datetime(2026, 10, 21, 20, 0, tzinfo=UTC)

BODY = {
    "limits": [
        {"kind": "session", "percent": 7, "resets_at": "2026-10-22T00:39:59.952178+00:00", "is_active": False},
        {"kind": "weekly_all", "percent": 4.5, "resets_at": "2026-10-26T05:59:59Z", "is_active": True},
        {
            "kind": "weekly_scoped",
            "percent": 0,
            "resets_at": None,
            "scope": {"model": {"display_name": "Fable"}},
        },
        {"kind": "something_new", "percent": 100, "resets_at": None},
    ],
    "extra_usage": {"is_enabled": False, "utilization": 0},
}


def credentials(tmp_path: Path, token: str | None = SENTINEL) -> Path:
    path = tmp_path / "credentials.json"
    payload = {"claudeAiOauth": {"accessToken": token}} if token is not None else {}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def scripted(tmp_path: Path, stdout: str, stderr: str = "", exit_code: int = 0) -> FakeCommandAdapter:
    out = tmp_path / "stdout.txt"
    err = tmp_path / "stderr.txt"
    out.write_text(stdout, encoding="utf-8")
    err.write_text(stderr, encoding="utf-8")
    return FakeCommandAdapter(
        AdapterIdentity("command", "1"),
        scripted_results=[CommandResult("usage", exit_code, str(out), str(err), "start", "end")],
    )


def adapter(tmp_path: Path, command: FakeCommandAdapter, token: str | None = SENTINEL) -> ClaudeUsageMeterAdapter:
    return ClaudeUsageMeterAdapter(
        command, tmp_path / "usage", credentials_path=credentials(tmp_path, token), now=lambda: NOW
    )


def test_the_adapters_satisfy_the_meter_port(tmp_path: Path) -> None:
    command = scripted(tmp_path, json.dumps(BODY))

    assert isinstance(adapter(tmp_path, command), UsageMeterPort)
    assert isinstance(NullUsageMeterAdapter(), UsageMeterPort)


def test_a_reading_is_normalised_to_kinds_percents_utc_resets_and_scope_labels(tmp_path: Path) -> None:
    reading = adapter(tmp_path, scripted(tmp_path, json.dumps(BODY))).read(CLAUDE_SUBSCRIPTION_METER)

    assert reading.ok and reading.error == ""
    assert reading.meter_id == CLAUDE_SUBSCRIPTION_METER
    assert [(limit.kind, limit.percent, limit.resets_at, limit.scope) for limit in reading.limits] == [
        (UsageLimitKind.SESSION, 7.0, "2026-10-22T00:39:59.952178+00:00", None),
        (UsageLimitKind.WEEKLY_ALL, 4.5, "2026-10-26T05:59:59+00:00", None),
        (UsageLimitKind.WEEKLY_SCOPED, 0.0, None, "Fable"),
    ]
    assert reading.extra_usage_enabled is False
    assert reading.fetched_at == NOW.isoformat()


def test_a_reset_without_an_offset_is_unusable_not_guessed() -> None:
    reading = normalise_usage(
        "m", {"limits": [{"kind": "weekly_all", "percent": 95, "resets_at": "2026-10-26T06:00:00"}]}, ""
    )

    assert reading.ok is False
    assert "without a UTC offset" in reading.error


def test_a_null_reset_on_a_limit_is_kept_as_no_reset() -> None:
    reading = normalise_usage("m", {"limits": [{"kind": "weekly_scoped", "percent": 0, "resets_at": None}]}, "")

    assert reading.ok and reading.limits[0].resets_at is None


# Bodies that once escaped the adapter as exceptions or passed as usable
# readings. Each must come back as a failed reading, never raise.
MALFORMED_LIMITS = {
    "kind-list": {"kind": ["weekly_all"], "percent": 95, "resets_at": "2026-10-26T06:00:00+00:00"},
    "kind-object": {"kind": {"name": "weekly_all"}, "percent": 95, "resets_at": "2026-10-26T06:00:00+00:00"},
    "reset-overflow": {"kind": "weekly_all", "percent": 95, "resets_at": "9999-12-31T23:59:59-23:00"},
    "percent-nan": {"kind": "weekly_all", "percent": float("nan"), "resets_at": "2026-10-26T06:00:00+00:00"},
    "percent-infinite": {"kind": "session", "percent": float("inf"), "resets_at": "2026-10-22T00:40:00+00:00"},
    "reset-number": {"kind": "weekly_all", "percent": 95, "resets_at": 1793080800},
    "scope-text": {"kind": "weekly_scoped", "percent": 100, "resets_at": None, "scope": "Fable"},
}


@pytest.mark.parametrize("case", sorted(MALFORMED_LIMITS))
def test_a_malformed_limit_through_the_real_adapter_is_a_failed_reading(tmp_path: Path, case: str) -> None:
    body = {"limits": [MALFORMED_LIMITS[case]], "extra_usage": {"is_enabled": False}}
    meter = adapter(tmp_path, scripted(tmp_path, json.dumps(body)))

    reading = meter.read(CLAUDE_SUBSCRIPTION_METER)

    assert reading.ok is False
    assert reading.error.startswith("meter limit")
    assert reading.limits == ()


def test_a_well_formed_limit_through_the_real_adapter_is_a_usable_reading(tmp_path: Path) -> None:
    body = {"limits": [{"kind": "weekly_all", "percent": 95, "resets_at": "2026-10-26T06:00:00+00:00"}]}

    reading = adapter(tmp_path, scripted(tmp_path, json.dumps(body))).read(CLAUDE_SUBSCRIPTION_METER)

    assert reading.ok and reading.limits[0].percent == 95.0


def test_read_turns_any_internal_exception_into_a_failed_reading(tmp_path: Path, monkeypatch) -> None:
    meter = adapter(tmp_path, scripted(tmp_path, json.dumps(BODY)))
    monkeypatch.setattr("autobuild.adapters.claude_usage._normalise", lambda *args: 1 / 0)

    reading = meter.read(CLAUDE_SUBSCRIPTION_METER)

    assert reading.ok is False and "ZeroDivisionError" in reading.error

    monkeypatch.setattr(meter, "_read", lambda *args: (_ for _ in ()).throw(RuntimeError("boom")))
    assert meter.read(CLAUDE_SUBSCRIPTION_METER).error == "meter read failed: RuntimeError"


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ([], "not a JSON object"),
        ({"type": "error", "error": {"type": "authentication_error"}}, "authentication_error"),
        ({"limits": ["bad"]}, "malformed limit"),
        ({"limits": [{"kind": "weekly_all", "percent": None}]}, "no finite numeric percent"),
        ({"limits": [{"kind": "session", "percent": True}]}, "no finite numeric percent"),
    ],
)
def test_a_malformed_body_is_a_failed_reading(body, error: str) -> None:
    reading = normalise_usage("m", body, "")

    assert reading.ok is False
    assert error in reading.error
    assert reading.limits == ()


def test_the_token_reaches_curl_only_on_stdin(tmp_path: Path) -> None:
    command = scripted(tmp_path, json.dumps(BODY))

    adapter(tmp_path, command).read(CLAUDE_SUBSCRIPTION_METER)

    [request] = command.requests
    assert request.argv[:2] == ("curl", "-q")
    assert request.redact == (SENTINEL,)
    assert "--ssl-no-revoke" in request.argv
    assert USAGE_ENDPOINT in request.argv
    assert ("-H", "@-") == request.argv[request.argv.index("@-") - 1 : request.argv.index("@-") + 1]
    assert all(SENTINEL not in part for part in request.argv)
    assert all(SENTINEL not in value for _name, value in request.environment)
    assert request.stdin_ref is None
    assert request.stdin_text == f"Authorization: Bearer {SENTINEL}\n"
    assert "SSLKEYLOGFILE" in request.remove_environment
    assert SENTINEL not in repr(request)


def test_a_transport_failure_redacts_the_token_from_the_error(tmp_path: Path) -> None:
    command = scripted(tmp_path, "", stderr=f"curl: header Authorization: Bearer {SENTINEL} refused", exit_code=6)

    reading = adapter(tmp_path, command).read(CLAUDE_SUBSCRIPTION_METER)

    assert reading.ok is False
    assert "exit 6" in reading.error
    assert "<redacted>" in reading.error
    assert SENTINEL not in reading.error
    assert SENTINEL not in repr(reading)


@pytest.mark.parametrize("stdout", ["", "<html>not json</html>"])
def test_an_empty_or_non_json_response_is_a_failed_reading(tmp_path: Path, stdout: str) -> None:
    reading = adapter(tmp_path, scripted(tmp_path, stdout)).read(CLAUDE_SUBSCRIPTION_METER)

    assert reading.ok is False
    assert reading.error


def test_a_missing_token_fails_without_running_curl(tmp_path: Path) -> None:
    command = scripted(tmp_path, json.dumps(BODY))

    reading = adapter(tmp_path, command, token=None).read(CLAUDE_SUBSCRIPTION_METER)

    assert reading.ok is False
    assert command.requests == []


def test_an_unknown_meter_id_fails_without_reading_the_token(tmp_path: Path) -> None:
    command = scripted(tmp_path, json.dumps(BODY))

    reading = adapter(tmp_path, command).read("another-meter")

    assert reading.ok is False
    assert command.requests == []


def test_a_command_port_exception_is_a_failed_reading(tmp_path: Path) -> None:
    command = FakeCommandAdapter(AdapterIdentity("command", "1"))

    reading = adapter(tmp_path, command).read(CLAUDE_SUBSCRIPTION_METER)

    assert reading.ok is False
    assert SENTINEL not in reading.error


def test_the_null_meter_always_fails() -> None:
    reading = NullUsageMeterAdapter().read("anything")

    assert reading.ok is False and reading.meter_id == "anything"


def test_only_the_claude_code_adapter_declares_a_meter(tmp_path: Path) -> None:
    command = FakeCommandAdapter(AdapterIdentity("command", "1"))

    assert ClaudeCodeHarnessAdapter(command, tmp_path / "c", ("claude",)).meter_id == CLAUDE_SUBSCRIPTION_METER
    assert CodexHarnessAdapter(command, tmp_path / "x", ("codex",)).meter_id is None
    assert CopilotCliHarnessAdapter(command, tmp_path / "p", ("copilot",)).meter_id is None
    assert CodexHarnessAdapter(command, tmp_path / "x", ("codex",)).meter_scope_covers("Fable", "reviewer") is False


@pytest.mark.parametrize(
    ("scope", "model_class", "covered"),
    [
        ("Fable", "reviewer", True),
        ("Fable", "builder", False),
        ("Opus", "builder", True),
        ("Opus 5.5", "builder", True),
        ("Sonnet", "builder", False),
        ("", "reviewer", False),
    ],
)
def test_the_claude_code_adapter_decides_which_seat_models_a_scope_covers(
    tmp_path: Path, scope: str, model_class: str, covered: bool
) -> None:
    harness = ClaudeCodeHarnessAdapter(
        FakeCommandAdapter(AdapterIdentity("command", "1")),
        tmp_path / "c",
        ("claude",),
        {"builder": "claude-opus-5-5", "reviewer": "fable"},
    )

    assert harness.meter_scope_covers(scope, model_class) is covered
