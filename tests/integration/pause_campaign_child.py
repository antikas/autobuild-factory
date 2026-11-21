"""Run one campaign in its own process for the pause test.

The run record and ``progress.log`` are real files under the given runs root;
every other port is a fake. With ``--release`` the first builder seat blocks
until that file exists, so the parent can pause the run while an item is in
flight. The outcome is written as JSON to ``--outcome``."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from autobuild.adapters import FileProgressAdapter, LocalRunRecordAdapter  # noqa: E402
from autobuild.application import CampaignRunner, WorkflowPorts  # noqa: E402
from autobuild.domain import (  # noqa: E402
    AdapterIdentity,
    BuilderReport,
    CampaignRef,
    CommandResult,
    DeliveryMode,
    DiffEvidence,
    ItemExecutionSpec,
    ReviewDecision,
    ReviewVerdict,
    Seat,
    SeatOutcome,
    SeatResult,
    SeatUsage,
    ToolPolicy,
    WorkItem,
    WorkspaceRef,
)
from autobuild.testing import (  # noqa: E402
    FakeCommandAdapter,
    FakeHarnessAdapter,
    FakeKnowledgeAdapter,
    FakeTrackerAdapter,
    FakeWorkspaceAdapter,
)

RELEASE_DEADLINE = 60.0


def _identity(name: str) -> AdapterIdentity:
    return AdapterIdentity(name, "1", frozenset({"test"}))


def _workspace(item_id: str) -> WorkspaceRef:
    return WorkspaceRef(Path(f"/worktrees/{item_id}"), f"autobuild/{item_id}", "base", f"lease-{item_id}")


def _results(item_id: str) -> list[SeatResult]:
    return [
        SeatResult(
            f"{item_id}:build", SeatOutcome.SUCCEEDED, BuilderReport(f"report:{item_id}", "built"),
            f"raw:{item_id}:build", SeatUsage(source="fake"), "start", "end",
        ),
        SeatResult(
            f"{item_id}:review", SeatOutcome.SUCCEEDED,
            ReviewVerdict(item_id, ReviewDecision.PASS, (), f"verdict:{item_id}"),
            f"raw:{item_id}:review", SeatUsage(source="fake"), "start", "end",
        ),
    ]


def _spec(item: WorkItem) -> ItemExecutionSpec:
    return ItemExecutionSpec(
        item,
        Path("brief.md"),
        "validator",
        ("python", "-m", "pytest"),
        ToolPolicy(frozenset({"python"}), (Path("/worktree"),)),
        "builder-class",
        "reviewer-class",
        "specialist-class",
        delivery_mode=DeliveryMode.PROTECTED_DEFAULT,
        delivery_target_branch="main",
        delivery_target_revision="base",
    )


def _block_first_builder(release: Path):
    seen = {"builders": 0}

    def hook(request) -> None:
        if request.seat is not Seat.BUILDER:
            return
        seen["builders"] += 1
        if seen["builders"] != 1:
            return
        deadline = time.monotonic() + RELEASE_DEADLINE
        while not release.exists():
            if time.monotonic() > deadline:
                raise RuntimeError("the release file never appeared")
            time.sleep(0.02)

    return hook


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs-root", type=Path, required=True)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--outcome", type=Path, required=True)
    parser.add_argument("--release", type=Path)
    parser.add_argument("--item", action="append", required=True)
    args = parser.parse_args()

    items = tuple(args.item)
    tracker = FakeTrackerAdapter(_identity("tracker"))
    tracker.queue.extend(WorkItem(item_id, f"title {item_id}", "plan", ("passes",)) for item_id in items)
    workspace = FakeWorkspaceAdapter(
        _identity("workspace"),
        workspaces_by_item={item_id: _workspace(item_id) for item_id in items},
        diffs_by_item={
            item_id: [DiffEvidence(_workspace(item_id), f"revision:{item_id}", (), f"patch:{item_id}")]
            for item_id in items
        },
        on_deliver=lambda item_id: tracker.mark_done(item_id) if item_id else None,
    )
    harness = FakeHarnessAdapter(
        _identity("harness"),
        results_by_item={item_id: _results(item_id) for item_id in items},
        on_invoke=_block_first_builder(args.release) if args.release else None,
    )
    commands = FakeCommandAdapter(
        _identity("command"),
        results_by_item={
            item_id: [CommandResult(f"command:{item_id}", 0, "out", "err", "start", "end")]
            for item_id in items
        },
    )
    ports = WorkflowPorts(
        tracker,
        workspace,
        harness,
        commands,
        LocalRunRecordAdapter(args.runs_root),
        FakeKnowledgeAdapter(_identity("knowledge")),
        progress=FileProgressAdapter(),
    )
    ref = CampaignRef("campaign", args.repository, max_items=len(items), concurrency=1)
    outcome = CampaignRunner(ports).run(ref, _spec)
    args.outcome.write_text(
        json.dumps(
            {
                "stop_reason": outcome.stop_reason.value,
                "items": [[item.item_id, item.disposition.value] for item in outcome.items],
                "claimed": [item_id for item_id, _actor in tracker.claims],
                "delivered": list(workspace.delivered),
                "progress": outcome.progress_ref,
            }
        ),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
