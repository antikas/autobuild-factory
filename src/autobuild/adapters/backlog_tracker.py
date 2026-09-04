"""Markdown BACKLOG tracker adapter for repositories that do not use Pinax."""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

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


_READY = {"ready", "approved", "queued", "todo", "to do", "not started"}
_KNOWN_PREFIXES = ("claimed", "in progress", "done", "accepted", "shipped", "parked", "blocked", "proposed")


@dataclass(frozen=True, slots=True)
class _Row:
    line_index: int
    cells: tuple[str, ...]
    item_id: str
    title: str
    status: str
    brief_ref: str


@dataclass(frozen=True, slots=True)
class _Table:
    lines: tuple[str, ...]
    columns: dict[str, int]
    separator_index: int
    rows: tuple[_Row, ...]


class BacklogTrackerAdapter:
    """Treat one small Markdown table as durable tracker state.

    The supported columns are ``Item``, ``Title``, ``Status`` and ``Brief``.
    Row order is queue order. Proposed rows are deliberately non-runnable.
    """

    def __init__(
        self,
        repository: Path,
        backlog_path: Path | str = "BACKLOG.md",
        proposal_prefix: str = "ABP",
        remote: str = "origin",
        push_primary: bool = True,
    ) -> None:
        self._repository = repository.resolve(strict=False)
        candidate = Path(backlog_path).expanduser()
        resolved = (
            candidate.resolve(strict=False)
            if candidate.is_absolute()
            else (self._repository / candidate).resolve(strict=False)
        )
        try:
            self._relative_path = resolved.relative_to(self._repository)
        except ValueError as exc:
            raise AdapterError("BACKLOG.md must be inside the campaign repository") from exc
        self._proposal_prefix = proposal_prefix
        self._remote = remote
        self._push_primary = push_primary

    @property
    def tracker_path(self) -> Path:
        return self._relative_path

    def probe(self) -> ProbeResult:
        path = self._path(self._repository)
        if not path.is_file():
            return ProbeResult.unavailable(f"BACKLOG tracker was not found: {path}")
        try:
            self._read(self._repository)
        except (AdapterError, EvidenceError) as exc:
            return ProbeResult.unavailable(str(exc))
        return ProbeResult.ready(
            AdapterIdentity(
                "markdown-backlog-tracker",
                "1",
                frozenset({"queue", "claim", "close", "park", "proposal-gate"}),
            ),
            str(path),
        )

    def next_item(self, campaign: CampaignRef) -> WorkItem | None:
        root = campaign.repository.resolve(strict=False)
        if root != self._repository:
            raise AdapterError("campaign repository does not match the bound BACKLOG repository")
        for row in self._read(root).rows:
            if self._status_kind(row.status) == "ready":
                return WorkItem(
                    row.item_id,
                    row.title,
                    row.brief_ref,
                    (f"Measurable acceptance is defined by {row.brief_ref}",),
                )
        return None

    def ready_items(self, campaign: CampaignRef) -> tuple[WorkItem, ...]:
        root = campaign.repository.resolve(strict=False)
        if root != self._repository:
            raise AdapterError("campaign repository does not match the bound BACKLOG repository")
        return tuple(
            WorkItem(
                row.item_id,
                row.title,
                row.brief_ref,
                (f"Measurable acceptance is defined by {row.brief_ref}",),
            )
            for row in self._read(root).rows
            if self._status_kind(row.status) == "ready"
        )

    def resumable_claims(self, campaign: CampaignRef) -> tuple[WorkItem, ...]:
        root = campaign.repository.resolve(strict=False)
        if root != self._repository:
            raise AdapterError("campaign repository does not match the bound BACKLOG repository")
        items: list[WorkItem] = []
        for row in self._read(root).rows:
            if self._status_kind(row.status) != "claimed":
                continue
            actor = self._claim_actor(row.status)
            if not actor.casefold().startswith("builder"):
                continue
            items.append(
                WorkItem(
                    row.item_id,
                    row.title,
                    row.brief_ref,
                    (f"Measurable acceptance is defined by {row.brief_ref}",),
                )
            )
        return tuple(items)

    @staticmethod
    def _claim_actor(status: str) -> str:
        normal = status.replace("*", "").strip()
        prefix = "claimed by "
        if normal.casefold().startswith(prefix):
            return normal[len(prefix) :].strip()
        return ""

    def claim(self, item: WorkItem, actor: str) -> ClaimReceipt:
        if self._git(
            self._repository, "status", "--porcelain", "--untracked-files=all"
        ).strip():
            raise AdapterError("primary checkout must be clean before a tracker claim")
        row = self._find(self._read(self._repository), item.item_id)
        if self._status_kind(row.status) != "ready":
            raise EvidenceError(f"BACKLOG item {item.item_id} is not ready")
        self._set_status(self._repository, item.item_id, f"Claimed by {actor}")
        self._commit_primary(f"MODIFIED: claim {item.item_id} in BACKLOG")
        return ClaimReceipt(item.item_id, actor, datetime.now(UTC).isoformat())

    def close(
        self,
        evidence: CloseEvidence,
        item_commit: str,
        workspace: WorkspaceRef,
        actor: str,
    ) -> None:
        del actor
        self._set_status(workspace.root, evidence.item_id, f"Done ({item_commit[:12]})")

    def park(
        self,
        item_id: str,
        reason: str,
        actor: str,
        workspace: WorkspaceRef | None = None,
    ) -> None:
        del actor
        clean_reason = " ".join(reason.replace("|", "/").split())
        root = workspace.root if workspace is not None else self._repository
        self._set_status(root, item_id, f"Parked: {clean_reason}")
        if workspace is None:
            self._commit_primary(f"MODIFIED: park {item_id} in BACKLOG")

    def propose(self, proposal: Proposal, actor: str) -> ProposalRef:
        del actor
        self.validate_proposal(proposal)
        table = self._read(self._repository)
        sequence = max(
            (
                int(match.group(1))
                for row in table.rows
                if (match := re.fullmatch(
                    rf"{re.escape(self._proposal_prefix)}-(\d+)",
                    row.item_id,
                    flags=re.IGNORECASE,
                ))
            ),
            default=0,
        )
        proposal_id = f"{self._proposal_prefix}-{sequence + 1:03d}"
        header_cells = self._cells(table.lines[table.separator_index - 1])
        cells = ["" for _ in header_cells]
        cells[table.columns["item"]] = proposal_id
        cells[table.columns["title"]] = proposal.title
        question = " ".join(proposal.question.replace("|", "/").split())
        rationale = " ".join(proposal.rationale.replace("|", "/").split())
        cells[table.columns["status"]] = f"Proposed: {question} {rationale}"
        cells[table.columns["brief"]] = proposal.brief_ref
        lines = list(table.lines)
        insert_at = table.rows[-1].line_index + 1 if table.rows else table.separator_index + 1
        lines.insert(insert_at, self._render(cells))
        self._write(self._repository, lines)
        self._commit_primary(f"ADDED: propose {proposal.title} in BACKLOG")
        return ProposalRef(proposal_id, runnable=False)

    @staticmethod
    def validate_proposal(proposal: Proposal) -> None:
        if any("|" in value or "\n" in value for value in (proposal.title, proposal.brief_ref)):
            raise EvidenceError("BACKLOG proposal title and brief_ref must fit one Markdown table cell")

    def _read(self, root: Path) -> _Table:
        path = self._path(root)
        try:
            lines = tuple(path.read_text(encoding="utf-8").splitlines())
        except OSError as exc:
            raise AdapterError(f"cannot read BACKLOG tracker {path}: {exc}") from exc
        header_index = -1
        columns: dict[str, int] = {}
        aliases = {
            "item": {"item", "id", "item id"},
            "title": {"title", "work item"},
            "status": {"status", "state"},
            "brief": {"brief", "brief ref", "brief_ref", "plan"},
        }
        for index, line in enumerate(lines[:-1]):
            if not self._is_table_line(line):
                continue
            cells = self._cells(line)
            lowered = [cell.casefold() for cell in cells]
            found = {
                name: next((i for i, value in enumerate(lowered) if value in names), -1)
                for name, names in aliases.items()
            }
            if all(value >= 0 for value in found.values()):
                header_index = index
                columns = found
                break
        if header_index < 0:
            raise EvidenceError(
                "BACKLOG tracker needs a Markdown table with Item, Title, Status and Brief columns"
            )
        separator_index = header_index + 1
        if separator_index >= len(lines) or not self._separator(lines[separator_index]):
            raise EvidenceError("BACKLOG tracker table is missing its Markdown separator row")
        rows: list[_Row] = []
        seen: set[str] = set()
        for index in range(separator_index + 1, len(lines)):
            line = lines[index]
            if not self._is_table_line(line):
                break
            cells = self._cells(line)
            if len(cells) <= max(columns.values()):
                raise EvidenceError(f"BACKLOG row {index + 1} does not contain every required column")
            item_id = cells[columns["item"]].strip()
            title = cells[columns["title"]].strip()
            status = cells[columns["status"]].strip()
            brief_ref = cells[columns["brief"]].strip()
            if not all((item_id, title, status, brief_ref)):
                raise EvidenceError(f"BACKLOG row {index + 1} has an empty required field")
            if item_id in seen:
                raise EvidenceError(f"BACKLOG contains duplicate item id {item_id}")
            self._status_kind(status)
            seen.add(item_id)
            rows.append(_Row(index, tuple(cells), item_id, title, status, brief_ref))
        return _Table(lines, columns, separator_index, tuple(rows))

    def _set_status(self, root: Path, item_id: str, status: str) -> None:
        table = self._read(root)
        row = self._find(table, item_id)
        cells = list(row.cells)
        cells[table.columns["status"]] = status
        lines = list(table.lines)
        lines[row.line_index] = self._render(cells)
        self._write(root, lines)

    @staticmethod
    def _find(table: _Table, item_id: str) -> _Row:
        for row in table.rows:
            if row.item_id == item_id:
                return row
        raise EvidenceError(f"BACKLOG item {item_id} was not found")

    @staticmethod
    def _status_kind(status: str) -> str:
        normal = status.replace("*", "").strip().casefold()
        if normal in _READY:
            return "ready"
        for prefix in _KNOWN_PREFIXES:
            if normal == prefix or normal.startswith(prefix + " ") or normal.startswith(prefix + ":") or normal.startswith(prefix + " ("):
                return prefix
        raise EvidenceError(f"BACKLOG status is not recognised: {status}")

    def _commit_primary(self, message: str) -> str:
        status = self._git(self._repository, "status", "--porcelain", "--untracked-files=all")
        non_tracker = [line for line in status.splitlines() if not self._tracker_status_line(line)]
        if non_tracker:
            raise AdapterError("primary checkout has non-tracker changes; refusing tracker commit")
        if not status.strip():
            raise EvidenceError("BACKLOG command produced no tracker state to commit")
        relative = self._relative_path.as_posix()
        self._git(self._repository, "add", "-A", "--", relative)
        self._git(self._repository, "commit", "-m", message, "--", relative)
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

    def _tracker_status_line(self, line: str) -> bool:
        return line[3:].replace("\\", "/") == self._relative_path.as_posix()

    def _path(self, root: Path) -> Path:
        return root / self._relative_path

    def _write(self, root: Path, lines: list[str]) -> None:
        self._path(root).write_text("\n".join(lines) + "\n", encoding="utf-8")

    @staticmethod
    def _is_table_line(line: str) -> bool:
        stripped = line.strip()
        return stripped.startswith("|") and stripped.endswith("|")

    @staticmethod
    def _cells(line: str) -> list[str]:
        return [cell.strip() for cell in line.strip().strip("|").split("|")]

    @classmethod
    def _separator(cls, line: str) -> bool:
        return cls._is_table_line(line) and all(
            re.fullmatch(r":?-{3,}:?", cell) is not None for cell in cls._cells(line)
        )

    @staticmethod
    def _render(cells: list[str]) -> str:
        return "| " + " | ".join(cells) + " |"

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
