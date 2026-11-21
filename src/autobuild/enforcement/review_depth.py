"""The acceptance checklist a review seat is given and the depth of its review.

A review seat receives the brief's acceptance clauses numbered ``A1``,
``A2`` and so on, and reports the paths it read and the clauses it judged.
The depth record checks that report against the diff: a review is shallow
when it names fewer changed paths than the diff touches, or judged no clause
it was given."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from autobuild.domain import (
    ChangedPath,
    ChangeKind,
    ClauseOutcome,
    ReviewDepth,
    ReviewVerdict,
    Seat,
)

_ACCEPTANCE_HEADING = "## acceptance"
_BULLET = re.compile(r"^(?:[-*+]|\d+[.)])\s+(.*)$")

SHALLOW_PATHS = "inspected fewer changed paths than the diff touches"
SHALLOW_CLAUSES = "checked no acceptance clause"


def acceptance_clauses(brief_text: str) -> tuple[str, ...]:
    """The bullets under the brief's ``## Acceptance`` heading, in order, each
    with its wrapped continuation lines joined; empty when the heading is
    absent."""

    clauses: list[str] = []
    collecting = False
    for line in brief_text.splitlines():
        if line.strip().lower() == _ACCEPTANCE_HEADING:
            collecting = True
            continue
        if not collecting:
            continue
        if line.startswith("## ") or line.startswith("# "):
            break
        bullet = _BULLET.match(line)
        if bullet is not None:
            clauses.append(bullet.group(1).strip())
        elif clauses and line.strip() and line[:1].isspace():
            clauses[-1] = f"{clauses[-1]} {line.strip()}"
    return tuple(clauses)


def numbered_clauses(clauses: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    """``clauses`` paired with their numbers ``A1``, ``A2`` and so on."""

    return tuple((f"A{index}", clause) for index, clause in enumerate(clauses, start=1))


def review_checklist(brief_text: str, fallback: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    """The numbered clauses a review seat is given: the brief's acceptance
    bullets, or ``fallback`` (the tracker's acceptance lines) when the brief
    has no acceptance heading."""

    clauses = acceptance_clauses(brief_text)
    has_heading = any(
        line.strip().lower() == _ACCEPTANCE_HEADING for line in brief_text.splitlines()
    )
    if not has_heading:
        clauses = tuple(line for line in fallback if line.strip())
    return numbered_clauses(clauses)


def _normalised(path: str, roots: tuple[str, ...]) -> str:
    value = path.strip().replace("\\", "/")
    for root in roots:
        if root and (value == root or value.startswith(root + "/")):
            value = value[len(root):]
            break
    value = value.lstrip("/")
    while value.startswith("./"):
        value = value[2:]
    return str(PurePosixPath(value)) if value else ""


def _roots(workspace_root: Path | None) -> tuple[str, ...]:
    if workspace_root is None:
        return ()
    forms = {workspace_root.as_posix().rstrip("/")}
    try:
        forms.add(workspace_root.resolve(strict=False).as_posix().rstrip("/"))
    except OSError:
        pass
    return tuple(sorted(forms, key=len, reverse=True))


def review_depth(
    seat: Seat,
    boundary: str | None,
    duration_seconds: float | None,
    changed: tuple[ChangedPath, ...],
    verdict: ReviewVerdict,
    clause_numbers: tuple[str, ...],
    workspace_root: Path | None = None,
) -> ReviewDepth:
    """How deeply ``verdict`` reviewed a diff of ``changed`` paths against the
    clauses numbered ``clause_numbers``. Deleted paths are not counted; a
    clause counts as checked when it was given and judged met or unmet."""

    roots = _roots(workspace_root)
    targets = {
        _normalised(entry.path.as_posix(), roots)
        for entry in changed
        if entry.kind is not ChangeKind.DELETED
    }
    inspected = {_normalised(entry.path, roots) for entry in verdict.inspected}
    given = set(clause_numbers)
    checked = {
        check.clause.strip()
        for check in verdict.acceptance
        if check.outcome in (ClauseOutcome.MET, ClauseOutcome.UNMET)
        and check.clause.strip() in given
    }
    inspected_changed = len(targets & inspected)
    reasons: list[str] = []
    if inspected_changed < len(targets):
        reasons.append(SHALLOW_PATHS)
    if not checked:
        reasons.append(SHALLOW_CLAUSES)
    return ReviewDepth(
        seat=seat,
        boundary=boundary,
        duration_seconds=duration_seconds,
        changed_paths=len(targets),
        inspected_changed_paths=inspected_changed,
        clauses=len(given),
        clauses_checked=len(checked),
        shallow=bool(reasons),
        reasons=tuple(reasons),
    )
