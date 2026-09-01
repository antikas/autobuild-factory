from __future__ import annotations

from pathlib import Path

import pytest

from autobuild.bootstrap.composition import run_campaign
from autobuild.bootstrap.profile import ConfigurationError, ProfileOverrides, load_settings
from autobuild.cli import _overrides, _parser
from autobuild.domain import DeliveryMode, FogRecord, Proposal, RefillPlan


PROFILE = """
[run]
harness = "codex"
max_items = 7

[models]
builder = "builder-model"
reviewer = "reviewer-model"

[validator]
id = "tests"
argv = ["uv", "run", "pytest", "-q"]

[policy]
allowed_tools = ["read", "write", "shell", "python", "git"]
allowed_roots = ["../shared-briefs"]
"""


def arguments(repository: Path, *extra: str):
    return _parser().parse_args(["run", "--repository", str(repository), *extra])


def test_profile_supplies_explicit_runtime_configuration(tmp_path: Path) -> None:
    repository = tmp_path / "project"
    repository.mkdir()
    (repository / ".autobuild.toml").write_text(PROFILE, encoding="utf-8")

    args = arguments(repository)
    settings = load_settings(repository, args.profile, _overrides(args))

    assert settings.harness == "codex"
    assert settings.builder_model == "builder-model"
    assert settings.reviewer_model == "reviewer-model"
    assert settings.specialist_model == "reviewer-model"
    assert settings.validator_argv == ("uv", "run", "pytest", "-q")
    assert settings.max_items == 7
    assert settings.scratch_root is None
    assert settings.tracker_kind == "auto"
    assert settings.backlog_path == (repository / "BACKLOG.md").resolve()
    assert settings.allowed_roots == ((tmp_path / "shared-briefs").resolve(),)


def test_command_line_selection_wins_without_changing_the_profile(tmp_path: Path) -> None:
    repository = tmp_path / "project"
    repository.mkdir()
    (repository / ".autobuild.toml").write_text(PROFILE, encoding="utf-8")

    args = arguments(
        repository,
        "--harness",
        "claude-code",
        "--builder-model",
        "other-builder",
        "--max-items",
        "1",
        "--tracker",
        "backlog",
        "--backlog",
        "docs/QUEUE.md",
    )
    settings = load_settings(repository, args.profile, _overrides(args))

    assert settings.harness == "claude-code"
    assert settings.builder_model == "other-builder"
    assert settings.max_items == 1
    assert settings.tracker_kind == "backlog"
    assert settings.backlog_path == (repository / "docs" / "QUEUE.md").resolve()


def test_refill_plan_and_knowledge_adapter_are_loaded_from_explicit_configuration(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "project"
    repository.mkdir()
    (repository / ".autobuild.toml").write_text(
        PROFILE
        + "\n[refill]\n"
        + 'plan = "refill.json"\n\n'
        + "[knowledge]\n"
        + 'command = ["koine-memory"]\n'
        + 'fog_ledger = "fog.md"\n',
        encoding="utf-8",
    )
    (repository / "refill.json").write_text(
        """{
  "schema": "autobuild.refill-plan.v1",
  "proposals": [
    {
      "title": "Candidate",
      "question": "What should be built?",
      "rationale": "The queue is dry.",
      "brief_ref": "docs/candidate.md"
    }
  ],
  "fog": [
    {
      "direction": "Explore another boundary",
      "blocking_question": "Which question is sharp enough?",
      "surface_when": "The first evidence arrives."
    }
  ]
}
""",
        encoding="utf-8",
    )

    args = arguments(repository)
    settings = load_settings(repository, args.profile, _overrides(args))

    assert settings.refill_plan == RefillPlan(
        (Proposal("Candidate", "What should be built?", "The queue is dry.", "docs/candidate.md"),),
        (FogRecord("Explore another boundary", "Which question is sharp enough?", "The first evidence arrives."),),
    )
    assert settings.knowledge_command == ("koine-memory",)
    assert settings.fog_ledger == (repository / "fog.md").resolve()


def test_refill_plan_with_fog_requires_a_knowledge_adapter(tmp_path: Path) -> None:
    repository = tmp_path / "project"
    repository.mkdir()
    (repository / ".autobuild.toml").write_text(
        PROFILE + "\n[refill]\n" + 'plan = "refill.json"\n',
        encoding="utf-8",
    )
    (repository / "refill.json").write_text(
        """{
  "schema": "autobuild.refill-plan.v1",
  "fog": [
    {
      "direction": "Explore",
      "blocking_question": "What is the question?",
      "surface_when": "Evidence arrives."
    }
  ]
}
""",
        encoding="utf-8",
    )

    args = arguments(repository)
    with pytest.raises(ConfigurationError, match="containing fog requires"):
        load_settings(repository, args.profile, _overrides(args))


def test_missing_profile_fails_with_the_exact_missing_facts(tmp_path: Path) -> None:
    repository = tmp_path / "project"
    repository.mkdir()

    with pytest.raises(ConfigurationError, match="run.harness.*models.builder"):
        args = arguments(repository)
        load_settings(repository, args.profile, _overrides(args))


def test_delivery_gate_fails_before_adapter_preflight(tmp_path: Path) -> None:
    repository = tmp_path / "project"
    repository.mkdir()
    (repository / ".autobuild.toml").write_text(PROFILE, encoding="utf-8")
    args = arguments(repository)
    settings = load_settings(repository, args.profile, _overrides(args))

    with pytest.raises(ConfigurationError, match="--allow-delivery"):
        run_campaign(
            settings,
            allow_delivery=False,
            delivery_mode=DeliveryMode.CURRENT_BRANCH_PR,
        )


def test_cli_exposes_the_two_delivery_modes_and_current_branch_options(tmp_path: Path) -> None:
    args = arguments(
        tmp_path,
        "--delivery-mode",
        "current-branch-pr",
        "--push-current-branch",
        "--allow-current-branch-default",
    )

    assert args.delivery_mode == DeliveryMode.CURRENT_BRANCH_PR.value
    assert args.push_current_branch is True
    assert args.allow_current_branch_default is True


def test_current_branch_options_are_rejected_with_protected_delivery_before_preflight(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "project"
    repository.mkdir()
    (repository / ".autobuild.toml").write_text(PROFILE, encoding="utf-8")
    settings = load_settings(repository, None, ProfileOverrides())

    with pytest.raises(ConfigurationError, match="require --delivery-mode current-branch-pr"):
        run_campaign(
            settings,
            allow_delivery=True,
            push_current_branch=True,
        )
