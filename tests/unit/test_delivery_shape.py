"""The delivery-shape check, on identities built by hand."""

from __future__ import annotations

from pathlib import Path

import pytest

from autobuild.bootstrap.composition import _require_delivery_shape
from autobuild.bootstrap.profile import ConfigurationError
from autobuild.domain import DeliveryMode, RepositoryIdentity

REVISION = "0123456789abcdef0123456789abcdef01234567"


def check(
    mode: DeliveryMode, branch: str, allow_default: bool = False, established: bool = True
) -> None:
    identity = RepositoryIdentity(
        Path("nowhere"), "trunk", branch, "origin", REVISION, established
    )
    _require_delivery_shape(mode, identity, allow_default)


def refusal(mode: DeliveryMode, branch: str, established: bool = True) -> str:
    with pytest.raises(ConfigurationError) as refused:
        check(mode, branch, established=established)
    return str(refused.value)


def test_protected_default_accepts_the_default_branch() -> None:
    check(DeliveryMode.PROTECTED_DEFAULT, "trunk")


def test_protected_default_refuses_another_branch_and_names_both() -> None:
    assert refusal(DeliveryMode.PROTECTED_DEFAULT, "feature/x") == (
        "protected-default delivery must be launched from the default branch trunk; "
        "the checkout is on feature/x; switch to trunk, "
        "or pass --delivery-mode current-branch-pr to deliver to feature/x"
    )


def test_protected_default_refuses_a_detached_head() -> None:
    assert refusal(DeliveryMode.PROTECTED_DEFAULT, "") == (
        "protected-default delivery must be launched from the default branch trunk; "
        "the checkout is on a detached head at 0123456789ab; switch to trunk, "
        "or pass --delivery-mode current-branch-pr"
    )


@pytest.mark.parametrize("branch", ("trunk", "feature/x"))
def test_protected_default_refuses_a_guessed_default_branch(branch: str) -> None:
    assert refusal(DeliveryMode.PROTECTED_DEFAULT, branch, established=False) == (
        "the default branch of origin could not be established: this clone records no "
        "remote head and origin did not answer with its head; make origin reachable, "
        "or set the remote head in this clone, then launch again"
    )


def test_current_branch_accepts_a_guessed_default_branch() -> None:
    check(DeliveryMode.CURRENT_BRANCH_PR, "feature/x", established=False)


def test_an_identity_built_without_the_flag_is_not_established() -> None:
    identity = RepositoryIdentity(Path("nowhere"), "trunk", "trunk", "origin", REVISION)
    assert identity.default_branch_established is False


def test_current_branch_refuses_the_default_branch_without_the_allow_flag() -> None:
    assert refusal(DeliveryMode.CURRENT_BRANCH_PR, "trunk") == (
        "current-branch-pr delivery refuses the detected default branch; "
        "pass --allow-current-branch-default after human approval"
    )
    check(DeliveryMode.CURRENT_BRANCH_PR, "trunk", allow_default=True)


def test_current_branch_accepts_a_named_branch_and_refuses_a_detached_head() -> None:
    check(DeliveryMode.CURRENT_BRANCH_PR, "feature/x")
    assert refusal(DeliveryMode.CURRENT_BRANCH_PR, "") == (
        "current-branch-pr delivery requires a named invoking branch"
    )
