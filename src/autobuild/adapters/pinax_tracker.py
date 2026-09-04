"""Pinax tracker adapter with committed claim, close, park and proposal state."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from autobuild.domain import (
    AdapterError,
    AdapterIdentity,
    CampaignRef,
    ClaimReceipt,
    CloseEvidence,
    EvidenceError,
    ProbeResult,
    Proposal,
    ProposalRef,
    WorkItem,
    WorkspaceRef,
)


class PinaxTrackerAdapter:
    def __init__(
        self,
        repository: Path,
        proposal_prefix: str = "abp",
        remote: str = "origin",
        push_primary: bool = True,
    ) -> None:
        self._repository = repository.resolve(strict=False)
        self._proposal_prefix = proposal_prefix
        self._remote = remote
        self._push_primary = push_primary

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
        completed = self._run(root, "next", "--actor", "coordinator@autobuild", "--json", check=False)
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
        title = str(payload["title"])
        brief_ref, caption = self._brief_note(root, item_id)
        return WorkItem(
            item_id,
            title,
            brief_ref,
            (caption or f"Measurable acceptance is defined by {brief_ref}",),
        )

    def ready_items(self, campaign: CampaignRef) -> tuple[WorkItem, ...]:
        root = campaign.repository.resolve(strict=False)
        if root != self._repository:
            raise AdapterError("campaign repository does not match the bound Pinax repository")
        completed = self._run(
            root, "ready", "--actor", "coordinator@autobuild", "--json", check=False
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
            brief_ref, caption = self._brief_note(root, item_id)
            items.append(
                WorkItem(
                    item_id,
                    title,
                    brief_ref,
                    (caption or f"Measurable acceptance is defined by {brief_ref}",),
                )
            )
        return tuple(items)

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
        if self._git(
            self._repository, "status", "--porcelain", "--untracked-files=all"
        ).strip():
            raise AdapterError("primary checkout must be clean before a tracker claim")
        self._json(self._repository, "claim", item.item_id, "--actor", actor, "--json")
        self._commit_primary(f"MODIFIED: claim {item.item_id} in Pinax")
        return ClaimReceipt(item.item_id, actor, datetime.now(UTC).isoformat())

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
            self._json(
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
    ) -> None:
        root = workspace.root if workspace is not None else self._repository
        self._json(root, "park", item_id, "--reason", reason, "--actor", actor, "--json")
        if workspace is None:
            self._commit_primary(f"MODIFIED: park {item_id} in Pinax")

    def propose(self, proposal: Proposal, actor: str) -> ProposalRef:
        self.validate_proposal(proposal)
        created = self._json(
            self._repository,
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
        self._json(
            self._repository,
            "block",
            proposal_id,
            "--gate",
            "proposal",
            "--actor",
            actor,
            "--json",
        )
        caption = f"{proposal.question} {proposal.rationale}"[:200]
        self._json(
            self._repository,
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
        self._commit_primary(f"ADDED: propose {proposal.title} in Pinax")
        return ProposalRef(proposal_id, runnable=False)

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

    def _commit_primary(self, message: str) -> str:
        status = self._git(self._repository, "status", "--porcelain", "--untracked-files=all")
        non_tracker = [line for line in status.splitlines() if not self._tracker_status_line(line)]
        if non_tracker:
            raise AdapterError("primary checkout has non-tracker changes; refusing tracker commit")
        if not status.strip():
            raise EvidenceError("Pinax command produced no tracker state to commit")
        self._git(self._repository, "add", "-A", "--", ".ergon")
        self._git(self._repository, "commit", "-m", message, "--", ".ergon")
        revision = self._git(self._repository, "rev-parse", "HEAD")
        if not self._push_primary:
            return revision
        self._git(self._repository, "push", self._remote, "HEAD")
        branch = self._git(self._repository, "branch", "--show-current")
        remote_line = self._git(
            self._repository, "ls-remote", self._remote, f"refs/heads/{branch}"
        )
        remote_revision = remote_line.split()[0] if remote_line.split() else ""
        if remote_revision != revision:
            raise AdapterError("remote verification did not observe the tracker commit")
        return revision

    @staticmethod
    def _tracker_status_line(line: str) -> bool:
        path = line[3:].replace("\\", "/")
        return path == ".ergon" or path.startswith(".ergon/")

    def _json(self, root: Path, *args: str) -> dict[str, object]:
        completed = self._run(root, *args)
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise AdapterError(f"pinax returned invalid JSON for {args[0]}") from exc
        if not isinstance(payload, dict):
            raise AdapterError(f"pinax returned a non-object JSON payload for {args[0]}")
        return payload

    @staticmethod
    def _run(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            ["pinax", "--root", str(root), *args],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if check and completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise AdapterError(f"pinax {args[0]} failed: {detail}")
        return completed

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
