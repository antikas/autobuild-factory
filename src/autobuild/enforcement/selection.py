"""The selection fence around the tracker port.

The campaign runner decides which ready item to build. This wrapper is the
deterministic guarantee that an item outside the allow-list, or inside the
exclude-list, can never be claimed. A tracker that offers such an item from
``next_item`` is a policy violation: the wrapper raises ``ScopeFenceViolation``
so the campaign stops without a claim. ``ready_items`` is the closed-universe
listing and passes through unchanged; the runner filters it against the fence.
"""

from __future__ import annotations

from autobuild.domain import (
    CampaignRef,
    CampaignSelection,
    ClaimReceipt,
    CloseEvidence,
    Proposal,
    ProposalRef,
    ScopeFenceViolation,
    WorkItem,
    WorkspaceRef,
)
from autobuild.ports import TrackerPort


class ScopedTrackerPort:
    """Wrap a tracker port and refuse any item outside the campaign selection."""

    def __init__(self, port: TrackerPort, selection: CampaignSelection) -> None:
        self._port = port
        self._selection = selection

    def probe(self):
        return self._port.probe()

    def ready_items(self, campaign: CampaignRef) -> tuple[WorkItem, ...]:
        return self._port.ready_items(campaign)

    def resumable_claims(self, campaign: CampaignRef) -> tuple[WorkItem, ...]:
        return self._port.resumable_claims(campaign)

    def next_item(self, campaign: CampaignRef) -> WorkItem | None:
        item = self._port.next_item(campaign)
        if item is not None and not self._selection.permits(item.item_id):
            raise ScopeFenceViolation(
                f"tracker offered out-of-fence item {item.item_id!r} from next_item"
            )
        return item

    def claim(self, item: WorkItem, actor: str) -> ClaimReceipt:
        if not self._selection.permits(item.item_id):
            raise ScopeFenceViolation(
                f"claim refused for out-of-fence item {item.item_id!r}"
            )
        return self._port.claim(item, actor)

    def close(
        self,
        evidence: CloseEvidence,
        item_commit: str,
        workspace: WorkspaceRef,
        actor: str,
    ) -> None:
        self._port.close(evidence, item_commit, workspace, actor)

    def park(
        self, item_id: str, reason: str, actor: str, workspace: WorkspaceRef | None = None
    ) -> None:
        self._port.park(item_id, reason, actor, workspace)

    def propose(self, proposal: Proposal, actor: str) -> ProposalRef:
        return self._port.propose(proposal, actor)
