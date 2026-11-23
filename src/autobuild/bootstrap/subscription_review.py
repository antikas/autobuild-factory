"""Independent, read-only subscription review of one immutable revision."""

from __future__ import annotations

import json
from dataclasses import asdict
import re
import subprocess
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from autobuild.application.subscriptions import portable_text
from autobuild.bootstrap.composition import subscription_context
from autobuild.bootstrap.environment import configure_scratch_environment
from autobuild.domain import (
    ReviewVerdict,
    Seat,
    SeatOutcome,
    SeatRequest,
    ToolPolicy,
    WorkspaceRef,
)

_FULL_SHA = re.compile(r"[0-9a-f]{40}")
_CITATION = re.compile(r"(?P<file>[^:\\;\x00-\x1f]+)(?::[1-9][0-9]*(?:-[1-9][0-9]*)?)?")
_NO_CITATION = "Local diagnostic withheld; no verified repository-relative evidence reference."


class ReviewError(ValueError):
    """An actionable review failure whose message is safe to display."""


@contextmanager
def _checkout(repository: Path, commit: str, scratch: Path):
    """Make a detached read-only review tree at exactly a full commit SHA."""

    if not _FULL_SHA.fullmatch(commit):
        raise ReviewError("review commit must be a full 40-character SHA")
    resolved = subprocess.run(
        ("git", "-C", str(repository), "rev-parse", "--verify", f"{commit}^{{commit}}"),
        capture_output=True,
        text=True,
        check=False,
    )
    if resolved.returncode or resolved.stdout.strip() != commit:
        raise ReviewError("review commit is not an available full commit SHA")
    root = scratch / "subscription-reviews" / "worktrees" / uuid4().hex
    root.parent.mkdir(parents=True, exist_ok=True)
    added = subprocess.run(
        (
            "git",
            "-C",
            str(repository),
            "worktree",
            "add",
            "--detach",
            str(root),
            commit,
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    if added.returncode:
        raise ReviewError("could not create isolated review checkout")
    try:
        yield root
    finally:
        subprocess.run(
            ("git", "-C", str(repository), "worktree", "remove", "--force", str(root)),
            capture_output=True,
            text=True,
            check=False,
        )


def _safe_evidence_ref(value: str, repository: Path) -> str:
    """Retain only complete file citations in the tree that was reviewed."""
    verified = []
    for part in value.split(";"):
        citation = part.strip()
        match = _CITATION.fullmatch(citation)
        if match is None:
            continue
        path = Path(match["file"])
        if path.is_absolute() or ".." in path.parts:
            continue
        try:
            target = (repository / path).resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            continue
        if target.is_relative_to(repository) and target.is_file():
            verified.append(citation)
    return "; ".join(verified) or _NO_CITATION


def _store_receipt(scratch: Path, receipt: dict[str, object], repository: Path) -> dict[str, object]:
    """Keep actionable detail locally and return stable public coordinates."""
    receipt_id = uuid4().hex
    repository = repository.resolve()
    findings = []
    for finding in receipt["findings"]:
        findings.append({
            "code": portable_text(finding["code"]),
            "consequence": portable_text(finding["consequence"]),
            "evidence_ref": _safe_evidence_ref(finding["evidence_ref"], repository),
            "blocking": finding["blocking"],
            "specialist_boundary": portable_text(finding.get("specialist_boundary")),
        })
    published = {**receipt, "findings": findings, "receipt_id": receipt_id}
    receipts = scratch / "subscription-reviews" / "receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    (receipts / f"{receipt_id}.json").write_text(
        json.dumps(published, sort_keys=True) + "\n", encoding="utf-8"
    )
    return {
        "commit": str(receipt["commit"]),
        "verdict": str(receipt["verdict"]),
        "lane": str(receipt["lane"]),
        "model": str(receipt.get("model", "")),
        "receipt_id": receipt_id,
    }


def subscription_review(
    settings, commit: str, brief: Path, timeout_seconds: float = 900.0
) -> dict[str, object]:
    """Reserve one reviewer subscription lane and return its typed verdict."""

    brief = brief.resolve()
    if not brief.is_file():
        raise ReviewError("review brief must be a file")
    if timeout_seconds <= 0:
        raise ReviewError("review timeout must be positive")
    routing, _manifest = subscription_context(settings)
    scratch = configure_scratch_environment(settings.scratch_root)
    with _checkout(settings.repository, commit, scratch) as root:
        delta = subprocess.check_output(
            ("git", "-C", str(root), "show", "--format=", "--first-parent", "--no-ext-diff", "--no-textconv", commit),
            stderr=subprocess.PIPE,
        )
        if len(delta) > 1_048_576:
            raise ReviewError("review patch exceeds the 1 MiB evidence limit")
        with routing.reserve(Seat.REVIEWER.value, timeout_seconds) as lane:
            if lane is None:
                raise ReviewError("no subscription reviewer lane is available")
            profile_lane = next(entry for entry in settings.lanes if entry.name == lane.name)
            request = SeatRequest(
                run_id=f"subscription-review-{uuid4().hex[:12]}",
                item_id="independent-review",
                seat=Seat.REVIEWER,
                model_class="reviewer",
                brief_path=brief,
                workspace=WorkspaceRef(root, "detached", commit, "review"),
                tool_policy=ToolPolicy(frozenset(), (root,)),
                instructions=(
                    "Review the following immutable implementation diff against the approved brief. "
                    "The coordinator owns test execution. Inspect the changes, cite their source lines, "
                    "and return the typed verdict. Base every finding on the supplied evidence. "
                    "Prior reviews, conversations, logs, and unrelated files are outside this review.\n\n"
                    + delta.decode("utf-8", errors="replace")
                ),
                result_contract="review-verdict-v1",
                timeout_seconds=timeout_seconds,
                effort=profile_lane.reviewer_effort,
            )
            result = lane.harness.invoke(request)
        if result.outcome is not SeatOutcome.SUCCEEDED or not isinstance(
            result.payload, ReviewVerdict
        ):
            raise ReviewError(
                "independent subscription review did not produce a typed verdict"
            )
        return _store_receipt(scratch, {
            "when": datetime.now(UTC).isoformat(),
            "lane": lane.name,
            "model": result.model or lane.models.get("reviewer", ""),
            "verdict": result.payload.decision.value,
            "commit": commit,
            "effort": request.effort.value if request.effort else None,
            "findings": [asdict(finding) for finding in result.payload.findings],
        }, root)
