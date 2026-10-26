"""Serialised delivery against real Git, real Pinax and a bare local origin.

Every scenario delivers through the real workspace and tracker adapters. The
item scenarios run the whole item workflow with a scripted builder and
reviewer and a validator that really runs in the item worktree, so the number
of validator runs and the commit each one saw are observed, not assumed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest
from scanner_retry import run_retrying

from autobuild.adapters import GitWorkspaceAdapter, PinaxTrackerAdapter
from autobuild.application import ItemWorkflow, WorkflowPorts
from autobuild.domain import (
    AdapterError,
    AdapterIdentity,
    BuilderReport,
    CampaignRef,
    CloseEvidence,
    CommandRequest,
    CommandResult,
    DeliveryMode,
    DeliveryRequest,
    FinaliseRequest,
    ItemDisposition,
    ItemExecutionSpec,
    ReviewDecision,
    ReviewVerdict,
    Seat,
    SeatOutcome,
    SeatRequest,
    SeatResult,
    SeatUsage,
    ToolPolicy,
    ValidationEvidence,
    WorkItem,
)
from autobuild.testing import FakeHarnessAdapter, FakeKnowledgeAdapter, FakeRunRecordAdapter

pytestmark = pytest.mark.usefixtures("self_committing_pinax")

# The validator fails when the shared library file carries the word "broken".
VALIDATOR = (
    sys.executable,
    "-c",
    "import pathlib, sys; "
    "sys.exit(1 if 'broken' in pathlib.Path('lib.txt').read_text(encoding='utf-8') else 0)",
)


def run(*argv: str, cwd: Path | None = None) -> str:
    completed = run_retrying(argv, cwd=cwd)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"{' '.join(argv)} failed: {detail}")
    return completed.stdout.strip()


def git(root: Path, *args: str) -> str:
    return run("git", "-C", str(root), *args)


def pinax(root: Path, *args: str) -> str:
    return run("pinax", "--root", str(root), *args, cwd=root)


def identity(name: str) -> AdapterIdentity:
    return AdapterIdentity(name, "1", frozenset({"test"}))


@dataclass
class Origin:
    repo: Path
    remote: Path
    items: list[str]
    tmp: Path

    def head(self) -> str:
        return git(self.repo, "ls-remote", str(self.remote), "refs/heads/main").split()[0]

    def clone(self, name: str) -> Path:
        other = self.tmp / name
        run("git", "clone", "--quiet", str(self.remote), str(other))
        git(other, "config", "user.name", "Upstream Writer")
        git(other, "config", "user.email", "upstream@example.invalid")
        return other

    def push_change(self, name: str, path: str, content: str) -> str:
        """Another writer commits ``content`` to ``path`` on origin main."""

        other = self.clone(name)
        (other / path).parent.mkdir(parents=True, exist_ok=True)
        (other / path).write_text(content, encoding="utf-8")
        git(other, "add", path)
        git(other, "commit", "-m", f"upstream change to {path}")
        git(other, "push", "origin", "HEAD:main")
        return git(other, "rev-parse", "HEAD")

    def remote_claim(self, name: str, item_id: str) -> str:
        """Another writer claims ``item_id`` in its own clone; Pinax publishes it."""

        other = self.clone(name)
        pinax(other, "claim", item_id, "--actor", "builder@other", "--json")
        return git(other, "rev-parse", "HEAD")

    def status(self) -> dict:
        return json.loads(pinax(self.repo, "status", "--json"))


def pinax_origin(tmp_path: Path, count: int = 2) -> Origin:
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    run("git", "init", "--bare", str(remote))
    run("git", "init", "-b", "main", str(repo))
    git(repo, "config", "user.name", "AutoBuild Test")
    git(repo, "config", "user.email", "autobuild@example.invalid")
    (repo / "lib.txt").write_text("ok\n", encoding="utf-8")
    (repo / "docs").mkdir()
    (repo / "docs" / "brief.md").write_text(
        "# Brief\n\n## Acceptance\n\n- The feature reaches the default branch.\n",
        encoding="utf-8",
    )
    pinax(repo, "init", "--actor", "coordinator@proof")
    items: list[str] = []
    for index in range(count):
        created = json.loads(
            pinax(
                repo, "add", "--title", f"Item {index}", "--prefix", "tst",
                "--actor", "coordinator@proof", "--json",
            )
        )
        item_id = str(created.get("item_id") or created["id"])
        pinax(
            repo, "note", "add", item_id, "--ref", "docs/brief.md", "--caption",
            "The feature reaches the default branch.", "--actor", "coordinator@proof", "--json",
        )
        items.append(item_id)
    git(repo, "add", ".")
    git(repo, "commit", "-m", "initial")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-u", "origin", "main")
    git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    return Origin(repo, remote, items, tmp_path)


@dataclass
class ValidatorRuns:
    """A command port that really runs the validator in the item worktree and
    records the commit and the feature file each run saw."""

    heads: list[str] = field(default_factory=list)
    features: list[str] = field(default_factory=list)
    exit_codes: list[int] = field(default_factory=list)

    def probe(self):  # pragma: no cover - the workflow never probes
        raise AssertionError("not probed")

    def run(self, request: CommandRequest) -> CommandResult:
        completed = subprocess.run(
            list(request.argv), cwd=request.cwd, capture_output=True, text=True, check=False
        )
        self.heads.append(git(request.cwd, "rev-parse", "HEAD"))
        feature = request.cwd / "feature.txt"
        self.features.append(feature.read_text(encoding="utf-8") if feature.exists() else "")
        self.exit_codes.append(completed.returncode)
        return CommandResult(
            request.command_id, completed.returncode, "stdout", "stderr", "start", "end"
        )

    def cancel(self, command_id: str) -> None:  # pragma: no cover
        return None


def _seat(run_id: str, payload) -> SeatResult:
    return SeatResult(
        run_id, SeatOutcome.SUCCEEDED, payload, f"raw:{run_id}", SeatUsage(source="fake"),
        "start", "end",
    )


def scripted_harness(
    item_id: str, after_review=None, feature_path: str = "feature.txt"
) -> FakeHarnessAdapter:
    """A builder that writes ``feature_path`` and a reviewer that passes; the
    optional hook runs when the reviewer is invoked, after validation."""

    def on_invoke(request: SeatRequest) -> None:
        if request.seat is Seat.BUILDER:
            target = request.workspace.root / feature_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"feature for {item_id}\n", encoding="utf-8")
        elif request.seat is Seat.REVIEWER and after_review is not None:
            after_review()

    return FakeHarnessAdapter(
        identity("harness"),
        scripted_results=[
            _seat("builder", BuilderReport("report:builder", "built")),
            _seat("reviewer", ReviewVerdict(item_id, ReviewDecision.PASS, (), "verdict")),
        ],
        on_invoke=on_invoke,
    )


def item_spec(origin: Origin, item_id: str) -> ItemExecutionSpec:
    return ItemExecutionSpec(
        WorkItem(item_id, f"Deliver {item_id}", "docs/brief.md", ("delivered",)),
        Path("docs/brief.md"),
        "tests",
        VALIDATOR,
        ToolPolicy(frozenset({"read", "write", "python", "git"}), (origin.repo,)),
        "builder",
        "reviewer",
        "specialist",
        delivery_mode=DeliveryMode.PROTECTED_DEFAULT,
        delivery_target_branch="main",
        delivery_target_revision=git(origin.repo, "rev-parse", "HEAD"),
    )


def run_item(
    origin: Origin, workspace: GitWorkspaceAdapter, tracker, harness, validator, brief_text=""
):
    ports = WorkflowPorts(
        tracker,
        workspace,
        harness,
        validator,
        FakeRunRecordAdapter(identity("records")),
        FakeKnowledgeAdapter(identity("knowledge")),
    )
    campaign = CampaignRef("delivery", origin.repo)
    item_id = origin.items[0]
    work = replace(item_spec(origin, item_id), brief_text=brief_text)
    outcome = ItemWorkflow(ports).run(campaign, work, ports.records.create(campaign))
    return outcome, ports


def default_adapters(origin: Origin):
    """The adapters wired as composition wires them: one shared lock, and the
    tracker as the projection the workspace regenerates."""

    lock = threading.Lock()
    tracker = PinaxTrackerAdapter(origin.repo, primary_lock=lock)
    workspace = GitWorkspaceAdapter(
        origin.tmp / "scratch", tracker_commits_itself=True, primary_lock=lock, projection=tracker
    )
    return tracker, workspace


def product_diff(root: Path, base: str, head: str) -> list[str]:
    return git(root, "diff", "--name-only", base, head, "--", ".", ":(exclude).ergon").split()


def ancestor(root: Path, commit: str, of: str) -> bool:
    return (
        subprocess.run(
            ["git", "-C", str(root), "merge-base", "--is-ancestor", commit, of],
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


def _status_ids(status: dict, key: str) -> set[str]:
    return {entry["id"] for entry in status["repo"].get(key, [])}


def item_status(origin: Origin, item_id: str) -> str:
    """The item's status as the Pinax projection on the primary records it."""

    text = (origin.repo / ".ergon" / "items" / f"{item_id}.md").read_text(encoding="utf-8")
    line = next(entry for entry in text.splitlines() if entry.startswith("status:"))
    return line.split(":", 1)[1].strip()


# --- (a) Base moved outside the tracker after validation -------------------


def test_a_base_moved_revalidates_once_and_pushes_the_revalidated_commit(tmp_path: Path) -> None:
    origin = pinax_origin(tmp_path)
    tracker, workspace = default_adapters(origin)
    validator = ValidatorRuns()
    harness = scripted_harness(
        origin.items[0],
        after_review=lambda: origin.push_change("upstream", "upstream.txt", "unrelated\n"),
    )

    outcome, _ = run_item(origin, workspace, tracker, harness, validator)

    assert outcome.disposition is ItemDisposition.ACCEPTED, outcome.reason
    assert validator.exit_codes == [0, 0]
    assert origin.head() == validator.heads[-1]
    assert outcome.finalise.merged_commit == validator.heads[-1]
    assert git(origin.repo, "show", f"{origin.head()}:upstream.txt") == "unrelated"
    assert git(origin.repo, "show", f"{origin.head()}:feature.txt") == f"feature for {origin.items[0]}"


# --- (b) Base unchanged ----------------------------------------------------


def test_b_an_unchanged_base_validates_once_and_pushes_the_validated_tree(tmp_path: Path) -> None:
    origin = pinax_origin(tmp_path)
    tracker, workspace = default_adapters(origin)
    validator = ValidatorRuns()

    outcome, _ = run_item(origin, workspace, tracker, scripted_harness(origin.items[0]), validator)

    assert outcome.disposition is ItemDisposition.ACCEPTED, outcome.reason
    assert len(validator.exit_codes) == 1
    pushed = origin.head()
    assert pushed == outcome.finalise.merged_commit
    assert product_diff(origin.repo, outcome.finalise.item_commit, pushed) == []
    assert git(origin.repo, "show", f"{pushed}:feature.txt") + "\n" == validator.features[0]


# --- (b2) Only a tracker commit moved the base -----------------------------


def test_b2_a_tracker_only_move_on_origin_is_not_revalidated(tmp_path: Path) -> None:
    origin = pinax_origin(tmp_path)
    tracker, workspace = default_adapters(origin)
    validator = ValidatorRuns()
    claims: list[str] = []
    harness = scripted_harness(
        origin.items[0],
        after_review=lambda: claims.append(origin.remote_claim("claimer", origin.items[1])),
    )

    outcome, _ = run_item(origin, workspace, tracker, harness, validator)

    assert outcome.disposition is ItemDisposition.ACCEPTED, outcome.reason
    assert len(validator.exit_codes) == 1
    pushed = origin.head()
    assert ancestor(origin.repo, claims[0], pushed)
    assert product_diff(origin.repo, outcome.finalise.item_commit, pushed) == []


# --- revalidation only on overlap -------------------------------------------


def _revalidation_events(ports) -> list[dict]:
    return [
        event.payload
        for event in ports.records.events
        if event.event_type == "delivery.revalidation"
    ]


def test_ra_a_docs_only_move_on_origin_is_not_revalidated(tmp_path: Path) -> None:
    origin = pinax_origin(tmp_path)
    tracker, workspace = default_adapters(origin)
    validator = ValidatorRuns()
    harness = scripted_harness(
        origin.items[0],
        after_review=lambda: origin.push_change("writer", "docs/notes.txt", "notes\n"),
        feature_path="apps/web/feature.txt",
    )

    outcome, ports = run_item(origin, workspace, tracker, harness, validator)

    assert outcome.disposition is ItemDisposition.ACCEPTED, outcome.reason
    assert len(validator.exit_codes) == 1
    pushed = origin.head()
    assert pushed == outcome.finalise.merged_commit
    assert git(origin.repo, "show", f"{pushed}:docs/notes.txt") == "notes"
    assert [(entry["run"], entry["reason"]) for entry in _revalidation_events(ports)] == [
        (False, "docs_only")
    ]


def test_rb_a_move_in_the_same_package_revalidates_once(tmp_path: Path) -> None:
    origin = pinax_origin(tmp_path)
    tracker, workspace = default_adapters(origin)
    validator = ValidatorRuns()
    harness = scripted_harness(
        origin.items[0],
        after_review=lambda: origin.push_change("writer", "apps/web/other.txt", "other\n"),
        feature_path="apps/web/feature.txt",
    )

    outcome, ports = run_item(origin, workspace, tracker, harness, validator)

    assert outcome.disposition is ItemDisposition.ACCEPTED, outcome.reason
    assert validator.exit_codes == [0, 0]
    assert origin.head() == validator.heads[-1] == outcome.finalise.merged_commit
    decisions = _revalidation_events(ports)
    assert [(entry["run"], entry["reason"]) for entry in decisions] == [(True, "overlap")]
    assert decisions[0]["overlapping"] == ["apps/web/other.txt"]


def test_rc_a_move_in_another_package_is_not_revalidated(tmp_path: Path) -> None:
    origin = pinax_origin(tmp_path)
    tracker, workspace = default_adapters(origin)
    validator = ValidatorRuns()
    harness = scripted_harness(
        origin.items[0],
        after_review=lambda: origin.push_change("writer", "apps/api/other.txt", "other\n"),
        feature_path="apps/web/feature.txt",
    )

    outcome, ports = run_item(origin, workspace, tracker, harness, validator)

    assert outcome.disposition is ItemDisposition.ACCEPTED, outcome.reason
    assert len(validator.exit_codes) == 1
    pushed = origin.head()
    assert pushed == outcome.finalise.merged_commit
    assert git(origin.repo, "show", f"{pushed}:apps/api/other.txt") == "other"
    assert git(origin.repo, "show", f"{pushed}:apps/web/feature.txt") == (
        f"feature for {origin.items[0]}"
    )
    decisions = _revalidation_events(ports)
    assert [(entry["run"], entry["reason"]) for entry in decisions] == [(False, "no_overlap")]
    assert decisions[0]["incoming"] == ["apps/api/other.txt"]


def test_rd_a_move_in_a_declared_package_revalidates(tmp_path: Path) -> None:
    origin = pinax_origin(tmp_path)
    tracker, workspace = default_adapters(origin)
    validator = ValidatorRuns()
    harness = scripted_harness(
        origin.items[0],
        after_review=lambda: origin.push_change("writer", "apps/api/other.txt", "other\n"),
        feature_path="apps/web/feature.txt",
    )
    brief = "# Brief\n\n## Declared paths\n\n`apps/web/feature.txt`, `apps/api/**`\n"

    outcome, _ports = run_item(origin, workspace, tracker, harness, validator, brief_text=brief)

    assert outcome.disposition is ItemDisposition.ACCEPTED, outcome.reason
    assert validator.exit_codes == [0, 0]


def test_re_a_validation_checkout_is_a_detached_worktree_at_the_target_head(
    tmp_path: Path,
) -> None:
    origin = pinax_origin(tmp_path)
    _tracker, workspace = default_adapters(origin)

    checkout = workspace.open_validation_checkout(origin.repo, None)

    assert checkout.revision == git(origin.repo, "rev-parse", "refs/heads/main")
    assert git(checkout.root, "rev-parse", "HEAD") == checkout.revision
    assert git(checkout.root, "branch", "--show-current") == ""
    assert (checkout.root / "lib.txt").is_file()
    workspace.close_validation_checkout(checkout)
    assert not checkout.root.exists()
    assert str(checkout.root.name) not in git(origin.repo, "worktree", "list", "--porcelain")
    assert git(origin.repo, "status", "--porcelain") == ""


def test_rf_a_refreshing_checkout_fast_forwards_to_a_fix_pushed_to_origin(
    tmp_path: Path,
) -> None:
    origin = pinax_origin(tmp_path)
    _tracker, workspace = default_adapters(origin)
    failing = workspace.open_validation_checkout(origin.repo, None)
    workspace.close_validation_checkout(failing)
    fix = origin.push_change("operator", "lib.txt", "fixed\n")

    stale = workspace.open_validation_checkout(origin.repo, None)
    workspace.close_validation_checkout(stale)
    fresh = workspace.open_validation_checkout(origin.repo, None, refresh=True)

    assert stale.revision == failing.revision
    assert fresh.revision == fix
    assert (fresh.root / "lib.txt").read_text(encoding="utf-8") == "fixed\n"
    workspace.close_validation_checkout(fresh)
    assert git(origin.repo, "rev-parse", "refs/heads/main") == fix
    assert git(origin.repo, "status", "--porcelain") == ""


def test_rg_a_refresh_never_moves_a_local_head_the_remote_lacks(tmp_path: Path) -> None:
    origin = pinax_origin(tmp_path)
    _tracker, workspace = default_adapters(origin)
    (origin.repo / "local.txt").write_text("local\n", encoding="utf-8")
    git(origin.repo, "add", "local.txt")
    git(origin.repo, "commit", "-m", "local only")
    local = git(origin.repo, "rev-parse", "HEAD")

    checkout = workspace.open_validation_checkout(origin.repo, None, refresh=True)
    workspace.close_validation_checkout(checkout)

    assert checkout.revision == local
    assert git(origin.repo, "rev-parse", "refs/heads/main") == local


# --- (c) Two closes on two branches, delivered one after the other ---------


def _close_in_worktree(origin: Origin, tracker, workspace_adapter, item_id: str):
    campaign = CampaignRef("two-closes", origin.repo)
    item = WorkItem(item_id, f"Item {item_id}", "docs/brief.md", ("delivered",))
    tracker.claim(item, "builder@proof")
    workspace = workspace_adapter.create_isolated(campaign, item)
    (workspace.root / f"{item_id}.txt").write_text(f"{item_id}\n", encoding="utf-8")
    diff = workspace_adapter.diff(workspace)
    command = CommandResult("run:item:tests", 0, "stdout", "stderr", "start", "end")
    evidence = CloseEvidence(
        item_id,
        diff.workspace_revision,
        diff,
        ValidationEvidence("tests", diff.workspace_revision, command, diff.changed_paths),
        ReviewVerdict(item_id, ReviewDecision.PASS, (), "review"),
        "trajectory",
    )
    item_commit = workspace_adapter.commit_item(
        workspace, FinaliseRequest(item_id, evidence, f"ADDED: {item_id}")
    )
    tracker.close(evidence, item_commit, workspace, "coordinator@proof")
    tracker_commit = workspace_adapter.commit_tracker(workspace, item_id, item_commit)
    return workspace, item_commit, tracker_commit


def _request(origin: Origin, item_id: str, item_commit, tracker_commit, revalidate=None):
    return DeliveryRequest(
        item_id,
        item_commit,
        tracker_commit,
        DeliveryMode.PROTECTED_DEFAULT,
        "main",
        git(origin.repo, "rev-parse", "HEAD"),
        revalidate=revalidate,
    )


def test_c_two_closed_branches_deliver_one_after_the_other(tmp_path: Path) -> None:
    origin = pinax_origin(tmp_path)
    lock = threading.Lock()
    tracker = PinaxTrackerAdapter(origin.repo, primary_lock=lock)
    workspace_adapter = GitWorkspaceAdapter(
        tmp_path / "scratch", tracker_commits_itself=True, primary_lock=lock, projection=tracker
    )
    first = _close_in_worktree(origin, tracker, workspace_adapter, origin.items[0])
    second = _close_in_worktree(origin, tracker, workspace_adapter, origin.items[1])
    revalidations: list[str] = []

    def revalidate(workspace):
        return lambda: revalidations.append(git(workspace.root, "rev-parse", "HEAD")) or True

    results = []
    for (workspace, item_commit, tracker_commit), item_id in zip((first, second), origin.items):
        results.append(
            workspace_adapter.deliver(
                workspace,
                _request(origin, item_id, item_commit, tracker_commit, revalidate(workspace)),
            )
        )
        workspace_adapter.release(workspace)

    assert [result.pushed for result in results] == [True, True]
    assert origin.head() == results[1].merged_commit
    assert ancestor(origin.repo, results[0].merged_commit, results[1].merged_commit)
    assert run_retrying(("pinax", "--root", str(origin.repo), "verify"), cwd=origin.repo).returncode == 0
    shipped = _status_ids(origin.status(), "shipped_recent")
    assert set(origin.items) <= shipped
    # The second delivery brought the first item's product in, so it was
    # validated again; the first delivery merged only tracker state.
    assert revalidations == [results[1].merged_commit]


# --- (d) A real conflict ---------------------------------------------------


def test_d_a_source_conflict_parks_delivery_conflict_and_pushes_nothing(tmp_path: Path) -> None:
    origin = pinax_origin(tmp_path)
    tracker, workspace = default_adapters(origin)
    validator = ValidatorRuns()
    upstream: list[str] = []
    harness = scripted_harness(
        origin.items[0],
        after_review=lambda: upstream.append(
            origin.push_change("upstream", "feature.txt", "a different feature\n")
        ),
    )

    outcome, ports = run_item(origin, workspace, tracker, harness, validator)

    assert outcome.disposition is ItemDisposition.PARKED
    assert outcome.reason.startswith("delivery_conflict")
    assert "feature.txt" in outcome.reason
    head = origin.head()
    assert product_diff(origin.repo, upstream[0], head) == []
    item_commits = [
        line for line in git(origin.repo, "log", "--all", "--format=%H %s").splitlines()
        if line.endswith(f"Deliver {origin.items[0]}")
    ]
    assert item_commits and not any(ancestor(origin.repo, line.split()[0], head) for line in item_commits)
    created = next(
        event for event in ports.records.events if event.event_type == "workspace.created"
    )
    worktree = Path(created.payload["worktree_root"])
    assert worktree.exists()
    assert git(worktree, "status", "--porcelain") == ""
    assert not (Path(git(worktree, "rev-parse", "--git-dir")) / "MERGE_HEAD").exists()
    assert item_status(origin, origin.items[0]) == "parked"


# --- (e) A contended push --------------------------------------------------


def test_e_three_rejected_pushes_park_delivery_contended_after_three_fetches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = pinax_origin(tmp_path)
    tracker, workspace = default_adapters(origin)
    fetches: list[tuple[str, ...]] = []
    original = workspace._git

    def contended(root: Path, *args: str, **kwargs) -> str:
        if args and args[0] == "fetch":
            fetches.append(args)
        if args and args[0] == "push":
            raise AdapterError(
                "git push failed: ! [rejected] main -> main (fetch first)\n"
                "error: failed to push some refs"
            )
        return original(root, *args, **kwargs)

    monkeypatch.setattr(workspace, "_git", contended)
    validator = ValidatorRuns()

    outcome, _ = run_item(origin, workspace, tracker, scripted_harness(origin.items[0]), validator)

    assert outcome.disposition is ItemDisposition.PARKED
    assert outcome.reason.startswith("delivery_contended")
    assert "! [rejected] main -> main (fetch first)" in outcome.reason
    assert len(fetches) == 3
    assert len(validator.exit_codes) == 1


def origin_item_status(origin: Origin, item_id: str) -> str:
    """The item's status as the Pinax projection on origin's head records it."""

    text = git(origin.repo, "show", f"{origin.head()}:.ergon/items/{item_id}.md")
    line = next(entry for entry in text.splitlines() if entry.startswith("status:"))
    return line.split(":", 1)[1].strip()


def on_origin(origin: Origin, path: str) -> bool:
    completed = subprocess.run(
        ["git", "-C", str(origin.repo), "cat-file", "-e", f"{origin.head()}:{path}"],
        capture_output=True,
        check=False,
    )
    return completed.returncode == 0


def test_e2_a_contended_park_publishes_none_of_the_items_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every push AutoBuild makes is rejected. The park that follows is a
    Pinax write on the primary checkout, which pushes the local target, so the
    local target must not hold the item's commits."""

    origin = pinax_origin(tmp_path)
    tracker, workspace = default_adapters(origin)
    original = workspace._git

    def rejected(root: Path, *args: str, **kwargs) -> str:
        if args and args[0] == "push":
            raise AdapterError("git push failed: ! [rejected] main -> main (fetch first)")
        return original(root, *args, **kwargs)

    monkeypatch.setattr(workspace, "_git", rejected)

    outcome, _ = run_item(
        origin, workspace, tracker, scripted_harness(origin.items[0]), ValidatorRuns()
    )

    assert outcome.disposition is ItemDisposition.PARKED
    assert outcome.reason.startswith("delivery_contended")
    assert not on_origin(origin, "feature.txt")
    assert origin_item_status(origin, origin.items[0]) == "parked"


def test_f2_a_rejected_push_then_a_failing_revalidation_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first push is rejected because another writer broke the library;
    the second attempt validates the merged candidate, which fails. Neither
    the item nor the failing combination may reach origin."""

    origin = pinax_origin(tmp_path)
    tracker, workspace = default_adapters(origin)
    original = workspace._git
    pushes: list[tuple[str, ...]] = []

    def reject_once(root: Path, *args: str, **kwargs) -> str:
        if args and args[0] == "push" and not pushes:
            pushes.append(args)
            origin.push_change("upstream", "lib.txt", "broken by another writer\n")
            raise AdapterError("git push failed: ! [rejected] main -> main (fetch first)")
        return original(root, *args, **kwargs)

    monkeypatch.setattr(workspace, "_git", reject_once)
    validator = ValidatorRuns()

    outcome, _ = run_item(origin, workspace, tracker, scripted_harness(origin.items[0]), validator)

    assert outcome.disposition is ItemDisposition.PARKED
    assert outcome.reason.startswith("delivery_revalidation_failed")
    assert validator.exit_codes == [0, 1]
    assert not on_origin(origin, "feature.txt")
    assert origin_item_status(origin, origin.items[0]) == "parked"


# --- (f) The merged candidate fails its tests ------------------------------


def test_f_a_failing_merged_candidate_parks_and_pushes_nothing(tmp_path: Path) -> None:
    origin = pinax_origin(tmp_path)
    tracker, workspace = default_adapters(origin)
    validator = ValidatorRuns()
    upstream: list[str] = []
    harness = scripted_harness(
        origin.items[0],
        after_review=lambda: upstream.append(
            origin.push_change("upstream", "lib.txt", "broken by another writer\n")
        ),
    )

    outcome, ports = run_item(origin, workspace, tracker, harness, validator)

    assert outcome.disposition is ItemDisposition.PARKED
    assert outcome.reason.startswith("delivery_revalidation_failed")
    assert validator.exit_codes == [0, 1]
    # The worktree head is the completed integration merge; the park evidence
    # still describes only the item's own change, not the upstream lib.txt.
    manifest = json.loads(ports.records.evidence_files[f"{origin.items[0]}-park/snapshot.json"])
    assert [entry["path"] for entry in manifest["tracked_paths"]] == ["feature.txt"]
    patch = ports.records.evidence_files[f"{origin.items[0]}-park/changes.patch"].decode("utf-8")
    assert "feature.txt" in patch and "lib.txt" not in patch
    head = origin.head()
    assert product_diff(origin.repo, upstream[0], head) == []
    assert git(origin.repo, "show", f"{head}:lib.txt") == "broken by another writer"
    assert origin.items[0] not in _status_ids(origin.status(), "shipped_recent")
    assert item_status(origin, origin.items[0]) == "parked"


# --- (g) A park and a delivery at the same time ----------------------------


def test_g_a_park_and_a_delivery_on_two_threads_both_complete(tmp_path: Path) -> None:
    origin = pinax_origin(tmp_path, count=3)
    lock = threading.Lock()
    tracker = PinaxTrackerAdapter(origin.repo, primary_lock=lock)
    workspace_adapter = GitWorkspaceAdapter(
        tmp_path / "scratch", tracker_commits_itself=True, primary_lock=lock, projection=tracker
    )
    workspace, item_commit, tracker_commit = _close_in_worktree(
        origin, tracker, workspace_adapter, origin.items[0]
    )
    tracker.claim(
        WorkItem(origin.items[1], "Item 1", "docs/brief.md", ("parked",)), "builder@proof"
    )
    start = threading.Barrier(2)
    errors: list[BaseException] = []
    results: list[object] = []

    def park() -> None:
        try:
            start.wait()
            results.append(tracker.park(origin.items[1], "parked by the test", "coordinator@proof"))
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    def deliver() -> None:
        try:
            start.wait()
            results.append(
                workspace_adapter.deliver(
                    workspace,
                    _request(origin, origin.items[0], item_commit, tracker_commit, lambda: True),
                )
            )
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=park), threading.Thread(target=deliver)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(120)

    assert errors == []
    assert len(results) == 2
    assert not any("index.lock" in str(result) for result in results)
    verify = run_retrying(("pinax", "--root", str(origin.repo), "verify"), cwd=origin.repo)
    assert verify.returncode == 0, verify.stdout + verify.stderr
    assert git(origin.repo, "status", "--porcelain") == ""
    workspace_adapter.release(workspace)


# --- (h) A claim lands on the primary during delivery ----------------------


class _ClaimOnNextHold:
    """A primary-checkout lock that, once armed, lands a real Pinax claim on
    the primary checkout just before the next holder takes the lock. The claim
    therefore arrives after integration and before the fast-forward."""

    def __init__(self, origin: Origin, item_id: str) -> None:
        self._inner = threading.Lock()
        self._origin = origin
        self._item_id = item_id
        self.armed = False
        self.claim = ""

    def __enter__(self):
        if self.armed:
            self.armed = False
            pinax(self._origin.repo, "claim", self._item_id, "--actor", "builder@other", "--json")
            self.claim = git(self._origin.repo, "rev-parse", "HEAD")
        return self._inner.__enter__()

    def __exit__(self, *exc):
        return self._inner.__exit__(*exc)


def test_h_a_claim_between_integration_and_fast_forward_is_carried_into_the_push(
    tmp_path: Path,
) -> None:
    origin = pinax_origin(tmp_path)
    lock = _ClaimOnNextHold(origin, origin.items[1])
    tracker = PinaxTrackerAdapter(origin.repo)
    workspace = GitWorkspaceAdapter(
        tmp_path / "scratch", tracker_commits_itself=True, primary_lock=lock, projection=tracker
    )
    validator = ValidatorRuns()
    pushes = count_pushes(workspace)

    def arm() -> None:
        lock.armed = True

    harness = scripted_harness(origin.items[0], after_review=arm)

    outcome, _ = run_item(origin, workspace, tracker, harness, validator)

    assert outcome.disposition is ItemDisposition.ACCEPTED, outcome.reason
    assert lock.claim
    assert len(validator.exit_codes) == 1
    # The claim was merged in under the lock, so the first push was accepted.
    assert len(pushes) == 1
    pushed = origin.head()
    assert pushed == outcome.finalise.merged_commit
    assert ancestor(origin.repo, lock.claim, pushed)
    assert product_diff(origin.repo, outcome.finalise.item_commit, pushed) == []


def test_h_guard_a_fast_forward_without_the_in_lock_tracker_merge_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wrong-answer guard for (h): with the in-lock tracker merge disabled
    the candidate lacks the claim Pinax already published, so the first push
    is rejected and (h)'s single-push assertion fails."""

    origin = pinax_origin(tmp_path)
    lock = _ClaimOnNextHold(origin, origin.items[1])
    tracker = PinaxTrackerAdapter(origin.repo)
    workspace = GitWorkspaceAdapter(
        tmp_path / "scratch", tracker_commits_itself=True, primary_lock=lock, projection=tracker
    )
    monkeypatch.setattr(
        workspace, "_merge_tracker_gap", lambda *args, **kwargs: None, raising=True
    )

    pushes = count_pushes(workspace)

    def arm() -> None:
        lock.armed = True

    outcome, _ = run_item(
        origin, workspace, tracker, scripted_harness(origin.items[0], after_review=arm), ValidatorRuns()
    )

    assert lock.claim
    assert outcome.disposition is not ItemDisposition.ACCEPTED or len(pushes) > 1


def count_pushes(workspace: GitWorkspaceAdapter) -> list[tuple[str, ...]]:
    """Record every push the workspace adapter makes."""

    pushes: list[tuple[str, ...]] = []
    original = workspace._git

    def recording(root: Path, *args: str, **kwargs) -> str:
        if args and args[0] == "push":
            pushes.append(args)
        return original(root, *args, **kwargs)

    workspace._git = recording
    return pushes
