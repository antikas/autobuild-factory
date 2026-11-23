"""The rule that decides whether a brief file can be used."""

from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from autobuild.domain.models import BriefDefect, BriefDefectCause
from autobuild.enforcement import PreflightRequest
from autobuild.enforcement import brief_file
from autobuild.enforcement.brief_file import (
    MAX_BRIEF_BYTES,
    brief_park_reason,
    is_absolute_reference,
    portable_path,
    read_brief,
)
from autobuild.testing import FakeFilesystemProbe

BRIEF = Path("docs/brief.md")
KEY = str(BRIEF)


def test_a_good_brief_returns_its_text() -> None:
    filesystem = FakeFilesystemProbe(files={KEY: MAX_BRIEF_BYTES}, texts={KEY: "# Build it\n"})

    assert read_brief(filesystem, BRIEF) == "# Build it\n"


def test_a_missing_brief() -> None:
    assert read_brief(FakeFilesystemProbe(), BRIEF) == BriefDefect(BriefDefectCause.MISSING)


@pytest.mark.parametrize("text", ["", " \n\t\r\n\u00a0"], ids=["zero-byte", "whitespace-only"])
def test_a_brief_without_text_is_empty(text: str) -> None:
    filesystem = FakeFilesystemProbe(files={KEY: len(text)}, texts={KEY: text})

    assert read_brief(filesystem, BRIEF) == BriefDefect(BriefDefectCause.EMPTY)


def test_a_brief_one_byte_over_the_limit_is_too_large_and_is_never_read() -> None:
    filesystem = FakeFilesystemProbe(
        files={KEY: MAX_BRIEF_BYTES + 1}, read_errors={KEY: AssertionError("read")}
    )

    assert read_brief(filesystem, BRIEF) == BriefDefect(BriefDefectCause.TOO_LARGE, size=1_048_577)


@pytest.mark.parametrize(
    "error",
    [OSError("denied"), UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")],
    ids=["os-error", "not-utf8"],
)
def test_a_read_error_is_unreadable_and_carries_the_error(error: Exception) -> None:
    filesystem = FakeFilesystemProbe(files={KEY: 10}, read_errors={KEY: error})

    assert read_brief(filesystem, BRIEF) == BriefDefect(
        BriefDefectCause.UNREADABLE, error=str(error)
    )


def test_the_setup_check_uses_the_same_limit() -> None:
    assert PreflightRequest.__dataclass_fields__["max_brief_bytes"].default == MAX_BRIEF_BYTES


@pytest.mark.parametrize("cause", list(BriefDefectCause))
def test_no_park_reason_holds_an_absolute_path(cause: BriefDefectCause, tmp_path: Path) -> None:
    absolute = tmp_path / "brief.md"
    defect = BriefDefect(cause, size=1_048_577, error=f"cannot read {absolute}")

    assert str(tmp_path) not in brief_park_reason(defect, str(absolute))
    assert brief_park_reason(defect, str(absolute)).endswith(" brief.md")
    assert brief_park_reason(defect, "~/private/brief.md").endswith(" brief.md")
    assert str(tmp_path) not in brief_park_reason(defect, "docs/brief.md")
    assert brief_park_reason(defect, "docs/brief.md").endswith(" docs/brief.md")


@pytest.mark.parametrize("host", (PurePosixPath, PureWindowsPath), ids=("posix", "windows"))
@pytest.mark.parametrize(
    "reference",
    (
        "/home/someone/brief.md",
        "C:\\someone\\brief.md",
        "c:/someone/brief.md",
        "C:brief.md",
        "\\\\server\\someone\\brief.md",
        "//server/someone/brief.md",
        "file:///home/someone/brief.md",
        "FILE:///C:/someone/brief.md",
        "file:/home/someone/brief.md",
        "file:///home%2Fsomeone%5Cbrief.md",
        "~/someone/brief.md",
        "  /home/someone/brief.md",
    ),
)
def test_an_absolute_reference_is_named_by_its_file_name_on_every_host(
    monkeypatch: pytest.MonkeyPatch, host: type, reference: str
) -> None:
    monkeypatch.setattr(brief_file, "Path", host)

    assert is_absolute_reference(reference)
    reason = brief_park_reason(BriefDefect(BriefDefectCause.MISSING), reference)
    assert reason == "brief:missing brief.md"


@pytest.mark.parametrize("host", (PurePosixPath, PureWindowsPath), ids=("posix", "windows"))
@pytest.mark.parametrize(
    "reference", ("docs/brief.md", "brief.md", "./docs/brief.md", "docs\\brief.md", "koine://plans/x")
)
def test_a_repository_relative_reference_passes_on_every_host(
    monkeypatch: pytest.MonkeyPatch, host: type, reference: str
) -> None:
    monkeypatch.setattr(brief_file, "Path", host)

    assert not is_absolute_reference(reference)
    reason = brief_park_reason(BriefDefect(BriefDefectCause.MISSING), reference)
    assert reason == f"brief:missing {reference}"


def test_a_path_is_named_relative_to_its_root_and_else_by_its_file_name(tmp_path: Path) -> None:
    assert portable_path(str(tmp_path / "docs" / "brief.md"), tmp_path) == "docs/brief.md"
    assert portable_path(str(tmp_path / "brief.md"), tmp_path / "worktree") == "brief.md"
    assert portable_path("docs/brief.md", tmp_path) == "docs/brief.md"
