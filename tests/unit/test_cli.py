from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

import pytest

from autobuild.adapters import (
    ClaudeCodeHarnessAdapter,
    CodexHarnessAdapter,
    CopilotCliHarnessAdapter,
)
from autobuild.bootstrap.composition import (
    _build_lanes,
    _lane_efforts,
    _max_seat_timeout,
    _require_lane_efforts,
    _specification,
    _starting_lane_efforts,
    run_campaign,
)
from autobuild.bootstrap.environment import default_scratch_root, resolve_runs_root
from autobuild.bootstrap.profile import (
    ConfigurationError,
    LaneProfile,
    ProfileOverrides,
    _PROFILE_ALLOWED_KEYS,
    load_settings,
)
from autobuild.bootstrap.registry import AdapterRegistry
from autobuild.cli import _overrides, _parser
from autobuild.domain import (
    AdapterIdentity,
    CampaignSelection,
    DeliveryMode,
    EffortLevel,
    FogRecord,
    PortKind,
    ProbeResult,
    Proposal,
    RefillPlan,
    Seat,
    WorkItem,
)
from autobuild.testing import FakeCommandAdapter, FakeLaneStateAdapter


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


def test_batch_and_patient_validator_settings_are_read_from_the_profile(tmp_path: Path) -> None:
    repository = tmp_path / "project"
    repository.mkdir()
    profile = repository / ".autobuild.toml"
    profile.write_text(PROFILE, encoding="utf-8")
    args = arguments(repository)

    defaults = load_settings(repository, args.profile, _overrides(args))

    assert defaults.batch_validate_every == 5
    assert defaults.validator_patient_argv == ()
    profile.write_text(
        PROFILE.replace(
            'argv = ["uv", "run", "pytest", "-q"]',
            'argv = ["uv", "run", "pytest", "-q"]\npatient_argv = ["uv", "run", "pytest", "-p", "x"]',
        ).replace("[run]\n", "[run]\nbatch_validate_every = 3\n"),
        encoding="utf-8",
    )
    settings = load_settings(repository, args.profile, _overrides(args))
    assert settings.batch_validate_every == 3
    assert settings.validator_patient_argv == ("uv", "run", "pytest", "-p", "x")
    profile.write_text(PROFILE.replace("[run]\n", "[run]\nbatch_validate_every = 0\n"), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="batch_validate_every"):
        load_settings(repository, args.profile, _overrides(args))


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


@pytest.mark.parametrize("key", ["builder_effort", "reviewer_effort", "specialist_effort"])
def test_an_invalid_parked_lane_effort_names_the_lane_key(tmp_path: Path, key: str) -> None:
    profile = LANE_PROFILE + f'\n[lanes.parked]\n{key} = "turbo"\n'

    with pytest.raises(ConfigurationError, match=rf"lanes\.parked\.{key} must be one of"):
        _load(tmp_path, profile)


@pytest.mark.parametrize("key", ["builder", "reviewer", "specialist"])
def test_an_empty_parked_lane_model_name_is_refused(tmp_path: Path, key: str) -> None:
    profile = LANE_PROFILE + f'\n[lanes.parked]\n{key} = ""\n'

    with pytest.raises(
        ConfigurationError, match=rf"lanes\.parked\.{key} must be a non-empty string"
    ):
        _load(tmp_path, profile)


@pytest.mark.parametrize(
    "parked_table",
    [
        '[lanes.parked]\nbuilder_effort = "low"\nreviewer_effort = "high"\n',
        '[lanes.parked]\nbuilder = "builder-model"\n',
        '[lanes.unconfigured-harness]\nbuilder = "builder-model"\n',
    ],
)
def test_a_parked_lane_accepts_present_values_without_becoming_active(
    tmp_path: Path, parked_table: str
) -> None:
    settings = _load(tmp_path, LANE_PROFILE + "\n" + parked_table)

    assert [lane.name for lane in settings.lanes] == ["claude-code", "codex"]


def test_harness_flag_refuses_a_parked_lane(tmp_path: Path) -> None:
    profile = LANE_PROFILE + '\n[lanes.parked]\nbuilder = "builder-model"\n'
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / ".autobuild.toml").write_text(profile, encoding="utf-8")
    args = arguments(repository, "--harness", "parked")

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


def _load(tmp_path: Path, profile: str):
    repository = tmp_path / "project"
    repository.mkdir()
    (repository / ".autobuild.toml").write_text(profile, encoding="utf-8")
    args = arguments(repository)
    return load_settings(repository, args.profile, _overrides(args))


def _spec_for(settings):
    for_item = _specification(
        settings, DeliveryMode.CURRENT_BRANCH_PR, "main", "base", False, False
    )
    return for_item(WorkItem("item", "title", "docs/brief.md", ("accepted",)))


def test_single_lane_effort_keys_resolve_per_seat_and_reach_the_spec(tmp_path: Path) -> None:
    settings = _load(
        tmp_path,
        PROFILE.replace(
            'reviewer = "reviewer-model"',
            'reviewer = "reviewer-model"\n'
            'builder_effort = "medium"\n'
            'reviewer_effort = "high"\n'
            'specialist_effort = "max"',
        ),
    )

    lane = settings.lanes[0]
    assert (lane.builder_effort, lane.reviewer_effort, lane.specialist_effort) == (
        EffortLevel.MEDIUM,
        EffortLevel.HIGH,
        EffortLevel.MAX,
    )
    spec = _spec_for(settings)
    assert spec.seat_effort(Seat.BUILDER, "codex") is EffortLevel.MEDIUM
    assert spec.seat_effort(Seat.REVIEWER, "codex") is EffortLevel.HIGH
    assert spec.seat_effort(Seat.SPECIALIST, "codex") is EffortLevel.MAX


def test_an_absent_specialist_effort_takes_the_reviewer_effort(tmp_path: Path) -> None:
    settings = _load(
        tmp_path,
        PROFILE.replace(
            'reviewer = "reviewer-model"',
            'reviewer = "reviewer-model"\nreviewer_effort = "xhigh"',
        ),
    )

    lane = settings.lanes[0]
    assert lane.builder_effort is None
    assert lane.specialist_effort is EffortLevel.XHIGH
    assert lane.efforts() == {Seat.REVIEWER: EffortLevel.XHIGH, Seat.SPECIALIST: EffortLevel.XHIGH}


def test_a_profile_without_effort_keys_gives_every_seat_no_effort(tmp_path: Path) -> None:
    settings = _load(tmp_path, PROFILE)

    lane = settings.lanes[0]
    assert (lane.builder_effort, lane.reviewer_effort, lane.specialist_effort) == (None, None, None)
    spec = _spec_for(settings)
    for seat in Seat:
        assert spec.seat_effort(seat, "codex") is None


def test_campaign_start_efforts_preserve_unset_profile_seats(tmp_path: Path) -> None:
    settings = _load(
        tmp_path,
        PROFILE.replace(
            'reviewer = "reviewer-model"',
            'reviewer = "reviewer-model"\nbuilder_effort = "high"',
        ),
    )

    efforts = _starting_lane_efforts(settings, _lane_efforts(settings))

    assert efforts == {
        "builder": EffortLevel.HIGH,
        "reviewer": None,
        "specialist": None,
    }


def test_campaign_start_efforts_are_all_unset_without_profile_keys(tmp_path: Path) -> None:
    settings = _load(tmp_path, PROFILE)

    assert _starting_lane_efforts(settings, _lane_efforts(settings)) == {
        "builder": None,
        "reviewer": None,
        "specialist": None,
    }


def test_lane_effort_keys_resolve_per_lane_and_seat(tmp_path: Path) -> None:
    settings = _load(
        tmp_path,
        LANE_PROFILE.replace(
            'specialist = "claude-opus"',
            'specialist = "claude-opus"\nbuilder_effort = "medium"\nreviewer_effort = "high"',
        ).replace(
            'specialist = "gpt-specialist"',
            'specialist = "gpt-specialist"\nbuilder_effort = "high"\nspecialist_effort = "low"',
        ),
    )

    claude, codex = settings.lanes
    assert claude.efforts() == {
        Seat.BUILDER: EffortLevel.MEDIUM,
        Seat.REVIEWER: EffortLevel.HIGH,
        Seat.SPECIALIST: EffortLevel.HIGH,
    }
    assert codex.efforts() == {Seat.BUILDER: EffortLevel.HIGH, Seat.SPECIALIST: EffortLevel.LOW}
    spec = _spec_for(settings)
    assert spec.seat_effort(Seat.BUILDER, "claude-code") is EffortLevel.MEDIUM
    assert spec.seat_effort(Seat.BUILDER, "codex") is EffortLevel.HIGH
    assert spec.seat_effort(Seat.REVIEWER, "codex") is None


@pytest.mark.parametrize("value", ['"extreme"', '"HIGH"', '""', "3", "true"])
def test_an_invalid_model_effort_names_the_key(tmp_path: Path, value: str) -> None:
    profile = PROFILE.replace(
        'reviewer = "reviewer-model"', f'reviewer = "reviewer-model"\nbuilder_effort = {value}'
    )

    with pytest.raises(ConfigurationError, match=r"models\.builder_effort must be one of: low, medium, high, xhigh, max"):
        _load(tmp_path, profile)


@pytest.mark.parametrize("key", ["builder_effort", "reviewer_effort", "specialist_effort"])
def test_an_invalid_lane_effort_names_the_lane_key(tmp_path: Path, key: str) -> None:
    profile = LANE_PROFILE.replace(
        'specialist = "gpt-specialist"', f'specialist = "gpt-specialist"\n{key} = "turbo"'
    )

    with pytest.raises(ConfigurationError, match=rf"lanes\.codex\.{key} must be one of"):
        _load(tmp_path, profile)


@pytest.mark.parametrize("key", ["builder_effort", "reviewer_effort", "specialist_effort"])
@pytest.mark.parametrize("value", ['"high"', '"turbo"'])
def test_a_models_effort_in_the_lane_form_is_refused_not_dropped(
    tmp_path: Path, key: str, value: str
) -> None:
    profile = LANE_PROFILE + f"\n[models]\nbuilder = \"ignored-model\"\n{key} = {value}\n"

    with pytest.raises(
        ConfigurationError,
        match=rf"models\.{key} is not used when run\.lanes is set; set {key} in the \[lanes\.<harness>\] tables",
    ):
        _load(tmp_path, profile)


@pytest.mark.parametrize("key", ["builder_effort", "reviewer_effort", "specialist_effort"])
@pytest.mark.parametrize("value", ['"high"', '"turbo"'])
def test_a_lane_table_effort_in_the_single_lane_form_is_refused_not_dropped(
    tmp_path: Path, key: str, value: str
) -> None:
    profile = PROFILE + f'\n[lanes.codex]\nbuilder = "ignored-model"\n{key} = {value}\n'

    with pytest.raises(
        ConfigurationError,
        match=(
            rf"lanes\.codex\.{key} is not used: lane tables are read only when "
            rf"run\.lanes is set; set {key} in \[models\]"
        ),
    ):
        _load(tmp_path, profile)


def test_model_names_in_lane_tables_stay_ignored_in_the_single_lane_form(tmp_path: Path) -> None:
    settings = _load(tmp_path, PROFILE + '\n[lanes.codex]\nbuilder = "ignored-model"\n')

    assert [lane.name for lane in settings.lanes] == ["codex"]
    assert settings.builder_model == "builder-model"


def test_a_lane_named_twice_in_run_lanes_is_refused(tmp_path: Path) -> None:
    profile = LANE_PROFILE.replace(
        'lanes = ["claude-code", "codex"]', 'lanes = ["codex", "claude-code", "codex"]'
    ).replace('specialist = "gpt-specialist"', 'specialist = "gpt-specialist"\nbuilder_effort = "high"')

    with pytest.raises(ConfigurationError, match=r"run\.lanes names a lane more than once: codex$"):
        _load(tmp_path, profile)


def test_a_spec_built_from_a_profile_with_efforts_is_hashable(tmp_path: Path) -> None:
    settings = _load(
        tmp_path,
        LANE_PROFILE.replace(
            'specialist = "claude-opus"',
            'specialist = "claude-opus"\nbuilder_effort = "medium"\nreviewer_effort = "high"',
        ).replace(
            'specialist = "gpt-specialist"',
            'specialist = "gpt-specialist"\nbuilder_effort = "high"',
        ),
    )
    first, second = _spec_for(settings), _spec_for(settings)

    assert first.lane_efforts
    assert hash(first) == hash(second)
    assert first == second


def test_model_names_in_models_stay_ignored_in_the_lane_form(tmp_path: Path) -> None:
    settings = _load(tmp_path, LANE_PROFILE + '\n[models]\nbuilder = "ignored-model"\n')

    assert settings.builder_model == "claude-opus"


class _StubHarness:
    def __init__(self, effort_levels=None) -> None:
        if effort_levels is not None:
            self.effort_levels = effort_levels
        self.probed = False

    def probe(self):
        self.probed = True
        return ProbeResult.ready(AdapterIdentity("stub", "1"))


def test_the_launch_refuses_a_lane_effort_before_probing_any_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _load(
        tmp_path,
        LANE_PROFILE.replace(
            'specialist = "claude-opus"',
            'specialist = "claude-opus"\nbuilder_effort = "medium"',
        ).replace(
            'specialist = "gpt-specialist"',
            'specialist = "gpt-specialist"\nreviewer_effort = "max"',
        ),
    )
    stubs = {
        "claude-code": _StubHarness(frozenset(EffortLevel)),
        "codex": _StubHarness(frozenset({EffortLevel.LOW, EffortLevel.MEDIUM})),
    }

    def register(registry) -> None:
        for name, stub in stubs.items():
            registry.register(PortKind.HARNESS, name, lambda config, stub=stub: stub)

    monkeypatch.setattr("autobuild.bootstrap.composition.register_first_party_harnesses", register)
    monkeypatch.setattr(AdapterRegistry, "load_entry_points", lambda self: None)
    lane_state = FakeLaneStateAdapter(AdapterIdentity("lane-state", "1"))

    with pytest.raises(
        ConfigurationError,
        match=r"lane codex sets reviewer effort max, which its harness adapter cannot pass \(accepted: low, medium\)",
    ):
        _build_lanes(
            settings,
            FakeCommandAdapter(AdapterIdentity("command", "1")),
            tmp_path / "scratch",
            lane_state,
            "campaign",
        )

    assert [stub.probed for stub in stubs.values()] == [False, False]
    assert lane_state.cools == []


def test_an_adapter_that_declares_no_effort_levels_refuses_any_lane_effort() -> None:
    lane = LaneProfile("third-party", "b", "r", "r", builder_effort=EffortLevel.LOW)

    with pytest.raises(
        ConfigurationError,
        match=r"lane third-party sets builder effort low, which its harness adapter cannot pass \(accepted: none\)",
    ):
        _require_lane_efforts(lane, _StubHarness())


def test_a_lane_without_efforts_launches_on_an_adapter_that_declares_none() -> None:
    _require_lane_efforts(LaneProfile("third-party", "b", "r", "r"), _StubHarness())


@pytest.mark.parametrize(
    "adapter_class", [ClaudeCodeHarnessAdapter, CodexHarnessAdapter, CopilotCliHarnessAdapter]
)
def test_every_first_party_adapter_accepts_every_effort_level(tmp_path: Path, adapter_class) -> None:
    adapter = adapter_class(
        FakeCommandAdapter(AdapterIdentity("command", "1")),
        tmp_path / "harness",
        command=(sys.executable,),
    )
    lane = LaneProfile(
        "lane", "b", "r", "r", EffortLevel.LOW, EffortLevel.XHIGH, EffortLevel.MAX
    )

    _require_lane_efforts(lane, adapter)
    assert adapter.effort_levels == frozenset(EffortLevel)


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


@pytest.mark.parametrize(
    ("table", "profile"),
    [
        ("run", PROFILE.replace("max_items = 7", 'max_items = 7\nunknown = "value"')),
        (
            "models",
            PROFILE.replace('builder = "builder-model"', 'builder = "builder-model"\nunknown = "value"'),
        ),
        (
            "validator",
            PROFILE.replace('id = "tests"', 'id = "tests"\nunknown = "value"'),
        ),
        (
            "policy",
            PROFILE.replace(
                'allowed_tools = ["read", "write", "shell", "python", "git"]',
                'allowed_tools = ["read", "write", "shell", "python", "git"]\nunknown = "value"',
            ),
        ),
        ("harness", PROFILE + '\n[harness]\nunknown = "value"\n'),
        ("tracker", PROFILE + '\n[tracker]\nunknown = "value"\n'),
        ("preflight", PROFILE + '\n[preflight]\nunknown = "value"\n'),
        ("refill", PROFILE + '\n[refill]\nunknown = "value"\n'),
        ("knowledge", PROFILE + '\n[knowledge]\nunknown = "value"\n'),
        ("selection", PROFILE + '\n[selection]\nunknown = "value"\n'),
        ("progress", PROFILE + '\n[progress]\nunknown = "value"\n'),
        (
            "lanes.arbitrary",
            LANE_PROFILE + '\n[lanes.arbitrary]\nbuilder = "builder"\nreviewer = "reviewer"\nunknown = "value"\n',
        ),
    ],
)
def test_profile_refuses_unknown_fields_in_each_table(
    tmp_path: Path, table: str, profile: str
) -> None:
    with pytest.raises(
        ConfigurationError, match=rf"{re.escape(table)} contains unknown fields: unknown"
    ):
        _load(tmp_path, profile)


def test_profile_refuses_all_unknown_top_level_fields_at_once(tmp_path: Path) -> None:
    profile = 'unknown_value = "value"\n' + PROFILE + '\n[unknown_table]\nvalue = "value"\n'

    with pytest.raises(
        ConfigurationError,
        match=r"profile contains unknown fields: unknown_table, unknown_value",
    ):
        _load(tmp_path, profile)


def test_profile_refuses_all_unknown_fields_in_a_known_table_at_once(
    tmp_path: Path,
) -> None:
    profile = PROFILE.replace(
        "max_items = 7", 'max_items = 7\nunknown_a = "value"\nunknown_b = "value"'
    )

    with pytest.raises(
        ConfigurationError,
        match=r"run contains unknown fields: unknown_a, unknown_b",
    ):
        _load(tmp_path, profile)


def test_profile_refuses_all_unknown_fields_across_known_tables_at_once(
    tmp_path: Path,
) -> None:
    profile = PROFILE.replace(
        "max_items = 7", 'max_items = 7\nunknown_run = "value"'
    ).replace(
        'builder = "builder-model"', 'builder = "builder-model"\nunknown_models = "value"'
    )

    with pytest.raises(
        ConfigurationError,
        match=r"run contains unknown fields: unknown_run; models contains unknown fields: unknown_models",
    ):
        _load(tmp_path, profile)


def test_profile_refuses_a_non_table_lanes_value(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match=r"\[lanes\] must be a TOML table"):
        _load(tmp_path, 'lanes = "not-a-table"\n' + PROFILE)


def test_profile_refuses_a_non_table_lanes_child_value(tmp_path: Path) -> None:
    with pytest.raises(
        ConfigurationError, match=r"\[lanes\.arbitrary\] must be a TOML table"
    ):
        _load(tmp_path, PROFILE + '\n[lanes]\narbitrary = "not-a-table"\n')


def test_profile_keeps_open_item_class_and_lane_names(tmp_path: Path) -> None:
    item_classes = _load(
        tmp_path,
        PROFILE + "\n[run.item_classes]\narbitrary_class = 123\n",
    )
    lane_root = tmp_path / "lanes"
    lane_root.mkdir()
    lanes = _load(
        lane_root,
        """
[run]
lanes = ["arbitrary_lane"]

[lanes.arbitrary_lane]
builder = "builder"
reviewer = "reviewer"

[models]
builder = "ignored"
reviewer = "ignored"

[validator]
id = "tests"
argv = ["test"]
""",
    )

    assert item_classes.item_classes == {"arbitrary_class": 123.0}
    assert lanes.harness == "arbitrary_lane"


def test_lane_profiles_keep_single_lane_fields_that_are_ignored_in_lane_mode(
    tmp_path: Path,
) -> None:
    profile = (
        LANE_PROFILE.replace(
            'lanes = ["claude-code", "codex"]',
            'harness = "ignored-harness"\nlanes = ["claude-code", "codex"]',
        )
        + '\n[models]\nbuilder = "ignored-builder"\nreviewer = "ignored-reviewer"\n'
    )

    settings = _load(tmp_path, profile)

    assert settings.harness == "claude-code"


def test_watcher_resolves_scratch_root_from_a_profile_with_an_unknown_field(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "project"
    repository.mkdir()
    (repository / ".autobuild.toml").write_text(
        '[run]\nscratch_root = "scratch"\nunknown = "value"\n', encoding="utf-8"
    )

    with pytest.raises(ConfigurationError, match="run contains unknown fields: unknown"):
        load_settings(repository, None, ProfileOverrides())

    assert resolve_runs_root(str(repository), None, None) == (repository / "scratch").resolve() / "runs"


def test_running_autobuild_toml_blocks_use_only_documented_profile_keys() -> None:
    document = Path(__file__).parents[2] / "docs" / "running-autobuild.md"
    blocks = re.findall(r"\x60\x60\x60toml\n(.*?)\n\x60\x60\x60", document.read_text(encoding="utf-8"), re.DOTALL)

    assert blocks
    for block in blocks:
        profile = tomllib.loads(block)
        assert set(profile) <= _PROFILE_ALLOWED_KEYS["profile"]
        for table_name, table in profile.items():
            assert isinstance(table, dict)
            if table_name == "lanes":
                for lane in table.values():
                    assert isinstance(lane, dict)
                    assert set(lane) <= _PROFILE_ALLOWED_KEYS["lanes.*"]
            else:
                assert set(table) <= _PROFILE_ALLOWED_KEYS[table_name]


def test_neutral_machine_profile_forms_load_with_their_known_fields(tmp_path: Path) -> None:
    profiles = (
        PROFILE,
        LANE_PROFILE
        + """
[models]
builder = "ignored"
reviewer = "ignored"

[harness]
command = ["harness"]
""",
        """
[run]
harness = "neutral"
max_items = 1
seat_timeout_seconds = 1
seat_stall_seconds = 1
lease_stale_seconds = 1
command_timeout_seconds = 1
scratch_root = "scratch"
lane_state_root = "lane-state"
lane_cool_seconds = 1

[run.item_classes]
arbitrary = 1

[models]
builder = "builder"
reviewer = "reviewer"
specialist = "specialist"
builder_effort = "low"
reviewer_effort = "medium"
specialist_effort = "high"

[validator]
id = "tests"
argv = ["test"]
budget_seconds = 1

[harness]
command = ["harness"]

[policy]
allowed_tools = ["read"]
allowed_roots = ["shared"]

[tracker]
kind = "backlog"
path = "BACKLOG.md"

[preflight]
tls_targets = ["example.test:443"]
accepted_environment = ["SSL_CERT_FILE"]

[selection]
allow = ["A"]
exclude = ["B"]

[progress]
file = true
stderr = false
command = ["notify"]
command_timeout_seconds = 1
""",
    )

    for index, profile in enumerate(profiles):
        profile_root = tmp_path / str(index)
        profile_root.mkdir()
        _load(profile_root, profile)
