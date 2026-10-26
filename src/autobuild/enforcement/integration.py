"""The delivery revalidation rule: whether an integrated candidate is
validated again.

An integration revalidates only when it brought a change to a path that
intersects the item's own changed paths or its declared paths. Two paths
intersect when they are the same file or sit in the same top-level package:
the first two segments under a monorepo directory (``apps/<x>``,
``packages/<x>``), the first segment otherwise. A declared path also covers
every path its glob matches, or every path under it when it names a
directory. An incoming file at the repository root (a manifest, lock file or
configuration) overlaps every scope. Changes confined to ``.ergon/``,
``docs/`` or Markdown files never revalidate.

This rule decides delivery revalidation; ``triage.share_declared_path``
decides admission and is a different rule."""

from __future__ import annotations

import re
from collections.abc import Iterable

from autobuild.domain.models import RevalidationDecision

# Top-level directories whose children are separate packages.
MONOREPO_ROOTS = frozenset({"apps", "packages"})
# Paths whose change never revalidates: the tracker, documentation and Markdown.
_NEVER_TRIGGER_DIRECTORIES = (".ergon", "docs")
_NEVER_TRIGGER_SUFFIX = ".md"
_GLOB_CHARACTERS = frozenset("*?[")


def _segments(path: str) -> list[str]:
    normal = path.replace("\\", "/").strip()
    while normal.startswith("./"):
        normal = normal[2:]
    parts = [part for part in normal.strip("/").split("/") if part and part != "."]
    for index, part in enumerate(parts):
        if any(character in _GLOB_CHARACTERS for character in part):
            # A glob segment ends the literal prefix; what follows is a
            # directory pattern, never a file of its own.
            return parts[:index] + ["*"]
    return parts


def package_key(path: str) -> str:
    """The top-level package ``path`` sits in. A root file is in the root
    package ``""``; a glob counts by the literal directories before it."""

    parts = _segments(path)
    directories = parts[:-1]
    if not directories:
        return ""
    if directories[0] in MONOREPO_ROOTS and len(directories) >= 2 and directories[1] != "*":
        return f"{directories[0]}/{directories[1]}"
    return directories[0]


def _never_triggers(path: str) -> bool:
    parts = _segments(path)
    if not parts:
        return True
    if parts[0] in _NEVER_TRIGGER_DIRECTORIES:
        return True
    return parts[-1].lower().endswith(_NEVER_TRIGGER_SUFFIX)


def _normal(path: str) -> str:
    return "/".join(_segments(path))


def _declared_pattern(token: str) -> re.Pattern[str]:
    """What a declared path covers, as one pattern over normalised paths: an
    exact path covers itself and everything under it, ``**`` spans any number
    of segments, ``*`` and ``?`` stay inside one segment, and a glob whose
    first segment is a pattern covers every path."""

    normal = token.replace("\\", "/").strip()
    while normal.startswith("./"):
        normal = normal[2:]
    parts = [part for part in normal.strip("/").split("/") if part and part != "."]
    if parts and any(character in _GLOB_CHARACTERS for character in parts[0]):
        return re.compile(r".+")
    pieces: list[str] = []
    for index, part in enumerate(parts):
        last = index == len(parts) - 1
        if part == "**":
            pieces.append(r".*" if last else r"(?:[^/]+/)*")
            continue
        segment = "".join(
            "[^/]*" if character == "*" else "[^/]" if character == "?" else re.escape(character)
            for character in part
        )
        pieces.append(segment if last else segment + "/")
    body = "".join(pieces)
    return re.compile(body + r"(?:/.*)?")


def revalidation_decision(
    incoming: Iterable[str], own: Iterable[str], declared: Iterable[str]
) -> RevalidationDecision:
    """The single owner of the revalidation rule.

    ``incoming`` are the product paths an integration changed relative to the
    validated tree, ``own`` the paths the item's own commit changed and
    ``declared`` the paths its brief declares. With no own or declared path
    known, any triggering change revalidates."""

    changed = sorted({_normal(path) for path in incoming if _normal(path)})
    if not changed:
        return RevalidationDecision(False, "no_product_change")
    triggering = tuple(path for path in changed if not _never_triggers(path))
    if not triggering:
        return RevalidationDecision(False, "docs_only", tuple(changed))
    scope = [path for path in (*own, *declared) if _segments(path)]
    if not scope:
        return RevalidationDecision(True, "unknown_scope", triggering, triggering)
    files = {_normal(path) for path in scope}
    packages = {package_key(path) for path in scope}
    # A declared glob over a whole monorepo directory covers every package in it.
    wide = {key for key in packages if key in MONOREPO_ROOTS}
    patterns = [_declared_pattern(token) for token in declared if _segments(token)]

    def intersects(path: str) -> bool:
        key = package_key(path)
        if key == "":
            # A root manifest or configuration file reaches every package.
            return True
        if path in files or key in packages or key.split("/", 1)[0] in wide:
            return True
        return any(pattern.fullmatch(path) for pattern in patterns)

    overlapping = tuple(path for path in triggering if intersects(path))
    if overlapping:
        return RevalidationDecision(True, "overlap", triggering, overlapping)
    return RevalidationDecision(False, "no_overlap", triggering)
