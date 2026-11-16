"""The ``[process]`` profile table: flow by default, strict on opt-in.

Strict limits a builder seat to the tests that cover its change: the brief
names them and a whole-suite run from a builder seat is refused."""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import pytest
from test_workflows import builder, campaign, command, make_ports, review, spec

from autobuild.application import ItemWorkflow
from autobuild.application.prompts import render_seat_instructions
from autobuild.bootstrap.composition import _specification
from autobuild.bootstrap.profile import ConfigurationError, load_settings
from autobuild.domain import DeliveryMode, ItemDisposition, ReviewDecision, Seat, WorkItem
from autobuild.domain.models import ProcessMode, ProcessPolicy, TestCoverage, TestGuard
from autobuild.enforcement.test_scope import (
    builder_test_refusal,
    item_covering_entries,
    seat_test_guard,
    shell_command_refusal,
    shell_commands,
)

BASE = """
[run]
harness = "claude-code"

[models]
builder = "claude-opus-5-5"
reviewer = "fable"

[validator]
id = "tests"
argv = ["uv", "run", "pytest", "tests", "-q"]
"""

STRICT = """
[process]
mode = "strict"
test_command = ["uv", "run", "pytest"]

[process.test_map]
"src/app/profile/**" = ["tests/unit/test_profile.py"]
"src/app/web/**" = "npm test -- web"
"""

BRIEF = """# Change the profile

## Declared paths

`src/app/profile/loader.py`
"""


def load(tmp_path: Path, profile: str):
    repository = tmp_path / "project"
    repository.mkdir(exist_ok=True)
    (repository / ".autobuild.toml").write_text(profile, encoding="utf-8")
    return load_settings(repository)


def strict_policy() -> ProcessPolicy:
    return ProcessPolicy(
        ProcessMode.STRICT,
        ("uv", "run", "pytest"),
        (
            TestCoverage("src/app/profile/**", tests=("tests/unit/test_profile.py",)),
            TestCoverage("src/app/web/**", command=("npm", "test", "--", "web")),
        ),
    )


WHOLE_SUITE = ("uv", "run", "pytest")
WHOLE_SUITE_DIRECTORY = ("uv", "run", "pytest", "tests", "-q")
COVERING = ("uv", "run", "pytest", "tests/unit/test_profile.py", "-q")


# --- the profile table --------------------------------------------------------------


def test_a_profile_without_the_process_table_is_flow(tmp_path: Path) -> None:
    settings = load(tmp_path, BASE)

    assert settings.process.mode is ProcessMode.FLOW
    assert settings.process.strict is False


def test_a_process_table_without_a_mode_is_flow(tmp_path: Path) -> None:
    settings = load(tmp_path, BASE + '\n[process]\ntest_command = ["uv", "run", "pytest"]\n')

    assert settings.process.mode is ProcessMode.FLOW


def test_strict_mode_loads_the_test_map(tmp_path: Path) -> None:
    settings = load(tmp_path, BASE + STRICT)

    assert settings.process.strict
    assert settings.process.test_command == ("uv", "run", "pytest")
    assert settings.process.test_map == strict_policy().test_map


def test_the_documented_strict_example_loads(tmp_path: Path) -> None:
    guide = (Path(__file__).parents[2] / "docs" / "running-autobuild.md").read_text(encoding="utf-8")
    block = next(
        block
        for block in re.findall(r"```toml\n(.*?)\n```", guide, re.DOTALL)
        if block.startswith("[process]")
    )

    settings = load(tmp_path, BASE + "\n" + block + "\n")

    assert settings.process.strict
    assert settings.process.test_map[-1].command == ("npm", "test", "--", "web")


def test_an_unknown_mode_is_refused_with_one_sentence(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError) as refusal:
        load(tmp_path, BASE + '\n[process]\nmode = "fast"\n')

    assert str(refusal.value) == "process.mode must be \"flow\" or \"strict\", not 'fast'."


@pytest.mark.parametrize("value", ['["strict"]', "1", "{ name = \"strict\" }"])
def test_a_mode_that_is_not_a_string_is_refused_with_one_sentence(tmp_path: Path, value: str) -> None:
    with pytest.raises(ConfigurationError) as refusal:
        load(tmp_path, BASE + f"\n[process]\nmode = {value}\n")

    assert str(refusal.value).startswith('process.mode must be "flow" or "strict", not ')


def test_an_unknown_process_field_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="process contains unknown fields: tests"):
        load(tmp_path, BASE + '\n[process]\ntests = "all"\n')


def test_strict_mode_without_a_test_map_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="strict process mode needs a test map"):
        load(tmp_path, BASE + '\n[process]\nmode = "strict"\n')


def test_test_paths_without_a_test_command_are_refused(tmp_path: Path) -> None:
    profile = BASE + '\n[process]\nmode = "strict"\n[process.test_map]\n"src/**" = ["tests"]\n'

    with pytest.raises(ConfigurationError, match="needs a test command"):
        load(tmp_path, profile)


def test_a_test_command_string_is_split_with_shell_quoting(tmp_path: Path) -> None:
    profile = (
        BASE
        + '\n[process]\nmode = "strict"\n[process.test_map]\n'
        + "\"web/**\" = \"npm test -- --grep 'profile page' web\"\n"
    )

    (entry,) = load(tmp_path, profile).process.test_map

    assert entry.command == ("npm", "test", "--", "--grep", "profile page", "web")


def test_a_test_command_string_with_an_open_quote_is_refused(tmp_path: Path) -> None:
    profile = BASE + '\n[process]\nmode = "strict"\n[process.test_map]\n"web/**" = "npm test \'web"\n'

    with pytest.raises(ConfigurationError) as refusal:
        load(tmp_path, profile)

    assert str(refusal.value) == (
        "process.test_map.web/** is not a command line the shell can split: No closing quotation."
    )


@pytest.mark.parametrize("value", ["[1, 2]", '["tests/unit", 3]', "[]", '[["tests"]]', '[""]'])
def test_a_test_map_array_that_is_not_a_list_of_strings_is_refused(tmp_path: Path, value: str) -> None:
    profile = (
        BASE
        + '\n[process]\nmode = "strict"\ntest_command = ["uv", "run", "pytest"]\n'
        + f'[process.test_map]\n"src/**" = {value}\n'
    )

    with pytest.raises(ConfigurationError) as refusal:
        load(tmp_path, profile)

    assert str(refusal.value) == (
        "process.test_map.src/** must be a command line or a non-empty list of test path strings."
    )


def test_the_profile_process_reaches_every_item_specification(tmp_path: Path) -> None:
    settings = load(tmp_path, BASE + STRICT)
    for_item = _specification(settings, DeliveryMode.PROTECTED_DEFAULT, "main", "base", False, False)

    work = for_item(WorkItem("item-1", "title", "brief.md", ("passes",)))

    assert work.process == settings.process


# --- the builder brief ----------------------------------------------------------------


def test_the_strict_builder_brief_names_the_covering_tests() -> None:
    work = replace(spec(brief_text=BRIEF), process=strict_policy())

    instructions = render_seat_instructions(work, Seat.BUILDER, ())

    assert "Process (strict):" in instructions
    assert "The covering tests for this item's declared paths are: tests/unit/test_profile.py." in instructions
    assert "A whole-suite run is not allowed." in instructions
    assert "npm test -- web" not in instructions


def test_a_strict_brief_whose_paths_match_no_entry_lists_the_map() -> None:
    work = replace(spec(brief_text="## Declared paths\n\n`docs/readme.md`\n"), process=strict_policy())

    instructions = render_seat_instructions(work, Seat.BUILDER, ())

    assert "src/app/profile/** is covered by tests/unit/test_profile.py" in instructions
    assert "src/app/web/** is covered by npm test -- web" in instructions


def test_the_flow_builder_brief_is_unchanged() -> None:
    flow = render_seat_instructions(spec(brief_text=BRIEF), Seat.BUILDER, ())
    flow_with_map = render_seat_instructions(
        replace(spec(brief_text=BRIEF), process=replace(strict_policy(), mode=ProcessMode.FLOW)),
        Seat.BUILDER,
        (),
    )

    assert "Process (strict):" not in flow
    assert flow_with_map == flow


@pytest.mark.parametrize("seat", [Seat.REVIEWER, Seat.SPECIALIST])
def test_review_seats_carry_no_strict_constraint(seat: Seat) -> None:
    work = replace(spec(brief_text=BRIEF), process=strict_policy())

    assert "Process (strict):" not in render_seat_instructions(work, seat, ())


# --- the builder test-run rule -----------------------------------------------------------


def covering() -> tuple[TestCoverage, ...]:
    return item_covering_entries(strict_policy(), BRIEF)


@pytest.mark.parametrize("argv", [WHOLE_SUITE, WHOLE_SUITE_DIRECTORY, ("uv", "run", "pytest", "-k", "profile")])
def test_strict_refuses_a_whole_suite_run_from_a_builder_seat(argv: tuple[str, ...]) -> None:
    refusal = builder_test_refusal(strict_policy(), Seat.BUILDER, argv, covering())

    assert refusal == (
        "A builder seat may not run the whole test suite or tests outside its change; "
        "run only the covering tests: tests/unit/test_profile.py."
    )


@pytest.mark.parametrize(
    "argv",
    [
        COVERING,
        ("uv", "run", "pytest", "./tests/unit/test_profile.py::test_one"),
    ],
)
def test_strict_admits_a_covering_run_from_a_builder_seat(argv: tuple[str, ...]) -> None:
    assert builder_test_refusal(strict_policy(), Seat.BUILDER, argv, covering()) is None


@pytest.mark.parametrize(
    "extra",
    [("tests",), (".",), ("./",), ("tests/",), ("-q", "tests"), ("tests/unit",), ("src",)],
)
def test_strict_refuses_a_covering_run_that_also_names_a_bare_directory(extra: tuple[str, ...]) -> None:
    argv = ("uv", "run", "pytest", "tests/unit/test_profile.py", *extra)

    assert builder_test_refusal(strict_policy(), Seat.BUILDER, argv, covering()) is not None


def test_strict_reads_a_bare_word_after_an_option_as_its_value() -> None:
    argv = ("uv", "run", "pytest", "-k", "profile", "tests/unit/test_profile.py", "-p", "no:cacheprovider")

    assert builder_test_refusal(strict_policy(), Seat.BUILDER, argv, covering()) is None


def test_strict_refuses_a_run_of_tests_outside_the_change() -> None:
    argv = ("uv", "run", "pytest", "tests/unit/test_profile.py", "tests/unit/test_other.py")

    assert builder_test_refusal(strict_policy(), Seat.BUILDER, argv, covering()) is not None


def test_strict_refuses_the_bare_form_of_a_mapped_test_command() -> None:
    web = item_covering_entries(strict_policy(), "## Declared paths\n\n`src/app/web/page.ts`\n")

    assert builder_test_refusal(strict_policy(), Seat.BUILDER, ("npm", "test"), web) == (
        "A builder seat may not run the whole test suite or tests outside its change; "
        "run only the covering tests: npm test -- web."
    )
    assert builder_test_refusal(strict_policy(), Seat.BUILDER, ("npm", "test", "--", "web"), web) is None


@pytest.mark.parametrize("argv", [WHOLE_SUITE, WHOLE_SUITE_DIRECTORY])
def test_flow_admits_the_same_whole_suite_run(argv: tuple[str, ...]) -> None:
    flow = replace(strict_policy(), mode=ProcessMode.FLOW)

    assert builder_test_refusal(flow, Seat.BUILDER, argv, covering()) is None
    assert builder_test_refusal(ProcessPolicy(), Seat.BUILDER, argv, ()) is None


@pytest.mark.parametrize("seat", [Seat.REVIEWER, Seat.SPECIALIST])
def test_strict_does_not_judge_review_seats(seat: Seat) -> None:
    assert builder_test_refusal(strict_policy(), seat, WHOLE_SUITE, covering()) is None


def test_strict_does_not_judge_a_command_that_is_not_a_test_run() -> None:
    assert builder_test_refusal(strict_policy(), Seat.BUILDER, ("git", "status"), covering()) is None


# --- every spelling of the runner ---------------------------------------------------------

RUNNER_SPELLINGS = [
    ("pytest",),
    ("python", "-m", "pytest"),
    ("python3", "-m", "pytest", "-q"),
    ("uv", "run", "python", "-m", "pytest"),
    ("uv", "run", "--native-tls", "python", "-m", "pytest", "tests"),
    ("uv", "run", "--with", "pytest-xdist", "pytest", "-q"),
    (".venv/bin/pytest",),
    ("/usr/local/bin/pytest", "tests"),
    ("uv", "--quiet", "run", "pytest"),
    ("uv", "--native-tls", "run", "python", "-m", "pytest"),
    ("uv", "--directory", ".", "run", "pytest"),
]


@pytest.mark.parametrize("argv", RUNNER_SPELLINGS, ids=" ".join)
def test_strict_refuses_a_whole_suite_run_however_the_runner_is_spelled(argv: tuple[str, ...]) -> None:
    assert builder_test_refusal(strict_policy(), Seat.BUILDER, argv, covering()) is not None


@pytest.mark.parametrize("argv", RUNNER_SPELLINGS, ids=" ".join)
def test_strict_admits_a_covering_run_however_the_runner_is_spelled(argv: tuple[str, ...]) -> None:
    runner = tuple(part for part in argv if part not in {"tests", "-q"})
    covering_run = (*runner, "tests/unit/test_profile.py")

    assert builder_test_refusal(strict_policy(), Seat.BUILDER, covering_run, covering()) is None


@pytest.mark.parametrize(
    "argv",
    [("uv", "-q", "run", "pytest"), ("uv", "-q", "run", "pytest", "tests"), ("uv", "-q", "run", "-q", "pytest")],
    ids=" ".join,
)
def test_a_uv_option_before_run_does_not_hide_the_runner(argv: tuple[str, ...]) -> None:
    covering_run = (*(part for part in argv if part != "tests"), "tests/unit/test_profile.py")

    assert builder_test_refusal(strict_policy(), Seat.BUILDER, argv, covering()) is not None
    assert builder_test_refusal(strict_policy(), Seat.BUILDER, covering_run, covering()) is None


def test_a_uv_command_that_is_not_run_is_not_judged() -> None:
    argv = ("uv", "-q", "pip", "install", "pytest")

    assert builder_test_refusal(strict_policy(), Seat.BUILDER, argv, covering()) is None


def test_the_runner_comes_from_the_configured_test_command() -> None:
    ward = replace(strict_policy(), test_command=("uv", "run", "ward"))

    assert builder_test_refusal(ward, Seat.BUILDER, ("ward",), covering()) is not None
    assert builder_test_refusal(ward, Seat.BUILDER, ("python", "-m", "ward"), covering()) is not None
    assert builder_test_refusal(ward, Seat.BUILDER, ("pytest",), covering()) is None


@pytest.mark.parametrize("argv", RUNNER_SPELLINGS, ids=" ".join)
def test_options_in_the_configured_test_command_are_not_part_of_the_runner(
    argv: tuple[str, ...],
) -> None:
    quiet = replace(strict_policy(), test_command=("uv", "run", "pytest", "-q", "-p", "no:cacheprovider"))

    runner = tuple(part for part in argv if part not in {"tests", "-q"})
    covering_run = (*runner, "tests/unit/test_profile.py")

    assert builder_test_refusal(quiet, Seat.BUILDER, argv, covering()) is not None
    assert builder_test_refusal(quiet, Seat.BUILDER, covering_run, covering()) is None


@pytest.mark.parametrize(
    "argv",
    [("npm", "install"), ("python", "-c", "import pytest"), ("python", "scripts/pytest_report.py")],
)
def test_a_command_that_only_mentions_the_runner_is_not_judged(argv: tuple[str, ...]) -> None:
    npm = replace(strict_policy(), test_command=("npm", "test"))

    assert builder_test_refusal(npm, Seat.BUILDER, argv, covering()) is None
    assert builder_test_refusal(strict_policy(), Seat.BUILDER, argv, covering()) is None


@pytest.mark.parametrize("argv", RUNNER_SPELLINGS, ids=" ".join)
def test_flow_admits_every_runner_spelling(argv: tuple[str, ...]) -> None:
    flow = replace(strict_policy(), mode=ProcessMode.FLOW)

    assert builder_test_refusal(flow, Seat.BUILDER, argv, covering()) is None


# --- bare words after a flag ---------------------------------------------------------------


@pytest.mark.parametrize(
    "tail",
    [("-q", "test_other.py"), ("-x", "-v", "test_other.py"), ("--", "test_other.py"), ("-vv", "conftest.py")],
    ids=" ".join,
)
def test_strict_reads_a_bare_word_after_a_flag_without_a_value_as_a_target(tail: tuple[str, ...]) -> None:
    argv = ("uv", "run", "pytest", "tests/unit/test_profile.py", *tail)

    assert builder_test_refusal(strict_policy(), Seat.BUILDER, argv, covering()) is not None


@pytest.mark.parametrize(
    "tail",
    [
        ("-k", "profile"),
        ("-qk", "profile"),
        ("-m", "slow"),
        ("--maxfail", "1"),
        ("--tb", "short"),
        ("-p", "no:cacheprovider"),
        ("-pno:cacheprovider",),
        ("--tb=short", "-q"),
    ],
    ids=" ".join,
)
def test_strict_reads_a_bare_word_after_a_flag_that_takes_a_value_as_its_value(
    tail: tuple[str, ...],
) -> None:
    argv = ("uv", "run", "pytest", "tests/unit/test_profile.py", *tail)

    assert builder_test_refusal(strict_policy(), Seat.BUILDER, argv, covering()) is None


# --- redirections ----------------------------------------------------------------------------


def guard() -> TestGuard:
    return TestGuard(strict_policy(), covering())


@pytest.mark.parametrize(
    "line",
    [
        'uv run pytest tests/unit/test_profile.py -k ">" tests',
        "uv run pytest tests/unit/test_profile.py -k '<' tests",
        'uv run pytest tests/unit/test_profile.py -k "a>b" tests',
        "uv run pytest tests/unit/test_profile.py -k a\\>b tests",
    ],
)
def test_a_quoted_angle_bracket_is_an_argument_not_a_redirection(line: str) -> None:
    assert shell_command_refusal(guard(), line) is not None


@pytest.mark.parametrize(
    "line",
    [
        "uv run pytest tests/unit/test_profile.py > out.txt 2>&1",
        "uv run pytest tests/unit/test_profile.py >> out.txt",
        "uv run pytest tests/unit/test_profile.py < /dev/null &> log",
        'uv run pytest tests/unit/test_profile.py -k "a>b"',
    ],
)
def test_an_unquoted_angle_bracket_is_a_redirection(line: str) -> None:
    assert shell_command_refusal(guard(), line) is None


@pytest.mark.parametrize(
    "line",
    [
        "echo hi;>x pytest",
        "echo hi;>x uv run pytest tests",
        "true&&>x uv run pytest",
        "echo hi|>x pytest",
        "echo hi&>x;pytest",
        "(>x pytest)",
    ],
)
def test_a_list_operator_joined_to_a_redirection_still_ends_the_command(line: str) -> None:
    assert shell_command_refusal(guard(), line) is not None


def test_shell_commands_split_an_operator_run_that_holds_a_redirection() -> None:
    assert shell_commands("echo hi;>x pytest") == (("echo", "hi"), ("pytest",))
    assert shell_commands("pytest tests 2>&1|tail") == (("pytest", "tests"), ("tail",))


def test_shell_commands_keep_a_quoted_angle_bracket_as_an_argument() -> None:
    assert shell_commands('pytest -k "x>y" tests 2> err | tail') == (
        ("pytest", "-k", "x>y", "tests"),
        ("tail",),
    )


# --- which seat the workflow gives the pre-command check -----------------------------------


def run_item(process: ProcessPolicy):
    ports = make_ports([builder(), review(ReviewDecision.PASS)], [command()])
    work = replace(spec(brief_text=BRIEF), process=process)

    outcome = ItemWorkflow(ports).run(campaign(), work, ports.records.create(campaign()))

    assert outcome.disposition is ItemDisposition.ACCEPTED
    return ports


def test_the_workflow_gives_the_strict_builder_seat_alone_the_test_guard() -> None:
    ports = run_item(strict_policy())
    builder_request, reviewer_request = ports.harness.requests

    assert builder_request.test_guard == TestGuard(strict_policy(), covering())
    assert reviewer_request.test_guard is None
    assert ports.command.requests[0].argv == spec().validator_argv


def test_the_workflow_gives_no_seat_a_test_guard_in_flow() -> None:
    ports = run_item(replace(strict_policy(), mode=ProcessMode.FLOW))

    assert [request.test_guard for request in ports.harness.requests] == [None, None]


def test_seat_test_guard_is_for_the_strict_builder_only() -> None:
    work = replace(spec(brief_text=BRIEF), process=strict_policy())

    assert seat_test_guard(work, Seat.BUILDER) == TestGuard(strict_policy(), covering())
    assert seat_test_guard(work, Seat.REVIEWER) is None
    assert seat_test_guard(work, Seat.SPECIALIST) is None
    assert seat_test_guard(replace(work, process=ProcessPolicy()), Seat.BUILDER) is None
