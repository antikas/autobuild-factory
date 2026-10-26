"""The Pinax adapter against a stub Pinax that commits its own events.

The stub stands in for the ``pinax`` command at the process boundary. It runs
as a real child process against a real Git repository, so every assertion
reads repository state the way the adapter does. Its behaviour for the next
command is set through ``STUB_PINAX_MODE``.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from autobuild.adapters import GitWorkspaceAdapter, PinaxTrackerAdapter
from autobuild.adapters.tracker_commits import SELF_COMMITTING_TRACKER_REQUIRED
from autobuild.domain import (
    AdapterError,
    CampaignRef,
    ClaimTaken,
    CloseEvidence,
    CommandResult,
    DeliveryMode,
    DeliveryRequest,
    EvidenceError,
    FinaliseRequest,
    Proposal,
    ReviewDecision,
    ReviewVerdict,
    TrackerStop,
    ValidationEvidence,
    WorkItem,
)

STUB = '''
import json, os, subprocess, sys, time
from pathlib import Path

args = sys.argv[1:]
root = Path(args[args.index("--root") + 1])
command = args[args.index("--root") + 2]
mode = os.environ.get("STUB_PINAX_MODE", "commit")
actor = args[args.index("--actor") + 1] if "--actor" in args else ""


def git(*argv):
    for attempt in range(5):
        done = subprocess.run(["git", "-C", str(root), *argv], capture_output=True)
        if done.returncode == 0:
            return
        time.sleep(0.2 * (attempt + 1))
    raise SystemExit(f"stub git {argv} failed: {done.stderr!r}")


def report(code, **fields):
    print(json.dumps(dict({"event_type": command, "remote_branch": "main"}, **fields)))
    raise SystemExit(code)


def append():
    log = root / ".ergon" / "log"
    log.mkdir(parents=True, exist_ok=True)
    shard = log / "stub.jsonl"
    count = len(shard.read_text(encoding="utf-8").splitlines()) if shard.exists() else 0
    with shard.open("a", encoding="utf-8", newline="\\n") as handle:
        handle.write(json.dumps({"type": command, "n": count, "actor": actor}) + "\\n")


if actor and "@" not in actor:
    sys.stderr.write(f"pinax: the actor {actor!r} is not a valid handle\\n")
    report(2, status="invalid_actor", committed=False, appended=False, pushed=False,
           message=f"the actor {actor!r} is not a valid handle")
if mode.startswith("exit:"):
    code = int(mode.split(":", 1)[1])
    if code == 7:
        append()
    sys.stderr.write(f"stub pinax refused {command} with exit {code}\\n")
    raise SystemExit(code)
if mode == "unreachable":
    sys.stderr.write("pinax: origin could not be reached, so nothing was appended.\\n")
    report(4, status="remote_unreachable", committed=False, appended=False, pushed=False,
           message="origin could not be reached, so nothing was appended")
if mode == "push-rejected-after-pull":
    # Pinax's pull fast-forwarded onto published commits before its own event.
    git("merge", "--ff-only", "origin/main")
append()
if mode in {"commit", "commit-note", "commit-push", "branch-local", "two-commits",
            "with-product", "exit-after-commit:3", "push-rejected", "push-rejected-claim",
            "push-rejected-with-product", "push-rejected-annulled", "push-rejected-after-pull"}:
    if mode in {"with-product", "push-rejected-with-product"}:
        (root / "product-by-tracker.txt").write_text("leak\\n", encoding="utf-8")
        git("add", "product-by-tracker.txt")
    git("add", ".ergon")
    git("commit", "-m", f"stub pinax {command}")
    if mode == "two-commits":
        append()
        git("add", ".ergon")
        git("commit", "-m", f"stub pinax {command} again")
if mode == "exit-after-commit:3":
    sys.stderr.write("claim superseded by an earlier claim\\n")
    raise SystemExit(3)
if mode == "branch-local":
    sys.stderr.write(
        "pinax: the push was refused because feature is not the origin default branch "
        "main; the event is committed locally and not published.\\n"
    )
    report(4, status="committed_local", committed=True, pushed=False, head_branch="feature",
           message="the sync publishes only main and feature is checked out")
if mode == "commit-note":
    sys.stderr.write("pinax: origin could not be reached; the event is recorded locally "
                     "and not published.\\n")
if mode == "commit-push":
    git("push", "origin", "HEAD:main")
    git("fetch", "origin")
if mode in {"push-rejected", "push-rejected-with-product", "push-rejected-after-pull"}:
    # Pinax's push lost a race with a concurrent delivery and its pull did not
    # complete: the event stays committed on the local branch, unpublished.
    sys.stderr.write("pinax: the push to main was rejected after 1 attempts.\\n")
    report(5, status="push_rejected", committed=True, pushed=False, attempts=1,
           item_id="stub-1", git_output="! [rejected] main -> main (fetch first)",
           message="the push to main was rejected and the event is not published")
if mode == "push-rejected-uncommitted":
    # The event was appended and never committed.
    report(5, status="push_rejected", committed=False, pushed=False, attempts=1,
           message="the push to main was rejected and the event is not published")
if mode in {"push-rejected-claim", "push-rejected-annulled"}:
    # A claim needs the remote: Pinax annuls its unpublished claim.
    report(5, status="push_rejected", committed=True, pushed=False, attempts=1,
           annulled={"reason": "the event could not be published"},
           message="the push to main was rejected and the event is not published")
print(json.dumps({"item_id": "stub-1", "id": "stub-1"}))
'''


def git(root: Path, *args: str) -> str:
    for attempt in range(5):
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode == 0 or attempt == 4:
            break
        time.sleep(0.2 * (attempt + 1))
    if completed.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {completed.stderr.strip()}")
    return completed.stdout.strip()


@pytest.fixture()
def stub_pinax(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    """Route the adapter's Pinax calls to the stub and record the adapter's own
    git calls, so a test can assert which git commands the adapter issued."""

    script = tmp_path / "stub_pinax.py"
    script.write_text(STUB, encoding="utf-8")

    def run_stub(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(script), "--root", str(root), *args],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

    calls: list[tuple[str, ...]] = []
    original_git = PinaxTrackerAdapter._git

    def recording_git(root: Path, *args: str) -> str:
        calls.append(args)
        return original_git(root, *args)

    monkeypatch.setattr(PinaxTrackerAdapter, "_run", staticmethod(run_stub))
    monkeypatch.setattr(PinaxTrackerAdapter, "_git", staticmethod(recording_git))
    monkeypatch.setenv("STUB_PINAX_MODE", "commit")
    return calls


def repository(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    git(repo, "config", "user.name", "AutoBuild Test")
    git(repo, "config", "user.email", "autobuild@example.invalid")
    (repo / ".ergon" / "log").mkdir(parents=True)
    (repo / ".ergon" / "log" / "seed.jsonl").write_text("", encoding="utf-8")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "initial")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-u", "origin", "main")
    return repo


ITEM = WorkItem("tst-1", "Stub item", "docs/brief.md", ("acceptance",))
MUTATING = {"add", "commit", "push"}


def close_evidence(workspace_adapter: GitWorkspaceAdapter, workspace) -> CloseEvidence:
    diff = workspace_adapter.diff(workspace)
    command = CommandResult("run:item:tests", 0, "stdout", "stderr", "start", "end")
    return CloseEvidence(
        ITEM.item_id,
        diff.workspace_revision,
        diff,
        ValidationEvidence("tests", diff.workspace_revision, command, diff.changed_paths),
        ReviewVerdict(ITEM.item_id, ReviewDecision.PASS, (), "review"),
        "trajectory",
    )


def test_pinax_commit_is_the_tracker_commit_and_the_adapter_never_commits(
    tmp_path: Path, stub_pinax: list[tuple[str, ...]]
) -> None:
    repo = repository(tmp_path)
    campaign = CampaignRef("campaign", repo)
    tracker = PinaxTrackerAdapter(repo)
    workspace_adapter = GitWorkspaceAdapter(tmp_path / "scratch", tracker_commits_itself=True)

    before_claim = git(repo, "rev-parse", "HEAD")
    tracker.claim(ITEM, "builder@proof")
    assert git(repo, "log", "-1", "--format=%s") == "stub pinax claim"
    assert git(repo, "rev-parse", "HEAD^") == before_claim
    assert git(repo, "status", "--porcelain") == ""

    workspace = workspace_adapter.create_isolated(campaign, ITEM)
    (workspace.root / "product.txt").write_text("accepted\n", encoding="utf-8")
    evidence = close_evidence(workspace_adapter, workspace)
    item_commit = workspace_adapter.commit_item(
        workspace, FinaliseRequest(ITEM.item_id, evidence, "ADDED: product")
    )
    tracker.close(evidence, item_commit, workspace, "coordinator@proof")
    pinax_commit = git(workspace.root, "rev-parse", "HEAD")
    tracker_commit = workspace_adapter.commit_tracker(workspace, ITEM.item_id, item_commit)

    assert tracker_commit == pinax_commit
    assert git(workspace.root, "log", "-1", "--format=%s", tracker_commit) == "stub pinax done"
    assert git(workspace.root, "rev-parse", f"{tracker_commit}^") == item_commit
    result = workspace_adapter.deliver(
        workspace,
        DeliveryRequest(
            ITEM.item_id,
            item_commit,
            tracker_commit,
            DeliveryMode.PROTECTED_DEFAULT,
            "main",
            git(repo, "rev-parse", "HEAD"),
        ),
    )
    workspace_adapter.release(workspace)
    tracker.park("tst-2", "parked for the test", "coordinator@proof")
    tracker.propose(
        Proposal("Candidate", "What should be built?", "Queue is dry", "docs/brief.md"),
        "coordinator@proof",
    )

    assert result.pushed is True
    assert [call for call in stub_pinax if call[0] in MUTATING] == []
    assert git(repo, "status", "--porcelain") == ""


def test_a_tracker_that_leaves_its_change_uncommitted_stops_at_the_first_mutation(
    tmp_path: Path, stub_pinax: list[tuple[str, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repository(tmp_path)
    head = git(repo, "rev-parse", "HEAD")
    monkeypatch.setenv("STUB_PINAX_MODE", "uncommitted")

    with pytest.raises(TrackerStop) as raised:
        PinaxTrackerAdapter(repo).claim(ITEM, "builder@proof")

    assert raised.value.environment is True
    assert "pinax-tracker 0.2.1 or later" in str(raised.value)
    assert str(raised.value) == SELF_COMMITTING_TRACKER_REQUIRED
    assert git(repo, "rev-parse", "HEAD") == head
    assert git(repo, "status", "--porcelain", "--", ".ergon") != ""


def test_a_superseded_claim_reports_the_item_taken(
    tmp_path: Path, stub_pinax: list[tuple[str, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repository(tmp_path)
    monkeypatch.setenv("STUB_PINAX_MODE", "exit-after-commit:3")

    with pytest.raises(ClaimTaken) as raised:
        PinaxTrackerAdapter(repo).claim(ITEM, "builder@proof")

    assert raised.value.item_id == ITEM.item_id
    assert "claim superseded by an earlier claim" in str(raised.value)
    assert not isinstance(raised.value, TrackerStop)


@pytest.mark.parametrize(
    ("code", "environment"),
    ((1, False), (2, False), (4, True), (5, False), (6, False), (7, False)),
)
def test_pinax_exit_codes_map_to_typed_stops_naming_the_code_and_message(
    tmp_path: Path,
    stub_pinax: list[tuple[str, ...]],
    monkeypatch: pytest.MonkeyPatch,
    code: int,
    environment: bool,
) -> None:
    repo = repository(tmp_path)
    monkeypatch.setenv("STUB_PINAX_MODE", f"exit:{code}")

    with pytest.raises(TrackerStop) as raised:
        PinaxTrackerAdapter(repo).claim(ITEM, "builder@proof")

    assert raised.value.code == code
    assert raised.value.environment is environment
    assert f"exit {code}" in str(raised.value)
    assert f"stub pinax refused claim with exit {code}" in str(raised.value)
    assert [call for call in stub_pinax if call[0] in MUTATING] == []


def test_exit_three_outside_a_claim_is_not_read_as_a_taken_item(
    tmp_path: Path, stub_pinax: list[tuple[str, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repository(tmp_path)
    monkeypatch.setenv("STUB_PINAX_MODE", "exit:3")

    with pytest.raises(Exception) as raised:
        PinaxTrackerAdapter(repo).park("tst-1", "reason", "coordinator@proof")

    assert not isinstance(raised.value, ClaimTaken)
    assert "exit 3" in str(raised.value)


def _closed_worktree(tmp_path: Path, mode: str, monkeypatch: pytest.MonkeyPatch):
    repo = repository(tmp_path)
    campaign = CampaignRef("campaign", repo)
    workspace_adapter = GitWorkspaceAdapter(tmp_path / "scratch", tracker_commits_itself=True)
    workspace = workspace_adapter.create_isolated(campaign, ITEM)
    (workspace.root / "product.txt").write_text("accepted\n", encoding="utf-8")
    evidence = close_evidence(workspace_adapter, workspace)
    item_commit = workspace_adapter.commit_item(
        workspace, FinaliseRequest(ITEM.item_id, evidence, "ADDED: product")
    )
    monkeypatch.setenv("STUB_PINAX_MODE", mode)
    script_run = PinaxTrackerAdapter._run
    script_run(workspace.root, "done", ITEM.item_id, "--json")
    return workspace_adapter, workspace, item_commit


@pytest.mark.parametrize(
    ("mode", "message"),
    (
        ("two-commits", "exactly one commit"),
        ("with-product", "non-tracker paths: product-by-tracker.txt"),
        ("exit:1", "no commit"),
    ),
)
def test_commit_tracker_refuses_anything_but_one_tracker_only_commit(
    tmp_path: Path,
    stub_pinax: list[tuple[str, ...]],
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    message: str,
) -> None:
    workspace_adapter, workspace, item_commit = _closed_worktree(tmp_path, mode, monkeypatch)
    head = git(workspace.root, "rev-parse", "HEAD")

    with pytest.raises(EvidenceError, match=message):
        workspace_adapter.commit_tracker(workspace, ITEM.item_id, item_commit)

    assert git(workspace.root, "rev-parse", "HEAD") == head


def test_commit_tracker_never_commits_uncommitted_tracker_state_itself(
    tmp_path: Path, stub_pinax: list[tuple[str, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_adapter, workspace, item_commit = _closed_worktree(
        tmp_path, "uncommitted", monkeypatch
    )

    with pytest.raises(TrackerStop, match="pinax-tracker 0.2.1 or later"):
        workspace_adapter.commit_tracker(workspace, ITEM.item_id, item_commit)

    assert git(workspace.root, "rev-parse", "HEAD") == item_commit
    assert git(workspace.root, "status", "--porcelain", "--", ".ergon") != ""


def test_commit_tracker_accepts_the_parked_tracker_commit_on_the_start_commit(
    tmp_path: Path, stub_pinax: list[tuple[str, ...]]
) -> None:
    repo = repository(tmp_path)
    workspace_adapter = GitWorkspaceAdapter(tmp_path / "scratch", tracker_commits_itself=True)
    workspace = workspace_adapter.create_isolated(CampaignRef("campaign", repo), ITEM)
    PinaxTrackerAdapter(repo).park(ITEM.item_id, "reason", "coordinator@proof", workspace)

    tracker_commit = workspace_adapter.commit_tracker(workspace, ITEM.item_id, item_commit=None)

    assert git(workspace.root, "rev-parse", f"{tracker_commit}^") == workspace.start_commit
    assert git(workspace.root, "log", "-1", "--format=%s") == "stub pinax park"
    assert json.loads(
        (workspace.root / ".ergon" / "log" / "stub.jsonl").read_text(encoding="utf-8").splitlines()[-1]
    )["type"] == "park"


@pytest.mark.parametrize("accept", (True, False))
def test_a_branch_local_claim_is_accepted_only_in_current_branch_pr_mode(
    tmp_path: Path,
    stub_pinax: list[tuple[str, ...]],
    monkeypatch: pytest.MonkeyPatch,
    accept: bool,
) -> None:
    repo = repository(tmp_path)
    head = git(repo, "rev-parse", "HEAD")
    monkeypatch.setenv("STUB_PINAX_MODE", "branch-local")
    tracker = PinaxTrackerAdapter(repo, accept_unpublished_claims=accept)

    if accept:
        receipt = tracker.claim(ITEM, "builder@proof")
        assert receipt.published is False
        assert "feature is not the origin default branch" in receipt.publication_note
        assert git(repo, "rev-parse", "HEAD^") == head
    else:
        with pytest.raises(TrackerStop) as raised:
            tracker.claim(ITEM, "builder@proof")
        assert raised.value.environment is True
        assert raised.value.code == 4
        assert "committed_local" in str(raised.value)
        assert "feature is not the origin default branch" in str(raised.value)


@pytest.mark.parametrize("accept", (True, False))
def test_an_unreachable_remote_stops_the_claim_in_every_mode_with_its_own_cause(
    tmp_path: Path,
    stub_pinax: list[tuple[str, ...]],
    monkeypatch: pytest.MonkeyPatch,
    accept: bool,
) -> None:
    repo = repository(tmp_path)
    monkeypatch.setenv("STUB_PINAX_MODE", "unreachable")

    with pytest.raises(TrackerStop) as raised:
        PinaxTrackerAdapter(repo, accept_unpublished_claims=accept).claim(ITEM, "builder@proof")

    assert raised.value.environment is True
    assert "remote_unreachable" in str(raised.value)
    assert "origin could not be reached" in str(raised.value)


@pytest.mark.parametrize("operation", ("park", "propose"))
def test_park_and_propose_refuse_a_primary_with_staged_or_modified_product_files(
    tmp_path: Path, stub_pinax: list[tuple[str, ...]], operation: str
) -> None:
    repo = repository(tmp_path)
    (repo / "README.md").write_text("edited by hand\n", encoding="utf-8")
    (repo / "staged.txt").write_text("staged by hand\n", encoding="utf-8")
    git(repo, "add", "staged.txt")
    head = git(repo, "rev-parse", "HEAD")
    tracker = PinaxTrackerAdapter(repo)

    with pytest.raises(AdapterError, match="README.md, staged.txt"):
        if operation == "park":
            tracker.park("tst-1", "reason", "coordinator@proof")
        else:
            tracker.propose(
                Proposal("Candidate", "What next?", "Queue is dry", "docs/brief.md"),
                "coordinator@proof",
            )

    assert git(repo, "rev-parse", "HEAD") == head
    assert not (repo / ".ergon" / "log" / "stub.jsonl").exists()


def test_a_tracker_commit_that_sweeps_in_a_product_file_is_refused(
    tmp_path: Path, stub_pinax: list[tuple[str, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repository(tmp_path)
    workspace_adapter = GitWorkspaceAdapter(tmp_path / "scratch", tracker_commits_itself=True)
    workspace = workspace_adapter.create_isolated(CampaignRef("campaign", repo), ITEM)
    monkeypatch.setenv("STUB_PINAX_MODE", "with-product")

    with pytest.raises(EvidenceError, match="non-tracker paths: product-by-tracker.txt"):
        PinaxTrackerAdapter(repo).park(ITEM.item_id, "reason", "coordinator@proof", workspace)


def test_a_primary_park_checks_only_its_own_commit_and_refuses_a_product_path(
    tmp_path: Path, stub_pinax: list[tuple[str, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A delivered product commit before the park is outside the park's own
    range; the park's commit that sweeps in a product file is refused."""

    repo = repository(tmp_path)
    (repo / "delivered.txt").write_text("delivered before the park\n", encoding="utf-8")
    git(repo, "add", "delivered.txt")
    git(repo, "commit", "-m", "ADDED: delivered item")
    tracker = PinaxTrackerAdapter(repo)

    tracker.park("tst-1", "reason", "coordinator@proof")
    monkeypatch.setenv("STUB_PINAX_MODE", "with-product")
    with pytest.raises(EvidenceError) as raised:
        tracker.park("tst-2", "reason", "coordinator@proof")

    assert "non-tracker paths: product-by-tracker.txt" in str(raised.value)
    assert "delivered.txt" not in str(raised.value)


@pytest.mark.parametrize(("mode", "published"), (("commit-push", True), ("commit-note", False)))
def test_a_primary_park_reports_whether_pinax_published_it(
    tmp_path: Path,
    stub_pinax: list[tuple[str, ...]],
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    published: bool,
) -> None:
    repo = repository(tmp_path)
    monkeypatch.setenv("STUB_PINAX_MODE", mode)

    publication = PinaxTrackerAdapter(repo).park("tst-1", "reason", "coordinator@proof")

    assert publication is not None
    assert publication.published is published
    assert ("recorded locally and not published" in publication.note) is not published


def test_a_follow_up_whose_push_lost_a_race_is_kept_and_published_by_the_next_delivery(
    tmp_path: Path, stub_pinax: list[tuple[str, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent delivery pushed main first, so Pinax's push was rejected
    with the follow-up committed on the local main. The proposal completes,
    unpublished, and the next delivery's fetch-merge-push publishes it."""

    repo = repository(tmp_path)
    remote = tmp_path / "remote.git"
    other = tmp_path / "other"
    subprocess.run(
        ["git", "clone", "-b", "main", str(remote), str(other)], check=True, capture_output=True
    )
    git(other, "config", "user.name", "Concurrent Delivery")
    git(other, "config", "user.email", "delivery@example.invalid")
    (other / "delivered.txt").write_text("delivered concurrently\n", encoding="utf-8")
    git(other, "add", "delivered.txt")
    git(other, "commit", "-m", "ADDED: concurrent delivery")
    git(other, "push", "origin", "HEAD:main")
    tracker = PinaxTrackerAdapter(repo)
    before = git(repo, "rev-parse", "HEAD")
    monkeypatch.setenv("STUB_PINAX_MODE", "push-rejected")

    ref = tracker.propose(
        Proposal("Follow-up: tst-1 F1", "What next?", "non-blocking finding", "docs/brief.md"),
        "coordinator@proof",
    )

    assert ref.proposal_id == "stub-1"
    assert ref.runnable is False
    assert ref.published is False
    assert "push_rejected" in ref.publication_note
    assert "next push" in ref.publication_note
    shard = (repo / ".ergon" / "log" / "stub.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["type"] for line in shard] == ["add", "block", "note"]
    follow_up = git(repo, "rev-parse", "HEAD")
    assert git(repo, "rev-list", "--count", f"{before}..{follow_up}") == "3"
    assert git(repo, "status", "--porcelain") == ""

    monkeypatch.setenv("STUB_PINAX_MODE", "commit")
    workspace_adapter = GitWorkspaceAdapter(tmp_path / "scratch", tracker_commits_itself=True)
    workspace = workspace_adapter.create_isolated(CampaignRef("campaign", repo), ITEM)
    (workspace.root / "product.txt").write_text("accepted\n", encoding="utf-8")
    evidence = close_evidence(workspace_adapter, workspace)
    item_commit = workspace_adapter.commit_item(
        workspace, FinaliseRequest(ITEM.item_id, evidence, "ADDED: product")
    )
    tracker.close(evidence, item_commit, workspace, "coordinator@proof")
    tracker_commit = workspace_adapter.commit_tracker(workspace, ITEM.item_id, item_commit)
    result = workspace_adapter.deliver(
        workspace,
        DeliveryRequest(
            ITEM.item_id,
            item_commit,
            tracker_commit,
            DeliveryMode.PROTECTED_DEFAULT,
            "main",
            git(repo, "rev-parse", "HEAD"),
        ),
    )
    workspace_adapter.release(workspace)

    assert result.pushed is True
    # The remote now holds the follow-up and the concurrent delivery.
    git(remote, "merge-base", "--is-ancestor", follow_up, "main")
    assert "delivered.txt" in git(remote, "ls-tree", "--name-only", "main")


def test_a_claim_whose_push_was_rejected_still_stops(
    tmp_path: Path, stub_pinax: list[tuple[str, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repository(tmp_path)
    monkeypatch.setenv("STUB_PINAX_MODE", "push-rejected-claim")

    with pytest.raises(TrackerStop) as raised:
        PinaxTrackerAdapter(repo).claim(ITEM, "builder@proof")

    assert raised.value.code == 5
    assert "push_rejected" in str(raised.value)


@pytest.mark.parametrize("operation", ("park", "propose"))
@pytest.mark.parametrize(
    ("mode", "stop", "message"),
    (
        ("push-rejected-with-product", EvidenceError, "non-tracker paths: product-by-tracker.txt"),
        ("push-rejected-annulled", TrackerStop, "push_rejected"),
        ("push-rejected-uncommitted", TrackerStop, "push_rejected"),
    ),
)
def test_a_rejected_push_that_fails_a_guard_still_stops(
    tmp_path: Path,
    stub_pinax: list[tuple[str, ...]],
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    mode: str,
    stop: type[Exception],
    message: str,
) -> None:
    repo = repository(tmp_path)
    tracker = PinaxTrackerAdapter(repo)
    monkeypatch.setenv("STUB_PINAX_MODE", mode)

    with pytest.raises(stop, match=message):
        if operation == "park":
            tracker.park("tst-1", "reason", "coordinator@proof")
        else:
            tracker.propose(
                Proposal("Follow-up: tst-1 F1", "What next?", "finding", "docs/brief.md"),
                "coordinator@proof",
            )


def test_a_rejected_push_after_a_pull_onto_published_commits_stands_unpublished(
    tmp_path: Path, stub_pinax: list[tuple[str, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pinax fast-forwarded onto a published product commit, appended its event
    and lost the push race. The published commit is not the write's own."""

    repo = repository(tmp_path)
    remote = tmp_path / "remote.git"
    other = tmp_path / "other"
    subprocess.run(
        ["git", "clone", "-b", "main", str(remote), str(other)], check=True, capture_output=True
    )
    git(other, "config", "user.name", "Concurrent Delivery")
    git(other, "config", "user.email", "delivery@example.invalid")
    (other / "delivered.txt").write_text("delivered concurrently\n", encoding="utf-8")
    git(other, "add", "delivered.txt")
    git(other, "commit", "-m", "ADDED: concurrent delivery")
    git(other, "push", "origin", "HEAD:main")
    delivered = git(other, "rev-parse", "HEAD")
    git(repo, "fetch", "origin")
    monkeypatch.setenv("STUB_PINAX_MODE", "push-rejected-after-pull")

    publication = PinaxTrackerAdapter(repo).park("tst-1", "reason", "coordinator@proof")

    assert publication is not None
    assert publication.published is False
    assert "push_rejected" in publication.note
    assert git(repo, "rev-parse", "HEAD^") == delivered
    assert git(repo, "log", "-1", "--format=%s") == "stub pinax park"
    assert git(repo, "status", "--porcelain") == ""


def test_trackers_declare_whether_they_commit_themselves() -> None:
    from autobuild.adapters import BacklogTrackerAdapter

    assert PinaxTrackerAdapter.commits_itself is True
    assert BacklogTrackerAdapter.commits_itself is False


def test_an_unnamed_lane_claims_and_parks_with_role_at_host_actors(
    tmp_path: Path, stub_pinax: list[tuple[str, ...]]
) -> None:
    from autobuild.application import ItemWorkflow, WorkflowPorts
    from autobuild.domain import (
        AdapterIdentity,
        ItemDisposition,
        ItemExecutionSpec,
        ToolPolicy,
    )
    from autobuild.testing import (
        FakeCommandAdapter,
        FakeHarnessAdapter,
        FakeKnowledgeAdapter,
        FakeRunRecordAdapter,
        FakeWorkspaceAdapter,
    )

    class _NoWorktree(FakeWorkspaceAdapter):
        def create_isolated(self, campaign, item):
            raise AdapterError("no worktree in this test")

    def identity(name: str) -> AdapterIdentity:
        return AdapterIdentity(name, "test", frozenset())

    repo = repository(tmp_path)
    ports = WorkflowPorts(
        PinaxTrackerAdapter(repo),
        _NoWorktree(identity("workspace")),
        FakeHarnessAdapter(identity("harness")),
        FakeCommandAdapter(identity("command")),
        FakeRunRecordAdapter(identity("records")),
        FakeKnowledgeAdapter(identity("knowledge")),
    )
    campaign = CampaignRef("campaign", repo)
    spec = ItemExecutionSpec(
        ITEM,
        Path("brief.md"),
        "validator",
        (sys.executable, "-c", "pass"),
        ToolPolicy(frozenset({"python"}), (repo,)),
        "builder-class",
        "reviewer-class",
        "specialist-class",
        delivery_mode=DeliveryMode.PROTECTED_DEFAULT,
        delivery_target_branch="main",
        delivery_target_revision=git(repo, "rev-parse", "HEAD"),
    )

    outcome = ItemWorkflow(ports).run(campaign, spec, ports.records.create(campaign))

    events = [
        json.loads(line)
        for line in (repo / ".ergon" / "log" / "stub.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [(event["type"], event["actor"]) for event in events] == [
        ("claim", "builder@autobuild"),
        ("park", "coordinator@autobuild"),
    ]
    assert outcome.disposition is ItemDisposition.PARKED
    assert outcome.tracker_stop is None


def test_a_merge_of_another_writers_product_change_does_not_refuse_the_claim(
    tmp_path: Path,
) -> None:
    from autobuild.adapters.tracker_commits import require_committed

    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    git(repo, "config", "user.name", "AutoBuild Test")
    git(repo, "config", "user.email", "autobuild@example.invalid")
    (repo / ".ergon").mkdir()
    (repo / ".ergon" / "log.jsonl").write_text("seed\n", encoding="utf-8")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "initial")
    before = git(repo, "rev-parse", "HEAD")
    git(repo, "branch", "other-writer")
    (repo / ".ergon" / "log.jsonl").write_text("seed\nclaim\n", encoding="utf-8")
    git(repo, "commit", "-am", "pinax: item.claimed tst-1")
    git(repo, "checkout", "other-writer")
    (repo / "README.md").write_text("another writer\n", encoding="utf-8")
    git(repo, "commit", "-am", "another writer's product change")
    git(repo, "checkout", "main")
    git(repo, "merge", "--no-edit", "other-writer")
    assert len(git(repo, "rev-list", "--parents", "-n", "1", "HEAD").split()) == 3

    assert require_committed(git, repo, before, (".ergon",)) == git(repo, "rev-parse", "HEAD")

    (repo / "README.md").write_text("swept\n", encoding="utf-8")
    (repo / ".ergon" / "log.jsonl").write_text("seed\nclaim\npark\n", encoding="utf-8")
    after_merge = git(repo, "rev-parse", "HEAD")
    git(repo, "commit", "-am", "pinax: item.parked tst-1")
    with pytest.raises(EvidenceError, match="non-tracker paths: README.md"):
        require_committed(git, repo, after_merge, (".ergon",))


def test_a_pull_onto_an_already_pushed_item_commit_does_not_refuse_the_park(
    tmp_path: Path,
) -> None:
    from autobuild.adapters.tracker_commits import require_committed

    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    git(repo, "config", "user.name", "AutoBuild Test")
    git(repo, "config", "user.email", "autobuild@example.invalid")
    (repo / ".ergon").mkdir()
    (repo / ".ergon" / "log.jsonl").write_text("seed\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "initial")
    before = git(repo, "rev-parse", "HEAD")
    # The delivery pushed the item commit; the local branch never moved onto it.
    git(repo, "checkout", "-b", "item")
    (repo / "src.py").write_text("item\n", encoding="utf-8")
    git(repo, "add", "src.py")
    git(repo, "commit", "-m", "ADDED: item")
    published = git(repo, "rev-parse", "HEAD")
    git(repo, "update-ref", "refs/remotes/origin/main", published)
    git(repo, "checkout", "main")
    # The tracker command pulls (a fast-forward onto the pushed item) and commits.
    git(repo, "merge", "--ff-only", published)
    (repo / ".ergon" / "log.jsonl").write_text("seed\npark\n", encoding="utf-8")
    git(repo, "commit", "-am", "pinax: item.parked tst-1")

    with pytest.raises(EvidenceError, match="non-tracker paths: src.py"):
        require_committed(git, repo, before, (".ergon",))
    assert require_committed(git, repo, before, (".ergon",), published) == git(
        repo, "rev-parse", "HEAD"
    )

    # The exclusion never hides the command's own product change.
    after = git(repo, "rev-parse", "HEAD")
    (repo / "src.py").write_text("swept\n", encoding="utf-8")
    (repo / ".ergon" / "log.jsonl").write_text("seed\npark\nmore\n", encoding="utf-8")
    git(repo, "commit", "-am", "pinax: item.noted tst-1")
    with pytest.raises(EvidenceError, match="non-tracker paths: src.py"):
        require_committed(git, repo, after, (".ergon",), published)


PINAX_SCRIPT = '''
import sys
from pathlib import Path

root = Path(sys.argv[sys.argv.index("--root") + 1])
(root / ".ergon" / "log" / "late.jsonl").write_text("event\\n", encoding="utf-8")
sys.stderr.write(sys.argv[-1] + "\\n")
raise SystemExit(7)
'''


@pytest.mark.parametrize(
    ("refusal", "completed"),
    (
        ("error: unable to write file .git/objects/ab/cdef: Permission denied", True),
        ("pre-commit hook: Permission denied to commit .ergon on this branch", False),
    ),
)
def test_the_scanner_retry_finishes_only_an_object_write_refusal(
    tmp_path: Path, refusal: str, completed: bool
) -> None:
    from scanner_retry import run_retrying

    repo = repository(tmp_path)
    script = tmp_path / "pinax.py"
    script.write_text(PINAX_SCRIPT, encoding="utf-8")
    head = git(repo, "rev-parse", "HEAD")

    result = run_retrying((sys.executable, str(script), "--root", str(repo), refusal), cwd=repo)

    assert (result.returncode == 0) is completed
    assert (git(repo, "rev-parse", "HEAD") != head) is completed
    if not completed:
        assert result.returncode == 7
        assert git(repo, "status", "--porcelain", "--", ".ergon") != ""
