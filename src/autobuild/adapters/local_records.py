"""Filesystem-backed run records with evidence references, not transcripts."""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any, Mapping
from uuid import uuid4

from autobuild.domain import AdapterIdentity, CampaignRef, ProbeResult, RunEvent, RunRecordRef


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    return cleaned or "record"


def _atomic_write(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(content, encoding="utf-8")
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
        payload = asdict(event)
        payload["evidence_refs"] = list(event.evidence_refs)
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

    def complete(self, record: RunRecordRef, summary: str) -> str:
        path = record.root / "report.txt"
        _atomic_write(path, summary)
        return str(path)
