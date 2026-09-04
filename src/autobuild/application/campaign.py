"""Campaign-level queue loop and proposal-only dry-queue behaviour."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from autobuild.application.dependencies import WorkflowPorts
from autobuild.application.item import ItemWorkflow
from autobuild.application.progress import render_progress_line
from autobuild.domain import (
    CampaignContext,
    CampaignOutcome,
    CampaignRef,
    CampaignReport,
    CampaignStopReason,
    DeliveryMode,
    ItemDisposition,
    ItemExecutionSpec,
    ItemOutcome,
    ItemState,
    LeaseGrant,
    LeaseHeld,
    LeaseRecord,
    LeaseRequest,
    LeaseSurface,
    PhaseMarker,
    RefillPlan,
    ResumePlan,
    RunEvent,
    RunRecordRef,
    ScopeFenceViolation,
    SurfaceKind,
    WorkItem,
    WorktreeStatus,
)


# The marker states a claimed item can be resumed from. A ``closed`` marker is a
# terminal value that is never resumed; any other value on a still-claimed item
# is a tracker mismatch.
_RESUMABLE_MARKER_STATES = frozenset(
    {
        ItemState.CLAIMED.value,
        ItemState.ISOLATED.value,
        ItemState.BUILT.value,
        ItemState.VALIDATED.value,
        ItemState.REVIEWED.value,
        ItemState.CORRECTING.value,
        ItemState.ESCALATED.value,
        ItemState.FINALISED.value,
    }
)
# The marker states whose recorded workspace revision must still match the live
# worktree digest for a good resume, per the founder ruling.
_DIGEST_CHECK_STATES = frozenset(
    {ItemState.BUILT.value, ItemState.VALIDATED.value, ItemState.REVIEWED.value}
)


@dataclass(frozen=True, slots=True)
class _ResumeAssessment:
    good: bool
    reason: str


class CampaignRunner:
    def __init__(self, ports: WorkflowPorts, items: ItemWorkflow | None = None) -> None:
        self._ports = ports
        self._items = items or ItemWorkflow(ports)

    def run(
        self,
        campaign: CampaignRef,
        spec_for: Callable[[WorkItem], ItemExecutionSpec],
        refill: RefillPlan = RefillPlan(),
        *,
        context: CampaignContext | None = None,
    ) -> CampaignOutcome:
        context = context or self._default_context(campaign)
        record = self._ports.records.create(campaign)
        self._ports.progress.begin(record)
        self._append(
            record,
            RunEvent(
                event_type="campaign.started",
                payload={
                    "harness": context.harness,
                    "models": dict(context.models),
                    "item_bound": campaign.max_items,
                    "delivery_mode": context.delivery_mode.value,
                    "validator_id": context.validator_id,
                    "manifest_path": str(record.root / "manifest.json"),
                },
            ),
        )
        tracker_grant, reclaims = self._acquire_tracker_lease(campaign, context, record)
        try:
            outcomes: list[ItemOutcome] = []
            follow_ups: list[str] = []
            parked_signatures: dict[str, str] = {}
            stop = CampaignStopReason.QUEUE_DRY
            resumed, orphans = self._resume_interrupted(campaign, spec_for, record, context)
            outcomes.extend(resumed)
            for resumed_outcome in resumed:
                follow_ups.extend(resumed_outcome.follow_ups)
                if resumed_outcome.lane_signature is not None:
                    parked_signatures[resumed_outcome.item_id] = resumed_outcome.lane_signature
            resume_stop = self._resume_stop(resumed)
            if resume_stop is not None:
                stop = resume_stop
            while len(outcomes) < campaign.max_items:
                if resume_stop is not None:
                    break
                if not self._lanes_available():
                    stop = CampaignStopReason.LANES_EXHAUSTED
                    self._append(
                        record,
                        RunEvent(
                            event_type="campaign.lanes_exhausted",
                            payload={"lanes": [lane.name for lane in self._ports.lanes]},
                        ),
                    )
                    break
                try:
                    item = self._select_item(campaign, frozenset(parked_signatures))
                except ScopeFenceViolation as exc:
                    stop = CampaignStopReason.SCOPE_FENCE_VIOLATION
                    self._append(
                        record,
                        RunEvent(
                            event_type="campaign.scope_fence_violation",
                            payload={"error": str(exc)},
                        ),
                    )
                    break
                if item is None:
                    if campaign.refill_enabled:
                        for proposal in refill.proposals:
                            ref = self._ports.tracker.propose(proposal, actor="coordinator")
                            if ref.runnable:
                                raise ValueError("refill made a workflow proposal runnable")
                            follow_ups.append(proposal.title)
                        for fog in refill.fog:
                            self._ports.knowledge.record_fog(fog)
                    stop = CampaignStopReason.QUEUE_DRY
                    break
                try:
                    outcome = self._items.run(campaign, spec_for(item), record)
                except Exception as exc:
                    outcome = ItemOutcome(
                        item_id=item.item_id,
                        disposition=ItemDisposition.FAILED,
                        states=(ItemState.READY,),
                        reason=str(exc),
                        structural_failure=True,
                        title=item.title,
                    )
                outcomes.append(outcome)
                follow_ups.extend(outcome.follow_ups)
                if outcome.lane_signature is not None:
                    parked_signatures[outcome.item_id] = outcome.lane_signature
                tracker_grant = self._renew_tracker_lease(tracker_grant, record)
                if outcome.lanes_exhausted:
                    stop = CampaignStopReason.LANES_EXHAUSTED
                    break
                if outcome.structural_failure:
                    stop = CampaignStopReason.STRUCTURAL_FAILURE
                    break
            else:
                if resume_stop is None:
                    stop = CampaignStopReason.ITEM_BOUND

            next_ref = self._next_ready(campaign, stop, frozenset(parked_signatures))
            unbuilt = self._allowed_unbuilt(campaign, outcomes, stop)
            relative_report = f"docs/campaigns/{campaign.campaign_id}.md"
            repository_report_ref = self._deliver_report(
                campaign,
                context,
                outcomes,
                stop,
                next_ref,
                follow_ups,
                unbuilt,
                reclaims,
                orphans,
                record,
                relative_report,
            )
            counts = self._counts(outcomes)
            self._append(
                record,
                RunEvent(
                    event_type="campaign.completed",
                    payload={
                        "stop_reason": stop.value,
                        "accepted": counts[ItemDisposition.ACCEPTED],
                        "parked": counts[ItemDisposition.PARKED],
                        "failed": counts[ItemDisposition.FAILED],
                        "report": relative_report,
                        "report_path": repository_report_ref,
                    },
                ),
            )
            summary = (
                f"campaign={campaign.campaign_id}; items={len(outcomes)}; "
                f"stop={stop.value}; report={repository_report_ref}"
            )
            report = self._ports.records.complete(record, summary)
            return CampaignOutcome(
                campaign.campaign_id,
                tuple(outcomes),
                stop,
                report,
                repository_report_ref,
                progress_ref=str(record.root / "progress.log"),
            )
        finally:
            self._release_tracker_lease(tracker_grant, record)

    def _append(self, record: RunRecordRef, event: RunEvent) -> str:
        """Append one run event and emit its owner progress line.

        The event is stamped here when it arrives without a timestamp, so its
        recorded payload and its rendered progress line carry the same instant.
        An event that renders to nothing (its type carries no owner meaning) is
        recorded without emitting a line."""

        stamped = (
            replace(event, occurred_at=datetime.now(UTC).isoformat())
            if not event.occurred_at.strip()
            else event
        )
        ref = self._ports.records.append(record, stamped)
        line = render_progress_line(stamped)
        if line:
            self._ports.progress.emit(line)
        return ref

    def _acquire_tracker_lease(
        self, campaign: CampaignRef, context: CampaignContext, record
    ) -> tuple[LeaseGrant | None, list[LeaseRecord]]:
        """Hold the tracker-root surface before the first claim.

        A live lease naming another campaign raises ``LeaseHeld`` here, before any
        claim, so a second campaign refuses the surface instead of writing into it.
        A stale lease is reclaimed and its previous holder recorded for the run
        record and the campaign report."""

        lease = self._ports.lease
        surface = context.tracker_surface
        if lease is None or surface is None:
            return None, []
        try:
            grant = lease.acquire(LeaseRequest(surface, campaign.campaign_id))
        except LeaseHeld as held:
            self._append(
                record,
                RunEvent(
                    event_type="campaign.lease_refused",
                    payload=_holder_payload(held.record),
                ),
            )
            raise
        reclaims: list[LeaseRecord] = []
        if grant.reclaimed is not None:
            reclaims.append(grant.reclaimed)
            self._append(
                record,
                RunEvent(
                    event_type="campaign.lease_reclaimed",
                    payload={
                        "surface_kind": surface.kind.value,
                        "surface_path": str(surface.path),
                        "previous_holder": _holder_payload(grant.reclaimed),
                    },
                ),
            )
        else:
            self._append(
                record,
                RunEvent(
                    event_type="campaign.lease_acquired",
                    payload=_holder_payload(grant.record),
                ),
            )
        return grant, reclaims

    def _renew_tracker_lease(
        self, grant: LeaseGrant | None, record
    ) -> LeaseGrant | None:
        if grant is None or self._ports.lease is None:
            return grant
        return self._ports.lease.renew(grant)

    def _release_tracker_lease(self, grant: LeaseGrant | None, record) -> None:
        if grant is None or self._ports.lease is None:
            return
        release = self._ports.lease.release(grant)
        if not release.released:
            self._append(
                record,
                RunEvent(
                    event_type="campaign.lease_release_diagnostic",
                    payload={
                        "surface_kind": grant.surface.kind.value,
                        "surface_path": str(grant.surface.path),
                        "diagnostics": list(release.diagnostics),
                    },
                ),
            )

    def _resume_interrupted(
        self,
        campaign: CampaignRef,
        spec_for: Callable[[WorkItem], ItemExecutionSpec],
        record,
        context: CampaignContext,
    ) -> tuple[list[ItemOutcome], list[tuple[str, str]]]:
        """Before the first selection, resume every good-state interrupted item
        and park the rest, so a killed run continues without a person
        reconciling first.

        For each item the tracker reports still claimed by the builder actor, the
        run-record port's phase marker is compared to the live worktree. A good
        state resumes from the marker; a mismatch parks with ``resume:<reason>``
        and snapshots the worktree in place. A registered worktree that no
        claimed item owns is reported as an orphan and left untouched."""

        try:
            claims = self._ports.tracker.resumable_claims(campaign)
        except Exception as exc:
            self._append(
                record,
                RunEvent(event_type="campaign.resume_scan_failed", payload={"error": str(exc)}),
            )
            return [], []
        statuses = {status.root: status for status in self._ports.workspace.list_worktrees(campaign)}
        outcomes: list[ItemOutcome] = []
        attached: set[Path] = set()
        for item in claims:
            if not campaign.selection.permits(item.item_id):
                continue
            marker = self._ports.records.latest_phase_marker(item.item_id)
            if marker is not None and marker.worktree_root in statuses:
                attached.add(marker.worktree_root)
            assessment = self._assess_resume(campaign, marker, statuses, context)
            if assessment.good:
                assert marker is not None
                self._append(
                    record,
                    RunEvent(
                        event_type="item.resume_selected",
                        item_id=item.item_id,
                        payload={
                            "prior_run_id": marker.run_id,
                            "marker_state": marker.state,
                            "correction_count": marker.correction_count,
                        },
                    ),
                )
                plan = ResumePlan(
                    marker.state, marker.worktree_root, marker.correction_count, marker.run_id
                )
                try:
                    outcome = self._items.run(campaign, spec_for(item), record, resume=plan)
                except Exception as exc:
                    outcome = ItemOutcome(
                        item_id=item.item_id,
                        disposition=ItemDisposition.FAILED,
                        states=(ItemState.CLAIMED,),
                        reason=str(exc),
                        structural_failure=True,
                        title=item.title,
                    )
                outcomes.append(outcome)
            else:
                outcomes.append(
                    self._park_resume(campaign, item, marker, record, assessment.reason, statuses)
                )
        orphans: list[tuple[str, str]] = []
        for root in statuses:
            if root in attached:
                continue
            self._append(
                record,
                RunEvent(
                    event_type="resume.orphan_worktree",
                    payload={"worktree_root": str(root)},
                ),
            )
            orphans.append((root.name, "resume:orphan-worktree"))
        return outcomes, orphans

    def _assess_resume(
        self,
        campaign: CampaignRef,
        marker: PhaseMarker | None,
        statuses: dict[Path, WorktreeStatus],
        context: CampaignContext,
    ) -> _ResumeAssessment:
        """Judge whether an interrupted item is in a good state to resume."""

        if marker is None:
            return _ResumeAssessment(False, "resume:missing-marker")
        if marker.state not in _RESUMABLE_MARKER_STATES:
            return _ResumeAssessment(False, "resume:tracker-mismatch")
        if marker.worktree_root not in statuses:
            return _ResumeAssessment(False, "resume:missing-worktree")
        if self._resume_lease_held(campaign, marker.worktree_root, context):
            return _ResumeAssessment(False, "resume:lease-held")
        status = statuses[marker.worktree_root]
        if status.head_commit != marker.head_commit:
            return _ResumeAssessment(False, "resume:head-moved")
        if (
            marker.state in _DIGEST_CHECK_STATES
            and status.workspace_revision != marker.workspace_revision
        ):
            return _ResumeAssessment(False, "resume:revision-changed")
        return _ResumeAssessment(True, "")

    def _resume_lease_held(
        self, campaign: CampaignRef, worktree_root: Path, context: CampaignContext
    ) -> bool:
        """True when another process holds a live lease on the worktree or the
        tracker root, so this campaign must not attach to it."""

        lease = self._ports.lease
        if lease is None:
            return False
        surfaces = [LeaseSurface(worktree_root, SurfaceKind.WORKTREE)]
        if context.tracker_surface is not None:
            surfaces.append(context.tracker_surface)
        for surface in surfaces:
            holder = lease.live_holder(surface)
            if holder is not None and holder.campaign_id != campaign.campaign_id:
                return True
        return False

    def _park_resume(
        self,
        campaign: CampaignRef,
        item: WorkItem,
        marker: PhaseMarker | None,
        record,
        reason: str,
        statuses: dict[Path, WorktreeStatus],
    ) -> ItemOutcome:
        """Park an interrupted item that is not in a good state and continue.

        The tracker records ``resume:<reason>``. When the worktree still exists
        and no other writer holds it, it is snapshotted into the run record and
        left in place so nothing is lost."""

        try:
            self._ports.tracker.park(item.item_id, reason, actor="coordinator")
        except Exception as exc:
            self._append(
                record,
                RunEvent(
                    event_type="item.resume_park_failed",
                    item_id=item.item_id,
                    payload={"reason": reason, "error": str(exc)},
                ),
            )
            return ItemOutcome(
                item_id=item.item_id,
                disposition=ItemDisposition.FAILED,
                states=(ItemState.CLAIMED,),
                reason=str(exc),
                title=item.title,
            )
        snapshot_dir = ""
        if (
            reason in {"resume:head-moved", "resume:revision-changed"}
            and marker is not None
            and marker.worktree_root in statuses
        ):
            snapshot_dir = self._snapshot_resume(campaign, item, marker, record, reason)
        self._append(
            record,
            RunEvent(
                event_type="item.resume_parked",
                item_id=item.item_id,
                payload={
                    "reason": reason,
                    "marker_state": marker.state if marker is not None else "",
                    "snapshot_path": snapshot_dir,
                },
            ),
        )
        return ItemOutcome(
            item_id=item.item_id,
            disposition=ItemDisposition.PARKED,
            states=(ItemState.PARKED,),
            reason=reason,
            title=item.title,
        )

    def _snapshot_resume(
        self,
        campaign: CampaignRef,
        item: WorkItem,
        marker: PhaseMarker,
        record,
        reason: str,
    ) -> str:
        """Snapshot a mismatched worktree into the run record and leave it in
        place. A failure to read the worktree is recorded and does not stop the
        campaign."""

        base = f"{item.item_id}-park"
        try:
            workspace = self._ports.workspace.adopt_worktree(
                campaign, item, marker.worktree_root
            )
            snapshot = self._ports.workspace.snapshot(workspace)
        except Exception as exc:
            self._append(
                record,
                RunEvent(
                    event_type="item.resume_snapshot_failed",
                    item_id=item.item_id,
                    payload={"reason": reason, "error": str(exc)},
                ),
            )
            return ""
        if snapshot.patch:
            self._ports.records.write_evidence_file(
                record, f"{base}/changes.patch", snapshot.patch
            )
        for file in snapshot.files:
            self._ports.records.write_evidence_file(
                record, f"{base}/files/{file.path}", file.content
            )
        manifest = {
            "schema": "autobuild.park-snapshot.v1",
            "item_id": item.item_id,
            "reason": reason,
            "start_commit": snapshot.start_commit,
            "patch": "changes.patch" if snapshot.patch else None,
            "tracked_paths": [
                {"path": entry.path.as_posix(), "kind": entry.kind.value, "digest": entry.digest}
                for entry in snapshot.changed_paths
            ],
            "files": [
                {"path": f"files/{file.path}", "source": file.path, "digest": file.digest}
                for file in snapshot.files
            ],
        }
        self._ports.records.write_evidence_file(
            record,
            f"{base}/snapshot.json",
            json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"),
        )
        return f"evidence/{base}"

    @staticmethod
    def _resume_stop(resumed: list[ItemOutcome]) -> CampaignStopReason | None:
        """A structural failure or exhausted lanes during resume stops the
        campaign before it selects new work."""

        for outcome in resumed:
            if outcome.lanes_exhausted:
                return CampaignStopReason.LANES_EXHAUSTED
        for outcome in resumed:
            if outcome.structural_failure:
                return CampaignStopReason.STRUCTURAL_FAILURE
        return None

    def _lanes_available(self) -> bool:
        """True unless every configured lane is currently cooled.

        Single-lane runs (no lanes or lane state configured) are always
        available; a multi-lane run whose lanes are all cooled has no capable
        lane and stops with the ``lanes_exhausted`` reason."""

        lanes = self._ports.lanes
        state = self._ports.lane_state
        if not lanes or state is None:
            return True
        return any(state.active(lane.name) is None for lane in lanes)

    def _select_item(
        self, campaign: CampaignRef, skip: frozenset[str] = frozenset()
    ) -> WorkItem | None:
        """Choose the next item to build. With an allow-list the closed universe is
        consulted in dispatch order; otherwise the tracker's next item is used. The
        exclude-list is checked at every selection. Items already parked with a lane
        signature this campaign are skipped, so a lane failure never re-picks the
        same item. When an allow-list has no ready item, the live queue is peeked so
        the scoped tracker can refuse out-of-fence work rather than let the campaign
        consult the live queue for a build."""

        selection = campaign.selection
        if selection.allow:
            ready = {item.item_id: item for item in self._ports.tracker.ready_items(campaign)}
            for item_id in selection.allow:
                if item_id in selection.exclude or item_id in skip:
                    continue
                if item_id in ready:
                    return ready[item_id]
            self._ports.tracker.next_item(campaign)
            return None
        if selection.exclude or skip:
            for item in self._ports.tracker.ready_items(campaign):
                if item.item_id not in selection.exclude and item.item_id not in skip:
                    return item
            return None
        return self._ports.tracker.next_item(campaign)

    def _deliver_report(
        self,
        campaign: CampaignRef,
        context: CampaignContext,
        outcomes: list[ItemOutcome],
        stop: CampaignStopReason,
        next_ref: str,
        follow_ups: list[str],
        unbuilt: list[tuple[str, str]],
        reclaims: list[LeaseRecord],
        orphans: list[tuple[str, str]],
        record: RunRecordRef,
        relative: str,
    ) -> str:
        content = _render_report(
            campaign, outcomes, next_ref, follow_ups, unbuilt, reclaims, orphans, record
        )
        try:
            self._ports.workspace.deliver_report(
                CampaignReport(
                    campaign_id=campaign.campaign_id,
                    repository=campaign.repository,
                    relative_path=relative,
                    content=content,
                    mode=context.delivery_mode,
                    target_branch=context.target_branch,
                    target_revision=context.target_revision,
                    push_current_branch=context.push_current_branch,
                    allow_current_branch_default=context.allow_current_branch_default,
                )
            )
        except Exception as exc:
            return f"report delivery failed: {exc}"
        return str(campaign.repository / relative)

    def _next_ready(
        self,
        campaign: CampaignRef,
        stop: CampaignStopReason,
        skip: frozenset[str] = frozenset(),
    ) -> str:
        if stop is CampaignStopReason.QUEUE_DRY:
            return "queue dry"
        if stop is CampaignStopReason.SCOPE_FENCE_VIOLATION:
            return "scope fence violation"
        if stop is CampaignStopReason.LANES_EXHAUSTED:
            return "lanes exhausted"
        try:
            peek = self._select_item(campaign, skip)
        except ScopeFenceViolation:
            return "scope fence violation"
        return peek.item_id if peek is not None else "queue dry"

    def _allowed_unbuilt(
        self,
        campaign: CampaignRef,
        outcomes: list[ItemOutcome],
        stop: CampaignStopReason,
    ) -> list[tuple[str, str]]:
        """Name every allow-list item that was not built and why. An item that ran
        (accepted, parked or failed) already appears in the report body; this list
        covers only allowed items the campaign never dispatched."""

        selection = campaign.selection
        if not selection.allow:
            return []
        accepted = {o.item_id for o in outcomes if o.disposition is ItemDisposition.ACCEPTED}
        processed = {o.item_id for o in outcomes}
        try:
            ready = {item.item_id for item in self._ports.tracker.ready_items(campaign)}
        except ScopeFenceViolation:
            ready = set()
        results: list[tuple[str, str]] = []
        for index, item_id in enumerate(selection.allow):
            if item_id in processed:
                continue
            results.append(
                (item_id, self._unbuilt_reason(item_id, index, selection, accepted, ready, stop))
            )
        return results

    @staticmethod
    def _unbuilt_reason(
        item_id: str,
        index: int,
        selection,
        accepted: set[str],
        ready: set[str],
        stop: CampaignStopReason,
    ) -> str:
        if item_id in selection.exclude:
            return "excluded"
        if stop is CampaignStopReason.ITEM_BOUND:
            return "item bound"
        if item_id in ready:
            if stop is CampaignStopReason.LANES_EXHAUSTED:
                return "lanes exhausted"
            if stop is CampaignStopReason.STRUCTURAL_FAILURE:
                return "structural failure"
            return "item bound"
        preceding = selection.allow[:index]
        if any(
            pid not in accepted and pid not in selection.exclude for pid in preceding
        ):
            return "blocked"
        return "not ready"

    @staticmethod
    def _counts(outcomes: list[ItemOutcome]) -> dict[ItemDisposition, int]:
        counts = {disposition: 0 for disposition in ItemDisposition}
        for outcome in outcomes:
            counts[outcome.disposition] += 1
        return counts

    @staticmethod
    def _default_context(campaign: CampaignRef) -> CampaignContext:
        return CampaignContext(
            harness="",
            models={},
            delivery_mode=DeliveryMode.PROTECTED_DEFAULT,
            validator_id="",
            target_branch="",
            target_revision="",
        )


def _seat_cell(value: object) -> str:
    return "-" if value is None else str(value)


def _holder_payload(record: LeaseRecord) -> dict[str, object]:
    return {
        "campaign_id": record.campaign_id,
        "process_id": record.process_id,
        "host": record.host,
        "started_at": record.started_at,
        "heartbeat_at": record.heartbeat_at,
        "surface_path": str(record.surface_path),
        "surface_kind": record.surface_kind.value,
    }


def _render_report(
    campaign: CampaignRef,
    outcomes: list[ItemOutcome],
    next_ref: str,
    follow_ups: list[str],
    unbuilt: list[tuple[str, str]],
    reclaims: list[LeaseRecord],
    orphans: list[tuple[str, str]],
    record: RunRecordRef,
) -> str:
    shipped = [o for o in outcomes if o.disposition is ItemDisposition.ACCEPTED]
    parked = [o for o in outcomes if o.disposition is ItemDisposition.PARKED]
    failed = [o for o in outcomes if o.disposition is ItemDisposition.FAILED]
    lines: list[str] = [f"# Campaign {campaign.campaign_id}", ""]

    lines.append("## Shipped")
    lines.append("")
    if shipped:
        for outcome in shipped:
            item_commit = outcome.finalise.item_commit if outcome.finalise else None
            merged = outcome.finalise.merged_commit if outcome.finalise else None
            lines.append(
                f"- {outcome.item_id} {outcome.title} "
                f"item={_seat_cell(item_commit)} merged={_seat_cell(merged)} verified"
            )
    else:
        lines.append("none")
    lines.append("")

    lines.append("## Parked")
    lines.append("")
    if parked or orphans:
        for outcome in parked:
            lines.append(f"- {outcome.item_id}: {outcome.reason or ''}")
        for name, reason in orphans:
            lines.append(f"- {name}: {reason}")
    else:
        lines.append("none")
    lines.append("")

    lines.append("## Failed")
    lines.append("")
    if failed:
        for outcome in failed:
            lines.append(f"- {outcome.item_id}: {outcome.reason or ''}")
    else:
        lines.append("none")
    lines.append("")

    lines.append("## Follow-ups")
    lines.append("")
    if follow_ups:
        for title in follow_ups:
            lines.append(f"- {title}")
    else:
        lines.append("none")
    lines.append("")

    if campaign.selection.allow:
        lines.append("## Allowed items left unbuilt")
        lines.append("")
        if unbuilt:
            for item_id, reason in unbuilt:
                lines.append(f"- {item_id}: {reason}")
        else:
            lines.append("none")
        lines.append("")

    if reclaims:
        lines.append("## Lease reclaims")
        lines.append("")
        for holder in reclaims:
            lines.append(
                f"- {holder.surface_kind.value} {holder.surface_path} reclaimed from "
                f"campaign {holder.campaign_id} (process {holder.process_id} on {holder.host})"
            )
        lines.append("")

    lines.append("## Next")
    lines.append("")
    lines.append(next_ref)
    lines.append("")

    lines.append("## Seat usage")
    lines.append("")
    lines.append("| item | seat | duration seconds | input tokens | output tokens | cost |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    any_seat = False
    for outcome in outcomes:
        for seat in outcome.seats:
            any_seat = True
            lines.append(
                f"| {outcome.item_id} | {seat.seat.value} | "
                f"{_seat_cell(seat.duration_seconds)} | {_seat_cell(seat.input_tokens)} | "
                f"{_seat_cell(seat.output_tokens)} | {_seat_cell(seat.cost)} |"
            )
    if not any_seat:
        lines.append("| none | - | - | - | - | - |")
    lines.append("")

    lines.append("## Run record")
    lines.append("")
    lines.append(f"- run report {record.root}")
    lines.append(f"- progress log {record.root / 'progress.log'}")
    lines.append("")
    return "\n".join(lines)
