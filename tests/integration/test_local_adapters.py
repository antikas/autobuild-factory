from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from autobuild.adapters import (
    KoineKnowledgeAdapter,
    LocalRunRecordAdapter,
    PosixCommandAdapter,
    WindowsCommandAdapter,
)
from autobuild.domain import CampaignRef, CommandRequest, FogRecord, RunEvent


def host_command_adapter(root: Path):
    if os.name == "nt":
        return WindowsCommandAdapter(root)
    return PosixCommandAdapter(root)


def test_run_record_persists_manifest_events_evidence_and_report(tmp_path: Path) -> None:
    adapter = LocalRunRecordAdapter(
        tmp_path / "runs", metadata={"workflow_version": "test"}
    )
    campaign = CampaignRef("campaign", tmp_path / "repo")

    record = adapter.create(campaign)
    event_ref = adapter.append(
        record,
        RunEvent("campaign.started", "2026-08-30T00:00:00Z", evidence_refs=("brief",)),
    )
    evidence_ref = adapter.write_evidence(record, "review verdict", "PASS")
    report_ref = adapter.complete(record, "accepted")

    manifest = json.loads((record.root / "manifest.json").read_text(encoding="utf-8"))
    event = json.loads((record.root / "events.jsonl").read_text(encoding="utf-8"))
    assert manifest["campaign_id"] == "campaign"
    assert manifest["runtime"]["workflow_version"] == "test"
    assert event["event_type"] == "campaign.started"
    assert event_ref.endswith("#L1")
    assert Path(evidence_ref).read_text(encoding="utf-8") == "PASS"
    assert Path(report_ref).read_text(encoding="utf-8") == "accepted"


def test_host_command_runs_argv_and_captures_both_streams(tmp_path: Path) -> None:
    adapter = host_command_adapter(tmp_path / "commands")
    request = CommandRequest(
        "capture",
        (
            sys.executable,
            "-c",
            "import sys; print('out'); print('err', file=sys.stderr)",
        ),
        tmp_path,
        environment=(("AUTOBUILD_TEST_VALUE", "declared"),),
        timeout_seconds=5,
    )

    result = adapter.run(request)

    assert result.exit_code == 0
    assert result.timed_out is False
    assert result.cancelled is False
    assert Path(result.stdout_ref).read_text(encoding="utf-8").strip() == "out"
    assert Path(result.stderr_ref).read_text(encoding="utf-8").strip() == "err"


def test_host_command_times_out_and_terminates_the_process_tree(tmp_path: Path) -> None:
    adapter = host_command_adapter(tmp_path / "commands")
    request = CommandRequest(
        "timeout",
        (sys.executable, "-c", "import time; time.sleep(30)"),
        tmp_path,
        timeout_seconds=0.1,
    )

    result = adapter.run(request)

    assert result.timed_out is True
    assert result.exit_code is not None


@pytest.mark.skipif(os.name != "nt", reason="Windows command wrapper proof")
def test_windows_command_resolves_a_cmd_shim_without_enabling_general_shell_mode(tmp_path: Path) -> None:
    shim = tmp_path / "shim.cmd"
    shim.write_text("@echo shim-ok\r\n", encoding="utf-8")
    adapter = WindowsCommandAdapter(tmp_path / "commands")

    result = adapter.run(CommandRequest("shim", (str(shim),), tmp_path))

    assert result.exit_code == 0
    assert Path(result.stdout_ref).read_text(encoding="utf-8").strip() == "shim-ok"


@pytest.mark.skipif(os.name != "nt", reason="Windows command wrapper proof")
def test_windows_cmd_shim_preserves_spaces_and_embedded_json_quotes(tmp_path: Path) -> None:
    echo = tmp_path / "echo_args.py"
    echo.write_text(
        "import json, sys\nprint(json.dumps(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    shim = tmp_path / "shim with spaces.cmd"
    shim.write_text(
        f'@"{sys.executable}" "{echo}" %*\r\n',
        encoding="utf-8",
    )
    adapter = WindowsCommandAdapter(tmp_path / "commands")
    schema = json.dumps(
        {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "minLength": 1},
                "report_ref": {"type": "string"},
            },
            "required": ["summary"],
            "additionalProperties": False,
        },
        separators=(",", ":"),
    )
    expected = ["value with spaces", schema]

    result = adapter.run(
        CommandRequest("shim-args", (str(shim), *expected), tmp_path)
    )

    assert result.exit_code == 0
    actual = json.loads(Path(result.stdout_ref).read_text(encoding="utf-8"))
    assert actual == expected


def test_koine_adapter_returns_references_and_appends_fog_to_its_ledger(tmp_path: Path) -> None:
    executable = tmp_path / "fake_koine.py"
    executable.write_text(
        "import sys\n"
        "if '--version' in sys.argv:\n"
        "    print('koine-test 1')\n"
        "else:\n"
        "    print('## 1. concepts/example.md — section')\n"
        "    print('durable context')\n",
        encoding="utf-8",
    )
    ledger = tmp_path / "backlog.md"
    ledger.write_text("# Backlog\n\n### B-007 — Existing\n", encoding="utf-8")
    adapter = KoineKnowledgeAdapter(ledger, (sys.executable, str(executable)))

    context = adapter.retrieve("question")
    fog_ref = adapter.record_fog(FogRecord("New direction", "Question is not sharp", "Evidence arrives"))

    assert context.references == ("concepts/example.md",)
    assert context.excerpts and "durable context" in context.excerpts[0]
    assert fog_ref.endswith("#B-008")
    written = ledger.read_text(encoding="utf-8")
    assert "### B-008 — New direction" in written
    assert "**Surface when**: Evidence arrives" in written


@pytest.mark.skipif(os.name == "nt", reason="POSIX execution is proved natively or through WSL")
def test_posix_explicit_shell_preserves_argument_boundaries(tmp_path: Path) -> None:
    adapter = PosixCommandAdapter(tmp_path / "commands")
    request = CommandRequest(
        "shell",
        ("printf", "%s", "value with spaces"),
        tmp_path,
        shell=Path("/bin/sh"),
    )

    result = adapter.run(request)

    assert result.exit_code == 0
    assert Path(result.stdout_ref).read_text(encoding="utf-8") == "value with spaces"
