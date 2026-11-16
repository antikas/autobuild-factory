"""One harness-neutral instruction renderer for every execution seat."""

from __future__ import annotations

from pathlib import Path

from autobuild.domain import ItemExecutionSpec, Seat
from autobuild.domain.models import ReviewBoundary
from autobuild.enforcement.test_scope import covering_label, item_covering_entries


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


def render_seat_instructions(
    spec: ItemExecutionSpec,
    seat: Seat,
    context_refs: tuple[str, ...],
    brief_path: Path | None = None,
    continuation: bool = False,
    boundary: ReviewBoundary | None = None,
) -> str:
    """The instructions for ``seat``. A specialist given ``boundary`` reviews
    the change for that boundary, with its name and invariants file; a
    specialist without one adjudicates an escalated finding."""

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
            '{"summary":"what changed and why","report_ref":"durable evidence reference"}.'
        )
    elif seat is Seat.REVIEWER:
        role = (
            "Review the actual brief, diff and validator evidence from a fresh context. Do not ask "
            "for or use the builder transcript. Judge intent, correctness, maintainability, "
            "security and scope. Mark a finding blocking only for a concrete consequence you would "
            "not merge under your own name; record everything else as a non-blocking finding with "
            "decision pass. Return correct or escalate only with a blocking finding."
        )
        contract = (
            'Return only JSON matching review-verdict-v1: '
            '{"decision":"pass|correct|escalate|park","findings":['
            '{"code":"...","consequence":"...","evidence_ref":"...",'
            '"blocking":true,"specialist_boundary":null}],"evidence_ref":"..."}.'
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
            f"covers the rest. {invariants} Mark a finding blocking only for a concrete "
            "consequence you would not merge under your own name; record everything else as a "
            "non-blocking finding with decision pass. Return correct or escalate only with a "
            "blocking finding."
        )
        contract = (
            'Return only JSON matching review-verdict-v1: '
            '{"decision":"pass|correct|escalate|park","findings":['
            '{"code":"...","consequence":"...","evidence_ref":"...",'
            f'"blocking":true,"specialist_boundary":"{boundary.name}"}}],"evidence_ref":"..."}}.'
        )
    else:
        role = (
            "Adjudicate only the specialist boundary named in the supplied finding. Preserve the "
            "same acceptance criteria and return a decisive disposition with concrete evidence. "
            "Mark a finding blocking only for a concrete consequence you would not merge under your "
            "own name; record everything else as a non-blocking finding with decision pass. Return "
            "correct or escalate only with a blocking finding."
        )
        contract = (
            'Return only JSON matching review-verdict-v1: '
            '{"decision":"pass|correct|escalate|park","findings":['
            '{"code":"...","consequence":"...","evidence_ref":"...",'
            '"blocking":true,"specialist_boundary":null}],"evidence_ref":"..."}.'
        )
    return f"{common}\nYour seat:\n{role}\n\nResult contract:\n{contract}\n"
