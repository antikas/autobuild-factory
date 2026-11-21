"""A park whose delivery from the worktree fails, against real Git, real Pinax
and a bare origin.

The item's worktree holds a staged deletion, so the integration merge of the
moved ``origin/main`` refuses. The park is then recorded from the primary
checkout with git's own message, the worktree is kept, and nothing from the
worktree reaches origin."""

from __future__ import annotations

import json
import sys
import threading
from dataclasses import replace
from pathlib import Path

import pytest
from test_serial_delivery import (
    ValidatorRuns,
    _seat,
    git,
    identity,
    item_spec,
    pinax_origin,
)

from autobuild.adapters import GitWorkspaceAdapter, LocalRunRecordAdapter, PinaxTrackerAdapter
from autobuild.application import ItemWorkflow, WorkflowPorts
from autobuild.domain import (
    BuilderReport,
    CampaignRef,
    ItemDisposition,
    ReviewDecision,
    ReviewFinding,
    ReviewVerdict,
    Seat,
    SeatRequest,
)
from autobuild.testing import FakeHarnessAdapter, FakeKnowledgeAdapter

pytestmark = pytest.mark.usefixtures("self_committing_pinax")


def _events(record_root: Path) -> list[dict]:
    lines = (record_root / "events.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _origin_log_events(repo: Path, head: str) -> list[dict]:
    paths = git(repo, "ls-tree", "-r", "--name-only", head, "--", ".ergon/log").split()
    entries: list[dict] = []
    for path in paths:
        if path.endswith(".jsonl"):
            text = git(repo, "show", f"{head}:{path}")
            entries.extend(json.loads(line) for line in text.splitlines() if line.strip())
    return entries


def test_a_park_whose_integration_merge_refuses_is_parked_from_the_primary(
    tmp_path: Path,
) -> None:
    origin = pinax_origin(tmp_path, count=1)
    item_id = origin.items[0]
    lock = threading.Lock()
    tracker = PinaxTrackerAdapter(origin.repo, primary_lock=lock)
    workspace = GitWorkspaceAdapter(
        tmp_path / "scratch", tracker_commits_itself=True, primary_lock=lock, projection=tracker
    )
    staged_at_park: list[str] = []
    worktrees: list[Path] = []

    def on_invoke(request: SeatRequest) -> None:
        root = request.workspace.root
        if request.seat is Seat.BUILDER:
            worktrees.append(root)
            (root / "feature.txt").write_text(f"feature for {item_id}\n", encoding="utf-8")
            git(root, "rm", "--quiet", "lib.txt")
        elif request.seat is Seat.REVIEWER:
            origin.push_change("upstream", "upstream.txt", "unrelated\n")
            staged_at_park.extend(git(root, "diff", "--cached", "--name-only").split())

    harness = FakeHarnessAdapter(
        identity("harness"),
        scripted_results=[
            _seat("builder", BuilderReport("report:builder", "built")),
            _seat(
                "reviewer",
                ReviewVerdict(
                    item_id,
                    ReviewDecision.PARK,
                    (ReviewFinding("scope", "needs a decision", "evidence:review", True),),
                    "verdict",
                ),
            ),
        ],
        on_invoke=on_invoke,
    )
    records = LocalRunRecordAdapter(tmp_path / "runs")
    ports = WorkflowPorts(
        tracker,
        workspace,
        harness,
        ValidatorRuns(),
        records,
        FakeKnowledgeAdapter(identity("knowledge")),
    )
    campaign = CampaignRef("park-fallback", origin.repo)
    spec = replace(item_spec(origin, item_id), validator_argv=(sys.executable, "-c", "pass"))
    record = records.create(campaign)

    outcome = ItemWorkflow(ports).run(campaign, spec, record)

    assert "lib.txt" in staged_at_park
    assert outcome.disposition is ItemDisposition.PARKED, outcome.reason
    item_events = [event for event in _events(record.root) if event.get("item_id") == item_id]
    kinds = [event["event_type"] for event in item_events]
    assert "item.park_failed" not in kinds
    failed = next(event for event in item_events if event["event_type"] == "item.park_delivery_failed")
    assert "integration merge of" in failed["payload"]["error"]
    assert "lib.txt" in failed["payload"]["error"]
    kept = Path(failed["payload"]["worktree_root"])
    assert kept == worktrees[0]
    assert kept.exists()

    head = origin.head()
    page = git(origin.repo, "show", f"{head}:.ergon/items/{item_id}.md")
    assert "status: parked" in page
    parked = [
        entry
        for entry in _origin_log_events(origin.repo, head)
        if entry.get("type") == "item.parked" and entry["payload"].get("item_id") == item_id
    ]
    assert len(parked) == 1
    reason = parked[0]["payload"]["reason"]
    assert "review disposition was park" in reason
    assert "lib.txt" in reason

    marker = records.latest_phase_marker(item_id)
    assert marker is not None and marker.state == "parked"
    later = CampaignRef("later", origin.repo)
    assert item_id not in {item.item_id for item in tracker.resumable_claims(later)}
    tree = git(origin.repo, "ls-tree", "-r", "--name-only", head).split()
    assert "feature.txt" not in tree
    assert "lib.txt" in tree
