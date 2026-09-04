from __future__ import annotations

from pathlib import Path

import pytest

from autobuild.bootstrap.composition import _max_seat_timeout, _specification, run_campaign
from autobuild.bootstrap.environment import default_scratch_root, resolve_runs_root
from autobuild.bootstrap.profile import ConfigurationError, ProfileOverrides, load_settings
from autobuild.cli import _overrides, _parser
from autobuild.domain import (
    CampaignSelection,
    DeliveryMode,
    FogRecord,
    Proposal,
    RefillPlan,
    WorkItem,
)


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


def test_preflight_and_budget_settings_are_read_from_the_profile(tmp_path: Path) -> None:
    repository = tmp_path / "project"
    repository.mkdir()
    (repository / ".autobuild.toml").write_text(
        PROFILE.replace(
            'argv = ["uv", "run", "pytest", "-q"]',
            'argv = ["uv", "run", "pytest", "-q"]\nbudget_seconds = 420',
        )
        + "\n[preflight]\n"
        + 'tls_targets = ["registry.example.com:443", "api.example.com:8443"]\n'
        + 'accepted_environment = ["NODE_EXTRA_CA_CERTS", "SSLKEYLOGFILE"]\n',
        encoding="utf-8",
    )

    args = arguments(repository)
    settings = load_settings(repository, args.profile, _overrides(args))

    assert settings.validator_budget_seconds == 420.0
    assert settings.tls_targets == ("registry.example.com:443", "api.example.com:8443")
    assert settings.accepted_environment == frozenset(
        {"NODE_EXTRA_CA_CERTS", "SSLKEYLOGFILE"}
    )


def test_preflight_defaults_to_no_targets_and_no_accepted_variables(tmp_path: Path) -> None:
    repository = tmp_path / "project"
    repository.mkdir()
    (repository / ".autobuild.toml").write_text(PROFILE, encoding="utf-8")

    args = arguments(repository)
    settings = load_settings(repository, args.profile, _overrides(args))

    assert settings.tls_targets == ()
    assert settings.accepted_environment == frozenset()
    assert settings.validator_budget_seconds is None


def test_malformed_tls_target_is_refused(tmp_path: Path) -> None:
    repository = tmp_path / "project"
    repository.mkdir()
    (repository / ".autobuild.toml").write_text(
        PROFILE + '\n[preflight]\ntls_targets = ["hostwithoutport"]\n',
        encoding="utf-8",
    )

    args = arguments(repository)
    with pytest.raises(ConfigurationError, match="host:port"):
        load_settings(repository, args.profile, _overrides(args))


def test_the_same_command_timeout_reaches_the_seat_validator(tmp_path: Path) -> None:
    repository = tmp_path / "project"
    repository.mkdir()
    (repository / ".autobuild.toml").write_text(
        PROFILE.replace("max_items = 7", "max_items = 7\ncommand_timeout_seconds = 321"),
        encoding="utf-8",
    )
    args = arguments(repository)
    settings = load_settings(repository, args.profile, _overrides(args))
    for_item = _specification(
        settings, DeliveryMode.CURRENT_BRANCH_PR, "main", "base", False, False
    )

    spec = for_item(WorkItem("item", "title", "docs/brief.md", ("accepted",)))

    assert settings.command_timeout_seconds == 321.0
    assert spec.command_timeout_seconds == 321.0


def test_item_class_sets_the_seat_timeout_and_raises_the_policy_ceiling(tmp_path: Path) -> None:
    repository = tmp_path / "project"
    repository.mkdir()
    (repository / ".autobuild.toml").write_text(
        PROFILE + "\n[run.item_classes]\nlarge = 7200\n",
        encoding="utf-8",
    )
    docs = repository / "docs"
    docs.mkdir()
    (docs / "brief.md").write_text("# Title\n\nItem class: large\n", encoding="utf-8")

    args = arguments(repository)
    settings = load_settings(repository, args.profile, _overrides(args))
    for_item = _specification(
        settings, DeliveryMode.CURRENT_BRANCH_PR, "main", "base", False, False
    )

    built = for_item(WorkItem("item", "title", "docs/brief.md", ("accepted",)))
    default = for_item(WorkItem("plain", "title", "docs/missing.md", ("accepted",)))

    assert settings.item_classes == {"large": 7200.0}
    assert built.seat_timeout_seconds == 7200.0
    assert default.seat_timeout_seconds == settings.seat_timeout_seconds
    assert built.seat_stall_seconds == settings.seat_stall_seconds
    assert _max_seat_timeout(settings) == 7200.0


def test_seat_stall_seconds_defaults_and_reads_from_the_profile(tmp_path: Path) -> None:
    repository = tmp_path / "project"
    repository.mkdir()
    (repository / ".autobuild.toml").write_text(PROFILE, encoding="utf-8")
    args = arguments(repository)
    assert load_settings(repository, args.profile, _overrides(args)).seat_stall_seconds == 900.0

    (repository / ".autobuild.toml").write_text(
        PROFILE.replace("max_items = 7", "max_items = 7\nseat_stall_seconds = 300"),
        encoding="utf-8",
    )
    assert load_settings(repository, args.profile, _overrides(args)).seat_stall_seconds == 300.0


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


def test_selection_defaults_to_empty_lists(tmp_path: Path) -> None:
    repository = tmp_path / "project"
    repository.mkdir()
    (repository / ".autobuild.toml").write_text(PROFILE, encoding="utf-8")

    args = arguments(repository)
    settings = load_settings(repository, args.profile, _overrides(args))

    assert settings.selection == CampaignSelection()


def test_selection_lists_come_from_the_profile_and_the_command_line(tmp_path: Path) -> None:
    repository = tmp_path / "project"
    repository.mkdir()
    (repository / ".autobuild.toml").write_text(
        PROFILE
        + "\n[selection]\n"
        + 'allow = ["APP-001", "APP-002"]\n'
        + 'exclude = ["APP-009"]\n',
        encoding="utf-8",
    )

    args = arguments(
        repository,
        "--allow-item",
        "APP-003",
        "--exclude-item",
        "APP-010",
    )
    settings = load_settings(repository, args.profile, _overrides(args))

    assert settings.selection.allow == ("APP-001", "APP-002", "APP-003")
    assert settings.selection.exclude == ("APP-009", "APP-010")
    assert ".autobuild.toml" in settings.selection.allow_source
    assert settings.selection.allow_source.endswith("command line")
    assert settings.selection.exclude_source.endswith("command line")


def test_command_line_selection_works_without_a_profile_list(tmp_path: Path) -> None:
    repository = tmp_path / "project"
    repository.mkdir()
    (repository / ".autobuild.toml").write_text(PROFILE, encoding="utf-8")

    args = arguments(repository, "--allow-item", "APP-004")
    settings = load_settings(repository, args.profile, _overrides(args))

    assert settings.selection.allow == ("APP-004",)
    assert settings.selection.allow_source == "command line"


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


LANE_PROFILE = """
[run]
lanes = ["claude-code", "codex"]
lane_cool_seconds = 1800
lane_state_root = "lane-state"
max_items = 5

[lanes.claude-code]
builder = "claude-opus"
reviewer = "claude-opus"
specialist = "claude-opus"

[lanes.codex]
builder = "gpt-builder"
reviewer = "gpt-reviewer"
specialist = "gpt-specialist"

[validator]
id = "tests"
argv = ["uv", "run", "pytest", "-q"]

[policy]
allowed_tools = ["read", "write", "shell", "python", "git"]
"""


def test_lane_tier_map_defines_ordered_lanes_and_a_primary(tmp_path: Path) -> None:
    repository = tmp_path / "project"
    repository.mkdir()
    (repository / ".autobuild.toml").write_text(LANE_PROFILE, encoding="utf-8")

    args = arguments(repository)
    settings = load_settings(repository, args.profile, _overrides(args))

    assert [lane.name for lane in settings.lanes] == ["claude-code", "codex"]
    assert settings.harness == "claude-code"
    assert settings.builder_model == "claude-opus"
    assert settings.lanes[1].builder_model == "gpt-builder"
    assert settings.lanes[1].specialist_model == "gpt-specialist"
    assert settings.lane_cool_seconds == 1800.0
    assert settings.lane_state_root == (repository / "lane-state").resolve()


def test_harness_flag_selects_the_first_lane(tmp_path: Path) -> None:
    repository = tmp_path / "project"
    repository.mkdir()
    (repository / ".autobuild.toml").write_text(LANE_PROFILE, encoding="utf-8")

    args = arguments(repository, "--harness", "codex")
    settings = load_settings(repository, args.profile, _overrides(args))

    assert [lane.name for lane in settings.lanes] == ["codex", "claude-code"]
    assert settings.harness == "codex"
    assert settings.builder_model == "gpt-builder"


def test_harness_flag_must_name_a_listed_lane(tmp_path: Path) -> None:
    repository = tmp_path / "project"
    repository.mkdir()
    (repository / ".autobuild.toml").write_text(LANE_PROFILE, encoding="utf-8")

    args = arguments(repository, "--harness", "github-copilot")
    with pytest.raises(ConfigurationError, match="not one of run.lanes"):
        load_settings(repository, args.profile, _overrides(args))


def test_single_lane_form_is_one_lane_and_defaults(tmp_path: Path) -> None:
    repository = tmp_path / "project"
    repository.mkdir()
    (repository / ".autobuild.toml").write_text(PROFILE, encoding="utf-8")

    args = arguments(repository)
    settings = load_settings(repository, args.profile, _overrides(args))

    assert [lane.name for lane in settings.lanes] == ["codex"]
    assert settings.lanes[0].builder_model == "builder-model"
    assert settings.lane_cool_seconds == 3600.0
    assert settings.lane_state_root is None


def _settings_with_progress(tmp_path: Path, table: str):
    repository = tmp_path / "project"
    repository.mkdir()
    (repository / ".autobuild.toml").write_text(PROFILE + table, encoding="utf-8")
    args = arguments(repository)
    return load_settings(repository, args.profile, _overrides(args))


def test_progress_table_defaults_to_file_and_stderr_true(tmp_path: Path) -> None:
    settings = _settings_with_progress(tmp_path, "")

    assert settings.progress_file is True
    assert settings.progress_stderr is True
    assert settings.progress_command is None
    assert settings.progress_command_timeout_seconds == 5.0


def test_progress_command_and_settings_are_read_from_the_profile(tmp_path: Path) -> None:
    settings = _settings_with_progress(
        tmp_path,
        "\n[progress]\n"
        'command = ["notify", "--stdin"]\n'
        "file = false\n"
        "stderr = true\n"
        "command_timeout_seconds = 3\n",
    )

    assert settings.progress_command == ("notify", "--stdin")
    assert settings.progress_file is False
    assert settings.progress_stderr is True
    assert settings.progress_command_timeout_seconds == 3.0


def test_progress_command_must_be_a_non_empty_string_array(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="progress.command"):
        _settings_with_progress(tmp_path, "\n[progress]\ncommand = []\n")


def test_progress_file_must_be_a_boolean(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="progress.file must be a boolean"):
        _settings_with_progress(tmp_path, '\n[progress]\nfile = "yes"\n')


def test_progress_command_timeout_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="progress.command_timeout_seconds"):
        _settings_with_progress(tmp_path, "\n[progress]\ncommand_timeout_seconds = 0\n")


def test_watch_runs_root_prefers_the_scratch_override(tmp_path: Path) -> None:
    repository = tmp_path / "project"
    repository.mkdir()
    # The override wins and is honoured without reading any profile at all.
    override = tmp_path / "elsewhere"

    root = resolve_runs_root(str(repository), None, str(override))

    assert root == override.resolve() / "runs"


def test_watch_runs_root_reads_only_the_run_scratch_root_from_the_profile(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "project"
    repository.mkdir()
    # A profile with [run] scratch_root but no models or validator: full
    # load_settings rejects it, yet the runs root still resolves from [run] alone.
    (repository / ".autobuild.toml").write_text(
        '[run]\nscratch_root = "scratch"\n', encoding="utf-8"
    )

    with pytest.raises(ConfigurationError):
        load_settings(repository, None, ProfileOverrides())

    root = resolve_runs_root(str(repository), None, None)

    assert root == (repository / "scratch").resolve() / "runs"


def test_watch_runs_root_falls_back_to_the_default_scratch_root(tmp_path: Path) -> None:
    repository = tmp_path / "project"
    repository.mkdir()

    root = resolve_runs_root(str(repository), None, None)

    assert root == default_scratch_root() / "runs"
