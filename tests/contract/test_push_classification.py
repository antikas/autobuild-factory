"""The delivery push classification: a push that loses a race is a rejected push.

A rejected push returns False, so delivery starts its next attempt; any other
push failure is raised. The stderr below is what a local bare origin printed
when two pushes to ``main`` raced on the server-side ref lock."""

from __future__ import annotations

from pathlib import Path

import pytest

from autobuild.adapters.git_workspace import GitWorkspaceAdapter, _Lease
from autobuild.domain import AdapterError, RepositoryIdentity, WorkspaceRef

REF_LOCK_RACE = (
    "git push origin 3c72a39:refs/heads/main failed: remote: error: cannot lock ref "
    "'refs/heads/main': is at 93ea3ec but expected 5801324\n"
    "To D:/fixture/remote.git\n"
    " ! [remote rejected] 3c72a39 -> main (failed to update ref)\n"
    "error: failed to push some refs to 'D:/fixture/remote.git'"
)
NON_FAST_FORWARD = (
    "git push origin abc:refs/heads/main failed: ! [rejected] abc -> main (fetch first)"
)
UNREACHABLE = "git push origin abc:refs/heads/main failed: fatal: could not read from remote"
HOOK_DECLINED = (
    "git push origin abc:refs/heads/main failed: remote: error: protected branch hook declined\n"
    "To D:/fixture/remote.git\n"
    " ! [remote rejected] abc -> main (pre-receive hook declined)\n"
    "error: failed to push some refs to 'D:/fixture/remote.git'"
)


def _push_with(tmp_path: Path, stderr: str, rejections: list[str] | None = None) -> bool:
    adapter = GitWorkspaceAdapter(tmp_path / "scratch")

    def failing_git(root: Path, *args: str, check: bool = True) -> str:
        if args and args[0] == "push":
            raise AdapterError(stderr)
        raise AssertionError(f"unexpected git call {args}")

    adapter._git = failing_git  # type: ignore[method-assign]
    workspace = WorkspaceRef(tmp_path / "worktree", "autobuild/item", "base", "lease")
    repository = RepositoryIdentity(tmp_path / "repo", "main", "main", "origin", "base")
    return adapter._push(
        _Lease(repository, workspace), workspace, "main", "3c72a39", rejections
    )


@pytest.mark.parametrize("stderr", [REF_LOCK_RACE, NON_FAST_FORWARD])
def test_a_push_that_lost_a_race_is_a_rejected_push(tmp_path: Path, stderr: str) -> None:
    rejections: list[str] = []
    assert _push_with(tmp_path, stderr, rejections) is False
    assert rejections == [stderr]


@pytest.mark.parametrize(
    ("stderr", "message"),
    [(UNREACHABLE, "could not read from remote"), (HOOK_DECLINED, "pre-receive hook declined")],
)
def test_any_other_push_failure_is_raised(tmp_path: Path, stderr: str, message: str) -> None:
    rejections: list[str] = []
    with pytest.raises(AdapterError, match=message):
        _push_with(tmp_path, stderr, rejections)
    assert rejections == []
