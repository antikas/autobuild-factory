"""Git worktree adapter with scoped commits and remotely verified delivery."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Lock
from typing import TypeVar
from uuid import uuid4

from autobuild.domain import (
    AdapterError,
    AdapterIdentity,
    CampaignRef,
    CampaignReport,
    ChangedPath,
    ChangeKind,
    DeliveryMode,
    DeliveryRequest,
    DiffEvidence,
    EvidenceError,
    FinaliseRequest,
    FinaliseResult,
    ProbeResult,
    RepositoryIdentity,
    SnapshotFile,
    WorkItem,
    WorkspaceRef,
    WorktreeSnapshot,
    WorktreeStatus,
)
from autobuild.domain.models import DeliveryParked, DeliveryParkReason, ValidationCheckout
from autobuild.adapters.tracker_commits import (
    require_one_tracker_commit,
)
from autobuild.enforcement.integration import revalidation_decision
from autobuild.ports.contracts import TrackerProjectionPort

# The subject every delivery integration merge carries. A head invariant steps
# over a two-parent commit with this subject to reach the tracker commit.
INTEGRATION_SUBJECT = "MERGED: integrate"
# Source moves of the target branch and rejected pushes, together, allowed per
# delivery before the item parks as contended.
DELIVERY_ATTEMPTS = 3
# Fetches tried per attempt when a concurrent writer holds a remote-tracking ref.
FETCH_ATTEMPTS = 3
_REF_LOCK_SIGNS = ("cannot lock ref", "unable to create", "unable to lock")
# A push that lost a race to another writer: the remote refused a non-fast-forward
# update, or its server-side ref lock was held by a concurrent push. A hook,
# protected-branch or unpacker refusal is not a race and is raised.
_PUSH_REJECTED_SIGNS = ("[rejected]", "non-fast-forward", "fetch first", "cannot lock ref")
_ABSENT_REMOTE_REF_SIGN = "couldn't find remote ref"
_MAX_INTEGRATION_MERGES = 64
# Tracker recoveries one locked delivery step may run when it finds the primary
# checkout dirty with tracker files only.
LEFTOVER_RECOVERIES = 3
_T = TypeVar("_T")


class _TrackerLeftover(AdapterError):
    """The primary checkout is dirty with tracker files only: a tracker write
    was appended and not committed. The tracker's recovery can finish it."""


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    return cleaned or "item"


@dataclass(frozen=True, slots=True)
class _Lease:
    repository: RepositoryIdentity
    workspace: WorkspaceRef


class GitWorkspaceAdapter:
    def __init__(
        self,
        scratch_root: Path,
        remote: str = "origin",
        tracker_paths: tuple[Path | str, ...] = (".ergon",),
        tracker_commits_itself: bool = False,
        primary_lock: AbstractContextManager[object] | None = None,
        projection: TrackerProjectionPort | None = None,
        tracker_recovery: Callable[[], object] | None = None,
    ) -> None:
        """``primary_lock`` is the one lock every local mutation of the primary
        checkout holds; composition shares it with the tracker adapter. It covers
        a worktree add or remove, the delivery fast-forward and the report commit,
        never a fetch, a push or a validator run. ``projection`` lets delivery
        regenerate a conflicted tracker projection from the merged log.
        ``tracker_recovery`` is the tracker's own recovery of a write it
        appended and did not commit; a locked delivery or report step that
        finds the primary dirty with tracker files only lets the lock go, runs
        it and tries again. It takes the primary-checkout lock itself, so it is
        never called under it."""

        self._primary_lock = primary_lock if primary_lock is not None else Lock()
        self._projection = projection
        self._tracker_recovery = tracker_recovery
        self._scratch_root = scratch_root.resolve(strict=False)
        self._remote = remote
        self._tracker_commits_itself = tracker_commits_itself
        self._tracker_paths = tuple(
            Path(path).as_posix().strip("/") for path in tracker_paths
        )
        if not self._tracker_paths or any(not path or path == "." for path in self._tracker_paths):
            raise ValueError("tracker_paths must contain repository-relative paths")
        self._leases: dict[str, _Lease] = {}
        self._lock = Lock()

    def probe(self) -> ProbeResult:
        try:
            completed = subprocess.run(
                ["git", "--version"], capture_output=True, text=True, check=False
            )
            self._scratch_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return ProbeResult.unavailable(f"git workspace is unavailable: {exc}")
        if completed.returncode != 0:
            return ProbeResult.unavailable(completed.stderr.strip() or "git probe failed")
        return ProbeResult.ready(
            AdapterIdentity(
                "git-workspace",
                completed.stdout.strip() or "unknown",
                frozenset({"worktree", "scoped-commit", "push-verify", "process-isolation"}),
            ),
            str(self._scratch_root),
        )

    def identify(self, root: Path) -> RepositoryIdentity:
        resolved = Path(self._git(root, "rev-parse", "--show-toplevel")).resolve(strict=True)
        revision = self._git(resolved, "rev-parse", "HEAD")
        default_branch = self._default_branch(resolved)
        current_branch = self._git(resolved, "branch", "--show-current")
        remotes = self._git(resolved, "remote").splitlines()
        if self._remote not in remotes:
            raise AdapterError(f"required git remote is missing: {self._remote}")
        return RepositoryIdentity(
            resolved, default_branch, current_branch, self._remote, revision
        )

    def create_isolated(self, campaign: CampaignRef, item: WorkItem) -> WorkspaceRef:
        repository = self.identify(campaign.repository)
        lease_id = uuid4().hex
        worktree_root = self._scratch_root / "worktrees" / f"{_slug(item.item_id)}-{lease_id[:8]}"
        worktree_root.parent.mkdir(parents=True, exist_ok=True)
        self._require_scratch_path(worktree_root)
        branch = f"autobuild/{_slug(campaign.campaign_id)}/{_slug(item.item_id)}-{lease_id[:8]}"
        with self._primary_lock:
            # The start revision is read under the lock, so the worktree starts
            # from the primary head no concurrent claim is moving.
            revision = self._git(repository.root, "rev-parse", "HEAD")
            self._git(repository.root, "worktree", "add", "-b", branch, str(worktree_root), revision)
        repository = replace(repository, revision=revision)
        workspace = WorkspaceRef(worktree_root, branch, revision, lease_id)
        with self._lock:
            self._leases[lease_id] = _Lease(repository, workspace)
        return workspace

    def list_worktrees(self, campaign: CampaignRef) -> tuple[WorktreeStatus, ...]:
        repository = self.identify(campaign.repository)
        listing = self._git(repository.root, "worktree", "list", "--porcelain")
        statuses: list[WorktreeStatus] = []
        for block in listing.split("\n\n"):
            root = head = branch = ""
            for line in block.splitlines():
                if line.startswith("worktree "):
                    root = line.removeprefix("worktree ").strip()
                elif line.startswith("HEAD "):
                    head = line.removeprefix("HEAD ").strip()
                elif line.startswith("branch "):
                    branch = line.removeprefix("branch ").removeprefix("refs/heads/").strip()
            if not root or not head:
                continue
            path = Path(root).resolve(strict=False)
            if not self._under_scratch(path) or not path.exists():
                continue
            changed = self._changed_against(path, head)
            statuses.append(
                WorktreeStatus(
                    path,
                    branch,
                    head,
                    self._revision_digest(head, changed),
                    self._integration_base(path, head),
                )
            )
        return tuple(statuses)

    def adopt_worktree(
        self, campaign: CampaignRef, item: WorkItem, root: Path
    ) -> WorkspaceRef:
        repository = self.identify(campaign.repository)
        resolved = root.resolve(strict=False)
        self._require_scratch_path(resolved)
        if not resolved.exists():
            raise EvidenceError(f"worktree to adopt does not exist: {resolved}")
        if not self._worktree_registered(repository.root, resolved):
            raise EvidenceError(f"worktree to adopt is not registered: {resolved}")
        head = self._git(resolved, "rev-parse", "HEAD")
        branch = self._git(resolved, "branch", "--show-current")
        lease_id = uuid4().hex
        workspace = WorkspaceRef(resolved, branch, head, lease_id)
        with self._lock:
            self._leases[lease_id] = _Lease(repository, workspace)
        return workspace

    def resume_delivery_commits(self, workspace: WorkspaceRef) -> tuple[str | None, str]:
        self._require_lease(workspace)
        head = self._git(workspace.root, "rev-parse", "HEAD")
        tracker_commit = self._integration_base(workspace.root, head)
        parent = self._git(
            workspace.root, "rev-parse", "--verify", "--quiet", f"{tracker_commit}^", check=False
        )
        return (parent.strip() or None, tracker_commit)

    def diff(self, workspace: WorkspaceRef) -> DiffEvidence:
        self._require_lease(workspace)
        head = self._git(workspace.root, "rev-parse", "HEAD")
        if head != workspace.start_commit:
            raise EvidenceError(
                "worktree head moved outside the finalisation boundary: "
                f"recorded {workspace.start_commit}, observed {head}"
            )
        changed = self._changed_paths(workspace)
        workspace_revision = self._revision_digest(head, changed)
        evidence_root = self._scratch_root / "evidence" / workspace.lease_id
        evidence_root.mkdir(parents=True, exist_ok=True)
        patch_path = evidence_root / "changes.patch"
        tracked_patch = self._git(
            workspace.root,
            "diff",
            "--binary",
            "--no-ext-diff",
            workspace.start_commit,
            "--",
            check=False,
        )
        untracked = [
            f"# untracked {entry.path.as_posix()} sha256={entry.digest}"
            for entry in changed
            if entry.kind is ChangeKind.ADDED
        ]
        patch_path.write_text(
            tracked_patch + ("\n" if tracked_patch and untracked else "") + "\n".join(untracked),
            encoding="utf-8",
        )
        return DiffEvidence(workspace, workspace_revision, changed, str(patch_path), head)

    def progress_digest(self, workspace: WorkspaceRef) -> str:
        """A cheap digest of the worktree's product state for stall detection.

        It combines ``git status --porcelain --untracked-files=all`` with the
        size and modification time of every changed product path, so a builder
        that is writing files keeps advancing the digest even between commits.
        The command adapter calls this between samples; git is never run there."""

        self._require_lease(workspace)
        status = self._git(
            workspace.root, "status", "--porcelain", "--untracked-files=all"
        )
        digest = hashlib.sha256()
        for line in status.splitlines():
            path = line[3:]
            if not self._is_product_path(path):
                continue
            digest.update(line.encode("utf-8", "replace"))
            try:
                stat = (workspace.root / path).stat()
                digest.update(f":{stat.st_size}:{stat.st_mtime_ns}".encode())
            except OSError:
                digest.update(b":absent")
            digest.update(b"\n")
        return digest.hexdigest()

    def commit_item(self, workspace: WorkspaceRef, request: FinaliseRequest) -> str:
        self._require_lease(workspace)
        expected = request.evidence.diff
        observed_head = self._git(workspace.root, "rev-parse", "HEAD")
        if observed_head != expected.head_commit:
            raise EvidenceError(
                "worktree head moved after review: "
                f"recorded {expected.head_commit}, observed {observed_head}"
            )
        current = self.diff(workspace)
        if current.workspace_revision != expected.workspace_revision or current.changed_paths != expected.changed_paths:
            raise EvidenceError("workspace changed after validation and review")
        if not current.changed_paths:
            raise EvidenceError("accepted item has no scoped product change")
        paths = [entry.path.as_posix() for entry in current.changed_paths]
        self._git(workspace.root, "add", "-A", "--", *paths)
        self._git(workspace.root, "commit", "-m", request.commit_message, "--", *paths)
        item_commit = self._git(workspace.root, "rev-parse", "HEAD")
        self._require_full_commit(workspace.root, paths)
        return item_commit

    def confirm_delivery(
        self, workspace: WorkspaceRef, result: FinaliseResult, target_branch: str
    ) -> None:
        lease = self._require_lease(workspace)
        primary = lease.repository.root
        for label, commit in (
            ("item", result.item_commit),
            ("tracker", result.tracker_commit),
            ("merged", result.merged_commit),
        ):
            if commit is None:
                continue
            present = self._git(
                primary, "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}", check=False
            )
            if not present.strip():
                raise EvidenceError(
                    f"reported {label} commit is absent from the repository: {commit}"
                )
        if result.merged_commit and target_branch.strip():
            # A pushed delivery is proven by the remote branch; the local branch
            # may not have moved onto it yet.
            reached = self._is_ancestor(primary, result.merged_commit, target_branch) or (
                result.pushed
                and self._is_ancestor(
                    primary,
                    result.merged_commit,
                    f"refs/remotes/{lease.repository.remote}/{target_branch}",
                )
            )
            if not reached:
                raise EvidenceError(
                    "merged commit is not reachable from the delivery target branch: "
                    f"merged {result.merged_commit}, branch {target_branch}"
                )

    def commit_tracker(
        self, workspace: WorkspaceRef, item_id: str, item_commit: str | None
    ) -> str:
        self._require_lease(workspace)
        if self._tracker_commits_itself:
            base = item_commit if item_commit is not None else workspace.start_commit
            return require_one_tracker_commit(
                self._git, workspace.root, base, self._tracker_paths
            )
        head = self._git(workspace.root, "rev-parse", "HEAD")
        if item_commit is not None and head != item_commit:
            raise EvidenceError("tracker close is not immediately after the item commit")
        if item_commit is None and head != workspace.start_commit:
            raise EvidenceError("parked delivery contains an out-of-band product commit")
        tracker_status = self._git(
            workspace.root,
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--",
            *self._tracker_paths,
        )
        if not tracker_status.strip():
            raise EvidenceError("tracker adapter produced no state to commit")
        self._git(workspace.root, "add", "-A", "--", *self._tracker_paths)
        action = "close" if item_commit is not None else "park"
        self._git(
            workspace.root,
            "commit",
            "-m",
            f"MODIFIED: {action} {item_id} in tracker",
            "--",
            *self._tracker_paths,
        )
        return self._git(workspace.root, "rev-parse", "HEAD")

    def snapshot(self, workspace: WorkspaceRef) -> WorktreeSnapshot:
        self._require_lease(workspace)
        head = self._git(workspace.root, "rev-parse", "HEAD")
        base = self._integration_base(workspace.root, head)
        if base != head:
            return self._committed_snapshot(workspace, base)
        changed = self._changed_paths(workspace)
        untracked = {
            path
            for path in filter(
                None,
                self._git(
                    workspace.root, "ls-files", "--others", "--exclude-standard", "-z"
                ).split("\0"),
            )
            if self._is_product_path(path)
        }
        tracked_specs = [
            entry.path.as_posix()
            for entry in changed
            if entry.path.as_posix() not in untracked
        ]
        patch = b""
        if tracked_specs:
            patch = self._git_bytes(
                workspace.root,
                "diff",
                "--binary",
                "--no-ext-diff",
                workspace.start_commit,
                "--",
                *tracked_specs,
            )
        files: list[SnapshotFile] = []
        for entry in changed:
            posix = entry.path.as_posix()
            if posix not in untracked:
                continue
            absolute = workspace.root / entry.path
            if absolute.is_symlink():
                content = str(absolute.readlink()).encode("utf-8")
            else:
                content = absolute.read_bytes()
            files.append(SnapshotFile(posix, content, entry.digest or ""))
        return WorktreeSnapshot(workspace.start_commit, patch, tuple(files), changed)

    def _committed_snapshot(self, workspace: WorkspaceRef, base: str) -> WorktreeSnapshot:
        """The snapshot of a worktree whose head is an integration merge: only
        the item's own change, from the start commit to ``base`` (the commit
        beneath the merges), never what the merged target brought in. A
        completed merge leaves nothing uncommitted, so there are no loose files."""

        root = workspace.root
        raw = self._git(
            root, "diff", "--name-status", "-z", "--no-renames", workspace.start_commit, base
        )
        fields = [field for field in raw.split("\0") if field]
        changed: list[ChangedPath] = []
        for status, value in zip(fields[0::2], fields[1::2]):
            if not self._is_product_path(value):
                continue
            if status.startswith("D"):
                changed.append(ChangedPath(Path(value), ChangeKind.DELETED, None))
                continue
            kind = ChangeKind.ADDED if status.startswith("A") else ChangeKind.MODIFIED
            blob = self._git_bytes(root, "cat-file", "blob", f"{base}:{value}")
            changed.append(ChangedPath(Path(value), kind, hashlib.sha256(blob).hexdigest()))
        changed.sort(key=lambda entry: entry.path.as_posix())
        patch = b""
        if changed:
            patch = self._git_bytes(
                root,
                "diff",
                "--binary",
                "--no-ext-diff",
                workspace.start_commit,
                base,
                "--",
                *(entry.path.as_posix() for entry in changed),
            )
        return WorktreeSnapshot(workspace.start_commit, patch, (), tuple(changed))

    def deliver(self, workspace: WorkspaceRef, request: DeliveryRequest) -> FinaliseResult:
        """Deliver one item branch: integrate, revalidate when the base moved,
        push, and only then move the local target.

        Each attempt fetches the remote target (outside the lock) and merges the
        fetched remote target and the local target head into the item branch in
        the worktree. A merge that changed a product path is put to the
        revalidation rule in ``enforcement.integration`` with the item commit's
        own paths and the declared paths; only an overlap is validated again
        through ``request.revalidate``, and every decision goes to
        ``request.note_revalidation``. Under the lock the local
        target must be an ancestor of the candidate; a gap confined to the
        tracker paths is merged in there, a gap in any other path starts the
        next attempt. The lock is released and the candidate pushed by refspec
        from the worktree. Only after the remote accepted and showed it does the
        lock fast-forward the local target to it, so the local target never
        holds an undelivered item commit. A source move or a rejected push uses
        one of ``DELIVERY_ATTEMPTS``. Without a push, the fast-forward is the
        delivery and runs in the same hold as the catch-up."""

        lease = self._require_lease(workspace)
        head = self._git(workspace.root, "rev-parse", "HEAD")
        if self._integration_base(workspace.root, head) != request.tracker_commit:
            raise EvidenceError("delivery head does not match the tracker commit")
        parent = self._git(workspace.root, "rev-parse", f"{request.tracker_commit}^")
        if request.item_commit is None:
            if parent != workspace.start_commit:
                raise EvidenceError("parked tracker commit is not based on the starting commit")
        elif parent != request.item_commit:
            raise EvidenceError("tracker commit is not immediately after the item commit")
        repository = lease.repository
        if request.mode is DeliveryMode.CURRENT_BRANCH_PR:
            if request.target_branch != repository.current_branch:
                raise EvidenceError(
                    "current-branch delivery target differs from the invoking branch"
                )
            if (
                request.target_branch == repository.default_branch
                and not request.allow_current_branch_default
            ):
                raise AdapterError(
                    "current-branch-pr delivery refuses the detected default branch; "
                    "pass --allow-current-branch-default after human approval"
                )
            target = request.target_branch
            pushing = request.push_current_branch
        elif request.mode is DeliveryMode.PROTECTED_DEFAULT:
            target = repository.default_branch
            pushing = True
        else:
            raise EvidenceError(f"unsupported delivery mode: {request.mode}")
        # The commit whose product tree last passed the validator. A park carries
        # no validated product change and is never validated again.
        validated = request.item_commit if request.revalidate is not None else None
        own = (
            tuple(
                self._product_changes(
                    workspace.root, f"{request.item_commit}^", request.item_commit
                )
            )
            if validated is not None
            else ()
        )
        spent = 0
        rejections: list[str] = []
        while True:
            candidate = self._integrate(lease, workspace, target)
            if validated is not None:
                incoming = self._product_changes(workspace.root, validated, candidate)
                if incoming:
                    decision = revalidation_decision(incoming, own, request.declared_paths)
                    if request.note_revalidation is not None:
                        request.note_revalidation(decision)
                    if decision.run:
                        assert request.revalidate is not None
                        if not request.revalidate():
                            raise DeliveryParked(
                                DeliveryParkReason.REVALIDATION_FAILED,
                                f"integrated candidate {candidate} failed the validator",
                            )
                        validated = candidate

            def catch_up() -> str | None:
                ready = self._catch_up(
                    lease, workspace, request, target, candidate, validated, own
                )
                if ready is not None and not pushing:
                    self._fast_forward_target(lease, target, ready)
                return ready

            ready = self._on_clean_primary(catch_up)
            if ready is not None and not pushing:
                return FinaliseResult(
                    request.item_commit,
                    request.tracker_commit,
                    ready,
                    False,
                    (f"local branch {target} updated without push",),
                )
            if ready is not None and self._push(lease, workspace, target, ready, rejections):
                pushed = ready
                notes = (f"remote {repository.remote}/{target} verified",)
                # The remote holds the item now, so it is delivered even when the
                # local target cannot move; the next delivery's merge of the
                # fetched remote catches the local target up.
                try:
                    self._on_clean_primary(
                        lambda: self._settle_target(lease, workspace, target, pushed)
                    )
                except AdapterError as error:
                    notes += (f"local {target} not advanced after the push: {error}",)
                return FinaliseResult(
                    request.item_commit,
                    request.tracker_commit,
                    ready,
                    True,
                    notes,
                )
            spent += 1
            if spent >= DELIVERY_ATTEMPTS:
                cause = (
                    "the target branch moved outside the tracker paths"
                    if ready is None
                    else "the push was rejected"
                )
                detail = f": {rejections[-1]}" if ready is not None and rejections else ""
                raise DeliveryParked(
                    DeliveryParkReason.CONTENDED,
                    f"{cause} on the last of {DELIVERY_ATTEMPTS} delivery attempts{detail}",
                )

    def _integrate(self, lease: _Lease, workspace: WorkspaceRef, target: str) -> str:
        """Fetch the remote target and merge it, then the local target head, into
        the item branch. Returns the integrated head. The local head carries
        claims and tracker writes not yet published, which go out with this push."""

        remote_head = self._fetch_target(workspace.root, lease.repository.remote, target)
        local_head = self._git(workspace.root, "rev-parse", "--verify", f"refs/heads/{target}")
        for label, commit in (
            (f"{lease.repository.remote}/{target}", remote_head),
            (target, local_head),
        ):
            if commit is not None:
                self._merge_into_item(workspace, commit, label)
        return self._git(workspace.root, "rev-parse", "HEAD")

    def _fetch_target(self, root: Path, remote: str, target: str) -> str | None:
        """The remote head of ``target``, or None when the remote has no such
        branch (a pushing delivery then creates it). A remote-tracking ref held
        by a concurrent writer is retried up to ``FETCH_ATTEMPTS`` times."""

        refspec = f"+refs/heads/{target}:refs/remotes/{remote}/{target}"
        for attempt in range(FETCH_ATTEMPTS):
            try:
                self._git(root, "fetch", remote, refspec)
            except AdapterError as exc:
                detail = str(exc).casefold()
                if _ABSENT_REMOTE_REF_SIGN in detail:
                    return None
                if attempt < FETCH_ATTEMPTS - 1 and any(
                    sign in detail for sign in _REF_LOCK_SIGNS
                ):
                    time.sleep(0.2 * (attempt + 1))
                    continue
                raise
            return self._git(root, "rev-parse", "--verify", f"refs/remotes/{remote}/{target}")
        raise AdapterError(f"fetch of {remote}/{target} did not complete")  # pragma: no cover

    def _merge_into_item(self, workspace: WorkspaceRef, commit: str, label: str) -> None:
        """Merge ``commit`` into the item branch with a recorded integration merge.

        A conflict confined to tracker projection files is settled by
        regenerating the projection from the merged log. Any other conflicted
        path aborts the merge and parks the delivery as a conflict."""

        root = workspace.root
        if self._is_ancestor(root, commit, "HEAD"):
            return
        before = self._git(root, "rev-parse", "HEAD")
        self._git(
            root,
            "merge",
            "--no-ff",
            "--no-edit",
            "-m",
            f"{INTEGRATION_SUBJECT} {label} into {workspace.branch}",
            commit,
            check=False,
        )
        merging = bool(
            self._git(root, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).strip()
        )
        if not merging and self._git(root, "rev-parse", "HEAD") != before:
            return
        conflicted = sorted(
            path
            for path in self._git(
                root, "diff", "--name-only", "-z", "--diff-filter=U", check=False
            ).split("\0")
            if path
        )
        if (
            merging
            and conflicted
            and self._projection is not None
            and all(self._projection.is_projection(path) for path in conflicted)
        ):
            self._git(root, "checkout", "--ours", "--", *conflicted)
            self._projection.regenerate_projection(root)
            self._git(root, "add", "-A", "--", *self._tracker_paths)
            unresolved = self._git(
                root, "diff", "--name-only", "--diff-filter=U", check=False
            ).strip()
            if not unresolved:
                self._git(root, "commit", "--no-edit")
                return
        if merging:
            self._git(root, "merge", "--abort")
        if conflicted:
            raise DeliveryParked(
                DeliveryParkReason.CONFLICT,
                f"integrating {label} conflicted in " + ", ".join(conflicted),
            )
        raise AdapterError(f"integration merge of {label} did not complete")

    def _merge_tracker_gap(self, workspace: WorkspaceRef, local_head: str, target: str) -> None:
        """Merge a local target head whose only new changes are tracker state
        into the item branch. It runs inside the primary-checkout lock so no
        further tracker write lands between this check and the next step."""

        self._merge_into_item(workspace, local_head, target)

    def _tracker_only_gap(self, root: Path, candidate: str, local_head: str) -> bool:
        """True when the local target head adds nothing outside the tracker
        paths that ``candidate`` lacks."""

        gap = self._git(
            root, "diff", "--name-only", "-z", "--no-renames", f"{candidate}...{local_head}"
        )
        return not any(self._is_product_path(path) for path in gap.split("\0") if path)

    def _catch_up(
        self,
        lease: _Lease,
        workspace: WorkspaceRef,
        request: DeliveryRequest,
        target: str,
        candidate: str,
        validated: str | None,
        own: tuple[str, ...] = (),
    ) -> str | None:
        """The pre-push check; the caller holds the primary-checkout lock. It
        changes only the item branch, never the local target. Returns the
        candidate to push, or None when the local target gained a change outside
        the tracker paths and the delivery must integrate again. A candidate
        that differs from the validated tree in a path the revalidation rule
        says overlaps the item is refused."""

        primary = lease.repository.root
        root = workspace.root
        self._require_clean_primary(primary, "before delivery")
        if request.mode is DeliveryMode.CURRENT_BRANCH_PR:
            if self._git(primary, "branch", "--show-current") != target:
                raise AdapterError("primary checkout no longer has the captured target branch")
            if not self._is_ancestor(primary, request.target_revision, "HEAD"):
                raise AdapterError(
                    "captured target revision is no longer in the current branch history"
                )
        local_head = self._git(root, "rev-parse", "--verify", f"refs/heads/{target}")
        if not self._is_ancestor(root, local_head, candidate):
            if not self._tracker_only_gap(root, candidate, local_head):
                return None
            self._merge_tracker_gap(workspace, local_head, target)
            candidate = self._git(root, "rev-parse", "HEAD")
        if validated is not None:
            differing = self._product_changes(root, validated, candidate)
            if differing and revalidation_decision(
                differing, own, request.declared_paths
            ).run:
                raise EvidenceError(
                    "delivery candidate differs from the validated tree in a path "
                    "that overlaps the item"
                )
        # Checked before the push: once pushed, the local target must be able
        # to move onto the candidate.
        self._require_no_collision(primary, candidate)
        return candidate

    def _settle_target(
        self, lease: _Lease, workspace: WorkspaceRef, target: str, pushed: str
    ) -> None:
        """Move the local target onto a commit the remote has accepted; the
        caller holds the primary-checkout lock. Tracker-only commits that landed
        on the local target since the catch-up are merged on top in the item
        branch first; they go out with the next push of the branch."""

        root = workspace.root
        local_head = self._git(root, "rev-parse", "--verify", f"refs/heads/{target}")
        commit = pushed
        if not self._is_ancestor(root, local_head, pushed):
            if not self._tracker_only_gap(root, pushed, local_head):
                raise AdapterError(
                    f"local {target} gained a product change after {pushed} was pushed"
                )
            self._merge_tracker_gap(workspace, local_head, target)
            commit = self._git(root, "rev-parse", "HEAD")
        self._fast_forward_target(lease, target, commit)

    def _fast_forward_target(self, lease: _Lease, target: str, commit: str) -> None:
        """Fast-forward the primary checkout's target branch to ``commit``; the
        caller holds the primary-checkout lock."""

        primary = lease.repository.root
        self._require_clean_primary(primary, "before delivery")
        self._require_no_collision(primary, commit)
        if self._git(primary, "branch", "--show-current") != target:
            self._git(primary, "checkout", target)
        self._git(primary, "merge", "--ff-only", commit)
        if self._git(primary, "rev-parse", "HEAD") != commit:
            raise AdapterError("target branch did not fast-forward to the delivery candidate")
        self._require_clean_primary(primary, "after delivery")

    def _push(
        self,
        lease: _Lease,
        workspace: WorkspaceRef,
        target: str,
        commit: str,
        rejections: list[str] | None = None,
    ) -> bool:
        """Push exactly ``commit`` to the remote target and verify it there.
        Returns False when the remote rejected it because another writer won a
        race, appending the push's own message to ``rejections``."""

        remote = lease.repository.remote
        try:
            self._git(workspace.root, "push", remote, f"{commit}:refs/heads/{target}")
        except AdapterError as exc:
            if any(sign in str(exc).casefold() for sign in _PUSH_REJECTED_SIGNS):
                if rejections is not None:
                    rejections.append(str(exc))
                return False
            raise
        remote_line = self._git(workspace.root, "ls-remote", remote, f"refs/heads/{target}")
        remote_revision = remote_line.split()[0] if remote_line.split() else ""
        if remote_revision != commit:
            raise AdapterError("remote verification did not observe the delivered commit")
        return True

    def _product_changes(self, root: Path, base: str, head: str) -> list[str]:
        raw = self._git(root, "diff", "--name-only", "-z", "--no-renames", base, head)
        return [path for path in raw.split("\0") if path and self._is_product_path(path)]

    def _is_ancestor(self, root: Path, commit: str, descendant: str) -> bool:
        completed = subprocess.run(
            ["git", "-C", str(root), "merge-base", "--is-ancestor", commit, descendant],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        return completed.returncode == 0

    def _integration_base(self, root: Path, commit: str) -> str:
        """``commit`` with every recorded integration merge stepped over along
        the first-parent chain: the commit the item's own history ends at."""

        for _ in range(_MAX_INTEGRATION_MERGES):
            line = self._git(root, "log", "-1", "--format=%P%x00%s", commit)
            parents, _, subject = line.partition("\0")
            parent_list = parents.split()
            if len(parent_list) < 2 or not subject.startswith(INTEGRATION_SUBJECT):
                return commit
            commit = parent_list[0]
        raise EvidenceError(f"more than {_MAX_INTEGRATION_MERGES} integration merges on one item")

    def deliver_report(self, request: CampaignReport) -> FinaliseResult:
        repository = self.identify(request.repository)
        primary = repository.root
        relative = Path(request.relative_path).as_posix().strip("/")
        if not relative or relative.startswith("../") or Path(relative).is_absolute():
            raise EvidenceError("campaign report path must be repository-relative")
        if request.mode is DeliveryMode.CURRENT_BRANCH_PR:
            branch = request.target_branch
            if (
                branch == repository.default_branch
                and not request.allow_current_branch_default
            ):
                raise AdapterError(
                    "current-branch-pr report refuses the detected default branch"
                )
        elif request.mode is DeliveryMode.PROTECTED_DEFAULT:
            branch = repository.default_branch
        else:
            raise EvidenceError(f"unsupported delivery mode: {request.mode}")
        target = primary / relative
        message = f"ADDED: campaign {request.campaign_id} report"
        pushed = request.mode is DeliveryMode.PROTECTED_DEFAULT or request.push_current_branch
        if not pushed:
            # Without a push the local commit is the delivery itself.
            def commit_locally() -> str:
                self._require_clean_primary(primary, "before report")
                self._require_report_path_free(primary, relative)
                if self._git(primary, "branch", "--show-current") != branch:
                    self._git(primary, "checkout", branch)
                commit = self._commit_report(primary, relative, request.content, message)
                self._require_clean_primary(primary, "after report")
                return commit

            report_commit = self._on_clean_primary(commit_locally)
            return FinaliseResult(
                None,
                report_commit,
                report_commit,
                False,
                (f"report committed locally on {branch}", str(target)),
            )
        # A pushed report is committed in a detached scratch worktree and pushed
        # from there, so the local branch only ever moves onto a commit the
        # remote has accepted.
        report_root = self._scratch_root / "reports" / f"{_slug(request.campaign_id)}-{uuid4().hex[:8]}"
        report_root.parent.mkdir(parents=True, exist_ok=True)
        self._require_scratch_path(report_root)
        def add_report_worktree() -> None:
            self._require_clean_primary(primary, "before report")
            self._require_report_path_free(primary, relative)
            base = self._git(primary, "rev-parse", "--verify", f"refs/heads/{branch}")
            self._git(primary, "worktree", "add", "--detach", str(report_root), base)

        self._on_clean_primary(add_report_worktree)
        try:
            report_commit = self._commit_report(report_root, relative, request.content, message)
            self._git(
                report_root, "push", repository.remote, f"{report_commit}:refs/heads/{branch}"
            )
            remote_line = self._git(
                report_root, "ls-remote", repository.remote, f"refs/heads/{branch}"
            )
            remote_revision = remote_line.split()[0] if remote_line.split() else ""
            if remote_revision != report_commit:
                raise AdapterError("remote verification did not observe the delivered report")
            diagnostics = [f"remote {repository.remote}/{branch} verified", str(target)]
            with self._primary_lock:
                local_head = self._git(primary, "rev-parse", "--verify", f"refs/heads/{branch}")
                if self._is_ancestor(primary, local_head, report_commit):
                    if self._git(primary, "branch", "--show-current") != branch:
                        self._git(primary, "checkout", branch)
                    self._git(primary, "merge", "--ff-only", report_commit)
                    self._require_clean_primary(primary, "after report")
                else:
                    diagnostics.append(
                        f"local {branch} moved during the report push; the report is on the remote"
                    )
        finally:
            with self._primary_lock:
                self._git(primary, "worktree", "remove", "--force", str(report_root), check=False)
        return FinaliseResult(None, report_commit, report_commit, True, tuple(diagnostics))

    def open_validation_checkout(
        self, repository: Path, branch: str | None, refresh: bool = False
    ) -> ValidationCheckout:
        """A detached scratch worktree at the local head of ``branch`` (the
        default branch when None). With ``refresh`` the remote branch is
        fetched first, outside the lock, and the local branch is fast-forwarded
        to it when the local head is its ancestor: the remote head is already
        delivered, so the local target still never holds an undelivered
        commit. The lock covers only that fast-forward, reading the head and
        adding the worktree."""

        identity = self.identify(repository)
        primary = identity.root
        target = branch or identity.default_branch
        remote_head = self._fetch_target(primary, identity.remote, target) if refresh else None
        root = self._scratch_root / "validation" / f"{_slug(target)}-{uuid4().hex[:8]}"
        root.parent.mkdir(parents=True, exist_ok=True)
        self._require_scratch_path(root)
        with self._primary_lock:
            revision = self._git(primary, "rev-parse", "--verify", f"refs/heads/{target}")
            if (
                remote_head is not None
                and remote_head != revision
                and self._is_ancestor(primary, revision, remote_head)
            ):
                revision = self._advance_local(primary, target, revision, remote_head)
            self._git(primary, "worktree", "add", "--detach", str(root), revision)
        return ValidationCheckout(root, revision, primary)

    def _advance_local(self, primary: Path, target: str, local: str, remote_head: str) -> str:
        """Fast-forward the local ``target`` from ``local`` to ``remote_head``;
        the caller holds the primary-checkout lock. A checked-out target moves
        by ``merge --ff-only`` on a clean checkout only; another branch's ref
        moves by a guarded ref update. Returns the head to validate."""

        if self._git(primary, "branch", "--show-current") == target:
            incoming = self._product_changes(primary, local, remote_head)
            if self._primary_dirt(primary) or self._untracked_collisions(primary, incoming):
                # A dirty primary is never touched; the fetched head is still
                # the one to validate.
                return remote_head
            self._git(primary, "merge", "--ff-only", remote_head)
        else:
            self._git(primary, "update-ref", f"refs/heads/{target}", remote_head, local)
        return self._git(primary, "rev-parse", "--verify", f"refs/heads/{target}")

    def close_validation_checkout(self, checkout: ValidationCheckout) -> None:
        self._require_scratch_path(checkout.root)
        with self._primary_lock:
            self._git(
                checkout.repository, "worktree", "remove", "--force", str(checkout.root),
                check=False,
            )

    def _commit_report(self, root: Path, relative: str, content: str, message: str) -> str:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self._git(root, "add", "-A", "--", relative)
        self._git(root, "commit", "-m", message, "--", relative)
        return self._git(root, "rev-parse", "HEAD")

    def release(self, workspace: WorkspaceRef) -> None:
        with self._lock:
            lease = self._leases.get(workspace.lease_id)
        if lease is None:
            return
        self._require_scratch_path(workspace.root)
        if workspace.root.exists():
            tracker_status = self._git(
                workspace.root,
                "status",
                "--porcelain",
                "--untracked-files=all",
                "--",
                *self._tracker_paths,
            )
            if tracker_status.strip():
                raise EvidenceError(
                    "refusing to remove a worktree with uncommitted tracker files: "
                    + ", ".join(sorted(line[3:] for line in tracker_status.splitlines() if line[3:]))
                )
        with self._lock:
            self._leases.pop(workspace.lease_id, None)
        if workspace.root.exists():
            try:
                with self._primary_lock:
                    self._git(
                        lease.repository.root,
                        "worktree",
                        "remove",
                        "--force",
                        str(workspace.root),
                    )
            except AdapterError:
                if self._worktree_registered(lease.repository.root, workspace.root):
                    with self._lock:
                        self._leases.setdefault(workspace.lease_id, lease)
                    raise

    def _worktree_registered(self, repository: Path, workspace: Path) -> bool:
        target = os.path.normcase(str(workspace.resolve(strict=False)))
        listing = self._git(repository, "worktree", "list", "--porcelain")
        return any(
            os.path.normcase(str(Path(line.removeprefix("worktree ")).resolve(strict=False)))
            == target
            for line in listing.splitlines()
            if line.startswith("worktree ")
        )

    def _require_lease(self, workspace: WorkspaceRef) -> _Lease:
        with self._lock:
            lease = self._leases.get(workspace.lease_id)
        if lease is None or lease.workspace != workspace:
            raise EvidenceError("workspace lease is unknown or does not match")
        self._require_scratch_path(workspace.root)
        return lease

    def _require_scratch_path(self, path: Path) -> None:
        if not self._under_scratch(path):
            raise AdapterError(f"worktree path is outside configured scratch space: {path}")

    def _under_scratch(self, path: Path) -> bool:
        resolved = path.resolve(strict=False)
        return resolved != self._scratch_root and self._scratch_root in resolved.parents

    def _changed_paths(self, workspace: WorkspaceRef) -> tuple[ChangedPath, ...]:
        return self._changed_against(workspace.root, workspace.start_commit)

    def _changed_against(self, root: Path, base: str) -> tuple[ChangedPath, ...]:
        entries: dict[str, ChangeKind] = {}
        raw = self._git(
            root,
            "diff",
            "--name-status",
            "-z",
            "--no-renames",
            base,
            "--",
        )
        fields = raw.split("\0")
        index = 0
        while index < len(fields) and fields[index]:
            status = fields[index]
            index += 1
            if index >= len(fields):
                break
            path = fields[index]
            index += 1
            if self._is_product_path(path):
                if status.startswith("D"):
                    kind = ChangeKind.DELETED
                elif status.startswith("A"):
                    kind = ChangeKind.ADDED
                else:
                    kind = ChangeKind.MODIFIED
                entries[path] = kind
        untracked = self._git(root, "ls-files", "--others", "--exclude-standard", "-z")
        for path in filter(None, untracked.split("\0")):
            if self._is_product_path(path):
                entries[path] = ChangeKind.ADDED
        changed: list[ChangedPath] = []
        for value, kind in sorted(entries.items()):
            path = root / value
            if kind is not ChangeKind.DELETED and path.is_symlink():
                kind = ChangeKind.SYMLINK
            digest = None if kind is ChangeKind.DELETED else self._digest(path)
            changed.append(ChangedPath(Path(value), kind, digest))
        return tuple(changed)

    @staticmethod
    def _revision_digest(head: str, changed: tuple[ChangedPath, ...]) -> str:
        digest = hashlib.sha256()
        digest.update(head.encode())
        for entry in changed:
            digest.update(entry.path.as_posix().encode())
            digest.update(entry.kind.value.encode())
            digest.update((entry.digest or "").encode())
        return digest.hexdigest()

    def _is_product_path(self, path: str) -> bool:
        normal = path.replace("\\", "/")
        return not any(
            normal == tracker_path or normal.startswith(tracker_path + "/")
            for tracker_path in self._tracker_paths
        )

    @staticmethod
    def _digest(path: Path) -> str:
        digest = hashlib.sha256()
        if path.is_symlink():
            digest.update(str(path.readlink()).encode())
        else:
            digest.update(path.read_bytes())
        return digest.hexdigest()

    def _product_status(self, root: Path, paths: tuple[str, ...] | list[str] = ()) -> str:
        args = ["status", "--porcelain", "--untracked-files=all"]
        if paths:
            args += ["--", *paths]
        lines = self._git(root, *args).splitlines()
        return "\n".join(line for line in lines if self._is_product_path(line[3:]))

    def _require_full_commit(self, root: Path, paths: list[str]) -> None:
        """The named close-completeness contract: after the scoped commit the whole
        product tree and the item's own changed paths must both report a clean
        status, so a close can never ship a partial tree."""
        remaining = self._product_status(root) or self._product_status(root, paths)
        if remaining:
            raise EvidenceError(
                f"uncommitted product paths remain after scoped commit: {remaining}"
            )

    def _on_clean_primary(self, step: Callable[[], _T]) -> _T:
        """Run ``step`` under the primary-checkout lock. When its clean-checkout
        check finds tracker files only, a tracker write that was appended and
        not committed, the lock is let go, the tracker's own recovery runs, and
        the step is tried again, up to ``LEFTOVER_RECOVERIES`` times. Product
        dirt is never touched and fails the step."""

        for _ in range(LEFTOVER_RECOVERIES):
            try:
                with self._primary_lock:
                    return step()
            except _TrackerLeftover:
                assert self._tracker_recovery is not None
                self._tracker_recovery()
        with self._primary_lock:
            return step()

    def _require_clean_primary(self, primary: Path, timing: str) -> None:
        dirt = self._primary_dirt(primary)
        if dirt:
            message = f"primary checkout must be clean {timing}"
            if self._tracker_recovery is not None and all(
                not self._is_product_path(path) for path in dirt
            ):
                raise _TrackerLeftover(message)
            raise AdapterError(message)

    def _primary_dirt(self, primary: Path) -> list[str]:
        """The paths that make the primary checkout dirty: staged or modified
        tracked files, and untracked tracker files (a tracker write left
        uncommitted). An untracked product file is not dirt; it blocks only a
        delivery that brings in its path (see ``_require_no_collision``)."""

        dirt: list[str] = []
        for line in self._git(primary, "status", "--porcelain", "--untracked-files=all").splitlines():
            if not line[3:]:
                continue
            paths = line[3:].split(" -> ")
            if line.startswith("??") and all(self._is_product_path(path) for path in paths):
                continue
            dirt.extend(paths)
        return dirt

    def _untracked_collisions(self, primary: Path, incoming: list[str]) -> list[str]:
        """The untracked product files in the primary checkout that a change
        to ``incoming`` would overwrite: the same path, or one path a folder
        of the other."""

        if not incoming:
            return []
        untracked = [
            path
            for path in self._git(
                primary, "ls-files", "--others", "--exclude-standard", "-z"
            ).split("\0")
            if path and self._is_product_path(path)
        ]
        return sorted(
            path
            for path in untracked
            if any(
                path == other or path.startswith(other + "/") or other.startswith(path + "/")
                for other in incoming
            )
        )

    def _require_report_path_free(self, primary: Path, relative: str) -> None:
        collisions = self._untracked_collisions(primary, [relative])
        if collisions:
            raise AdapterError(
                "untracked files in the primary checkout are at the report path: "
                + ", ".join(collisions)
            )

    def _require_no_collision(self, primary: Path, commit: str) -> None:
        """Refuse a delivery of ``commit`` that would overwrite an untracked
        file in the primary checkout when its target moves onto it."""

        incoming = [
            path
            for path in self._git(
                primary, "diff", "--name-only", "-z", "--no-renames", "HEAD", commit
            ).split("\0")
            if path
        ]
        collisions = self._untracked_collisions(primary, incoming)
        if collisions:
            raise AdapterError(
                "untracked files in the primary checkout are at paths the delivery "
                "brings in: " + ", ".join(collisions)
            )

    def _default_branch(self, root: Path) -> str:
        symbolic = self._git(
            root, "symbolic-ref", "--quiet", "--short", f"refs/remotes/{self._remote}/HEAD", check=False
        )
        prefix = f"{self._remote}/"
        if symbolic.startswith(prefix):
            return symbolic[len(prefix) :]
        remote_head = self._git(root, "ls-remote", "--symref", self._remote, "HEAD", check=False)
        for line in remote_head.splitlines():
            if not line.startswith("ref: refs/heads/"):
                continue
            reference, _, name = line.partition("\t")
            if name == "HEAD":
                return reference.removeprefix("ref: refs/heads/")
        current = self._git(root, "branch", "--show-current")
        if not current:
            raise AdapterError("cannot determine the repository default branch")
        return current

    @staticmethod
    def _git(root: Path, *args: str, check: bool = True) -> str:
        attempts = 5 if args and args[0] in {"add", "commit", "merge", "push", "worktree"} else 1
        for attempt in range(attempts):
            completed = subprocess.run(
                ["git", "-C", str(root), *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            detail = completed.stderr.strip() or completed.stdout.strip()
            transient = "permission denied" in detail.casefold() or "unpacker error" in detail.casefold()
            if completed.returncode == 0 or not transient or attempt == attempts - 1:
                break
            time.sleep(0.2 * (attempt + 1))
        if check and completed.returncode != 0:
            raise AdapterError(f"git {' '.join(args)} failed: {detail}")
        return completed.stdout.rstrip("\r\n")

    @staticmethod
    def _git_bytes(root: Path, *args: str) -> bytes:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", "replace").strip()
            raise AdapterError(f"git {' '.join(args)} failed: {detail}")
        return completed.stdout
