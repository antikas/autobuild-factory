"""The git staging reader against neutral copies of delivered and refused briefs.

Each excerpt holds every line of a cited brief that contains a git invocation,
with repository-specific identifiers replaced.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from autobuild.enforcement import git_staging_lines

# The corrected brief of item A, which delivered.
EXCERPT_A = (
    "- Reproduced on current main (`1a2b3c4d`) in a scratch worktree: `git apply -3` of "
    "`5e6f7a8b` applies cleanly (`AGENTS.md` needs the three-way merge; every other file "
    "applies directly), and `pnpm run type-check:e2e` then fails with exactly the two "
    'TS7016 lines. Adding `"allowJs": true` to `e2e/fixtures/app/tsconfig.json` alone '
    "makes it exit 0.\n"
    "- Carry: the first build's work is sound apart from this one setting. The builder "
    "starts from `docs/campaigns/example/carry/item-a-carry.patch` (the `5e6f7a8b` commit; "
    "apply with `git apply -3`, then `git reset -q` so nothing stays staged: AutoBuild "
    "stages at delivery), then makes the fix below.\n"
)

CARRY_LINE = (
    "- Everything else in the first build passed: type-check, every scan and check step, "
    "and every other test, including the three dashboard test files it changed or added. "
    "Its full work is carried: `docs/campaigns/example/carry/item-b-carry.patch` (21 "
    "files). It applies cleanly to main `1a2b3c4d` (`git apply --index`), and "
    "`src/components/dashboard/__tests__/` then passes (4 files, 27 tests).\n"
)

# The corrected brief of item B, which delivered.
EXCERPT_B = CARRY_LINE + (
    "Second build (campaign `run-2`): the builder took 9.2 min, validation passed, the "
    "reviewer passed it with 5 findings, then delivery parked it: AutoBuild's "
    "`git add -A -- <changed paths>` failed with `fatal: pathspec "
    "'src/components/legacy/OldCard.css' did not match any files`. Cause: "
    "the deletions were staged (the patch applied with `git apply --index`, and Scope 7 "
    "said `git rm`), so the deleted paths were in neither the index nor the working tree. "
    "Coordinator decision: the next build starts from that reviewed work, "
    "`docs/campaigns/example/carry/item-b-carry-2.patch` (11 deletions, 3 new files), "
    "which supersedes `item-b-carry.patch`. Apply it with plain `git apply <patch>`, never "
    "`--index` or `-3`. Delete files only with plain `rm`, never `git rm`, and stage "
    "nothing: AutoBuild stages and commits at delivery. Before finishing, "
    "`git -C <worktree> diff --cached --name-only` prints nothing.\n"
)

# The brief of item B before correction, which caused the delivery failure.
EXCERPT_C = CARRY_LINE + (
    "7. Remove with `git rm` the forked tracker, its local re-export and the "
    "`components/legacy/*` duplicates once nothing imports them, or name in the "
    "work record the importer that keeps each one [R-3].\n"
)

# Lines of earlier briefs that order a removal in the present-tense passive.
PASSIVE_REMOVALS = (
    "7. The forked tracker, its local re-export and the "
    "`components/legacy/*` duplicates are removed with `git rm` once nothing "
    "imports them, or the work record names the importer that keeps each one [R-3].",
    "1. `e2e/tests/**`, `e2e/pages/**`, `e2e/fixtures/data.ts` and `playwright.config.ts` "
    "are removed with `git rm`, after a search confirms nothing else imports them.",
)

# The carry line of item A before correction, without the reset.
UNCORRECTED_A_CARRY = (
    "- Carry: the first build's work is sound apart from this one setting. The builder "
    "starts from `docs/campaigns/example/carry/item-a-carry.patch` (the `5e6f7a8b` commit; "
    "apply with `git apply -3`), then makes the fix below.\n"
)


def test_the_corrected_briefs_pass_and_the_uncorrected_instruction_is_refused() -> None:
    assert git_staging_lines(EXCERPT_A) == ()
    assert git_staging_lines(EXCERPT_B) == ()
    (refused,) = git_staging_lines(EXCERPT_C)
    assert refused.line_number == 2
    assert refused.invocations == ("git rm",)


def test_the_uncorrected_carry_line_is_refused_for_its_three_way_apply() -> None:
    (refused,) = git_staging_lines(UNCORRECTED_A_CARRY)
    assert refused.line_number == 1
    assert refused.invocations == ("git apply -3",)


def test_a_removal_ordered_in_the_present_tense_passive_is_refused() -> None:
    for line in PASSIVE_REMOVALS:
        (refused,) = git_staging_lines(line)
        assert refused.invocations == ("git rm",), line


def test_every_item_brief_passes_the_reader() -> None:
    root = Path(__file__).resolve().parents[2]
    if not (root / "docs" / "items").is_dir():
        pytest.skip("item briefs are private and absent from the public tree")
    briefs = sorted((root / "docs" / "items").glob("*.md"))
    assert root / "docs" / "items" / "brief-git-check-passive-orders.md" in briefs
    for brief in briefs:
        assert git_staging_lines(brief.read_text(encoding="utf-8")) == (), brief.name
