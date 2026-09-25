from __future__ import annotations

from pathlib import Path

from autobuild.application.prompts import render_seat_instructions
from autobuild.domain import ItemExecutionSpec, Seat, ToolPolicy, WorkItem

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
