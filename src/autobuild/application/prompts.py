"""One harness-neutral instruction renderer for every execution seat."""

from __future__ import annotations

from pathlib import Path

from autobuild.domain import ItemExecutionSpec, Seat


def render_seat_instructions(
    spec: ItemExecutionSpec,
    seat: Seat,
    context_refs: tuple[str, ...],
    brief_path: Path | None = None,
) -> str:
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
            "follow established patterns, run focused checks where useful, and leave all product "
            "changes uncommitted for deterministic validation and review. If this is a correction "
            "round, address only the concrete findings in the supplied evidence."
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
