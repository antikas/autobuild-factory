"""Pinax tracker adapter. Pinax commits every mutation itself and publishes it
from the checked-out remote default branch. When Pinax appends an event and
its commit is refused, the adapter commits exactly the tracker files Pinax
left behind and publishes them as Pinax would have."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from threading import Lock

from autobuild.domain import (
    AdapterError,
    AdapterIdentity,
    AutoBuildError,
    CampaignRef,
    ClaimReceipt,
    ClaimTaken,
    CloseEvidence,
    EvidenceError,
    ProbeResult,
    Proposal,
    ProposalRef,
    TrackerPublication,
    TrackerStop,
    WorkItem,
    WorkspaceRef,
)
from autobuild.domain.models import HoldCondition, SkippedItem, TrackerRecovery
from autobuild.adapters.tracker_commits import (
    head_on_remote,
    only_tracker_changes,
    require_committed,
    require_no_foreign_changes,
)

_TRACKER_PATHS = (".ergon",)
_REMOTE = "origin"

# Pinax exit codes for a mutating command. 0 is committed and 3 on a claim is
# a superseded claim; both are handled by the adapter. 4 is an environment
# stop and the rest are refusals; every stop carries Pinax's own explanation.
_ENVIRONMENT_EXITS = frozenset({4})
_REFUSED_EXITS = frozenset({1, 2, 5, 6, 7})
# Exit 7 with one of these report statuses: Pinax appended its event and its
# staging or commit was refused, so the event is left uncommitted and Pinax
# asks the caller to commit the shard and the projection.
_PARTIAL_WRITE_EXIT = 7
_PARTIAL_WRITE_STATUSES = frozenset({"commit_refused", "stage_failed"})
# The waits before each retry of a partial write's recovery commit, and of its
# publishing push. The first attempt runs at once.
RECOVERY_WAITS = (2.0, 5.0, 10.0)
_RECOVERY_SUBJECT = "pinax: commit the appended tracker event"


class _PartialWrite(TrackerStop):
    """Pinax appended its event and did not commit it. ``report`` is Pinax's
    own JSON report, which names the commit subject and the remote branch."""

    def __init__(self, message: str, report: dict[str, object]) -> None:
        super().__init__(message, code=_PARTIAL_WRITE_EXIT, environment=False)
        self.report = report


class PinaxTrackerAdapter:
    # Pinax commits every mutation itself; the workspace adapter verifies the
    # tracker commit instead of creating one.
    commits_itself = True

    def __init__(
        self,
        repository: Path,
        proposal_prefix: str = "abp",
        accept_unpublished_claims: bool = False,
        primary_lock: AbstractContextManager[object] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """``accept_unpublished_claims`` accepts a claim Pinax committed but
        did not publish because the checked-out branch is not the remote
        default branch; the claim then travels with the branch's pull request.
        Every other unpublished claim stops the campaign.

        ``primary_lock`` is the primary-checkout lock composition shares with
        the workspace adapter. Each Pinax command on the primary checkout is one
        indivisible call (it fetches, commits and pushes itself), so the lock is
        held for that one call and the checks of its own commit, and nothing
        else. A partial write's recovery holds it for one commit attempt at a
        time, never across a retry wait or a push. ``sleep`` is the wait
        between recovery attempts; tests inject a recorder."""

        self._primary_lock = primary_lock if primary_lock is not None else Lock()
        self._repository = repository.resolve(strict=False)
        self._proposal_prefix = proposal_prefix
        self._accept_unpublished_claims = accept_unpublished_claims
        self._sleep = sleep
        # The report of the last partial write on the primary checkout whose
        # recovery did not succeed, so a later ``recover`` commits it with
        # Pinax's own subject. The guard covers only this reference.
        self._pending: dict[str, object] | None = None
        # Ready items passed over for want of a brief, by item id. The same
        # guard covers it.
        self._skipped: dict[str, SkippedItem] = {}
        self._pending_guard = Lock()

    def probe(self) -> ProbeResult:
        executable = shutil.which("pinax")
        if executable is None:
            return ProbeResult.unavailable("pinax executable was not found")
        if not (self._repository / ".ergon").is_dir():
            return ProbeResult.unavailable(f"Pinax is not initialised at {self._repository}")
        completed = subprocess.run(
            [executable, "--help"], capture_output=True, text=True, check=False
        )
        if completed.returncode != 0:
            return ProbeResult.unavailable(completed.stderr.strip() or "pinax probe failed")
        return ProbeResult.ready(
            AdapterIdentity(
                "pinax-tracker",
                "cli",
                frozenset({"queue", "claim", "close", "park", "proposal-gate"}),
            ),
            str(self._repository),
        )

    def next_item(self, campaign: CampaignRef) -> WorkItem | None:
        root = campaign.repository.resolve(strict=False)
        if root != self._repository:
            raise AdapterError("campaign repository does not match the bound Pinax repository")
        completed = self._run(root, "next", "--actor", "coordinator@autobuild", "--json")
        if completed.returncode != 0:
            combined = f"{completed.stdout}\n{completed.stderr}".casefold()
            if "no ready" in combined or "queue" in combined and "empty" in combined:
                return None
            raise AdapterError(completed.stderr.strip() or completed.stdout.strip() or "pinax next failed")
        if not completed.stdout.strip():
            return None
        payload = json.loads(completed.stdout)
        if payload.get("item_id") is None:
            return None
        item_id = str(payload["item_id"])
        head = self._fenced_item(campaign, root, item_id, str(payload["title"]))
        if head is not None:
            return head
        # The head is outside the selection fence or has no brief: the next
        # item is the first ready one inside the fence that has a brief.
        return next(iter(self.ready_items(campaign)), None)

    def ready_items(self, campaign: CampaignRef) -> tuple[WorkItem, ...]:
        root = campaign.repository.resolve(strict=False)
        if root != self._repository:
            raise AdapterError("campaign repository does not match the bound Pinax repository")
        completed = self._run(
            root, "ready", "--actor", "coordinator@autobuild", "--json"
        )
        if completed.returncode != 0:
            combined = f"{completed.stdout}\n{completed.stderr}".casefold()
            if "no ready" in combined or ("queue" in combined and "empty" in combined):
                return ()
            raise AdapterError(
                completed.stderr.strip() or completed.stdout.strip() or "pinax ready failed"
            )
        if not completed.stdout.strip():
            return ()
        payload = json.loads(completed.stdout)
        rows = payload.get("items", payload) if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            raise AdapterError("pinax ready returned an unexpected JSON shape")
        titles = self._titles(root)
        items: list[WorkItem] = []
        for row in rows:
            if isinstance(row, dict):
                item_id = str(row.get("item_id") or "")
                title = str(row.get("title") or titles.get(item_id, "") or item_id)
            elif isinstance(row, str):
                item_id = row
                title = titles.get(item_id, "") or item_id
            else:
                continue
            if not item_id:
                continue
            item = self._fenced_item(campaign, root, item_id, title)
            if item is not None:
                items.append(item)
        return tuple(items)

    def _fenced_item(
        self, campaign: CampaignRef, root: Path, item_id: str, title: str
    ) -> WorkItem | None:
        """The ready item as a ``WorkItem``, or None when it is not runnable
        in this campaign. An item outside the allow and exclude lists is never
        brief-checked. An admissible item without an approved brief, for
        example a proposal another thread is still adding and gating, is
        recorded as skipped so the campaign reports it."""

        if not campaign.selection.permits(item_id):
            return None
        try:
            brief_ref, caption = self._brief_note(root, item_id)
        except EvidenceError:
            with self._pending_guard:
                self._skipped.setdefault(item_id, SkippedItem(item_id, "no_brief"))
            return None
        return WorkItem(
            item_id,
            title or item_id,
            brief_ref,
            (caption or f"Measurable acceptance is defined by {brief_ref}",),
        )

    @staticmethod
    def _titles(root: Path) -> dict[str, str]:
        titles: dict[str, str] = {}
        for log in (root / ".ergon" / "log").glob("*.jsonl"):
            for line in log.read_text(encoding="utf-8").splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") != "item.created":
                    continue
                payload = event.get("payload", {})
                item_id = str(payload.get("item_id", ""))
                title = str(payload.get("title", ""))
                if item_id and title:
                    titles[item_id] = title
        return titles

    def resumable_claims(self, campaign: CampaignRef) -> tuple[WorkItem, ...]:
        root = campaign.repository.resolve(strict=False)
        if root != self._repository:
            raise AdapterError("campaign repository does not match the bound Pinax repository")
        items_dir = root / ".ergon" / "items"
        if not items_dir.is_dir():
            return ()
        terminal = {"done", "shipped", "parked", "cancelled", "blocked"}
        result: list[WorkItem] = []
        for path in sorted(items_dir.glob("*.md")):
            front = self._frontmatter(path)
            item_id = front.get("id", "").strip()
            owner = front.get("owner", "").strip()
            status = front.get("status", "").strip().casefold()
            if not item_id or not owner.casefold().startswith("builder"):
                continue
            if status in terminal:
                continue
            try:
                brief_ref, caption = self._brief_note(root, item_id)
            except EvidenceError:
                continue
            result.append(
                WorkItem(
                    item_id,
                    front.get("title", "").strip() or item_id,
                    brief_ref,
                    (caption or f"Measurable acceptance is defined by {brief_ref}",),
                )
            )
        return tuple(result)

    @staticmethod
    def _frontmatter(path: Path) -> dict[str, str]:
        front: dict[str, str] = {}
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return front
        if not lines or lines[0].strip() != "---":
            return front
        for line in lines[1:]:
            if line.strip() == "---":
                break
            key, sep, value = line.partition(":")
            if sep:
                front[key.strip()] = value.strip()
        return front

    def claim(self, item: WorkItem, actor: str) -> ClaimReceipt:
        def clean() -> None:
            # A write left uncommitted by an earlier command or run is
            # committed first, under this same lock hold; the claim's own push
            # publishes it.
            if only_tracker_changes(self._git, self._repository, _TRACKER_PATHS):
                self._commit_leftover(self._repository, self._pending_subject())
            # An untracked product file is not dirt: the claim commits tracker
            # paths only, so it can never sweep one in.
            dirty = [
                line
                for line in self._git(
                    self._repository, "status", "--porcelain", "--untracked-files=all"
                ).splitlines()
                if line[3:]
                and not (
                    line.startswith("??")
                    and not any(
                        line[3:] == path or line[3:].startswith(path + "/")
                        for path in _TRACKER_PATHS
                    )
                )
            ]
            if dirty:
                raise AdapterError("primary checkout must be clean before a tracker claim")

        _, publication = self._on_primary(
            clean, "claim", item.item_id, "--actor", actor, "--json"
        )
        return ClaimReceipt(
            item.item_id,
            actor,
            datetime.now(UTC).isoformat(),
            published=publication.published,
            publication_note=publication.note,
        )

    def close(
        self,
        evidence: CloseEvidence,
        item_commit: str,
        workspace: WorkspaceRef,
        actor: str,
    ) -> None:
        briefing = workspace.root / f".autobuild-{evidence.item_id}-briefing.md"
        briefing.write_text(self._briefing(evidence, item_commit), encoding="utf-8")
        try:
            self._in_worktree(
                workspace.root,
                "done",
                evidence.item_id,
                "--briefing",
                str(briefing),
                "--actor",
                actor,
                "--json",
            )
        finally:
            briefing.unlink(missing_ok=True)

    def park(
        self, item_id: str, reason: str, actor: str, workspace: WorkspaceRef | None = None
    ) -> TrackerPublication | None:
        if workspace is not None:
            self._in_worktree(
                workspace.root, "park", item_id, "--reason", reason, "--actor", actor, "--json"
            )
            return None
        _, publication = self._on_primary(
            self._no_foreign_changes,
            "park", item_id, "--reason", reason, "--actor", actor, "--json",
        )
        return publication

    def propose(self, proposal: Proposal, actor: str) -> ProposalRef:
        self.validate_proposal(proposal)
        created, added = self._on_primary(
            self._no_foreign_changes,
            "add",
            "--title",
            proposal.title,
            "--prefix",
            self._proposal_prefix,
            "--allow-new-prefix",
            "--actor",
            actor,
            "--json",
        )
        proposal_id = str(created.get("item_id") or created.get("id") or "")
        if not proposal_id:
            raise AdapterError("pinax add did not return a proposal id")
        _, blocked = self._on_primary(
            None,
            "block",
            proposal_id,
            "--gate",
            "proposal",
            "--actor",
            actor,
            "--json",
        )
        caption = f"{proposal.question} {proposal.rationale}"[:200]
        _, noted = self._on_primary(
            None,
            "note",
            "add",
            proposal_id,
            "--ref",
            proposal.brief_ref,
            "--caption",
            caption,
            "--actor",
            actor,
            "--json",
        )
        writes = (added, blocked, noted)
        return ProposalRef(
            proposal_id,
            runnable=False,
            published=all(write.published for write in writes),
            publication_note=next((write.note for write in writes if not write.published), ""),
        )

    @staticmethod
    def is_projection(path: str) -> bool:
        """True for a file Pinax generates from its log: the board and the
        per-item pages. The ``.jsonl`` log shards are the source, never a
        projection."""

        normal = path.replace("\\", "/")
        return normal == ".ergon/board.md" or (
            normal.startswith(".ergon/items/") and normal.endswith(".md")
        )

    def regenerate_projection(self, root: Path) -> None:
        """Rebuild the projection in ``root`` from its log. Pinax writes the
        files; the caller stages and commits them."""

        completed = self._run(root.resolve(strict=False), "verify", "--fix")
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise AdapterError(f"pinax verify --fix exit {completed.returncode}: {detail}")

    @staticmethod
    def validate_proposal(proposal: Proposal) -> None:
        reference = proposal.brief_ref.strip()
        if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", reference):
            return
        normalised = reference.replace("\\", "/")
        path = PurePosixPath(normalised)
        unsafe = (
            normalised.startswith(("/", "~"))
            or re.match(r"^[A-Za-z]:/", normalised) is not None
            or any(part in {".", ".."} for part in path.parts)
        )
        if unsafe:
            raise EvidenceError(
                "proposal brief_ref must be a repository-relative path or durable URI"
            )

    @staticmethod
    def _brief_note(root: Path, item_id: str) -> tuple[str, str]:
        notes: list[tuple[str, str, str]] = []
        for log in (root / ".ergon" / "log").glob("*.jsonl"):
            for line in log.read_text(encoding="utf-8").splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = event.get("payload", {})
                if event.get("type") == "note.added" and payload.get("item_id") == item_id:
                    notes.append(
                        (
                            str(event.get("ts", "")),
                            str(payload.get("ref", "")),
                            str(payload.get("caption", "")),
                        )
                    )
        if not notes:
            raise EvidenceError(f"Pinax item {item_id} has no approved brief reference")
        _, ref, caption = max(notes)
        if not ref:
            raise EvidenceError(f"Pinax item {item_id} has an empty brief reference")
        return ref, caption

    @staticmethod
    def _briefing(evidence: CloseEvidence, item_commit: str) -> str:
        changed = "\n".join(f"- {entry.kind.value}: `{entry.path.as_posix()}`" for entry in evidence.diff.changed_paths)
        return (
            f"## Outcome\n\nAccepted and delivered by AutoBuild.\n\n"
            f"## Evidence\n\n"
            f"- Item commit: `{item_commit}`\n"
            f"- Workspace revision: `{evidence.workspace_revision}`\n"
            f"- Validator: `{evidence.validation.validator_id}`\n"
            f"- Validator output: `{evidence.validation.command.stdout_ref}`\n"
            f"- Review verdict: `{evidence.verdict.evidence_ref}`\n"
            f"- Trajectory: `{evidence.trajectory_ref}`\n\n"
            f"## Changed paths\n\n{changed or '- None'}\n"
        )

    def recover(self) -> TrackerRecovery:
        """Commit a partial write left on the primary checkout, publish it,
        and check that the remote answers. One attempt; the campaign repeats
        it while it holds admissions."""

        try:
            with self._primary_lock:
                committed = self._commit_leftover(self._repository, self._pending_subject())
        except Exception as exc:
            return TrackerRecovery(False, HoldCondition.TRACKER_PENDING, str(exc))
        try:
            default_branch = self._remote_default_branch()
        except AdapterError as exc:
            return TrackerRecovery(False, HoldCondition.TRACKER_UNREACHABLE, str(exc))
        if committed is not None:
            self._publish(self._repository, default_branch)
        return TrackerRecovery()

    def skipped_items(self) -> tuple[SkippedItem, ...]:
        with self._pending_guard:
            return tuple(self._skipped.values())

    def _pending_subject(self) -> str:
        """Pinax's own commit subject for the pending partial write, when one
        is known."""

        with self._pending_guard:
            pending = self._pending
        return str((pending or {}).get("commit_message") or _RECOVERY_SUBJECT)

    def _no_foreign_changes(self) -> None:
        require_no_foreign_changes(self._git, self._repository, _TRACKER_PATHS)

    def _on_primary(
        self, check: Callable[[], None] | None, *args: str
    ) -> tuple[dict[str, object], TrackerPublication]:
        """Run one mutating command on the primary checkout under the lock,
        after ``check``. A partial write is recovered after the lock is let go."""

        try:
            with self._primary_lock:
                if check is not None:
                    check()
                return self._mutate(self._repository, *args)
        except _PartialWrite as partial:
            return self._finish_partial(self._repository, partial)

    def _in_worktree(self, root: Path, *args: str) -> None:
        """Run one mutating command in an item worktree, which only its item
        writes, recovering a partial write there."""

        try:
            self._mutate(root, *args)
        except _PartialWrite as partial:
            self._finish_partial(root, partial)

    def _finish_partial(
        self, root: Path, partial: _PartialWrite
    ) -> tuple[dict[str, object], TrackerPublication]:
        """Do what Pinax would have done after its refused commit: commit the
        tracker paths it left behind, with its own subject, then publish from
        the primary checkout when the remote default branch is checked out.

        Each commit attempt holds the primary-checkout lock (for the primary)
        for that attempt alone; the waits between attempts and the push run
        outside it. When every attempt fails the write stays pending, the
        error is raised as the original refusal, and ``recover`` retries it."""

        primary = root == self._repository
        subject = str(partial.report.get("commit_message") or _RECOVERY_SUBJECT)
        failure = ""
        for wait in (0.0, *RECOVERY_WAITS):
            if wait:
                self._sleep(wait)
            try:
                with self._primary_lock if primary else nullcontext():
                    self._commit_leftover(root, subject)
            except AutoBuildError as exc:
                failure = str(exc)
                continue
            break
        else:
            if primary:
                with self._pending_guard:
                    self._pending = partial.report
            raise TrackerStop(
                f"{partial}; recovery of the appended event failed: {failure}",
                code=partial.code,
                environment=False,
            )
        if not primary:
            return partial.report, TrackerPublication(False, "")
        published = self._publish(root, str(partial.report.get("remote_branch") or ""))
        note = "" if published else (
            "the recovered tracker commit was not pushed; it goes out with the next push of its branch"
        )
        return partial.report, TrackerPublication(published, note)

    def _commit_leftover(self, root: Path, subject: str) -> str | None:
        """Commit the tracker files left uncommitted in ``root``; the caller
        holds the lock that covers ``root``. Returns the new commit, or None
        when nothing is left (another tracker commit already took it in). The
        projection is regenerated first when it no longer matches the log, and
        the commit must touch tracker paths only."""

        leftover = self._git(
            root, "status", "--porcelain", "--untracked-files=all", "--", *_TRACKER_PATHS
        ).strip()
        if not leftover:
            return None
        if self._run(root, "verify").returncode != 0:
            self.regenerate_projection(root)
        before = self._git(root, "rev-parse", "HEAD")
        self._git(root, "add", "-A", "--", *_TRACKER_PATHS)
        self._git(root, "commit", "-m", subject, "--", *_TRACKER_PATHS)
        head = require_committed(self._git, root, before, _TRACKER_PATHS)
        if root == self._repository:
            with self._pending_guard:
                self._pending = None
        return head

    def _publish(self, root: Path, branch: str) -> bool:
        """Push the primary checkout's head to ``branch`` on the remote when
        that branch is checked out, as Pinax publishes. A failed push is tried
        again after each recovery wait. Returns whether the remote took it."""

        if not branch or self._git(root, "branch", "--show-current") != branch:
            return False
        head = self._git(root, "rev-parse", "HEAD")
        for wait in (0.0, *RECOVERY_WAITS):
            if wait:
                self._sleep(wait)
            try:
                self._git(root, "push", _REMOTE, f"{head}:refs/heads/{branch}")
            except AdapterError:
                continue
            return True
        return False

    def _remote_default_branch(self) -> str:
        """The remote's default branch, read from the remote itself, so a
        remote that does not answer raises."""

        listing = self._git(self._repository, "ls-remote", "--symref", _REMOTE, "HEAD")
        for line in listing.splitlines():
            reference, _, name = line.partition("\t")
            if name == "HEAD" and reference.startswith("ref: refs/heads/"):
                return reference.removeprefix("ref: refs/heads/")
        return ""

    def _mutate(
        self, root: Path, *args: str
    ) -> tuple[dict[str, object], TrackerPublication]:
        """Run one mutating Pinax command and confirm it committed its event.

        Pinax appends, commits and, on the checked-out remote default branch,
        pushes in the one command, so the adapter stages or commits tracker
        files only to finish a write Pinax appended and could not commit
        (``_PartialWrite``, finished by the caller). A nonzero exit is mapped
        to a typed stop that carries Pinax's own report. Returns the command's
        JSON and its publication."""

        command = args[0]
        before = self._git(root, "rev-parse", "HEAD")
        completed = self._run(root, *args)
        code = completed.returncode
        report = self._last_json(completed.stdout)
        notes = completed.stderr.strip()
        cause = "; ".join(
            part for part in (str(report.get("message", "")).strip(), notes) if part
        ) or completed.stdout.strip()
        if code == 3 and command == "claim":
            raise ClaimTaken(args[1], cause or "claim superseded")
        if code == 4 and command == "claim" and self._branch_local_claim(report):
            require_committed(self._git, root, before, _TRACKER_PATHS)
            return report, TrackerPublication(False, cause)
        status = str(report.get("status", "")).strip()
        label = f"pinax {command} exit {code}" + (f" ({status})" if status else "")
        if (
            code == _PARTIAL_WRITE_EXIT
            and report.get("committed") is False
            and status in _PARTIAL_WRITE_STATUSES
        ):
            raise _PartialWrite(f"{label}: {cause}", report)
        if code in _ENVIRONMENT_EXITS:
            raise TrackerStop(f"{label}: {cause}", code=code, environment=True)
        if code in _REFUSED_EXITS:
            raise TrackerStop(f"{label}: {cause}", code=code, environment=False)
        if code != 0:
            raise AdapterError(f"{label}: {cause}")
        require_committed(self._git, root, before, _TRACKER_PATHS)
        payload = self._parse(command, completed.stdout)
        pushed = payload.get("pushed")
        published = pushed if isinstance(pushed, bool) else head_on_remote(self._git, root)
        return payload, TrackerPublication(published, "" if published else notes)

    def _branch_local_claim(self, report: dict[str, object]) -> bool:
        """A claim Pinax committed and did not push only because the checked-out
        branch is not the remote default branch, in a mode that accepts it."""

        head_branch = str(report.get("head_branch") or "")
        return (
            self._accept_unpublished_claims
            and report.get("status") == "committed_local"
            and report.get("committed") is True
            and bool(head_branch)
            and head_branch != str(report.get("remote_branch") or "")
        )

    @staticmethod
    def _last_json(stdout: str) -> dict[str, object]:
        for line in reversed(stdout.strip().splitlines()):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            return payload if isinstance(payload, dict) else {}
        return {}

    @staticmethod
    def _parse(command: str, stdout: str) -> dict[str, object]:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise AdapterError(f"pinax returned invalid JSON for {command}") from exc
        if not isinstance(payload, dict):
            raise AdapterError(f"pinax returned a non-object JSON payload for {command}")
        return payload

    @staticmethod
    def _run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["pinax", "--root", str(root), *args],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

    @staticmethod
    def _git(root: Path, *args: str) -> str:
        attempts = 5 if args and args[0] in {"add", "commit", "push"} else 1
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
        if completed.returncode != 0:
            raise AdapterError(f"git {' '.join(args)} failed: {detail}")
        return completed.stdout.rstrip("\r\n")
