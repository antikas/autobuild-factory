"""The repository-state rules for a tracker that commits its own changes.

A self-committing tracker (Pinax 0.2.1 or later) appends its event and commits
it in the same command. These checks read the repository after such a command
and refuse any state that breaks the evidence chain. A tracker that leaves its
change uncommitted is detected here from the repository state, never from a
version string.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from autobuild.domain import AdapterError, EvidenceError, TrackerStop

Git = Callable[..., str]

SELF_COMMITTING_TRACKER_REQUIRED = (
    "the tracker left its change uncommitted; AutoBuild needs a tracker that "
    "commits its own changes (pinax-tracker 0.2.1 or later); upgrade it before "
    "the next campaign and commit or discard the uncommitted tracker files"
)


def _under(path: str, tracker_paths: tuple[str, ...]) -> bool:
    return any(path == tracker or path.startswith(tracker + "/") for tracker in tracker_paths)


def only_tracker_changes(git: Git, root: Path, tracker_paths: tuple[str, ...]) -> bool:
    """True when the checkout is dirty and every dirty path is a tracker path:
    the shape a tracker write leaves when it was appended and not committed."""

    status = git(root, "status", "--porcelain", "--untracked-files=all")
    # A rename or copy line reads "old -> new"; both sides must be tracker paths.
    dirty = [
        path
        for line in status.splitlines()
        if line[3:]
        for path in line[3:].split(" -> ")
    ]
    return bool(dirty) and all(_under(path, tracker_paths) for path in dirty)


def require_no_foreign_changes(git: Git, root: Path, tracker_paths: tuple[str, ...]) -> None:
    """Refuse a tracker write while the checkout holds staged or modified
    non-tracker files, which a tracker's own commit could sweep in."""

    status = git(root, "status", "--porcelain", "--untracked-files=no")
    foreign = [line[3:] for line in status.splitlines() if line[3:] and not _under(line[3:], tracker_paths)]
    if foreign:
        raise AdapterError(
            "checkout has staged or modified non-tracker changes; refusing a tracker write: "
            + ", ".join(sorted(foreign))
        )


def require_committed(
    git: Git, root: Path, before: str, tracker_paths: tuple[str, ...]
) -> str:
    """The command committed its change: no tracker path is left uncommitted,
    HEAD moved, and every non-merge commit the command added on the first-parent
    chain touches tracker paths only. A merge the command made to take another
    writer's published work is not the command's own change and is not checked.
    Returns the new HEAD. HEAD unmoved with tracker files changed is a tracker
    that does not commit its own changes, which stops the campaign."""

    status = git(root, "status", "--porcelain", "--untracked-files=all", "--", *tracker_paths)
    leftover = [line[3:] for line in status.splitlines() if line[3:]]
    head = git(root, "rev-parse", "HEAD")
    if leftover and head == before:
        raise TrackerStop(SELF_COMMITTING_TRACKER_REQUIRED, code=None, environment=True)
    if leftover:
        raise EvidenceError(
            "tracker command left uncommitted tracker files beside its commit: "
            + ", ".join(sorted(leftover))
        )
    if head == before:
        raise EvidenceError("tracker command produced no commit")
    own = git(root, "rev-list", "--first-parent", "--no-merges", f"{before}..{head}").split()
    if not own:
        raise EvidenceError(f"tracker command added no commit of its own after {before}")
    for commit in own:
        touched = [
            path
            for path in git(
                root, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", commit
            ).split("\0")
            if path
        ]
        if not touched:
            raise EvidenceError(f"tracker commit {commit} changes nothing")
        outside = [path for path in touched if not _under(path, tracker_paths)]
        if outside:
            raise EvidenceError(
                f"tracker commit {commit} touches non-tracker paths: "
                + ", ".join(sorted(outside))
            )
    return head


def require_one_tracker_commit(
    git: Git, root: Path, before: str, tracker_paths: tuple[str, ...]
) -> str:
    """The command produced exactly one new commit, directly on ``before``, and
    that commit touches only tracker paths. Returns the commit."""

    head = require_committed(git, root, before, tracker_paths)
    parents = git(root, "rev-list", "--parents", "-n", "1", head).split()[1:]
    if parents != [before]:
        raise EvidenceError(
            f"tracker command must add exactly one commit on {before}; "
            f"observed {head} with parents {' '.join(parents) or 'none'}"
        )
    return head


def head_on_remote(git: Git, root: Path) -> bool:
    """True when a remote-tracking branch already contains HEAD, which is the
    repository's own record that the tracker's commit was published."""

    return bool(git(root, "branch", "-r", "--contains", "HEAD").strip())
