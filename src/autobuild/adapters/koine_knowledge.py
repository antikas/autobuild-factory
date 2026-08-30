"""Koine knowledge adapter for durable recall and the vault fog ledger."""

from __future__ import annotations

import os
import re
import subprocess
from datetime import date
from pathlib import Path
from threading import Lock
from uuid import uuid4

from autobuild.domain import (
    AdapterError,
    AdapterIdentity,
    DurableContext,
    FogRecord,
    ProbeResult,
)


class KoineKnowledgeAdapter:
    def __init__(
        self,
        fog_ledger: Path,
        command: tuple[str, ...] = ("koine-memory",),
        top_k: int = 5,
    ) -> None:
        if not command:
            raise ValueError("Koine command must not be empty")
        self._fog_ledger = fog_ledger.resolve(strict=False)
        self._command = command
        self._top_k = top_k
        self._lock = Lock()

    def probe(self) -> ProbeResult:
        try:
            completed = subprocess.run(
                [*self._command, "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=self._environment(),
                check=False,
            )
        except OSError as exc:
            return ProbeResult.unavailable(f"Koine command is unavailable: {exc}")
        if completed.returncode != 0:
            return ProbeResult.unavailable(completed.stderr.strip() or "Koine probe failed")
        if not self._fog_ledger.is_file():
            return ProbeResult.unavailable(f"fog ledger is unavailable: {self._fog_ledger}")
        return ProbeResult.ready(
            AdapterIdentity(
                "koine-knowledge",
                completed.stdout.strip() or "unknown",
                frozenset({"durable-recall", "fog-ledger"}),
            ),
            str(self._fog_ledger),
        )

    def retrieve(self, query: str) -> DurableContext:
        completed = subprocess.run(
            [*self._command, "query", query, "-k", str(self._top_k), "--no-fallback"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self._environment(),
            check=False,
        )
        if completed.returncode != 0:
            raise AdapterError(completed.stderr.strip() or "Koine recall failed")
        report = completed.stdout.strip()
        references = tuple(
            match.group(1).strip()
            for match in re.finditer(r"(?m)^## \d+\. (.+?)(?: —|$)", report)
        )
        return DurableContext(query, references, (report,) if report else ())

    def record_fog(self, fog: FogRecord) -> str:
        with self._lock:
            content = self._fog_ledger.read_text(encoding="utf-8")
            ids = [int(value) for value in re.findall(r"(?m)^### B-(\d+)\b", content)]
            item_id = f"B-{max(ids, default=0) + 1:03d}"
            block = (
                f"\n\n### {item_id} — {fog.direction.strip()}\n\n"
                f"- **Added**: {date.today().isoformat()}\n"
                f"- **Themes**: autobuild, fog\n"
                f"- **Surface when**: {fog.surface_when.strip()}\n"
                f"- **Context**: {fog.blocking_question.strip()}\n"
            )
            temporary = self._fog_ledger.with_name(
                f".{self._fog_ledger.name}.{uuid4().hex}.tmp"
            )
            temporary.write_text(content.rstrip() + block, encoding="utf-8")
            temporary.replace(self._fog_ledger)
        return f"{self._fog_ledger}#{item_id}"

    @staticmethod
    def _environment() -> dict[str, str]:
        environment = os.environ.copy()
        environment["PYTHONUTF8"] = "1"
        return environment
