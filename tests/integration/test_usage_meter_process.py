"""Real-process proof that the usage meter's token reaches curl only on stdin and
never lands in a command line, a file or a run record."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from autobuild.adapters import FilesystemLaneStateAdapter, LocalRunRecordAdapter, PosixCommandAdapter, WindowsCommandAdapter
from autobuild.adapters.claude_usage import CLAUDE_SUBSCRIPTION_METER, ClaudeUsageMeterAdapter
from autobuild.domain import CampaignRef, PreflightError, UsagePolicy, UsageProbeFailure
from autobuild.enforcement.preflight import UsageLane, run_usage_preflight

SENTINEL = "sk-ant-oat01-PROCESS-SENTINEL-9876543210"

# A stand-in for curl. It records its own command line, checks the header on
# stdin without writing it anywhere, and fails loudly when SSLKEYLOGFILE leaks
# into its environment. Mode "echo" prints the header to stderr and fails, so the
# adapter's redaction is exercised against real process output.
FAKE_CURL = r'''
import json, os, sys
from pathlib import Path
mode = sys.argv[1]
Path("argv.json").write_text(json.dumps(sys.argv[2:]), encoding="utf-8")
header = sys.stdin.read()
if "SSLKEYLOGFILE" in os.environ:
    sys.stderr.write("SSLKEYLOGFILE leaked")
    sys.exit(4)
if not header.startswith("Authorization: Bearer ") or "@-" not in sys.argv:
    sys.stderr.write("no bearer header on stdin")
    sys.exit(3)
if mode == "echo":
    sys.stderr.write("refused " + header)
    sys.exit(22)
print(json.dumps({"limits": [
    {"kind": "weekly_all", "percent": 91, "resets_at": "2026-10-26T06:00:00+00:00"}
], "extra_usage": {"is_enabled": False}}))
'''


def _command_adapter(root: Path):
    return WindowsCommandAdapter(root) if os.name == "nt" else PosixCommandAdapter(root)


def _meter(tmp_path: Path, mode: str) -> tuple[ClaudeUsageMeterAdapter, Path]:
    script = tmp_path / "fake_curl.py"
    script.write_text(FAKE_CURL, encoding="utf-8")
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    credential_file = secrets / "credentials.json"
    credential_file.write_text(json.dumps({"claudeAiOauth": {"accessToken": SENTINEL}}), encoding="utf-8")
    output = tmp_path / "meter"
    meter = ClaudeUsageMeterAdapter(
        _command_adapter(tmp_path / "commands"),
        output,
        credentials_path=credential_file,
        curl=(sys.executable, str(script), mode),
    )
    return meter, output


def _files_containing(root: Path, text: str, skip: Path) -> list[Path]:
    found = []
    for path in root.rglob("*"):
        if path.is_file() and skip not in path.parents:
            if text in path.read_text(encoding="utf-8", errors="replace"):
                found.append(path)
    return found


def test_the_meter_child_gets_the_token_on_stdin_only_and_without_sslkeylogfile(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("SSLKEYLOGFILE", str(tmp_path / "keys.log"))
    meter, output = _meter(tmp_path, "ok")

    reading = meter.read(CLAUDE_SUBSCRIPTION_METER)

    assert reading.ok, reading.error
    assert reading.limits[0].percent == 91
    argv = json.loads((output / "argv.json").read_text(encoding="utf-8"))
    assert "--ssl-no-revoke" in argv and "@-" in argv
    assert all(SENTINEL not in part for part in argv)
    assert _files_containing(tmp_path, SENTINEL, tmp_path / "secrets") == []


def test_a_failing_meter_child_that_echoes_the_token_leaves_it_in_no_record(tmp_path: Path) -> None:
    meter, _output = _meter(tmp_path, "echo")
    records = LocalRunRecordAdapter(tmp_path / "runs")
    record = records.create(CampaignRef("campaign", tmp_path))
    state = FilesystemLaneStateAdapter(tmp_path / "lanes")

    probe = run_usage_preflight(
        UsagePolicy(), (UsageLane("claude-code", CLAUDE_SUBSCRIPTION_METER),),
        meter=meter, lane_state=state, campaign_id="campaign",
    )
    records.complete(record, probe.detail)
    try:
        run_usage_preflight(
            UsagePolicy(on_probe_failure=UsageProbeFailure.REFUSE),
            (UsageLane("claude-code", CLAUDE_SUBSCRIPTION_METER),),
            meter=meter, lane_state=state, campaign_id="campaign",
        )
    except PreflightError as refused:
        refusal = str(refused)
    else:
        raise AssertionError("a refused meter failure must stop the launch")

    assert "<redacted>" in probe.detail
    assert SENTINEL not in probe.detail and SENTINEL not in refusal
    assert state.snapshot() == ()
    # Every file the run wrote, including the meter child's captured stderr
    # under the command output root, is free of the token.
    saved_stderr = list((tmp_path / "commands").rglob("stderr.txt"))
    assert saved_stderr and all(
        "Bearer <redacted>" in path.read_text(encoding="utf-8") for path in saved_stderr
    )
    assert _files_containing(tmp_path, SENTINEL, tmp_path / "secrets") == []


def test_the_command_adapter_writes_unredacted_output_when_no_secret_is_named(tmp_path: Path) -> None:
    from autobuild.domain import CommandRequest

    script = tmp_path / "echo.py"
    script.write_text("import sys\nsys.stderr.write(sys.stdin.read())\n", encoding="utf-8")
    result = _command_adapter(tmp_path / "commands").run(
        CommandRequest("echo", (sys.executable, str(script)), tmp_path, stdin_text="visible text\n")
    )

    assert Path(result.stderr_ref).read_text(encoding="utf-8") == "visible text\n"
