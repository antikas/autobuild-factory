"""One harness-neutral instruction renderer for every execution seat."""

from __future__ import annotations

from pathlib import Path

from autobuild.domain import ItemExecutionSpec, Seat
from autobuild.domain.models import ReviewBoundary
from autobuild.enforcement.review_depth import review_checklist
from autobuild.enforcement.test_scope import covering_label, item_covering_entries

_DEPTH_FIELDS = (
    '"inspected":[{"path":"...","lines":"1-120,300-340|all"}],'
    '"acceptance":[{"clause":"A1","outcome":"met|unmet|not_checked","evidence_ref":"..."}]'
)


def review_checklist_numbers(spec: ItemExecutionSpec) -> tuple[str, ...]:
    """The clause numbers a reviewer or boundary specialist of ``spec`` is given."""

    return tuple(number for number, _ in review_checklist(spec.brief_text, spec.item.acceptance))


def _checklist_section(spec: ItemExecutionSpec) -> str:
    checklist = review_checklist(spec.brief_text, spec.item.acceptance)
    listed = "\n".join(f"{number}. {clause}" for number, clause in checklist) or "- None"
    return (
        f"\nAcceptance checklist:\n{listed}\n"
        "Read every changed file in the diff, new tests included, not only the summary in "
        "this prompt. Judge each numbered clause against the diff and the validator output, "
        "and report it in acceptance as met, unmet or not_checked with its number. A clause "
        "the change does not deliver is a blocking finding, and a pass verdict must not "
        "report any clause unmet. Report in inspected every path you read and the line "
        "ranges you read."
    )


def _strict_test_constraint(spec: ItemExecutionSpec) -> str:
    entries = item_covering_entries(spec.process, spec.brief_text)
    if entries:
        named = f"The covering tests for this item's declared paths are: {covering_label(entries)}."
    else:
        mapped = "; ".join(
            f"{entry.pattern} is covered by {entry.label()}" for entry in spec.process.test_map
        )
        named = (
            "This item's declared paths match no test-map entry. Run only the mapped tests "
            f"for the files you change: {mapped}."
        )
    return (
        "Process (strict): run only the tests that cover what you changed. "
        f"{named} A whole-suite run is not allowed."
    )


BUILDER_REPORT_SENTENCE = (
    "The builder report is the builder's own claim about its work, including any runs it "
    "says it saw fail; check each claim against the diff and the validator output, and "
    "record a claim you cannot confirm as a non-blocking finding."
)
NO_BUILDER_REPORT_SENTENCE = (
    "No builder report was recorded for this candidate; mark any clause that rests on the "
    "builder's own runs as not checked."
)
BUILDER_SUMMARY_SENTENCE = (
    "Your summary is handed to the reviewer: quote each run the brief asks to be seen "
    "failing, before the change or under a named mutant, with its command and result."
)


def _builder_report_section(builder_report_ref: str | None) -> str:
    if builder_report_ref:
        return f" The builder report is {builder_report_ref}. {BUILDER_REPORT_SENTENCE}"
    return f" {NO_BUILDER_REPORT_SENTENCE}"


def render_seat_instructions(
    spec: ItemExecutionSpec,
    seat: Seat,
    context_refs: tuple[str, ...],
    brief_path: Path | None = None,
    continuation: bool = False,
    boundary: ReviewBoundary | None = None,
    builder_report_ref: str | None = None,
) -> str:
    """The instructions for ``seat``. A specialist given ``boundary`` reviews
    the change for that boundary, with its name and invariants file; a
    specialist without one adjudicates an escalated finding. A reviewer or
    boundary specialist is told whether ``builder_report_ref`` names the
    builder's report for the candidate."""

    acceptance = "\n".join(f"- {criterion}" for criterion in spec.item.acceptance)
    context = "\n".join(f"- {reference}" for reference in context_refs) or "- None"
    common = (
        f"Tracked item: {spec.item.item_id} — {spec.item.title}\n"
        f"Approved brief: {brief_path or spec.brief_path}\n"
        f"Workspace: use only the supplied isolated workspace.\n"
        f"Acceptance criteria:\n{acceptance}\n"
        f"Evidence references supplied to this seat:\n{context}\n"
    )
    if seat is Seat.BUILDER:
        role = (
            "Build the approved item completely. Inspect the repository before changing it, "
            "follow established patterns, and leave all product "
            "changes uncommitted for deterministic validation and review. If this is a correction "
            "round, address only the concrete findings in the supplied evidence. Only write a code "
            "comment to state a constraint the code itself cannot show, never to say where it came "
            "from, what the next line does, or which item changed it; such comments speak to the "
            "reviewer, not the next reader.\n"
            "Constraint (focused tests): run only the tests that cover the files you changed. "
            "Never run the repository's full test suite or the validator command; the validator "
            "runs after you finish and is the acceptance authority."
        )
        if spec.process.strict:
            role += "\n" + _strict_test_constraint(spec)
        if continuation:
            role += (
                "\nContinuation: A previous builder seat timed out in this workspace before it "
                "finished. Its partial work is present in the workspace as uncommitted changes. "
                "Inspect it first. Complete that partial work; do not restart the item, discard "
                "the changes or rebuild what is already done."
            )
        contract = (
            'Return only JSON matching builder-report-v1: '
            '{"summary":"what changed and why","report_ref":"durable evidence reference"}. '
            f"{BUILDER_SUMMARY_SENTENCE}"
        )
    elif seat is Seat.REVIEWER:
        role = (
            "Review the actual brief, diff and validator evidence from a fresh context. Do not ask "
            "for or use the builder transcript."
            + _builder_report_section(builder_report_ref)
            + " Judge intent, correctness, maintainability, "
            "security and scope. Mark a finding blocking only for a concrete consequence you would "
            "not merge under your own name; record everything else as a non-blocking finding with "
            "decision pass. Return correct or escalate only with a blocking finding."
        ) + _checklist_section(spec)
        contract = (
            'Return only JSON matching review-verdict-v1: '
            '{"decision":"pass|correct|escalate|park","findings":['
            '{"code":"...","consequence":"...","evidence_ref":"...",'
            '"blocking":true,"specialist_boundary":null}],"evidence_ref":"...",'
            f"{_DEPTH_FIELDS}}}."
        )
    elif boundary is not None:
        invariants = (
            f"Read the invariants file {boundary.invariants} first and judge the change against "
            "each invariant it lists."
            if boundary.invariants
            else "No invariants file is named for this boundary; judge the change against what "
            "a change to this area must preserve."
        )
        role = (
            f"Specialist review for the {boundary.name} boundary. This change touches paths in "
            f"that boundary ({', '.join(boundary.patterns)}). Review the actual brief, diff and "
            "validator evidence from a fresh context, for this boundary only; a blind reviewer "
            "covers the rest. Do not ask for or use the builder transcript."
            + _builder_report_section(builder_report_ref)
            + f" {invariants} Mark a finding blocking only for a concrete "
            "consequence you would not merge under your own name; record everything else as a "
            "non-blocking finding with decision pass. Return correct or escalate only with a "
            "blocking finding."
        ) + _checklist_section(spec)
        contract = (
            'Return only JSON matching review-verdict-v1: '
            '{"decision":"pass|correct|escalate|park","findings":['
            '{"code":"...","consequence":"...","evidence_ref":"...",'
            f'"blocking":true,"specialist_boundary":"{boundary.name}"}}],"evidence_ref":"...",'
            f"{_DEPTH_FIELDS}}}."
        )
    else:
        role = (
            "Adjudicate only the specialist boundary named in the supplied finding. Preserve the "
            "same acceptance criteria and return a decisive disposition with concrete evidence. "
            "Mark a finding blocking only for a concrete consequence you would not merge under your "
            "own name; record everything else as a non-blocking finding with decision pass. Return "
            "correct or escalate only with a blocking finding. Return the inspected and "
            "acceptance arrays too; they are usually empty for an adjudication, which is "
            "given no clause list."
        )
        contract = (
            'Return only JSON matching review-verdict-v1: '
            '{"decision":"pass|correct|escalate|park","findings":['
            '{"code":"...","consequence":"...","evidence_ref":"...",'
            '"blocking":true,"specialist_boundary":null}],"evidence_ref":"...",'
            f"{_DEPTH_FIELDS}}}."
        )
    return f"{common}\nYour seat:\n{role}\n\nResult contract:\n{contract}\n"
