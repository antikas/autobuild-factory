"""The one rule that decides whether a brief file can be used.

The setup check and the read before every claim apply the same rule, so an item
whose brief cannot reach its seats is refused before it costs a claim."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

from autobuild.domain.models import BriefDefect, BriefDefectCause, SeatRequest
from autobuild.ports import FilesystemProbePort

MAX_BRIEF_BYTES = 1_048_576
_FILE_URL = re.compile(r"file:", re.IGNORECASE)
_DRIVE = re.compile(r"[A-Za-z]:")
_SEPARATORS = re.compile(r"[/\\]")


def read_brief(
    filesystem: FilesystemProbePort, path: Path, *, max_bytes: int = MAX_BRIEF_BYTES
) -> str | BriefDefect:
    """Return the brief's text, or the defect that makes the file unusable."""

    try:
        if not filesystem.is_file(path):
            return BriefDefect(BriefDefectCause.MISSING)
        size = filesystem.size(path)
        if size > max_bytes:
            return BriefDefect(BriefDefectCause.TOO_LARGE, size=size)
        text = filesystem.read_text(path)
    except Exception as exc:
        return BriefDefect(BriefDefectCause.UNREADABLE, error=str(exc))
    if not text.strip():
        return BriefDefect(BriefDefectCause.EMPTY)
    return text


def is_absolute_reference(reference: str) -> bool:
    """Whether ``reference`` names a place on one machine: a POSIX absolute
    path, a Windows drive-letter or rooted path, a UNC path, a home-directory
    path or a file URL. It reads the text alone, so every host gives the same
    answer."""

    text = reference.strip()
    return (
        text.startswith(("/", "\\", "~"))
        or _DRIVE.match(text) is not None
        or _FILE_URL.match(text) is not None
    )


def portable_reference(reference: str) -> str:
    """``reference`` as a pushed record may hold it: unchanged, or only its
    last name when it is absolute."""

    if not is_absolute_reference(reference):
        return reference
    text = reference.strip()
    if (url := _FILE_URL.match(text)) is not None:
        text = unquote(text[url.end() :])
    if (drive := _DRIVE.match(text)) is not None:
        text = text[drive.end() :]
    names = [name for name in _SEPARATORS.split(text) if name]
    return names[-1] if names else ""


def portable_path(path: str, root: Path) -> str:
    """``path`` relative to ``root`` when it lies under it, else as
    ``portable_reference`` names it."""

    try:
        inside = Path(path).relative_to(root).as_posix()
    except ValueError:
        inside = path
    return portable_reference(inside)


def seat_brief_name(request: SeatRequest) -> str:
    """The seat's brief as a refusal names it."""

    if request.brief_ref:
        return portable_reference(request.brief_ref)
    return portable_path(str(request.brief_path), request.workspace.root)


def seat_brief_refusal(defect: BriefDefect, named: str) -> str:
    """Why a seat cannot start on its brief."""

    if defect.cause is BriefDefectCause.TOO_LARGE:
        return f"seat brief exceeds 1 MiB: {named}"
    if defect.cause is BriefDefectCause.UNREADABLE:
        return f"seat brief cannot be read as text: {named}"
    return f"seat brief is {defect.cause.value}: {named}"


def brief_park_reason(defect: BriefDefect, brief_ref: str) -> str:
    """The park reason for ``defect``.

    The reason is pushed with the tracker, so it leaves out the read error,
    which can quote a local path."""

    named = portable_reference(brief_ref)
    if defect.cause is BriefDefectCause.TOO_LARGE:
        return f"brief:too-large {defect.size} bytes, over {MAX_BRIEF_BYTES} {named}"
    return f"brief:{defect.cause.value} {named}"
