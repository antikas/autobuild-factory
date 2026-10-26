"""Campaign recovery against real Git, real Pinax and a bare origin.

An untracked file in the primary checkout must not block a delivery unless the
delivery brings in its path, and an accepted item whose delivery failed while
it stayed claimed must be delivered by the next campaign, not parked as a
tracker mismatch."""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest
from test_serial_delivery import (
    ValidatorRuns,
    default_adapters,
    git,
    pinax_origin,
    product_diff,
    run_item,
    scripted_harness,
)

from autobuild.adapters import GitWorkspaceAdapter, LocalRunRecordAdapter, PinaxTrackerAdapter
from autobuild.application import ItemWorkflow, WorkflowPorts
from autobuild.cli import main as campaign_main
from autobuild.domain import AdapterError, CampaignRef, ItemDisposition
from autobuild.testing import FakeKnowledgeAdapter
from test_serial_delivery import identity, item_spec

pytestmark = pytest.mark.usefixtures("self_committing_pinax")


# --- 1. Untracked files in the primary checkout ------------------------------


def test_an_untracked_file_in_the_primary_never_blocks_the_delivery(tmp_path: Path) -> None:
    origin = pinax_origin(tmp_path, count=1)
    tracker, workspace = default_adapters(origin)
    stray = origin.repo / "docs" / "x.md"
    stray.write_text("a brief written by hand\n", encoding="utf-8")

    outcome, _ = run_item(origin, workspace, tracker, scripted_harness(origin.items[0]), ValidatorRuns())

    assert outcome.disposition is ItemDisposition.ACCEPTED, outcome.reason
    assert git(origin.repo, "status", "--porcelain", "--untracked-files=all") == "?? docs/x.md"
    assert stray.read_text(encoding="utf-8") == "a brief written by hand\n"
    pushed = git(origin.repo, "ls-tree", "-r", "--name-only", origin.head()).split()
    assert "feature.txt" in pushed and "docs/x.md" not in pushed
    assert git(origin.repo, "rev-parse", "HEAD") == origin.head()


def test_an_untracked_file_at_a_delivered_path_still_blocks_the_delivery(tmp_path: Path) -> None:
    origin = pinax_origin(tmp_path, count=1)
    tracker, workspace = default_adapters(origin)
    before = origin.head()
    collision = origin.repo / "feature.txt"
    collision.write_text("hand-made, not the item's\n", encoding="utf-8")

    outcome, _ = run_item(origin, workspace, tracker, scripted_harness(origin.items[0]), ValidatorRuns())

    assert outcome.disposition is ItemDisposition.PARKED, outcome.reason
    assert "untracked" in (outcome.reason or "") and "feature.txt" in (outcome.reason or "")
    assert collision.read_text(encoding="utf-8") == "hand-made, not the item's\n"
    assert "feature.txt" not in git(origin.repo, "ls-tree", "-r", "--name-only", origin.head()).split()
    assert product_diff(origin.repo, before, origin.head()) == []


# --- 2. Resume an accepted item whose delivery failed --------------------------


HARNESS = """\
import json
import sys

args = sys.argv[2:]
if args == ["--version"]:
    print("claude-code fake 1.0")
    raise SystemExit(0)
if args[:2] == ["auth", "status"]:
    print(json.dumps({"loggedIn": True}))
    raise SystemExit(0)
print("a resumed delivery must not run a seat", file=sys.stderr)
raise SystemExit(3)
"""


class _ParkRefused(PinaxTrackerAdapter):
    """The tracker of the first campaign: its park write fails, so the item
    stays claimed."""

    def park(self, item_id, reason, actor, workspace=None):
        raise AdapterError("simulated tracker park failure")


def _push_fails(adapter: GitWorkspaceAdapter) -> None:
    real = adapter._git

    def git_without_push(root: Path, *args: str, **kwargs) -> str:
        if args and args[0] == "push":
            raise AdapterError("git push failed: simulated connection reset by the remote")
        return real(root, *args, **kwargs)

    adapter._git = git_without_push  # type: ignore[method-assign]


def _profile(tmp_path: Path) -> Path:
    executable = tmp_path / "fake resume harness.py"
    executable.write_text(HARNESS, encoding="utf-8")
    command = [sys.executable, str(executable), "claude-code"]
    profile = tmp_path / "autobuild.toml"
    profile.write_text(
        "[run]\n"
        'harness = "claude-code"\n'
        "max_items = 1\n\n"
        "[models]\n"
        'builder = "builder-model"\n'
        'reviewer = "reviewer-model"\n\n'
        "[validator]\n"
        'id = "tests"\n'
        f"argv = {json.dumps([sys.executable, '-c', 'pass'])}\n\n"
        "[harness]\n"
        f"command = {json.dumps(command)}\n\n"
        "[preflight]\n"
        'accepted_environment = ["NODE_EXTRA_CA_CERTS", "SSLKEYLOGFILE", "HTTP_PROXY", '
        '"HTTPS_PROXY", "ALL_PROXY"]\n\n'
        "[pressure]\n"
        "high_memory_percent = 0\nhigh_memory_gb = 0\n"
        "critical_memory_percent = 0\ncritical_memory_gb = 0\nhigh_commit_percent = 0\n",
        encoding="utf-8",
    )
    return profile


def test_an_accepted_item_whose_delivery_failed_is_delivered_by_the_next_campaign(
    tmp_path: Path,
) -> None:
    origin = pinax_origin(tmp_path, count=1)
    item_id = origin.items[0]
    scratch = (tmp_path / "scratch").resolve()
    lock = threading.Lock()
    tracker = _ParkRefused(origin.repo, primary_lock=lock)
    workspace = GitWorkspaceAdapter(
        scratch / "workspace", tracker_commits_itself=True, primary_lock=lock, projection=tracker
    )
    _push_fails(workspace)
    records = LocalRunRecordAdapter(scratch / "runs")
    ports = WorkflowPorts(
        tracker,
        workspace,
        scripted_harness(item_id),
        ValidatorRuns(),
        records,
        FakeKnowledgeAdapter(identity("knowledge")),
    )
    first = CampaignRef("first", origin.repo)
    failed = ItemWorkflow(ports).run(first, item_spec(origin, item_id), records.create(first))

    assert failed.disposition is ItemDisposition.FAILED, failed.reason
    assert "simulated tracker park failure" in (failed.reason or "")
    assert "feature.txt" not in git(origin.repo, "ls-tree", "-r", "--name-only", origin.head())
    output = tmp_path / "second.json"

    exit_code = campaign_main(
        [
            "run",
            "--repository", str(origin.repo),
            "--profile", str(_profile(tmp_path)),
            "--scratch-root", str(scratch),
            "--allow-delivery",
            "--output", str(output),
        ]
    )

    result = json.loads(output.read_text(encoding="utf-8"))
    assert [(entry["item_id"], entry["disposition"]) for entry in result["items"]] == [
        (item_id, "accepted")
    ], result
    assert exit_code == 0, result
    head = origin.head()
    assert git(origin.repo, "show", f"{head}:feature.txt") == f"feature for {item_id}"
    page = git(origin.repo, "show", f"{head}:.ergon/items/{item_id}.md")
    assert "status: done" in page
    events_log = Path(result["report_ref"]).parent / "events.jsonl"
    kinds = [json.loads(line)["event_type"] for line in events_log.read_text(encoding="utf-8").splitlines()]
    assert "item.resume_parked" not in kinds
    assert "item.resumed" in kinds
