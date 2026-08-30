"""Campaign-level queue loop and proposal-only dry-queue behaviour."""

from __future__ import annotations

from collections.abc import Callable

from autobuild.application.dependencies import WorkflowPorts
from autobuild.application.item import ItemWorkflow
from autobuild.domain import (
    CampaignOutcome,
    CampaignRef,
    CampaignStopReason,
    ItemExecutionSpec,
    RefillPlan,
    RunEvent,
    WorkItem,
)


class CampaignRunner:
    def __init__(self, ports: WorkflowPorts) -> None:
        self._ports = ports
        self._items = ItemWorkflow(ports)

    def run(
        self,
        campaign: CampaignRef,
        spec_for: Callable[[WorkItem], ItemExecutionSpec],
        refill: RefillPlan = RefillPlan(),
    ) -> CampaignOutcome:
        record = self._ports.records.create(campaign)
        self._ports.records.append(
            record, RunEvent(event_type="campaign.started", occurred_at="adapter-time")
        )
        outcomes = []
        stop = CampaignStopReason.QUEUE_DRY
        while len(outcomes) < campaign.max_items:
            item = self._ports.tracker.next_item(campaign)
            if item is None:
                if campaign.refill_enabled:
                    for proposal in refill.proposals:
                        ref = self._ports.tracker.propose(proposal, actor="coordinator")
                        if ref.runnable:
                            raise ValueError("refill made a workflow proposal runnable")
                    for fog in refill.fog:
                        self._ports.knowledge.record_fog(fog)
                stop = CampaignStopReason.QUEUE_DRY
                break
            outcome = self._items.run(campaign, spec_for(item), record)
            outcomes.append(outcome)
            if outcome.structural_failure:
                stop = CampaignStopReason.STRUCTURAL_FAILURE
                break
        else:
            stop = CampaignStopReason.ITEM_BOUND

        report = self._ports.records.complete(
            record,
            f"campaign={campaign.campaign_id}; items={len(outcomes)}; stop={stop.value}",
        )
        return CampaignOutcome(campaign.campaign_id, tuple(outcomes), stop, report)
