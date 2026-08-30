"""Git worktree adapter with scoped commits and remotely verified delivery."""

from __future__ import annotations

import hashlib
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from uuid import uuid4

from autobuild.domain import (
    AdapterError,
    AdapterIdentity,
    CampaignRef,
    ChangedPath,
    ChangeKind,
    DeliveryRequest,
    DiffEvidence,
    EvidenceError,
    FinaliseRequest,
    FinaliseResult,
    ProbeResult,
    RepositoryIdentity,
    WorkItem,
    WorkspaceRef,
)


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
    ) -> None:
        self._scratch_root = scratch_root.resolve(strict=False)
        self._remote = remote
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
        remotes = self._git(resolved, "remote").splitlines()
        if self._remote not in remotes:
            raise AdapterError(f"required git remote is missing: {self._remote}")
        return RepositoryIdentity(resolved, default_branch, self._remote, revision)

    def create_isolated(self, campaign: CampaignRef, item: WorkItem) -> WorkspaceRef:
        repository = self.identify(campaign.repository)
        lease_id = uuid4().hex
        worktree_root = self._scratch_root / "worktrees" / f"{_slug(item.item_id)}-{lease_id[:8]}"
        worktree_root.parent.mkdir(parents=True, exist_ok=True)
        self._require_scratch_path(worktree_root)
        branch = f"autobuild/{_slug(campaign.campaign_id)}/{_slug(item.item_id)}-{lease_id[:8]}"
        self._git(repository.root, "worktree", "add", "-b", branch, str(worktree_root), repository.revision)
        workspace = WorkspaceRef(worktree_root, branch, repository.revision, lease_id)
        with self._lock:
            self._leases[lease_id] = _Lease(repository, workspace)
        return workspace

    def diff(self, workspace: WorkspaceRef) -> DiffEvidence:
        self._require_lease(workspace)
        head = self._git(workspace.root, "rev-parse", "HEAD")
        if head != workspace.start_commit:
            raise EvidenceError("workspace history changed outside the finalisation boundary")
        changed = self._changed_paths(workspace)
        digest = hashlib.sha256()
        digest.update(head.encode())
        for entry in changed:
            digest.update(entry.path.as_posix().encode())
            digest.update(entry.kind.value.encode())
            digest.update((entry.digest or "").encode())
        workspace_revision = digest.hexdigest()
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
        return DiffEvidence(workspace, workspace_revision, changed, str(patch_path))

    def commit_item(self, workspace: WorkspaceRef, request: FinaliseRequest) -> str:
        self._require_lease(workspace)
        current = self.diff(workspace)
        expected = request.evidence.diff
        if current.workspace_revision != expected.workspace_revision or current.changed_paths != expected.changed_paths:
            raise EvidenceError("workspace changed after validation and review")
        if not current.changed_paths:
            raise EvidenceError("accepted item has no scoped product change")
        paths = [entry.path.as_posix() for entry in current.changed_paths]
        self._git(workspace.root, "add", "-A", "--", *paths)
        self._git(workspace.root, "commit", "-m", request.commit_message, "--", *paths)
        item_commit = self._git(workspace.root, "rev-parse", "HEAD")
        remaining = self._product_status(workspace.root)
        if remaining:
            raise EvidenceError(f"uncommitted product paths remain after scoped commit: {remaining}")
        return item_commit

    def commit_tracker(
        self, workspace: WorkspaceRef, item_id: str, item_commit: str | None
    ) -> str:
        self._require_lease(workspace)
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

    def deliver(self, workspace: WorkspaceRef, request: DeliveryRequest) -> FinaliseResult:
        lease = self._require_lease(workspace)
        head = self._git(workspace.root, "rev-parse", "HEAD")
        if head != request.tracker_commit:
            raise EvidenceError("delivery head does not match the tracker commit")
        parent = self._git(workspace.root, "rev-parse", f"{request.tracker_commit}^")
        if request.item_commit is None:
            if parent != workspace.start_commit:
                raise EvidenceError("parked tracker commit is not based on the starting commit")
        elif parent != request.item_commit:
            raise EvidenceError("tracker commit is not immediately after the item commit")
        if not request.merge_to_default:
            return FinaliseResult(request.item_commit, request.tracker_commit, None, False)
        primary = lease.repository.root
        if self._git(primary, "status", "--porcelain", "--untracked-files=all"):
            raise AdapterError("primary checkout must be clean before delivery")
        self._git(primary, "checkout", lease.repository.default_branch)
        self._git(primary, "merge", "--no-ff", "--no-edit", workspace.branch)
        merged_commit = self._git(primary, "rev-parse", "HEAD")
        self._git(primary, "push", lease.repository.remote, lease.repository.default_branch)
        remote_line = self._git(
            primary,
            "ls-remote",
            lease.repository.remote,
            f"refs/heads/{lease.repository.default_branch}",
        )
        remote_revision = remote_line.split()[0] if remote_line.split() else ""
        if remote_revision != merged_commit:
            raise AdapterError("remote verification did not observe the delivered commit")
        return FinaliseResult(
            request.item_commit,
            request.tracker_commit,
            merged_commit,
            True,
            (f"remote {lease.repository.remote}/{lease.repository.default_branch} verified",),
        )

    def release(self, workspace: WorkspaceRef) -> None:
        with self._lock:
            lease = self._leases.pop(workspace.lease_id, None)
        if lease is None:
            return
        self._require_scratch_path(workspace.root)
        if workspace.root.exists():
            self._git(lease.repository.root, "worktree", "remove", "--force", str(workspace.root))

    def _require_lease(self, workspace: WorkspaceRef) -> _Lease:
        with self._lock:
            lease = self._leases.get(workspace.lease_id)
        if lease is None or lease.workspace != workspace:
            raise EvidenceError("workspace lease is unknown or does not match")
        self._require_scratch_path(workspace.root)
        return lease

    def _require_scratch_path(self, path: Path) -> None:
        resolved = path.resolve(strict=False)
        if resolved == self._scratch_root or self._scratch_root not in resolved.parents:
            raise AdapterError(f"worktree path is outside configured scratch space: {path}")

    def _changed_paths(self, workspace: WorkspaceRef) -> tuple[ChangedPath, ...]:
        entries: dict[str, ChangeKind] = {}
        raw = self._git(
            workspace.root,
            "diff",
            "--name-status",
            "-z",
            "--no-renames",
            workspace.start_commit,
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
        untracked = self._git(
            workspace.root, "ls-files", "--others", "--exclude-standard", "-z"
        )
        for path in filter(None, untracked.split("\0")):
            if self._is_product_path(path):
                entries[path] = ChangeKind.ADDED
        changed: list[ChangedPath] = []
        for value, kind in sorted(entries.items()):
            path = workspace.root / value
            if kind is not ChangeKind.DELETED and path.is_symlink():
                kind = ChangeKind.SYMLINK
            digest = None if kind is ChangeKind.DELETED else self._digest(path)
            changed.append(ChangedPath(Path(value), kind, digest))
        return tuple(changed)

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

    def _product_status(self, root: Path) -> str:
        lines = self._git(root, "status", "--porcelain", "--untracked-files=all").splitlines()
        return "\n".join(line for line in lines if self._is_product_path(line[3:]))

    def _default_branch(self, root: Path) -> str:
        symbolic = self._git(
            root, "symbolic-ref", "--quiet", "--short", f"refs/remotes/{self._remote}/HEAD", check=False
        )
        prefix = f"{self._remote}/"
        if symbolic.startswith(prefix):
            return symbolic[len(prefix) :]
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
