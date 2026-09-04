from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from autobuild.adapters import (
    CommandHookProgressAdapter,
    CompositeProgressAdapter,
    FileProgressAdapter,
    FilesystemLaneStateAdapter,
    FilesystemLeaseAdapter,
    KoineKnowledgeAdapter,
    LocalRunRecordAdapter,
    PosixCommandAdapter,
    WindowsCommandAdapter,
)
from autobuild.adapters.local_command import ProgressSample, progress_stalled
from autobuild.application import CampaignRunner, WorkflowPorts
from autobuild.cli import main
from autobuild.domain import (
    AdapterIdentity,
    BuilderReport,
    CampaignRef,
    CommandRequest,
    CommandResult,
    DiffEvidence,
    DeliveryMode,
    FogRecord,
    ItemDisposition,
    ItemExecutionSpec,
    LaneSignal,
    LaneSignalKind,
    LeaseHeld,
    LeaseRequest,
    LeaseSurface,
    ReviewDecision,
    ReviewVerdict,
    RunEvent,
    RunRecordRef,
    SeatOutcome,
    SeatResult,
    SeatUsage,
    SurfaceKind,
    ToolPolicy,
    WorkItem,
    WorkspaceRef,
)
from autobuild.testing import (
    FakeCommandAdapter,
    FakeHarnessAdapter,
    FakeKnowledgeAdapter,
    FakeLeaseAdapter,
    FakeRunRecordAdapter,
    FakeTrackerAdapter,
    FakeWorkspaceAdapter,
)


def host_command_adapter(root: Path):
    if os.name == "nt":
        return WindowsCommandAdapter(root)
    return PosixCommandAdapter(root)


# --- Progress sinks behind the ProgressPort -----------------------------------


def test_file_progress_adapter_appends_lines_under_the_run_record(tmp_path: Path) -> None:
    root = tmp_path / "run"
    root.mkdir()
    record = RunRecordRef("run-1", root)

    first = FileProgressAdapter()
    first.begin(record)
    first.emit("2026-09-04T12:00:00+00:00 line one")
    first.emit("2026-09-04T12:00:01+00:00 line two")

    # A second opener appends rather than rewriting in place.
    second = FileProgressAdapter()
    second.begin(record)
    second.emit("2026-09-04T12:00:02+00:00 line three")

    log = (root / "progress.log").read_text(encoding="utf-8")
    assert log == (
        "2026-09-04T12:00:00+00:00 line one\n"
        "2026-09-04T12:00:01+00:00 line two\n"
        "2026-09-04T12:00:02+00:00 line three\n"
    )


def test_command_hook_progress_adapter_passes_each_line_on_stdin(tmp_path: Path) -> None:
    sink = tmp_path / "hook-stdin.txt"
    hook = tmp_path / "hook.py"
    hook.write_text(
        "import sys\n"
        f"with open(r'{sink}', 'a', encoding='utf-8') as stream:\n"
        "    stream.write(sys.stdin.read())\n",
        encoding="utf-8",
    )
    adapter = CommandHookProgressAdapter((sys.executable, str(hook)), timeout_seconds=10)

    adapter.emit("line one")
    adapter.emit("line two")

    assert sink.read_text(encoding="utf-8") == "line one\nline two\n"
    assert adapter.failures == 0


# --- Watching a campaign from the command line --------------------------------


def _write_progress(run_dir: Path, lines: list[str]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "progress.log").write_text(
        "".join(f"{line}\n" for line in lines), encoding="utf-8", newline="\n"
    )


def test_watch_prints_every_line_in_order_and_exits_zero(
    tmp_path: Path, capsys
) -> None:
    scratch = tmp_path / "scratch"
    run_dir = scratch / "runs" / "campaign-1"
    # The final lines and the completion line are all present before the first
    # poll, so the watcher drains them in one pass and stops.
    lines = [
        "2026-09-04T12:00:00+00:00 campaign started: harness codex, models builder m, up to 1 items",
        "2026-09-04T12:00:01+00:00 item item-1 claimed: build a thing",
        "2026-09-04T12:00:02+00:00 item item-1 validation passed",
        "2026-09-04T12:00:03+00:00 campaign completed: shipped 1, parked 0, failed 0; "
        "report r.md; stop item_bound progress hook failures: 0",
    ]
    _write_progress(run_dir, lines)

    code = main(["watch", "--run", "campaign-1", "--scratch-root", str(scratch)])

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.splitlines() == lines
    assert captured.err == ""


def test_watch_times_out_without_a_completion_line(tmp_path: Path, capsys) -> None:
    scratch = tmp_path / "scratch"
    run_dir = scratch / "runs" / "campaign-2"
    _write_progress(run_dir, ["2026-09-04T12:00:00+00:00 item item-1 claimed: build a thing"])

    code = main(
        [
            "watch",
            "--run",
            "campaign-2",
            "--scratch-root",
            str(scratch),
            "--timeout-seconds",
            "2",
        ]
    )

    captured = capsys.readouterr()
    assert code == 3
    assert "item item-1 claimed" in captured.out
    assert "timed out" in captured.err


def test_watch_missing_run_exits_two_with_a_message_on_stderr(
    tmp_path: Path, capsys
) -> None:
    scratch = tmp_path / "scratch"

    code = main(["watch", "--run", "absent", "--scratch-root", str(scratch)])

    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert "absent" in captured.err


def _identity(name: str) -> AdapterIdentity:
    return AdapterIdentity(name, "1", frozenset({"test"}))


def _accepted_campaign_ports(records: LocalRunRecordAdapter, progress) -> WorkflowPorts:
    workspace_ref = WorkspaceRef(Path("/worktree"), "run", "base", "lease")
    tracker = FakeTrackerAdapter(
        _identity("tracker"), queue=[WorkItem("item-1", "test item", "plan", ("passes",))]
    )
    workspace = FakeWorkspaceAdapter(
        _identity("workspace"),
        workspace=workspace_ref,
        diffs=[DiffEvidence(workspace_ref, "revision-0", (), "patch:0")],
    )
    builder = SeatResult(
        "builder",
        SeatOutcome.SUCCEEDED,
        BuilderReport("report:b", "built"),
        "raw:b",
        SeatUsage(source="fake"),
        "start",
        "end",
    )
    reviewer = SeatResult(
        "review",
        SeatOutcome.SUCCEEDED,
        ReviewVerdict("item-1", ReviewDecision.PASS, (), "verdict:1"),
        "raw:r",
        SeatUsage(source="fake"),
        "start",
        "end",
    )
    harness = FakeHarnessAdapter(_identity("harness"), scripted_results=[builder, reviewer])
    commands = FakeCommandAdapter(
        _identity("command"),
        scripted_results=[CommandResult("command:1", 0, "stdout:1", "stderr:1", "start", "end")],
    )
    knowledge = FakeKnowledgeAdapter(_identity("knowledge"))
    lease = FakeLeaseAdapter(_identity("lease"))
    return WorkflowPorts(
        tracker,
        workspace,
        harness,
        commands,
        records,
        knowledge,
        progress=progress,
        lease=lease,
    )


def _accepted_spec(item: WorkItem) -> ItemExecutionSpec:
    return ItemExecutionSpec(
        item,
        Path("brief.md"),
        "validator",
        ("python", "-m", "pytest"),
        ToolPolicy(frozenset({"python"}), (Path("/worktree"),)),
        "builder-class",
        "reviewer-class",
        "specialist-class",
        delivery_mode=DeliveryMode.PROTECTED_DEFAULT,
        delivery_target_branch="main",
        delivery_target_revision="base",
    )


def test_failing_progress_hooks_are_counted_and_leave_the_outcome_unchanged(
    tmp_path: Path,
) -> None:
    nonzero = tmp_path / "nonzero.py"
    nonzero.write_text("import sys\nsys.stdin.read()\nsys.exit(1)\n", encoding="utf-8")
    hook_nonzero = CommandHookProgressAdapter(
        (sys.executable, str(nonzero)), timeout_seconds=10
    )
    hook_missing = CommandHookProgressAdapter(
        ("autobuild-nonexistent-progress-hook-binary",), timeout_seconds=10
    )
    composite = CompositeProgressAdapter(
        (FileProgressAdapter(), hook_nonzero, hook_missing)
    )
    records = LocalRunRecordAdapter(tmp_path / "runs")
    ports = _accepted_campaign_ports(records, composite)

    outcome = CampaignRunner(ports).run(
        CampaignRef("campaign", tmp_path / "repo", max_items=1), _accepted_spec
    )

    # The swallowed hook failures never disturb the campaign outcome.
    assert outcome.items[0].disposition is ItemDisposition.ACCEPTED
    assert outcome.stop_reason.value == "item_bound"

    run_dir = next((tmp_path / "runs").iterdir())
    lines = (run_dir / "progress.log").read_text(encoding="utf-8").splitlines()
    completion = [line for line in lines if "campaign completed" in line]
    assert len(completion) == 1
    reported = int(completion[0].rsplit("progress hook failures: ", 1)[1])
    # Both hooks fail on every line before completion; the total is reported once.
    assert reported == 2 * (len(lines) - 1)
    assert reported > 0


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


def test_run_record_stamps_a_utc_timestamp_and_persists_the_payload(tmp_path: Path) -> None:
    from datetime import datetime, timedelta

    adapter = LocalRunRecordAdapter(tmp_path / "runs")
    campaign = CampaignRef("campaign", tmp_path / "repo")
    record = adapter.create(campaign)

    adapter.append(
        record,
        RunEvent("seat.completed", item_id="item-1", payload={"seat": "builder", "input_tokens": 11}),
    )

    event = json.loads((record.root / "events.jsonl").read_text(encoding="utf-8"))
    stamped = datetime.fromisoformat(event["occurred_at"])
    assert stamped.tzinfo is not None and stamped.utcoffset() == timedelta(0)
    assert event["payload"] == {"seat": "builder", "input_tokens": 11}


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


def test_host_command_streams_declared_stdin_file(tmp_path: Path) -> None:
    stdin = tmp_path / "prompt.txt"
    stdin.write_text("x" * 200_000, encoding="utf-8")
    adapter = host_command_adapter(tmp_path / "commands")

    result = adapter.run(
        CommandRequest(
            "stdin",
            (sys.executable, "-c", "import sys; print(len(sys.stdin.read()))"),
            tmp_path,
            timeout_seconds=5,
            stdin_ref=str(stdin),
        )
    )

    assert result.exit_code == 0
    assert Path(result.stdout_ref).read_text(encoding="utf-8").strip() == "200000"


# --- Stall deadline: the pure decision over sample tuples ----------------------


def _sample(
    at: float,
    *,
    out: int = 0,
    err: int = 0,
    digest: str = "flat",
    cpu: float | None = 0.0,
) -> ProgressSample:
    return ProgressSample(at, out, err, digest, cpu)


def test_all_three_signals_flat_past_the_deadline_is_a_kill() -> None:
    samples = (_sample(0.0), _sample(1.5), _sample(3.0))

    assert progress_stalled(samples, 3.0) is True
    assert progress_stalled(samples, 4.0) is False


def test_output_growth_alone_prevents_a_kill() -> None:
    samples = (
        _sample(0.0, out=0),
        _sample(1.0, out=0),
        _sample(2.0, out=0),
        _sample(3.0, out=0),
        _sample(4.0, out=64),
    )

    assert progress_stalled(samples, 3.0) is False


def test_worktree_digest_change_alone_prevents_a_kill() -> None:
    samples = (
        _sample(0.0, digest="a"),
        _sample(1.0, digest="a"),
        _sample(2.0, digest="a"),
        _sample(3.0, digest="a"),
        _sample(4.0, digest="b"),
    )

    assert progress_stalled(samples, 3.0) is False


def test_child_cpu_growth_alone_prevents_a_kill() -> None:
    samples = (
        _sample(0.0, cpu=0.10),
        _sample(1.0, cpu=0.10),
        _sample(2.0, cpu=0.10),
        _sample(3.0, cpu=0.10),
        _sample(4.0, cpu=0.42),
    )

    assert progress_stalled(samples, 3.0) is False


def test_an_unknown_cpu_signal_never_contributes_to_a_kill() -> None:
    samples = (
        _sample(0.0, cpu=None),
        _sample(1.0, cpu=None),
        _sample(2.0, cpu=None),
        _sample(3.0, cpu=None),
    )

    assert progress_stalled(samples, 3.0) is False


def test_a_single_sample_or_zero_deadline_is_never_a_kill() -> None:
    assert progress_stalled((_sample(0.0),), 3.0) is False
    assert progress_stalled((_sample(0.0), _sample(3.0)), 0.0) is False


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


# --- Single-writer lease adapter ----------------------------------------------


_T0 = datetime(2026, 9, 3, 12, 0, 0, tzinfo=UTC)


def _lease(
    root: Path,
    *,
    process_id: int,
    host: str = "host-a",
    now: datetime = _T0,
    is_alive=lambda pid: True,
    stale_seconds: float = 1800.0,
) -> FilesystemLeaseAdapter:
    return FilesystemLeaseAdapter(
        root,
        stale_seconds=stale_seconds,
        now=lambda: now,
        host=host,
        process_id=process_id,
        is_alive=is_alive,
    )


def _tracker_surface(tmp_path: Path) -> LeaseSurface:
    return LeaseSurface(tmp_path / "repo" / ".ergon", SurfaceKind.TRACKER)


def test_lease_acquire_writes_a_sha256_named_record_under_leases(tmp_path: Path) -> None:
    surface = _tracker_surface(tmp_path)
    adapter = _lease(tmp_path / "scratch", process_id=111)

    grant = adapter.acquire(LeaseRequest(surface, "campaign-a"))

    digest = hashlib.sha256(
        os.path.normcase(str(surface.path.resolve(strict=False))).encode("utf-8")
    ).hexdigest()
    lease_file = tmp_path / "scratch" / "leases" / f"{digest}.json"
    assert lease_file.is_file()
    payload = json.loads(lease_file.read_text(encoding="utf-8"))
    assert payload["campaign_id"] == "campaign-a"
    assert payload["process_id"] == 111
    assert payload["surface_kind"] == "tracker"
    assert grant.reclaimed is None
    assert grant.record.campaign_id == "campaign-a"


def test_a_live_foreign_lease_refuses_acquire_and_names_the_holder(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    surface = _tracker_surface(tmp_path)
    holder = _lease(scratch, process_id=111, host="host-a")
    holder.acquire(LeaseRequest(surface, "campaign-a"))

    contender = _lease(scratch, process_id=222, host="host-a", now=_T0 + timedelta(seconds=5))

    with pytest.raises(LeaseHeld) as excinfo:
        contender.acquire(LeaseRequest(surface, "campaign-b"))

    message = str(excinfo.value)
    assert "campaign-a" in message and "111" in message


def test_a_dead_process_lease_is_reclaimed_with_the_previous_holder(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    surface = _tracker_surface(tmp_path)
    _lease(scratch, process_id=111, host="host-a").acquire(LeaseRequest(surface, "campaign-a"))

    contender = _lease(
        scratch, process_id=222, host="host-a", is_alive=lambda pid: pid != 111
    )
    grant = contender.acquire(LeaseRequest(surface, "campaign-b"))

    assert grant.reclaimed is not None
    assert grant.reclaimed.campaign_id == "campaign-a"
    assert grant.reclaimed.process_id == 111
    assert grant.record.campaign_id == "campaign-b"


def test_a_lease_older_than_the_stale_window_is_reclaimed(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    surface = _tracker_surface(tmp_path)
    _lease(scratch, process_id=111, host="host-a", now=_T0).acquire(
        LeaseRequest(surface, "campaign-a")
    )

    contender = _lease(
        scratch,
        process_id=111,
        host="host-a",
        now=_T0 + timedelta(seconds=100),
        stale_seconds=1.0,
    )
    grant = contender.acquire(LeaseRequest(surface, "campaign-b"))

    assert grant.reclaimed is not None
    assert grant.reclaimed.campaign_id == "campaign-a"


def test_release_is_idempotent_and_diagnoses_a_release_not_held(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    surface = _tracker_surface(tmp_path)
    holder = _lease(scratch, process_id=111, host="host-a")
    grant = holder.acquire(LeaseRequest(surface, "campaign-a"))

    first = holder.release(grant)
    assert first.released is True
    second = holder.release(grant)
    assert second.released is False
    assert any("no lease file" in note for note in second.diagnostics)

    holder.acquire(LeaseRequest(surface, "campaign-a"))
    stranger = _lease(scratch, process_id=999, host="host-a")
    outcome = stranger.release(grant)
    assert outcome.released is False
    assert any("another writer" in note for note in outcome.diagnostics)


def test_renew_refreshes_the_heartbeat_and_keeps_the_start_time(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    surface = _tracker_surface(tmp_path)
    holder = _lease(scratch, process_id=111, host="host-a", now=_T0)
    grant = holder.acquire(LeaseRequest(surface, "campaign-a"))

    later = _lease(scratch, process_id=111, host="host-a", now=_T0 + timedelta(seconds=30))
    renewed = later.renew(grant)

    assert renewed.record.started_at == grant.record.started_at
    assert renewed.record.heartbeat_at != grant.record.heartbeat_at


def test_live_holder_hides_a_stale_lease_and_names_a_live_one(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    surface = _tracker_surface(tmp_path)
    _lease(scratch, process_id=111, host="host-a").acquire(LeaseRequest(surface, "campaign-a"))

    live_view = _lease(scratch, process_id=222, host="host-a")
    assert live_view.live_holder(surface).campaign_id == "campaign-a"

    dead_view = _lease(
        scratch, process_id=222, host="host-a", is_alive=lambda pid: pid != 111
    )
    assert dead_view.live_holder(surface) is None


def test_liveness_tracks_a_real_child_process(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    surface = _tracker_surface(tmp_path)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        host = socket.gethostname()
        holder = FilesystemLeaseAdapter(scratch, host=host, process_id=child.pid)
        holder.acquire(LeaseRequest(surface, "campaign-a"))
        contender = FilesystemLeaseAdapter(scratch)
        with pytest.raises(LeaseHeld):
            contender.acquire(LeaseRequest(surface, "campaign-b"))
    finally:
        child.terminate()
        child.wait()
    # The pid is now dead, so the same contender reclaims the surface.
    for _ in range(10):
        grant = FilesystemLeaseAdapter(scratch).acquire(LeaseRequest(surface, "campaign-b"))
        if grant.reclaimed is not None:
            break
        time.sleep(0.1)
    assert grant.reclaimed is not None
    assert grant.reclaimed.campaign_id == "campaign-a"


def test_two_campaigns_sharing_lanes_json_see_each_others_cooling(tmp_path: Path) -> None:
    root = tmp_path / "lane-state"
    first = FilesystemLaneStateAdapter(root, cool_seconds=3600)
    second = FilesystemLaneStateAdapter(root, cool_seconds=3600)

    first.cool("claude-code", LaneSignal(LaneSignalKind.RATE_LIMIT), "campaign-a")

    cooling = second.active("claude-code")
    assert cooling is not None
    assert cooling.signature == "rate_limit"
    assert cooling.campaign_id == "campaign-a"
    assert second.active("codex") is None
    assert (root / "lanes.json").is_file()


def test_lane_cooling_expires_and_a_vendor_reset_time_is_honoured(tmp_path: Path) -> None:
    now = [datetime(2026, 9, 3, tzinfo=UTC)]
    adapter = FilesystemLaneStateAdapter(
        tmp_path / "lane-state", cool_seconds=60, now=lambda: now[0]
    )

    adapter.cool("codex", LaneSignal(LaneSignalKind.RATE_LIMIT), "campaign-a")
    assert adapter.active("codex") is not None

    now[0] = datetime(2026, 9, 3, 0, 2, tzinfo=UTC)
    assert adapter.active("codex") is None

    reset = datetime(2026, 9, 3, 5, tzinfo=UTC).isoformat()
    adapter.cool("codex", LaneSignal(LaneSignalKind.QUOTA, reset_at=reset), "campaign-a")
    cooling = adapter.active("codex")
    assert cooling is not None
    assert cooling.cooled_until == reset
    assert cooling.signature == "quota"


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
