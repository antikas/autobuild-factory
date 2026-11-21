"""The watch and pause commands end on, or refuse, only the real
campaign-completion line, and report an unreadable profile with exit 2."""

from __future__ import annotations

from pathlib import Path

import pytest

from autobuild.adapters.local_records import PAUSE_MARKER
from autobuild.adapters.progress import CompositeProgressAdapter, is_completion_line
from autobuild.application.progress import render_progress_line
from autobuild.bootstrap.profile import ConfigurationError, read_run_scratch_root
from autobuild.cli import _parser, _watch, main
from autobuild.domain import RunEvent

STAMP = "2026-11-19T11:22:43.663507+00:00"
COMPLETED_PAYLOAD = {
    "accepted": 1,
    "parked": 0,
    "failed": 0,
    "report": "report.md",
    "stop_reason": "queue_dry",
}


def _completed(occurred_at: str = STAMP) -> str:
    return render_progress_line(RunEvent("campaign.completed", occurred_at, payload=COMPLETED_PAYLOAD))


def _claimed(title: str, occurred_at: str = STAMP) -> str:
    return render_progress_line(
        RunEvent("item.claimed", occurred_at, item_id="item-1", payload={"title": title})
    )


# --- the completion predicate -------------------------------------------------


@pytest.mark.parametrize("occurred_at", [STAMP, ""])
def test_the_rendered_completion_line_is_the_completion_line(occurred_at: str) -> None:
    line = _completed(occurred_at)

    assert is_completion_line(line)
    assert is_completion_line(line + " progress hook failures: 2")


@pytest.mark.parametrize(
    "line",
    [
        _claimed("Report when the campaign completed: shipped 3"),
        _claimed("Report when the campaign completed: shipped 3", occurred_at=""),
        f"{STAMP} seat note: campaign completed",
        _claimed("campaign completed: shipped 1"),
        render_progress_line(
            RunEvent(
                "item.parked",
                STAMP,
                item_id="item-1",
                payload={"reason": "campaign completed: shipped 2, parked 0"},
            )
        ),
        f"not-a-stamp campaign completed: shipped 1, parked 0, failed 0",
        f"{STAMP} {STAMP} campaign completed: shipped 1, parked 0, failed 0",
        "campaign completed",
        "",
    ],
)
def test_campaign_completed_inside_any_other_line_is_not_the_completion_line(line: str) -> None:
    assert not is_completion_line(line)


class _Sink:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.failures = 0

    def begin(self, record) -> None:
        return None

    def emit(self, line: str) -> None:
        self.lines.append(line)


def test_the_composite_appends_hook_failures_only_to_the_completion_line() -> None:
    sink = _Sink()
    sink.failures = 4
    composite = CompositeProgressAdapter((sink,))
    stray = _claimed("campaign completed: shipped 1")

    composite.emit(stray)
    composite.emit(_completed())

    assert sink.lines == [stray, _completed() + " progress hook failures: 4"]


def test_the_composite_appends_hook_failures_to_an_unstamped_completion_line() -> None:
    sink = _Sink()
    sink.failures = 4
    composite = CompositeProgressAdapter((sink,))
    unstamped = _completed(occurred_at="")

    composite.emit(unstamped)

    assert sink.lines == [unstamped + " progress hook failures: 4"]


def test_the_composite_leaves_a_line_with_a_false_stamp_unchanged() -> None:
    sink = _Sink()
    sink.failures = 4
    composite = CompositeProgressAdapter((sink,))
    line = "not-a-stamp campaign completed: shipped 1, parked 0, failed 0"

    composite.emit(line)

    assert sink.lines == [line]


# --- watch and pause on a stray title ----------------------------------------


def _log(scratch: Path, name: str, lines: list[str]) -> Path:
    run_dir = scratch / "runs" / name
    run_dir.mkdir(parents=True)
    (run_dir / "progress.log").write_text(
        "".join(f"{line}\n" for line in lines), encoding="utf-8", newline="\n"
    )
    return run_dir


class _FakeTime:
    def __init__(self, on_sleep=None) -> None:
        self.now = 0.0
        self.sleeps = 0
        self._on_sleep = on_sleep

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.now += seconds
        if self._on_sleep is not None:
            self._on_sleep(self.sleeps)


def test_watch_prints_a_stray_title_and_exits_zero_only_on_the_real_completion_line(
    tmp_path: Path, capsys
) -> None:
    scratch = tmp_path / "scratch"
    stray = _claimed("campaign completed: shipped 1")
    run_dir = _log(scratch, "run-1", [stray])
    completion = _completed()

    def append_completion(sleeps: int) -> None:
        if sleeps == 2:
            with (run_dir / "progress.log").open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(completion + "\n")

    fake = _FakeTime(append_completion)
    args = _parser().parse_args(["watch", "--run", "run-1", "--scratch-root", str(scratch)])

    code = _watch(args, clock=fake.clock, sleep=fake.sleep)

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.splitlines() == [stray, completion]
    assert fake.sleeps == 2


def test_watch_times_out_when_only_a_stray_title_holds_the_words(tmp_path: Path, capsys) -> None:
    scratch = tmp_path / "scratch"
    stray = _claimed("campaign completed: shipped 1")
    _log(scratch, "run-2", [stray])
    fake = _FakeTime()
    args = _parser().parse_args(
        ["watch", "--run", "run-2", "--scratch-root", str(scratch), "--timeout-seconds", "1"]
    )

    code = _watch(args, clock=fake.clock, sleep=fake.sleep)

    captured = capsys.readouterr()
    assert code == 3
    assert captured.out.splitlines() == [stray]
    assert "timed out" in captured.err


def test_pause_on_a_run_with_only_a_stray_title_writes_the_marker(tmp_path: Path, capsys) -> None:
    scratch = tmp_path / "scratch"
    run_dir = _log(scratch, "live", [_claimed("Report when the campaign completed: shipped 3")])

    code = main(["pause", "--run", "live", "--scratch-root", str(scratch)])

    captured = capsys.readouterr()
    assert code == 0
    assert (run_dir / PAUSE_MARKER).exists()
    assert "already completed" not in captured.err


# --- an unreadable profile ----------------------------------------------------


@pytest.mark.parametrize("command", ["watch", "pause"])
@pytest.mark.parametrize(
    ("profile", "named"),
    [
        ("[run\nscratch_root = 'x'\n", "invalid TOML"),
        ("[run]\nscratch_root = 5\n", "run.scratch_root"),
    ],
)
def test_an_unreadable_profile_exits_two_with_one_stderr_line(
    tmp_path: Path, capsys, command: str, profile: str, named: str
) -> None:
    (tmp_path / ".autobuild.toml").write_text(profile, encoding="utf-8")

    code = main([command, "--latest", "--repository", str(tmp_path)])

    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    lines = captured.err.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith(f"autobuild {command}:")
    assert named in lines[0]


def test_a_missing_explicit_profile_exits_two(tmp_path: Path, capsys) -> None:
    code = main(
        ["watch", "--latest", "--repository", str(tmp_path), "--profile", str(tmp_path / "absent.toml")]
    )

    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert captured.err.startswith("autobuild watch:")


def _assert_one_stderr_line(capsys, command: str) -> str:
    captured = capsys.readouterr()
    assert captured.out == ""
    lines = captured.err.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith(f"autobuild {command}:")
    return lines[0]


@pytest.mark.parametrize("command", ["watch", "pause"])
def test_a_directory_profile_is_reported_as_not_a_file(
    tmp_path: Path, capsys, command: str
) -> None:
    profile_dir = tmp_path / "profile-dir"
    profile_dir.mkdir()

    code = main([command, "--latest", "--repository", str(tmp_path), "--profile", str(profile_dir)])

    assert code == 2
    line = _assert_one_stderr_line(capsys, command)
    assert "is not a file" in line
    assert "does not exist" not in line


def test_a_missing_explicit_profile_still_does_not_exist(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="does not exist"):
        read_run_scratch_root(tmp_path, tmp_path / "absent.toml", None)


@pytest.mark.parametrize("command", ["watch", "pause"])
def test_a_profile_that_is_not_utf8_exits_two(tmp_path: Path, capsys, command: str) -> None:
    (tmp_path / ".autobuild.toml").write_bytes(b"\xff\xfe[run]\n")

    code = main([command, "--latest", "--repository", str(tmp_path)])

    assert code == 2
    _assert_one_stderr_line(capsys, command)


@pytest.mark.parametrize("command", ["watch", "pause"])
def test_a_profile_that_cannot_be_read_exits_two(
    tmp_path: Path, capsys, monkeypatch, command: str
) -> None:
    def denied(repository, profile, scratch_root):
        raise PermissionError(13, "Permission denied", str(tmp_path / ".autobuild.toml"))

    monkeypatch.setattr("autobuild.cli.resolve_runs_root", denied)

    code = main([command, "--latest", "--repository", str(tmp_path)])

    assert code == 2
    line = _assert_one_stderr_line(capsys, command)
    assert "Permission denied" in line


def test_watch_ends_on_a_completion_line_that_names_a_failed_batch(tmp_path: Path, capsys) -> None:
    scratch = tmp_path / "scratch"
    completion = render_progress_line(
        RunEvent(
            "campaign.completed",
            STAMP,
            payload={
                **COMPLETED_PAYLOAD,
                "batch_validation": "failed",
                "batch_revision": "7aa2fef",
                "batch_stdout_ref": "stdout:batch-1",
            },
        )
    )
    assert "batch validation failed on 7aa2fef; output stdout:batch-1;" in completion
    assert is_completion_line(completion)
    _log(scratch, "run-1", [completion])
    fake = _FakeTime()
    args = _parser().parse_args(
        ["watch", "--run", "run-1", "--scratch-root", str(scratch), "--timeout-seconds", "1"]
    )

    code = _watch(args, clock=fake.clock, sleep=fake.sleep)

    assert code == 0
    assert capsys.readouterr().out.splitlines() == [completion]


def _failed_batch_line(**evidence: object) -> str:
    return render_progress_line(
        RunEvent(
            "campaign.completed",
            STAMP,
            payload={
                **COMPLETED_PAYLOAD,
                "batch_validation": "failed",
                "batch_revision": "7aa2fef",
                **evidence,
            },
        )
    )


@pytest.mark.parametrize(
    "evidence",
    [{}, {"batch_stdout_ref": ""}, {"batch_stdout_ref": None}],
    ids=["absent", "empty", "none"],
)
def test_a_failed_batch_with_no_evidence_reads_output_none(evidence: dict) -> None:
    line = _failed_batch_line(**evidence)

    assert "batch validation failed on 7aa2fef; output none;" in line
    assert "None" not in line
    assert is_completion_line(line)


def test_a_failed_batch_error_is_one_ascii_line() -> None:
    line = _failed_batch_line(batch_error="OSError: caf\u00e9 missing\r\n\tsecond line")

    assert line.isascii()
    assert not any(ord(character) < 32 for character in line)
    assert "error OSError: caf\\xe9 missing second line;" in line
    assert is_completion_line(line)


def test_a_failed_batch_output_reference_is_one_ascii_line() -> None:
    line = _failed_batch_line(batch_stdout_ref="stdout:caf\u00e9\nx")

    assert line.isascii()
    assert "output stdout:caf\\xe9 x;" in line
    assert is_completion_line(line)
