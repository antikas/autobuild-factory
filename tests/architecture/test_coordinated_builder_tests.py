from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills" / "autobuild-coordinated"

BUILDER_VALIDATOR_PHRASES = (
    "scoped validator",
    "builder's validator run",
    "Treat its validator run",
    "builder's own validator run",
    "<validator runs>",
    "VALIDATOR command",
)

TESTS_RULING_PREFIX = (
    "TESTS (owner ruling, repeated): run only the test files that cover the files you changed"
)


def test_no_coordinated_skill_file_tells_a_builder_to_run_the_validator() -> None:
    offenders = []
    for path in sorted(SKILL.rglob("*")):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for phrase in BUILDER_VALIDATOR_PHRASES:
            if phrase in text:
                offenders.append(f"{path.relative_to(ROOT).as_posix()}: {phrase}")
    assert offenders == []


def test_builder_brief_carries_the_focused_tests_ruling() -> None:
    brief = (SKILL / "templates" / "builder-brief.md").read_text(encoding="utf-8")
    assert any(line.startswith(TESTS_RULING_PREFIX) for line in brief.splitlines())
