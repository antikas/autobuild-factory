"""The fixed per-item sequence, expressed only through typed ports."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from autobuild.application.dependencies import Lane, WorkflowPorts
from autobuild.application.progress import render_progress_line
from autobuild.application.prompts import render_seat_instructions
from autobuild.application.state_machine import ItemStateMachine
from autobuild.domain import (
    AdapterError,
    BuilderReport,
    CampaignRef,
    CloseEvidence,
    CommandRequest,
    DiffEvidence,
    DeliveryRequest,
    EvidenceError,
    FinaliseRequest,
    ItemDisposition,
    ItemExecutionSpec,
    ItemNature,
    ItemOutcome,
    ItemState,
    LaneSignal,
    LanesExhausted,
    LeaseGrant,
    LeaseRequest,
    LeaseSurface,
    Proposal,
    ResumePlan,
    ReviewDecision,
    ReviewVerdict,
    RunEvent,
    RunRecordRef,
    Seat,
    SeatObservation,
    SeatOutcome,
    SeatRequest,
    SeatResult,
    SurfaceKind,
    ToolPolicy,
    ValidationEvidence,
    WorkspaceRef,
    review_verdict_rule_error,
)
from autobuild.enforcement import classify_item_nature
from autobuild.ports import RunRecordPort

# The three progress signals a stall kill reports as absent, in the order the
# command adapter samples them. Kept here so the park reason names them without
# the application layer importing an adapter.
_STALL_SIGNALS = ("output", "worktree", "cpu")

# The phase a resumed item re-enters the sequence at, per its marker state. A
# marker that has not passed the builder re-enters at its own phase and re-runs
# the builder; a marker past the build re-enters at BUILT and re-runs validation
# and review only, so an accepted resume never respends a good build.
_RESUME_SEED = {
    ItemState.CLAIMED.value: ItemState.ISOLATED,
    ItemState.ISOLATED.value: ItemState.ISOLATED,
    ItemState.CORRECTING.value: ItemState.CORRECTING,
    ItemState.BUILT.value: ItemState.BUILT,
    ItemState.VALIDATED.value: ItemState.BUILT,
    ItemState.REVIEWED.value: ItemState.BUILT,
    ItemState.ESCALATED.value: ItemState.BUILT,
    ItemState.FINALISED.value: ItemState.FINALISED,
}
# Marker states whose build is not yet trusted, so a resume re-runs the builder.
_RESUME_REBUILD = frozenset(
    {ItemState.CLAIMED.value, ItemState.ISOLATED.value, ItemState.CORRECTING.value}
)


def _duration_seconds(started_at: str, ended_at: str) -> float | None:
    try:
        start = datetime.fromisoformat(started_at)
        end = datetime.fromisoformat(ended_at)
    except (TypeError, ValueError):
        return None
    return (end - start).total_seconds()


def _stderr_ref(diagnostics: tuple[str, ...]) -> str:
    for entry in diagnostics:
        if entry.startswith("stderr="):
            return entry.removeprefix("stderr=")
    return ""


class _LaneRouter:
    """Ordered lane selection and cooling for one item run.

    The router walks the configured lanes in preference order, skipping any lane
    the shared lane state reports as cooled. A structural signal cools the
    current lane and moves the router to the next capable one; when none remains
    it raises ``LanesExhausted`` carrying the signature the item parks under."""

    __slots__ = ("_lanes", "_state", "_campaign_id", "_emit", "_index", "_last_signature")

    def __init__(self, lanes, lane_state, campaign_id, emit) -> None:
        self._lanes: tuple[Lane, ...] = lanes
        self._state = lane_state
        self._campaign_id = campaign_id
        self._emit = emit
        self._index = 0
        self._last_signature = "lanes_exhausted"

    def _cooled(self, name: str) -> bool:
        return self._state is not None and self._state.active(name) is not None

    def current_lane(self) -> Lane:
        index = self._index
        while index < len(self._lanes):
            lane = self._lanes[index]
            if not self._cooled(lane.name):
                self._index = index
                return lane
            index += 1
        raise LanesExhausted(self._last_signature, self._last_lane_name())

    def signal(self, lane: Lane, signal: LaneSignal) -> None:
        """Cool ``lane`` in the shared state and move past it in preference order."""

        if self._state is not None:
            self._state.cool(lane.name, signal, self._campaign_id)
        self._last_signature = signal.signature
        self._emit(
            "lane.cooled",
            {
                "lane": lane.name,
                "kind": signal.kind.value,
                "signature": signal.signature,
                "reset_at": signal.reset_at,
            },
        )
        self._index += 1

    def _last_lane_name(self) -> str:
        return self._lanes[-1].name if self._lanes else ""


class _ParkGuard:
    """Signals whether a park that failed part way through has deliberately left the
    worktree in place, so the finally path does not remove preserved evidence."""

    __slots__ = ("leave_worktree",)

    def __init__(self) -> None:
        self.leave_worktree = False


@dataclass(slots=True)
class _PhaseMarker:
    """The per-item phase marker rewritten at every state transition. It pins the
    branch head and workspace revision each disposition was decided against, so a
    resume (item 6) can compare an observed head against the recorded one. Its
    terminal value ('closed' or 'parked') is written only after delivery, and is a
    marker-only value that is not part of the item state machine."""

    records: RunRecordPort
    record: RunRecordRef
    item_id: str
    workspace: WorkspaceRef | None = None
    head_commit: str = ""
    workspace_revision: str = ""
    corrections: int = 0

    def observe(
        self,
        *,
        workspace: WorkspaceRef | None = None,
        diff: DiffEvidence | None = None,
        corrections: int | None = None,
    ) -> None:
        if workspace is not None:
            self.workspace = workspace
        if diff is not None:
            self.head_commit = diff.head_commit
            self.workspace_revision = diff.workspace_revision
        if corrections is not None:
            self.corrections = corrections

    def write(self, state: str) -> None:
        head = self.head_commit
        if not head and self.workspace is not None:
            head = self.workspace.start_commit
        payload = {
            "schema": "autobuild.item-phase.v1",
            "item_id": self.item_id,
            "state": state,
            "worktree_root": str(self.workspace.root) if self.workspace else "",
            "branch": self.workspace.branch if self.workspace else "",
            "head_commit": head,
            "workspace_revision": self.workspace_revision,
            "correction_count": self.corrections,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        self.records.write_evidence_file(
            self.record,
            f"{self.item_id}-phase.json",
            json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"),
        )


class ItemWorkflow:
    def __init__(self, ports: WorkflowPorts) -> None:
        self._ports = ports
        # Per-item lane routing state, reset at the start of every ``run``.
        self._router: _LaneRouter | None = None
        self._builder_lane: str | None = None

    def _lanes(self) -> tuple[Lane, ...]:
        """The ordered lanes for this run, or a single unnamed fallback lane."""

        if self._ports.lanes:
            return self._ports.lanes
        return (Lane("", self._ports.harness),)

    @staticmethod
    def _lane_actor(lane_name: str) -> str:
        return f"builder@{lane_name}" if lane_name else "builder"

    def run(
        self,
        campaign: CampaignRef,
        spec: ItemExecutionSpec,
        record: RunRecordRef,
        *,
        resume: ResumePlan | None = None,
    ) -> ItemOutcome:
        machine = ItemStateMachine()
        workspace: WorkspaceRef | None = None
        worktree_grant: LeaseGrant | None = None
        claimed = False
        seats: list[SeatObservation] = []
        guard = _ParkGuard()
        marker = _PhaseMarker(self._ports.records, record, spec.item.item_id)
        result: ItemOutcome | None = None
        self._builder_lane = None
        self._router = _LaneRouter(
            self._lanes(),
            self._ports.lane_state,
            campaign.campaign_id,
            lambda event_type, payload: self._event(
                record, event_type, spec.item.item_id, payload=payload
            ),
        )
        try:
            if resume is not None:
                claimed = True
                workspace = self._ports.workspace.adopt_worktree(
                    campaign, spec.item, resume.worktree_root
                )
                worktree_grant = self._acquire_worktree_lease(campaign, workspace, record)
                marker.observe(workspace=workspace, corrections=resume.correction_count)
                self._seed_resume(machine, resume.marker_state)
                self._event(
                    record,
                    "item.resumed",
                    spec.item.item_id,
                    payload={
                        "marker_state": resume.marker_state,
                        "prior_run_id": resume.prior_run_id,
                        "correction_count": resume.correction_count,
                        "worktree_root": str(workspace.root),
                        "branch": workspace.branch,
                    },
                )
                if resume.marker_state == ItemState.FINALISED.value:
                    result = self._resume_finalised(
                        spec, machine, marker, record, workspace, seats
                    )
                    worktree_grant = self._release_worktree_lease(worktree_grant, record)
                    return result
                verdict, diff, validation, corrections = self._resume_first_verdict(
                    spec, workspace, machine, marker, record, seats, resume
                )
            else:
                nature = classify_item_nature(
                    spec.brief_text,
                    repository_root=campaign.repository,
                    allowed_roots=spec.tool_policy.allowed_roots,
                )
                if nature is not ItemNature.REPOSITORY:
                    result = self._park_by_nature(
                        spec, machine, marker, record, seats, guard, nature
                    )
                    return result
                self._advance(machine, marker, ItemState.VERIFIED)
                start_lane = self._router.current_lane().name
                claim_actor = self._lane_actor(start_lane)
                self._ports.tracker.claim(spec.item, actor=claim_actor)
                claimed = True
                self._advance(machine, marker, ItemState.CLAIMED)
                self._event(
                    record,
                    "item.claimed",
                    spec.item.item_id,
                    payload={
                        "actor": claim_actor,
                        "lane": start_lane,
                        "brief_ref": spec.item.brief_ref,
                        "title": spec.item.title,
                    },
                )

                workspace = self._ports.workspace.create_isolated(campaign, spec.item)
                worktree_grant = self._acquire_worktree_lease(campaign, workspace, record)
                marker.observe(workspace=workspace)
                self._advance(machine, marker, ItemState.ISOLATED)
                self._event(
                    record,
                    "workspace.created",
                    spec.item.item_id,
                    workspace.start_commit,
                    payload={
                        "worktree_root": str(workspace.root),
                        "branch": workspace.branch,
                        "start_commit": workspace.start_commit,
                    },
                )

                verdict, diff, validation = self._build_validate_review(
                    spec, workspace, machine, marker, record, seats, context_refs=()
                )
                self._reclaim_if_builder_flipped(spec, record, start_lane)
                corrections = 0
            while verdict.decision is ReviewDecision.CORRECT:
                if corrections >= spec.max_corrections:
                    result = self._park(
                        spec,
                        machine,
                        marker,
                        record,
                        workspace,
                        seats,
                        guard,
                        "material finding remained after the correction ceiling",
                        corrections=corrections,
                    )
                    return result
                corrections += 1
                marker.observe(corrections=corrections)
                self._advance(machine, marker, ItemState.CORRECTING)
                self._event(
                    record,
                    "item.correcting",
                    spec.item.item_id,
                    payload={
                        "round": corrections,
                        "triggering_evidence_ref": verdict.evidence_ref,
                    },
                )
                verdict, diff, validation = self._build_validate_review(
                    spec,
                    workspace,
                    machine,
                    marker,
                    record,
                    seats,
                    context_refs=(verdict.evidence_ref,),
                )

            if verdict.decision is ReviewDecision.ESCALATE:
                self._advance(machine, marker, ItemState.ESCALATED)
                verdict = self._specialist_review(spec, workspace, verdict, record, seats)
                if verdict.decision is ReviewDecision.CORRECT:
                    if corrections >= spec.max_corrections:
                        result = self._park(
                            spec,
                            machine,
                            marker,
                            record,
                            workspace,
                            seats,
                            guard,
                            "specialist finding reached the correction ceiling",
                            corrections=corrections,
                        )
                        return result
                    corrections += 1
                    marker.observe(corrections=corrections)
                    self._advance(machine, marker, ItemState.CORRECTING)
                    self._event(
                        record,
                        "item.correcting",
                        spec.item.item_id,
                        payload={
                            "round": corrections,
                            "triggering_evidence_ref": verdict.evidence_ref,
                        },
                    )
                    verdict, diff, validation = self._build_validate_review(
                        spec,
                        workspace,
                        machine,
                        marker,
                        record,
                        seats,
                        context_refs=(verdict.evidence_ref,),
                    )

            if verdict.decision is not ReviewDecision.PASS:
                result = self._park(
                    spec,
                    machine,
                    marker,
                    record,
                    workspace,
                    seats,
                    guard,
                    f"review disposition was {verdict.decision.value}",
                    corrections=corrections,
                )
                return result

            trajectory_ref = self._ports.records.write_evidence(
                record,
                f"{spec.item.item_id}-trajectory",
                self._trajectory_text(
                    ItemDisposition.ACCEPTED,
                    tuple(machine.history) + (ItemState.FINALISED, ItemState.RELEASED),
                    seats,
                    f"accepted after {corrections} correction round(s); review={verdict.evidence_ref}",
                ),
            )
            close = CloseEvidence(
                item_id=spec.item.item_id,
                workspace_revision=diff.workspace_revision,
                diff=diff,
                validation=validation,
                verdict=verdict,
                trajectory_ref=trajectory_ref,
            )
            item_commit = self._ports.workspace.commit_item(
                workspace,
                FinaliseRequest(
                    item_id=spec.item.item_id,
                    evidence=close,
                    commit_message=f"ADDED: {spec.item.title}",
                ),
            )
            self._ports.tracker.close(close, item_commit, workspace, actor="coordinator")
            tracker_commit = self._ports.workspace.commit_tracker(
                workspace, spec.item.item_id, item_commit
            )
            finalise = self._ports.workspace.deliver(
                workspace, self._delivery_request(spec, item_commit, tracker_commit)
            )
            self._ports.workspace.confirm_delivery(
                workspace, finalise, spec.delivery_target_branch
            )
            self._advance(machine, marker, ItemState.FINALISED)
            self._event(
                record,
                "item.finalised",
                spec.item.item_id,
                finalise.item_commit or "",
                tracker_commit,
                payload={
                    "item_commit": finalise.item_commit,
                    "tracker_commit": tracker_commit,
                    "merged_commit": finalise.merged_commit,
                    "pushed": finalise.pushed,
                    "seat_lanes": [
                        {"seat": observation.seat.value, "lane": observation.lane}
                        for observation in seats
                    ],
                },
            )
            self._ports.workspace.release(workspace)
            self._advance(machine, marker, ItemState.RELEASED)
            worktree_grant = self._release_worktree_lease(worktree_grant, record)
            marker.write("closed")
            follow_ups = self._record_follow_ups(spec, campaign, verdict, record)
            result = ItemOutcome(
                item_id=spec.item.item_id,
                disposition=ItemDisposition.ACCEPTED,
                states=tuple(machine.history),
                finalise=finalise,
                title=spec.item.title,
                seats=tuple(seats),
                follow_ups=follow_ups,
            )
            return result
        except LanesExhausted as exc:
            result = self._park_lanes_exhausted(
                spec, machine, marker, record, workspace, seats, guard, exc, claimed
            )
            return result
        except Exception as exc:
            structural = isinstance(exc, (EvidenceError, TypeError, ValueError))
            if claimed:
                try:
                    result = self._park(
                        spec,
                        machine,
                        marker,
                        record,
                        workspace,
                        seats,
                        guard,
                        str(exc),
                        structural=structural,
                        kill=getattr(exc, "seat_kill", None),
                    )
                    return result
                except Exception:
                    structural = True
            self._write_trajectory(
                record, spec.item.item_id, ItemDisposition.FAILED, machine, seats, str(exc)
            )
            result = ItemOutcome(
                item_id=spec.item.item_id,
                disposition=ItemDisposition.FAILED,
                states=tuple(machine.history),
                reason=str(exc),
                structural_failure=structural,
                title=spec.item.title,
                seats=tuple(seats),
            )
            return result
        finally:
            if (
                workspace is not None
                and machine.current is not ItemState.RELEASED
                and not guard.leave_worktree
            ):
                try:
                    self._ports.workspace.release(workspace)
                    machine.transition(ItemState.RELEASED)
                    worktree_grant = self._release_worktree_lease(worktree_grant, record)
                except Exception as exc:
                    self._event(
                        record,
                        "workspace.release_refused",
                        spec.item.item_id,
                        payload={"error": str(exc)},
                    )
                    if result is not None:
                        diagnostic = "; ".join(
                            part
                            for part in (result.reason, f"workspace release refused: {exc}")
                            if part
                        )
                        return replace(result, reason=diagnostic)

    def _acquire_worktree_lease(
        self, campaign: CampaignRef, workspace: WorkspaceRef, record: RunRecordRef
    ) -> LeaseGrant | None:
        """Hold the worktree surface for this item, so no second writer enters the
        same isolated tree. Skipped when no lease mechanism is bound."""

        if self._ports.lease is None:
            return None
        surface = LeaseSurface(workspace.root, SurfaceKind.WORKTREE)
        return self._ports.lease.acquire(LeaseRequest(surface, campaign.campaign_id))

    def _release_worktree_lease(
        self, grant: LeaseGrant | None, record: RunRecordRef
    ) -> None:
        """Release the worktree lease once its tree is released. A no-op release
        (this process did not hold it) is recorded as a diagnostic."""

        if grant is None or self._ports.lease is None:
            return None
        release = self._ports.lease.release(grant)
        if not release.released:
            self._event(
                record,
                "workspace.lease_release_diagnostic",
                None,
                payload={
                    "surface_path": str(grant.surface.path),
                    "diagnostics": list(release.diagnostics),
                },
            )
        return None

    @staticmethod
    def _seed_resume(machine: ItemStateMachine, marker_state: str) -> None:
        """Seed the state machine to the phase a resumed item re-enters at."""

        seed = _RESUME_SEED.get(marker_state)
        if seed is None:
            raise EvidenceError(f"phase marker state is not resumable: {marker_state}")
        resumed = ItemStateMachine.resume_at(seed)
        machine.current = resumed.current
        machine.history[:] = resumed.history

    def _resume_first_verdict(
        self,
        spec: ItemExecutionSpec,
        workspace: WorkspaceRef,
        machine: ItemStateMachine,
        marker: _PhaseMarker,
        record: RunRecordRef,
        seats: list[SeatObservation],
        resume: ResumePlan,
    ) -> tuple[ReviewVerdict, DiffEvidence, ValidationEvidence, int]:
        """Produce the first review verdict for a resumed item.

        A marker before the build re-runs the builder; a marker past the build
        re-runs validation and review only against the work already on disk. In
        both cases the shared correction, escalation and delivery tail follows."""

        if resume.marker_state in _RESUME_REBUILD:
            verdict, diff, validation = self._build_validate_review(
                spec, workspace, machine, marker, record, seats, context_refs=()
            )
        else:
            verdict, diff, validation = self._validate_and_review(
                spec, workspace, machine, marker, record, seats
            )
        return verdict, diff, validation, resume.correction_count

    def _resume_finalised(
        self,
        spec: ItemExecutionSpec,
        machine: ItemStateMachine,
        marker: _PhaseMarker,
        record: RunRecordRef,
        workspace: WorkspaceRef,
        seats: list[SeatObservation],
    ) -> ItemOutcome:
        """Re-run delivery for a marker interrupted after finalisation.

        The product and tracker commits already exist in the worktree, so this
        re-delivers them and re-runs the delivery checks. A merge that already
        landed is a no-op; the worktree is then released and the marker closed."""

        item_commit, tracker_commit = self._ports.workspace.resume_delivery_commits(workspace)
        finalise = self._ports.workspace.deliver(
            workspace, self._delivery_request(spec, item_commit, tracker_commit)
        )
        self._ports.workspace.confirm_delivery(
            workspace, finalise, spec.delivery_target_branch
        )
        self._ports.workspace.release(workspace)
        self._advance(machine, marker, ItemState.RELEASED)
        marker.write("closed")
        self._event(
            record,
            "item.finalised",
            spec.item.item_id,
            finalise.item_commit or "",
            tracker_commit,
            payload={
                "item_commit": finalise.item_commit,
                "tracker_commit": tracker_commit,
                "merged_commit": finalise.merged_commit,
                "pushed": finalise.pushed,
                "resumed": True,
            },
        )
        return ItemOutcome(
            item_id=spec.item.item_id,
            disposition=ItemDisposition.ACCEPTED,
            states=tuple(machine.history),
            finalise=finalise,
            title=spec.item.title,
            seats=tuple(seats),
        )

    def _build_validate_review(
        self,
        spec: ItemExecutionSpec,
        workspace: WorkspaceRef,
        machine: ItemStateMachine,
        marker: _PhaseMarker,
        record: RunRecordRef,
        seats: list[SeatObservation],
        context_refs: tuple[str, ...],
    ) -> tuple[ReviewVerdict, DiffEvidence, ValidationEvidence]:
        self._run_builder(spec, workspace, machine, marker, record, seats, context_refs)
        return self._validate_and_review(spec, workspace, machine, marker, record, seats)

    def _run_builder(
        self,
        spec: ItemExecutionSpec,
        workspace: WorkspaceRef,
        machine: ItemStateMachine,
        marker: _PhaseMarker,
        record: RunRecordRef,
        seats: list[SeatObservation],
        context_refs: tuple[str, ...],
    ) -> None:
        brief_path = self._workspace_brief(spec, workspace)
        builder_policy = self._workspace_policy(spec.tool_policy, workspace)
        builder = self._invoke_seat(
            record,
            seats,
            SeatRequest(
                run_id=record.run_id,
                item_id=spec.item.item_id,
                seat=Seat.BUILDER,
                model_class=spec.builder_model_class,
                brief_path=brief_path,
                workspace=workspace,
                tool_policy=builder_policy,
                instructions=render_seat_instructions(
                    spec, Seat.BUILDER, context_refs, brief_path
                ),
                result_contract="builder-report-v1",
                timeout_seconds=spec.seat_timeout_seconds,
                context_refs=context_refs,
                progress_deadline_seconds=spec.seat_stall_seconds,
                progress_digest=self._progress_digest(workspace),
            ),
        )
        if builder.outcome is not SeatOutcome.SUCCEEDED:
            raise self._seat_failure(Seat.BUILDER, builder)
        if not isinstance(builder.payload, BuilderReport):
            raise EvidenceError("builder result did not contain a typed BuilderReport")
        self._advance(machine, marker, ItemState.BUILT)

    def _validate_and_review(
        self,
        spec: ItemExecutionSpec,
        workspace: WorkspaceRef,
        machine: ItemStateMachine,
        marker: _PhaseMarker,
        record: RunRecordRef,
        seats: list[SeatObservation],
    ) -> tuple[ReviewVerdict, DiffEvidence, ValidationEvidence]:
        brief_path = self._workspace_brief(spec, workspace)
        diff = self._ports.workspace.diff(workspace)
        marker.observe(diff=diff)
        command = self._ports.command.run(
            CommandRequest(
                command_id=f"{record.run_id}:{spec.item.item_id}:{spec.validator_id}",
                argv=spec.validator_argv,
                cwd=workspace.root,
                timeout_seconds=spec.command_timeout_seconds,
            )
        )
        validation = ValidationEvidence(
            validator_id=spec.validator_id,
            workspace_revision=diff.workspace_revision,
            command=command,
            changed_paths=diff.changed_paths,
        )
        self._event(
            record,
            "validation.completed",
            spec.item.item_id,
            command.stdout_ref,
            payload={
                "validator_id": spec.validator_id,
                "exit_code": command.exit_code,
                "timed_out": command.timed_out,
                "duration_seconds": _duration_seconds(command.started_at, command.ended_at),
                "stdout_ref": command.stdout_ref,
                "stderr_ref": command.stderr_ref,
                "changed_path_count": len(diff.changed_paths),
            },
        )
        if command.exit_code != 0 or command.timed_out or command.cancelled:
            raise AdapterError(f"validator {spec.validator_id} did not pass")
        self._advance(machine, marker, ItemState.VALIDATED)

        reviewer_policy = self._workspace_policy(
            spec.reviewer_tool_policy or self._read_only_policy(spec), workspace
        )
        reviewer = self._invoke_seat(
            record,
            seats,
            SeatRequest(
                run_id=record.run_id,
                item_id=spec.item.item_id,
                seat=Seat.REVIEWER,
                model_class=spec.reviewer_model_class,
                brief_path=brief_path,
                workspace=workspace,
                tool_policy=reviewer_policy,
                instructions=render_seat_instructions(
                    spec, Seat.REVIEWER, (diff.patch_ref, command.stdout_ref), brief_path
                ),
                result_contract="review-verdict-v1",
                timeout_seconds=spec.seat_timeout_seconds,
                context_refs=(diff.patch_ref, command.stdout_ref),
                progress_deadline_seconds=spec.seat_stall_seconds,
                progress_digest=self._progress_digest(workspace),
            ),
        )
        if reviewer.outcome is not SeatOutcome.SUCCEEDED:
            raise self._seat_failure(Seat.REVIEWER, reviewer)
        if not isinstance(reviewer.payload, ReviewVerdict):
            raise EvidenceError("reviewer result did not contain a typed ReviewVerdict")
        if reviewer.payload.item_id != spec.item.item_id:
            raise EvidenceError("review verdict item does not match the selected item")
        rule_error = review_verdict_rule_error(reviewer.payload)
        if rule_error is not None:
            raise EvidenceError(rule_error)
        self._advance(machine, marker, ItemState.REVIEWED)
        self._event(
            record,
            "review.completed",
            spec.item.item_id,
            reviewer.payload.evidence_ref,
            payload=self._verdict_payload(reviewer.payload),
        )
        return reviewer.payload, diff, validation

    def _specialist_review(
        self,
        spec: ItemExecutionSpec,
        workspace: WorkspaceRef,
        triggering: ReviewVerdict,
        record: RunRecordRef,
        seats: list[SeatObservation],
    ) -> ReviewVerdict:
        brief_path = self._workspace_brief(spec, workspace)
        reviewer_policy = self._workspace_policy(
            spec.reviewer_tool_policy or self._read_only_policy(spec), workspace
        )
        result = self._invoke_seat(
            record,
            seats,
            SeatRequest(
                run_id=record.run_id,
                item_id=spec.item.item_id,
                seat=Seat.SPECIALIST,
                model_class=spec.specialist_model_class,
                brief_path=brief_path,
                workspace=workspace,
                tool_policy=reviewer_policy,
                instructions=render_seat_instructions(
                    spec, Seat.SPECIALIST, (triggering.evidence_ref,), brief_path
                ),
                result_contract="review-verdict-v1",
                timeout_seconds=spec.seat_timeout_seconds,
                context_refs=(triggering.evidence_ref,),
                progress_deadline_seconds=spec.seat_stall_seconds,
                progress_digest=self._progress_digest(workspace),
            ),
        )
        if result.outcome in {SeatOutcome.STALLED, SeatOutcome.TIMED_OUT}:
            raise self._seat_failure(Seat.SPECIALIST, result)
        if result.outcome is not SeatOutcome.SUCCEEDED or not isinstance(result.payload, ReviewVerdict):
            raise EvidenceError("specialist result did not contain a typed ReviewVerdict")
        if result.payload.item_id != spec.item.item_id:
            raise EvidenceError("specialist verdict item does not match the selected item")
        rule_error = review_verdict_rule_error(result.payload)
        if rule_error is not None:
            raise EvidenceError(rule_error)
        self._event(
            record,
            "specialist.completed",
            spec.item.item_id,
            result.payload.evidence_ref,
            payload=self._verdict_payload(result.payload),
        )
        return result.payload

    def _progress_digest(self, workspace: WorkspaceRef):
        """Bind the workspace's progress digest into a zero-argument callable the
        seat request carries; the command adapter samples it and never runs the
        digest mechanism itself."""

        return lambda: self._ports.workspace.progress_digest(workspace)

    def _seat_failure(self, seat: Seat, result: SeatResult) -> AdapterError:
        """Turn a non-succeeded seat into the park reason and kill evidence.

        A stall kill and a cap kill carry the signature reasons the tracker parks
        under; other failures keep their plain outcome text. The stall or timeout
        sample times ride on the raised error so the park payload can record the
        last time each progress signal advanced."""

        if result.outcome is SeatOutcome.STALLED:
            signals = [name for name, _ in result.stall_sample_times] or list(_STALL_SIGNALS)
            reason = f"stall:{seat.value}:{'+'.join(signals)}"
            kind = "stall"
        elif result.outcome is SeatOutcome.TIMED_OUT:
            reason = f"timeout:{seat.value}"
            kind = "timeout"
        else:
            reason = f"{seat.value} ended {result.outcome.value}"
            kind = result.outcome.value
        error = AdapterError(reason)
        error.seat_kill = {
            "seat": seat.value,
            "kind": kind,
            "last_sample_times": {name: value for name, value in result.stall_sample_times},
        }
        return error

    def _invoke_seat(
        self, record: RunRecordRef, seats: list[SeatObservation], request: SeatRequest
    ) -> SeatResult:
        """Invoke one seat, moving lanes on a structural limit signal.

        Every attempt is recorded as its own ``seat.completed`` event stamped with
        the lane it ran on. A limit signal cools the lane and re-runs the same
        seat on the next capable lane; when none remains ``current_lane`` raises
        ``LanesExhausted``."""

        assert self._router is not None
        while True:
            lane = self._router.current_lane()
            result = lane.harness.invoke(request)
            if result.lane != lane.name:
                result = replace(result, lane=lane.name)
            observation = SeatObservation(
                seat=request.seat,
                model_class=request.model_class,
                model=result.model,
                outcome=result.outcome,
                exit_code=result.exit_code,
                started_at=result.started_at,
                ended_at=result.ended_at,
                duration_seconds=_duration_seconds(result.started_at, result.ended_at),
                input_tokens=result.usage.input_tokens,
                output_tokens=result.usage.output_tokens,
                cost=result.usage.cost,
                raw_output_ref=result.raw_output_ref,
                stderr_ref=_stderr_ref(result.diagnostics),
                lane=lane.name,
            )
            seats.append(observation)
            self._event(
                record,
                "seat.completed",
                request.item_id,
                result.raw_output_ref,
                payload=self._seat_payload(observation),
            )
            if result.outcome is SeatOutcome.SUCCEEDED:
                self._note_builder_lane(request, result)
                return result
            signal = lane.harness.classify_failure(result)
            if signal is None:
                self._note_builder_lane(request, result)
                return result
            self._router.signal(lane, signal)

    def _note_builder_lane(self, request: SeatRequest, result: SeatResult) -> None:
        if request.seat is Seat.BUILDER:
            self._builder_lane = result.lane

    def _reclaim_if_builder_flipped(
        self, spec: ItemExecutionSpec, record: RunRecordRef, start_lane: str
    ) -> None:
        """Re-record the claim when the builder settled on a different lane.

        The claim actor names the lane the builder ran on. When a limit on the
        starting lane moved the builder to another lane, the tracker claim is
        re-issued so the actor reflects the lane that produced the build. A
        tracker that refuses the re-claim leaves a diagnostic and does not fail
        the item."""

        builder_lane = self._builder_lane
        if not builder_lane or builder_lane == start_lane:
            return
        actor = self._lane_actor(builder_lane)
        try:
            self._ports.tracker.claim(spec.item, actor=actor)
        except Exception as exc:
            self._event(
                record,
                "item.lane_reclaim_failed",
                spec.item.item_id,
                payload={"lane": builder_lane, "error": str(exc)},
            )
            return
        self._event(
            record,
            "item.lane_changed",
            spec.item.item_id,
            payload={"actor": actor, "lane": builder_lane, "from_lane": start_lane},
        )

    def _park_lanes_exhausted(
        self,
        spec: ItemExecutionSpec,
        machine: ItemStateMachine,
        marker: _PhaseMarker,
        record: RunRecordRef,
        workspace: WorkspaceRef | None,
        seats: list[SeatObservation],
        guard: _ParkGuard,
        exc: LanesExhausted,
        claimed: bool,
    ) -> ItemOutcome:
        """Park a claimed item with the lane signature, keeping its tree evidence.

        The campaign reads ``lanes_exhausted`` and stops with that reason."""

        reason = f"{exc.lane}:{exc.signature}" if exc.lane else exc.signature
        if claimed:
            parked = self._park(spec, machine, marker, record, workspace, seats, guard, reason)
            return replace(parked, lanes_exhausted=True, lane_signature=exc.signature)
        self._write_trajectory(
            record, spec.item.item_id, ItemDisposition.FAILED, machine, seats, reason
        )
        return ItemOutcome(
            item_id=spec.item.item_id,
            disposition=ItemDisposition.FAILED,
            states=tuple(machine.history),
            reason=reason,
            title=spec.item.title,
            seats=tuple(seats),
            lane_signature=exc.signature,
            lanes_exhausted=True,
        )

    @staticmethod
    def _seat_payload(observation: SeatObservation) -> dict[str, object]:
        return {
            "seat": observation.seat.value,
            "model_class": observation.model_class,
            "model": observation.model,
            "outcome": observation.outcome.value,
            "exit_code": observation.exit_code,
            "started_at": observation.started_at,
            "ended_at": observation.ended_at,
            "duration_seconds": observation.duration_seconds,
            "input_tokens": observation.input_tokens,
            "output_tokens": observation.output_tokens,
            "cost": observation.cost,
            "raw_output_ref": observation.raw_output_ref,
            "stderr_ref": observation.stderr_ref,
            "lane": observation.lane,
        }

    def _record_follow_ups(
        self,
        spec: ItemExecutionSpec,
        campaign: CampaignRef,
        verdict: ReviewVerdict,
        record: RunRecordRef,
    ) -> tuple[str, ...]:
        """Turn each non-blocking finding on an accepted item into a propose-only
        tracker follow-up. A passing verdict carries non-blocking findings only, so
        every finding here is a reservation the reviewer would still merge past."""

        brief_ref = f"docs/campaigns/{campaign.campaign_id}.md"
        titles: list[str] = []
        for finding in verdict.findings:
            proposal = Proposal(
                title=f"Follow-up: {spec.item.item_id} {finding.code}",
                question=finding.consequence,
                rationale=(
                    f"non-blocking reviewer finding {finding.code}; "
                    f"reviewer evidence {verdict.evidence_ref}"
                ),
                brief_ref=brief_ref,
            )
            ref = self._ports.tracker.propose(proposal, actor="coordinator")
            if ref.runnable:
                raise EvidenceError("follow-up proposal must remain non-runnable")
            self._event(
                record,
                "item.follow_up_proposed",
                spec.item.item_id,
                ref.proposal_id,
                payload={
                    "code": finding.code,
                    "title": proposal.title,
                    "brief_ref": brief_ref,
                },
            )
            titles.append(proposal.title)
        return tuple(titles)

    @staticmethod
    def _verdict_payload(verdict: ReviewVerdict) -> dict[str, object]:
        return {
            "decision": verdict.decision.value,
            "finding_codes": [finding.code for finding in verdict.findings],
            "blocking_finding_codes": [
                finding.code for finding in verdict.findings if finding.blocking
            ],
            "non_blocking_finding_codes": [
                finding.code for finding in verdict.findings if not finding.blocking
            ],
            "evidence_ref": verdict.evidence_ref,
        }

    @staticmethod
    def _trajectory_text(
        disposition: ItemDisposition,
        states: tuple[ItemState, ...],
        seats: list[SeatObservation],
        reason: str,
    ) -> str:
        lines = [
            f"disposition: {disposition.value}",
            "states: " + " -> ".join(state.value for state in states),
        ]
        for observation in seats:
            lane = f", lane={observation.lane}" if observation.lane else ""
            lines.append(
                f"seat {observation.seat.value}: {observation.outcome.value} "
                f"(model_class={observation.model_class}{lane})"
            )
        lines.append(f"reason: {reason}")
        return "\n".join(lines)

    def _write_trajectory(
        self,
        record: RunRecordRef,
        item_id: str,
        disposition: ItemDisposition,
        machine: ItemStateMachine,
        seats: list[SeatObservation],
        reason: str,
    ) -> str:
        return self._ports.records.write_evidence(
            record,
            f"{item_id}-trajectory",
            self._trajectory_text(disposition, tuple(machine.history), seats, reason),
        )

    @staticmethod
    def _read_only_policy(spec: ItemExecutionSpec):
        return type(spec.tool_policy)(
            allowed_tools=frozenset({"read"}),
            allowed_roots=spec.tool_policy.allowed_roots,
        )

    @staticmethod
    def _workspace_policy(policy: ToolPolicy, workspace: WorkspaceRef) -> ToolPolicy:
        external_roots = policy.allowed_roots[1:]
        return ToolPolicy(
            allowed_tools=policy.allowed_tools,
            allowed_roots=(workspace.root, *external_roots),
            allow_destructive=policy.allow_destructive,
            allow_publication=policy.allow_publication,
            allow_protected_merge=policy.allow_protected_merge,
        )

    @staticmethod
    def _workspace_brief(spec: ItemExecutionSpec, workspace: WorkspaceRef) -> Path:
        if spec.brief_path.is_absolute():
            return spec.brief_path
        return workspace.root / spec.brief_path

    def _park_by_nature(
        self,
        spec: ItemExecutionSpec,
        machine: ItemStateMachine,
        marker: _PhaseMarker,
        record: RunRecordRef,
        seats: list[SeatObservation],
        guard: _ParkGuard,
        nature: ItemNature,
    ) -> ItemOutcome:
        """Park an item the worktree fence cannot build before any claim or
        worktree, so a machine or cross-repository item spends no builder seat.
        The park reason names the triage class."""

        reason = f"nature:{nature.value}"
        self._event(
            record,
            "item.triaged",
            spec.item.item_id,
            payload={"nature": nature.value, "reason": reason},
        )
        return self._park(spec, machine, marker, record, None, seats, guard, reason)

    def _park(
        self,
        spec: ItemExecutionSpec,
        machine: ItemStateMachine,
        marker: _PhaseMarker,
        record: RunRecordRef,
        workspace: WorkspaceRef | None,
        seats: list[SeatObservation],
        guard: _ParkGuard,
        reason: str,
        structural: bool = False,
        corrections: int = 0,
        kill: dict[str, object] | None = None,
    ) -> ItemOutcome:
        marker.observe(corrections=corrections)
        last_state = machine.current.value
        if machine.current not in {ItemState.PARKED, ItemState.RELEASED}:
            self._advance(machine, marker, ItemState.PARKED)
        snapshot_dir = ""
        snapshotted: tuple[str, ...] = ()
        if workspace is not None:
            snapshot_dir = f"evidence/{spec.item.item_id}-park"
            snapshotted = self._snapshot(spec, record, workspace, reason, seats)
        trajectory_ref = self._write_trajectory(
            record, spec.item.item_id, ItemDisposition.PARKED, machine, seats, reason
        )
        tracker_reason = f"{reason} [snapshot={snapshot_dir}]" if snapshot_dir else reason
        finalise = None
        try:
            self._ports.tracker.park(
                spec.item.item_id, tracker_reason, actor="coordinator", workspace=workspace
            )
            if workspace is not None:
                tracker_commit = self._ports.workspace.commit_tracker(
                    workspace, spec.item.item_id, item_commit=None
                )
                finalise = self._ports.workspace.deliver(
                    workspace, self._delivery_request(spec, None, tracker_commit)
                )
        except Exception as exc:
            if workspace is not None:
                guard.leave_worktree = True
            self._event(
                record,
                "item.park_failed",
                spec.item.item_id,
                trajectory_ref,
                payload={
                    "reason": reason,
                    "error": str(exc),
                    "snapshot_path": snapshot_dir,
                },
            )
            return ItemOutcome(
                item_id=spec.item.item_id,
                disposition=ItemDisposition.FAILED,
                states=tuple(machine.history),
                reason=str(exc),
                structural_failure=False,
                title=spec.item.title,
                seats=tuple(seats),
            )
        self._emit_parked(
            record,
            spec,
            trajectory_ref,
            reason,
            structural,
            corrections,
            last_state,
            snapshot_dir,
            snapshotted,
            seats,
            kill,
        )
        marker.write("parked")
        return ItemOutcome(
            item_id=spec.item.item_id,
            disposition=ItemDisposition.PARKED,
            states=tuple(machine.history),
            reason=reason,
            finalise=finalise,
            structural_failure=structural,
            title=spec.item.title,
            seats=tuple(seats),
        )

    def _snapshot(
        self,
        spec: ItemExecutionSpec,
        record: RunRecordRef,
        workspace: WorkspaceRef,
        reason: str,
        seats: list[SeatObservation],
    ) -> tuple[str, ...]:
        snapshot = self._ports.workspace.snapshot(workspace)
        base = f"{spec.item.item_id}-park"
        written: list[str] = []
        if snapshot.patch:
            self._ports.records.write_evidence_file(
                record, f"{base}/changes.patch", snapshot.patch
            )
            written.append("changes.patch")
        for file in snapshot.files:
            relative = f"files/{file.path}"
            self._ports.records.write_evidence_file(
                record, f"{base}/{relative}", file.content
            )
            written.append(relative)
        manifest = {
            "schema": "autobuild.park-snapshot.v1",
            "item_id": spec.item.item_id,
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
            "seats": [self._seat_evidence(observation) for observation in seats],
        }
        self._ports.records.write_evidence_file(
            record,
            f"{base}/snapshot.json",
            json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"),
        )
        written.append("snapshot.json")
        return tuple(written)

    def _emit_parked(
        self,
        record: RunRecordRef,
        spec: ItemExecutionSpec,
        trajectory_ref: str,
        reason: str,
        structural: bool,
        corrections: int,
        last_state: str,
        snapshot_dir: str,
        snapshotted: tuple[str, ...],
        seats: list[SeatObservation],
        kill: dict[str, object] | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "reason": reason,
            "structural": structural,
            "corrections_completed": corrections,
            "last_state": last_state,
        }
        if kill:
            payload["kill_kind"] = kill.get("kind")
            payload["last_sample_times"] = kill.get("last_sample_times", {})
        if snapshot_dir:
            payload["snapshot_path"] = snapshot_dir
            payload["snapshotted_paths"] = list(snapshotted)
            payload["seat_evidence"] = [
                self._seat_evidence(observation) for observation in seats
            ]
        self._event(record, "item.parked", spec.item.item_id, trajectory_ref, payload=payload)

    @staticmethod
    def _seat_evidence(observation: SeatObservation) -> dict[str, object]:
        return {
            "seat": observation.seat.value,
            "outcome": observation.outcome.value,
            "raw_output_ref": observation.raw_output_ref,
            "stderr_ref": observation.stderr_ref,
        }

    @staticmethod
    def _advance(machine: ItemStateMachine, marker: _PhaseMarker, state: ItemState) -> None:
        machine.transition(state)
        marker.write(state.value)

    def _event(
        self,
        record: RunRecordRef,
        event_type: str,
        item_id: str | None = None,
        *refs: str,
        payload: dict[str, object] | None = None,
    ) -> None:
        # The event is stamped once, here, so its recorded payload and its owner
        # progress line carry exactly the same timestamp. The run-record adapter
        # keeps a non-empty ``occurred_at`` and stamps only events created without
        # one.
        event = RunEvent(
            event_type=event_type,
            occurred_at=datetime.now(UTC).isoformat(),
            item_id=item_id,
            evidence_refs=tuple(refs),
            payload=dict(payload or {}),
        )
        self._ports.records.append(record, event)
        line = render_progress_line(event)
        if line:
            self._ports.progress.emit(line)

    @staticmethod
    def _delivery_request(
        spec: ItemExecutionSpec, item_commit: str | None, tracker_commit: str
    ) -> DeliveryRequest:
        return DeliveryRequest(
            spec.item.item_id,
            item_commit,
            tracker_commit,
            spec.delivery_mode,
            spec.delivery_target_branch,
            spec.delivery_target_revision,
            spec.push_current_branch,
            spec.allow_current_branch_default,
        )
