from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = ROOT / "skills" / "autobuild-coordinated"


def read(relative: str) -> str:
    return (SKILL_ROOT / relative).read_text(encoding="utf-8")


def test_skill_names_the_lanes_and_the_frozen_queue_without_the_retired_rules() -> None:
    skill = read("SKILL.md")

    assert "goes to the owner" not in skill
    assert re.search(r"^#{1,6} Fix-forwards\s*$", skill, re.MULTILINE) is None
    for lane in ("Direct", "Small", "Material"):
        assert lane in skill
    assert "frozen at launch" in skill


def test_skill_states_the_reviewer_floor_and_the_round_limit() -> None:
    skill = read("SKILL.md")

    assert "one specialist per boundary" in skill
    assert "one fix round" in skill
    assert "final correction round" in skill


def test_reviewer_brief_carries_the_focus_line_and_the_scope_list() -> None:
    brief = read("templates/reviewer-brief.md")

    assert "FOCUS:" in brief
    assert "SCOPE" in brief


def test_rules_register_non_blocking_findings_and_park_on_red_validation() -> None:
    rules = read("rules.md")

    assert "gate `scope`" in rules
    assert "red validation parks the item" in rules
