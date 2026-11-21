"""The pause marker through real files: the run-record adapter, the ``pause``
command, and a campaign in a separate process that drains, stops with
``paused`` and is resumed by the next run."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from autobuild.adapters import LocalRunRecordAdapter
from autobuild.adapters.local_records import PAUSE_MARKER, request_pause
from autobuild.cli import main
from autobuild.domain import CampaignRef

CHILD = Path(__file__).with_name("pause_campaign_child.py")
DEADLINE = 60.0
POLL = 0.02


def _campaign(repository: Path) -> CampaignRef:
    return CampaignRef("campaign", repository, max_items=1)


def _run_dir(root: Path, name: str, repository: Path, created_at: str) -> Path:
    run_dir = root / name
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {"run_id": name, "repository": str(repository.resolve()), "created_at": created_at}
        ),
        encoding="utf-8",
    )
    return run_dir


# --- the run-record adapter ---------------------------------------------------


def test_clear_prior_pause_lifts_only_the_newest_prior_run_of_the_same_repository(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runs"
    repository = tmp_path / "repo"
    other = tmp_path / "other"
    older = _run_dir(root, "a-older", repository, "2026-11-19T10:00:00+00:00")
    prior = _run_dir(root, "b-prior", repository, "2026-11-19T11:00:00+00:00")
    foreign = _run_dir(root, "c-foreign", other, "2026-11-19T12:00:00+00:00")
    for run_dir in (older, prior, foreign):
        request_pause(run_dir)
    records = LocalRunRecordAdapter(root)
    current = records.create(_campaign(repository))
    request_pause(current.root)

    assert records.clear_prior_pause(current) == "b-prior"

    assert not (prior / PAUSE_MARKER).exists()
    assert (older / PAUSE_MARKER).exists()
    assert (foreign / PAUSE_MARKER).exists()
    assert (current.root / PAUSE_MARKER).exists()
    assert records.pause_requested(current)
    assert records.clear_prior_pause(current) is None


def test_clear_prior_pause_returns_none_when_the_prior_run_has_no_marker(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runs"
    repository = tmp_path / "repo"
    older = _run_dir(root, "a-older", repository, "2026-11-19T10:00:00+00:00")
    _run_dir(root, "b-prior", repository, "2026-11-19T11:00:00+00:00")
    request_pause(older)
    records = LocalRunRecordAdapter(root)
    current = records.create(_campaign(repository))

    assert records.clear_prior_pause(current) is None
    assert (older / PAUSE_MARKER).exists()
    assert not records.pause_requested(current)


# --- the pause command --------------------------------------------------------


def _live_run(scratch: Path, name: str, completed: bool = False) -> Path:
    run_dir = scratch / "runs" / name
    run_dir.mkdir(parents=True)
    lines = ["2026-11-19T12:00:00+00:00 item item-1 claimed: build a thing"]
    if completed:
        lines.append(
            "2026-11-19T12:01:00+00:00 campaign completed: shipped 1, parked 0, failed 0; "
            "report r.md; stop queue_dry"
        )
    (run_dir / "progress.log").write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return run_dir


def test_pause_on_a_missing_run_exits_two(tmp_path: Path, capsys) -> None:
    code = main(["pause", "--run", "absent", "--scratch-root", str(tmp_path / "scratch")])

    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert "absent" in captured.err


def test_pause_on_a_completed_run_exits_two_and_writes_no_marker(tmp_path: Path, capsys) -> None:
    scratch = tmp_path / "scratch"
    run_dir = _live_run(scratch, "done", completed=True)

    code = main(["pause", "--run", "done", "--scratch-root", str(scratch)])

    captured = capsys.readouterr()
    assert code == 2
    assert "already completed" in captured.err
    assert not (run_dir / PAUSE_MARKER).exists()


def test_pause_on_a_live_run_writes_one_marker_and_exits_zero(tmp_path: Path, capsys) -> None:
    scratch = tmp_path / "scratch"
    run_dir = _live_run(scratch, "live")

    first = main(["pause", "--run", "live", "--scratch-root", str(scratch)])
    second = main(["pause", "--run", "live", "--scratch-root", str(scratch)])

    lines = capsys.readouterr().out.splitlines()
    assert (first, second) == (0, 0)
    assert len(lines) == 2
    assert "live" in lines[0] and str(run_dir / PAUSE_MARKER) in lines[0]
    assert [entry.name for entry in run_dir.iterdir() if "pause" in entry.name] == [PAUSE_MARKER]


def test_pause_latest_picks_the_newest_run(tmp_path: Path, capsys) -> None:
    scratch = tmp_path / "scratch"
    older = _live_run(scratch, "older")
    newer = _live_run(scratch, "newer")
    os.utime(older, (1_000_000, 1_000_000))
    os.utime(newer, (2_000_000, 2_000_000))

    code = main(["pause", "--latest", "--scratch-root", str(scratch)])

    capsys.readouterr()
    assert code == 0
    assert (newer / PAUSE_MARKER).exists()
    assert not (older / PAUSE_MARKER).exists()


# --- a campaign in another process --------------------------------------------


def _wait_for(predicate, what: str) -> None:
    deadline = time.monotonic() + DEADLINE
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(POLL)


def _child(runs_root: Path, repository: Path, outcome: Path, items, release: Path | None = None):
    argv = [
        sys.executable, str(CHILD),
        "--runs-root", str(runs_root),
        "--repository", str(repository),
        "--outcome", str(outcome),
    ]
    for item_id in items:
        argv += ["--item", item_id]
    if release is not None:
        argv += ["--release", str(release)]
    return subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _finish(process: subprocess.Popen) -> None:
    try:
        out, err = process.communicate(timeout=DEADLINE)
    except subprocess.TimeoutExpired:
        process.kill()
        out, err = process.communicate()
        raise AssertionError(f"the campaign process did not exit: {err}")
    assert process.returncode == 0, err or out


def _events(run_dir: Path) -> list[dict]:
    text = (run_dir / "events.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _builder_started(run_dir: Path) -> bool:
    try:
        text = (run_dir / "progress.log").read_text(encoding="utf-8")
    except OSError:
        return False
    return "seat builder started" in text


def test_a_campaign_in_another_process_pauses_drains_and_resumes(tmp_path: Path, capsys) -> None:
    scratch = tmp_path / "scratch"
    runs_root = scratch / "runs"
    runs_root.mkdir(parents=True)
    repository = tmp_path / "repo"
    repository.mkdir()
    release = tmp_path / "release"
    ids = ("item-1", "item-2", "item-3")

    first = _child(runs_root, repository, tmp_path / "first.json", ids, release)
    try:
        _wait_for(lambda: any(runs_root.iterdir()), "the run directory")
        run_dir = next(runs_root.iterdir())
        _wait_for(lambda: _builder_started(run_dir), "the first builder to start")

        code = main(["pause", "--run", run_dir.name, "--scratch-root", str(scratch)])
        capsys.readouterr()
        assert code == 0
        assert (run_dir / PAUSE_MARKER).is_file()
        release.touch()
    finally:
        if not release.exists():
            release.touch()
        _finish(first)

    recorded = _events(run_dir)
    claimed = [event["item_id"] for event in recorded if event["event_type"] == "item.claimed"]
    delivered = [event["item_id"] for event in recorded if event["event_type"] == "item.finalised"]
    completed = [event for event in recorded if event["event_type"] == "campaign.completed"]
    assert claimed == ["item-1"]
    assert delivered == ["item-1"]
    assert [event["payload"]["stop_reason"] for event in completed] == ["paused"]
    assert json.loads((tmp_path / "first.json").read_text())["delivered"] == ["item-1"]

    second = _child(runs_root, repository, tmp_path / "second.json", ids[1:])
    _finish(second)

    assert not (run_dir / PAUSE_MARKER).exists()
    resumed_dir = next(entry for entry in runs_root.iterdir() if entry != run_dir)
    resumed = _events(resumed_dir)
    assert [
        event["payload"]["prior_run_id"]
        for event in resumed
        if event["event_type"] == "campaign.resumed"
    ] == [run_dir.name]
    outcome = json.loads((tmp_path / "second.json").read_text())
    assert sorted(outcome["delivered"]) == ["item-2", "item-3"]
    assert outcome["stop_reason"] != "paused"
