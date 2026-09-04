"""Filesystem-backed run records with evidence references, not transcripts."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any, Mapping
from uuid import uuid4

from autobuild.domain import (
    AdapterIdentity,
    CampaignRef,
    PhaseMarker,
    ProbeResult,
    RunEvent,
    RunRecordRef,
)


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    return cleaned or "record"


def _safe_relative(value: str) -> str:
    parts = [part for part in value.replace("\\", "/").split("/") if part and part != "."]
    if not parts or any(part == ".." for part in parts):
        raise ValueError(f"evidence path must be relative and free of traversal: {value}")
    return "/".join(_safe_name(part) for part in parts)


def _atomic_write(path: Path, content: str) -> None:
    _atomic_write_bytes(path, content.encode("utf-8"))


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


class LocalRunRecordAdapter:
    def __init__(self, root: Path, metadata: Mapping[str, Any] | None = None) -> None:
        self._root = root.resolve(strict=False)
        self._metadata = dict(metadata or {})
        self._lock = Lock()

    def probe(self) -> ProbeResult:
        try:
            self._root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return ProbeResult.unavailable(f"run-record root is unavailable: {exc}")
        return ProbeResult.ready(
            AdapterIdentity("local-run-record", "1", frozenset({"jsonl", "atomic-evidence"})),
            str(self._root),
        )

    def create(self, campaign: CampaignRef) -> RunRecordRef:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        run_id = f"{_safe_name(campaign.campaign_id)}-{stamp}-{uuid4().hex[:8]}"
        record_root = self._root / run_id
        record_root.mkdir(parents=True, exist_ok=False)
        manifest = {
            "schema": "autobuild.run.v1",
            "run_id": run_id,
            "campaign_id": campaign.campaign_id,
            "repository": str(campaign.repository.resolve(strict=False)),
            "created_at": datetime.now(UTC).isoformat(),
            "runtime": self._metadata,
        }
        _atomic_write(record_root / "manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
        (record_root / "events.jsonl").touch()
        (record_root / "evidence").mkdir()
        return RunRecordRef(run_id, record_root)

    def append(self, record: RunRecordRef, event: RunEvent) -> str:
        path = record.root / "events.jsonl"
        stamped = event
        if not event.occurred_at.strip():
            stamped = replace(event, occurred_at=datetime.now(UTC).isoformat())
        payload = asdict(stamped)
        payload["evidence_refs"] = list(stamped.evidence_refs)
        payload["payload"] = dict(stamped.payload)
        line = json.dumps(payload, sort_keys=True)
        with self._lock:
            with path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(line + "\n")
            with path.open("r", encoding="utf-8") as stream:
                sequence = sum(1 for _ in stream)
        return f"{path}#L{sequence}"

    def write_evidence(self, record: RunRecordRef, name: str, content: str) -> str:
        path = record.root / "evidence" / f"{_safe_name(name)}.txt"
        _atomic_write(path, content)
        return str(path)

    def write_evidence_file(
        self, record: RunRecordRef, relative_path: str, content: bytes
    ) -> str:
        path = record.root / "evidence" / _safe_relative(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_bytes(path, content)
        return str(path)

    def latest_phase_marker(self, item_id: str) -> PhaseMarker | None:
        relative = _safe_relative(f"{item_id}-phase.json")
        best: tuple[float, str, Path] | None = None
        try:
            run_dirs = list(self._root.iterdir())
        except OSError:
            return None
        for run_dir in run_dirs:
            candidate = run_dir / "evidence" / relative
            try:
                mtime = candidate.stat().st_mtime
            except OSError:
                continue
            if best is None or mtime > best[0]:
                best = (mtime, run_dir.name, candidate)
        if best is None:
            return None
        return self._read_marker(best[2], best[1])

    @staticmethod
    def _read_marker(path: Path, run_id: str) -> PhaseMarker | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict) or payload.get("schema") != "autobuild.item-phase.v1":
            return None
        try:
            return PhaseMarker(
                item_id=str(payload["item_id"]),
                state=str(payload["state"]),
                worktree_root=Path(str(payload.get("worktree_root", ""))),
                branch=str(payload.get("branch", "")),
                head_commit=str(payload.get("head_commit", "")),
                workspace_revision=str(payload.get("workspace_revision", "")),
                correction_count=int(payload.get("correction_count", 0)),
                run_id=run_id,
            )
        except (KeyError, TypeError, ValueError):
            return None

    def complete(self, record: RunRecordRef, summary: str) -> str:
        path = record.root / "report.txt"
        _atomic_write(path, summary)
        return str(path)
