"""Deterministic item-nature triage from a brief, before any claim.

The campaign must not spend a builder seat on work that a fenced worktree
cannot build. This module reads the brief text and its declared paths and
returns the item class without a model call, so machine and cross-repository
items can be parked at once. The rules are the only source of the class:

- The brief line ``Item nature: <class>`` names the class. An absent or
  unrecognised line means ``repository``.
- Independently, any path under the ``## Declared paths`` heading that resolves
  outside the repository root and the allowed roots forces ``cross-repository``,
  because such an edit cannot happen inside the worktree fence.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from autobuild.domain import ItemNature

_NATURE_LABEL = "item nature:"
_CLASS_LABEL = "item class:"
_DECLARED_HEADING = "## declared paths"
_BACKTICK_TOKEN = re.compile(r"`([^`]+)`")

_BY_VALUE = {nature.value: nature for nature in ItemNature}

DEFAULT_ITEM_CLASS = "default"


def declared_item_class(brief_text: str) -> str:
    """Return the item class named by the brief's ``Item class:`` line.

    An absent line means ``default``, which the composition root maps to
    ``run.seat_timeout_seconds``. The token is normalised the same way as the
    item-nature line so a trailing period or backticks do not change the class."""

    for line in brief_text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith(_CLASS_LABEL):
            token = (
                stripped.split(":", 1)[1].strip().strip("`").strip().rstrip(".").strip()
            )
            return token or DEFAULT_ITEM_CLASS
    return DEFAULT_ITEM_CLASS


def classify_item_nature(
    brief_text: str,
    *,
    repository_root: Path,
    allowed_roots: tuple[Path, ...] = (),
) -> ItemNature:
    """Return the item class for ``brief_text`` without a model call.

    The declared class from the brief line wins when it names a non-repository
    class. Otherwise a declared path that escapes the repository root and the
    allowed roots upgrades the class to ``cross-repository``."""

    declared = _declared_class(brief_text)
    if declared is ItemNature.REPOSITORY and _has_out_of_fence_path(
        brief_text, repository_root, allowed_roots
    ):
        return ItemNature.CROSS_REPOSITORY
    return declared


def _declared_class(brief_text: str) -> ItemNature:
    for line in brief_text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith(_NATURE_LABEL):
            value = stripped.split(":", 1)[1]
            token = value.strip().strip("`").strip().rstrip(".").strip().lower()
            return _BY_VALUE.get(token, ItemNature.REPOSITORY)
    return ItemNature.REPOSITORY


def _has_out_of_fence_path(
    brief_text: str, repository_root: Path, allowed_roots: tuple[Path, ...]
) -> bool:
    roots = (repository_root, *allowed_roots)
    return any(
        _resolves_outside(raw, repository_root, roots)
        for raw in _declared_paths(brief_text)
    )


def _declared_paths(brief_text: str) -> tuple[str, ...]:
    section = _section_after_heading(brief_text, _DECLARED_HEADING)
    if section is None:
        return ()
    return tuple(match.group(1).strip() for match in _BACKTICK_TOKEN.finditer(section))


def _section_after_heading(brief_text: str, heading: str) -> str | None:
    collecting = False
    collected: list[str] = []
    for line in brief_text.splitlines():
        if line.strip().lower() == heading:
            collecting = True
            continue
        if collecting and line.startswith("## "):
            break
        if collecting:
            collected.append(line)
    if not collecting:
        return None
    return "\n".join(collected)


def _resolves_outside(
    raw: str, repository_root: Path, roots: tuple[Path, ...]
) -> bool:
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = repository_root / candidate
    resolved = _normalise(candidate)
    return not any(_within(resolved, _normalise(root)) for root in roots)


def _normalise(path: Path) -> Path:
    return Path(os.path.normpath(path))


def _within(resolved: Path, root: Path) -> bool:
    return resolved == root or resolved.is_relative_to(root)
