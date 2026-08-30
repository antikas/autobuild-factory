"""Knowledge boundary for campaigns that cannot produce refill work."""

from __future__ import annotations

from autobuild.domain import (
    AdapterIdentity,
    DurableContext,
    FogRecord,
    ProbeResult,
)


class NoRefillKnowledgeAdapter:
    def probe(self) -> ProbeResult:
        return ProbeResult.ready(
            AdapterIdentity("no-refill-knowledge", "1", frozenset({"no-refill"}))
        )

    def retrieve(self, query: str) -> DurableContext:
        return DurableContext(query, ())

    def record_fog(self, fog: FogRecord) -> str:
        raise RuntimeError("this run has no configured refill plan")
