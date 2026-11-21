from __future__ import annotations

from pathlib import Path

from autobuild.application.prompts import render_seat_instructions
from autobuild.domain import ItemExecutionSpec, Seat, ToolPolicy, WorkItem
from autobuild.domain.models import ReviewBoundary

_CODE_COMMENT_SENTENCE = (
    "Only write a code comment to state a constraint the code itself cannot show, "
    "never to say where it came from, what the next line does, or which item changed "
    "it; such comments speak to the reviewer, not the next reader."
)


def _spec() -> ItemExecutionSpec:
    return ItemExecutionSpec(
        item=WorkItem(
            item_id="item-1",
            title="test item",
            brief_ref="plan",
            acceptance=("passes",),
        ),
        brief_path=Path("brief.md"),
        validator_id="validator",
        validator_argv=("python", "-m", "pytest"),
        tool_policy=ToolPolicy(
            allowed_tools=frozenset({"python"}),
            allowed_roots=(Path("/worktree"),),
        ),
        builder_model_class="builder-class",
        reviewer_model_class="reviewer-class",
        specialist_model_class="specialist-class",
    )


def test_builder_instructions_carry_the_code_comment_sentence() -> None:
    instructions = render_seat_instructions(_spec(), Seat.BUILDER, ())

    assert _CODE_COMMENT_SENTENCE in instructions


def test_reviewer_instructions_omit_the_code_comment_sentence() -> None:
    instructions = render_seat_instructions(_spec(), Seat.REVIEWER, ())

    assert _CODE_COMMENT_SENTENCE not in instructions


def test_specialist_instructions_omit_the_code_comment_sentence() -> None:
    instructions = render_seat_instructions(_spec(), Seat.SPECIALIST, ())

    assert _CODE_COMMENT_SENTENCE not in instructions


_REPORT_SENTENCE = (
    "The builder report is the builder's own claim about its work, including any runs it "
    "says it saw fail; check each claim against the diff and the validator output, and "
    "record a claim you cannot confirm as a non-blocking finding."
)
_NO_REPORT_SENTENCE = (
    "No builder report was recorded for this candidate; mark any clause that rests on the "
    "builder's own runs as not checked."
)
_BUILDER_SENTENCE = (
    "Your summary is handed to the reviewer: quote each run the brief asks to be seen "
    "failing, before the change or under a named mutant, with its command and result."
)
_TRANSCRIPT_SENTENCE = "Do not ask for or use the builder transcript"


def _review_instructions(report: str | None) -> list[str]:
    boundary = ReviewBoundary("sign-in", ("src/auth/**",), "docs/invariants/sign-in.md")
    refs = ("patch:0", "stdout:1") + ((report,) if report else ())
    return [
        render_seat_instructions(_spec(), Seat.REVIEWER, refs, builder_report_ref=report),
        render_seat_instructions(
            _spec(), Seat.SPECIALIST, refs, boundary=boundary, builder_report_ref=report
        ),
    ]


def test_review_instructions_name_the_builder_report_as_a_claim_to_check() -> None:
    for instructions in _review_instructions("evidence:item-1-builder-report-1"):
        assert _REPORT_SENTENCE in instructions
        assert _TRANSCRIPT_SENTENCE in instructions
        assert _NO_REPORT_SENTENCE not in instructions


def test_review_instructions_without_a_builder_report_say_so() -> None:
    for instructions in _review_instructions(None):
        assert _NO_REPORT_SENTENCE in instructions
        assert _REPORT_SENTENCE not in instructions
        assert _TRANSCRIPT_SENTENCE in instructions


def test_builder_instructions_ask_for_the_failing_runs_in_the_summary() -> None:
    assert _BUILDER_SENTENCE in render_seat_instructions(_spec(), Seat.BUILDER, ())
