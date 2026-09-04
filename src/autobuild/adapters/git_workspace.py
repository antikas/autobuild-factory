"""Git worktree adapter with scoped commits and remotely verified delivery."""

from __future__ import annotations

import hashlib
import os
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
        self._git(repository.root, "worktree", "add", "-b", branch, str(worktree_root), repository.revision)
        workspace = WorkspaceRef(worktree_root, branch, repository.revision, lease_id)
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
                WorktreeStatus(path, branch, head, self._revision_digest(head, changed))
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
        tracker_commit = self._git(workspace.root, "rev-parse", "HEAD")
        parent = self._git(workspace.root, "rev-parse", "--verify", "--quiet", "HEAD^", check=False)
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
            ancestry = subprocess.run(
                [
                    "git",
                    "-C",
                    str(primary),
                    "merge-base",
                    "--is-ancestor",
                    result.merged_commit,
                    target_branch,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if ancestry.returncode != 0:
                raise EvidenceError(
                    "merged commit is not reachable from the delivery target branch: "
                    f"merged {result.merged_commit}, branch {target_branch}"
                )

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

    def snapshot(self, workspace: WorkspaceRef) -> WorktreeSnapshot:
        self._require_lease(workspace)
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
        if request.mode is DeliveryMode.CURRENT_BRANCH_PR:
            return self._deliver_current_branch(lease, workspace, request)
        if request.mode is not DeliveryMode.PROTECTED_DEFAULT:
            raise EvidenceError(f"unsupported delivery mode: {request.mode}")
        primary = lease.repository.root
        self._require_clean_primary(primary, "before delivery")
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

    def _deliver_current_branch(
        self, lease: _Lease, workspace: WorkspaceRef, request: DeliveryRequest
    ) -> FinaliseResult:
        repository = lease.repository
        if request.target_branch != repository.current_branch:
            raise EvidenceError("current-branch delivery target differs from the invoking branch")
        if (
            request.target_branch == repository.default_branch
            and not request.allow_current_branch_default
        ):
            raise AdapterError(
                "current-branch-pr delivery refuses the detected default branch; "
                "pass --allow-current-branch-default after human approval"
            )
        primary = repository.root
        self._require_clean_primary(primary, "before delivery")
        if self._git(primary, "branch", "--show-current") != request.target_branch:
            raise AdapterError("primary checkout no longer has the captured target branch")
        ancestry = subprocess.run(
            [
                "git",
                "-C",
                str(primary),
                "merge-base",
                "--is-ancestor",
                request.target_revision,
                "HEAD",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if ancestry.returncode != 0:
            raise AdapterError("captured target revision is no longer in the current branch history")
        self._git(primary, "merge", "--no-ff", "--no-edit", workspace.branch)
        merged_commit = self._git(primary, "rev-parse", "HEAD")
        self._require_clean_primary(primary, "after delivery")
        if not request.push_current_branch:
            return FinaliseResult(
                request.item_commit,
                request.tracker_commit,
                merged_commit,
                False,
                (f"local branch {request.target_branch} updated without push",),
            )
        self._git(primary, "push", repository.remote, request.target_branch)
        remote_line = self._git(
            primary,
            "ls-remote",
            repository.remote,
            f"refs/heads/{request.target_branch}",
        )
        remote_revision = remote_line.split()[0] if remote_line.split() else ""
        if remote_revision != merged_commit:
            raise AdapterError("remote verification did not observe the delivered commit")
        return FinaliseResult(
            request.item_commit,
            request.tracker_commit,
            merged_commit,
            True,
            (f"remote {repository.remote}/{request.target_branch} verified",),
        )

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
        self._require_clean_primary(primary, "before report")
        if self._git(primary, "branch", "--show-current") != branch:
            self._git(primary, "checkout", branch)
        target = primary / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(request.content, encoding="utf-8")
        self._git(primary, "add", "-A", "--", relative)
        self._git(
            primary,
            "commit",
            "-m",
            f"ADDED: campaign {request.campaign_id} report",
            "--",
            relative,
        )
        report_commit = self._git(primary, "rev-parse", "HEAD")
        self._require_clean_primary(primary, "after report")
        pushed = request.mode is DeliveryMode.PROTECTED_DEFAULT or request.push_current_branch
        if not pushed:
            return FinaliseResult(
                None,
                report_commit,
                report_commit,
                False,
                (f"report committed locally on {branch}", str(target)),
            )
        self._git(primary, "push", repository.remote, branch)
        remote_line = self._git(
            primary, "ls-remote", repository.remote, f"refs/heads/{branch}"
        )
        remote_revision = remote_line.split()[0] if remote_line.split() else ""
        if remote_revision != report_commit:
            raise AdapterError("remote verification did not observe the delivered report")
        return FinaliseResult(
            None,
            report_commit,
            report_commit,
            True,
            (f"remote {repository.remote}/{branch} verified", str(target)),
        )

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

    def _require_clean_primary(self, primary: Path, timing: str) -> None:
        if self._git(primary, "status", "--porcelain", "--untracked-files=all"):
            raise AdapterError(f"primary checkout must be clean {timing}")

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
