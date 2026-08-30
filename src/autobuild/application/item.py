"""The fixed per-item sequence, expressed only through typed ports."""

from __future__ import annotations

from pathlib import Path

from autobuild.application.dependencies import WorkflowPorts
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
    ItemOutcome,
    ItemState,
    ReviewDecision,
    ReviewVerdict,
    RunEvent,
    RunRecordRef,
    Seat,
    SeatOutcome,
    SeatRequest,
    ToolPolicy,
    ValidationEvidence,
    WorkspaceRef,
)


class ItemWorkflow:
    def __init__(self, ports: WorkflowPorts) -> None:
        self._ports = ports

    def run(self, campaign: CampaignRef, spec: ItemExecutionSpec, record: RunRecordRef) -> ItemOutcome:
        machine = ItemStateMachine()
        workspace: WorkspaceRef | None = None
        claimed = False
        try:
            machine.transition(ItemState.VERIFIED)
            self._ports.tracker.claim(spec.item, actor="builder")
            claimed = True
            machine.transition(ItemState.CLAIMED)
            self._event(record, "item.claimed", spec.item.item_id)

            workspace = self._ports.workspace.create_isolated(campaign, spec.item)
            machine.transition(ItemState.ISOLATED)
            self._event(record, "workspace.created", spec.item.item_id, workspace.start_commit)

            verdict, diff, validation = self._build_validate_review(
                spec, workspace, machine, record, context_refs=()
            )
            corrections = 0
            while verdict.decision is ReviewDecision.CORRECT:
                if corrections >= spec.max_corrections:
                    return self._park(
                        spec,
                        machine,
                        record,
                        workspace,
                        "material finding remained after the correction ceiling",
                    )
                corrections += 1
                machine.transition(ItemState.CORRECTING)
                verdict, diff, validation = self._build_validate_review(
                    spec,
                    workspace,
                    machine,
                    record,
                    context_refs=(verdict.evidence_ref,),
                )

            if verdict.decision is ReviewDecision.ESCALATE:
                machine.transition(ItemState.ESCALATED)
                verdict = self._specialist_review(spec, workspace, verdict, record)
                if verdict.decision is ReviewDecision.CORRECT:
                    if corrections >= spec.max_corrections:
                        return self._park(
                            spec,
                            machine,
                            record,
                            workspace,
                            "specialist finding reached the correction ceiling",
                        )
                    corrections += 1
                    machine.transition(ItemState.CORRECTING)
                    verdict, diff, validation = self._build_validate_review(
                        spec,
                        workspace,
                        machine,
                        record,
                        context_refs=(verdict.evidence_ref,),
                    )

            if verdict.decision is not ReviewDecision.PASS:
                return self._park(
                    spec,
                    machine,
                    record,
                    workspace,
                    f"review disposition was {verdict.decision.value}",
                )

            trajectory_ref = self._ports.records.write_evidence(
                record,
                f"{spec.item.item_id}-trajectory",
                f"accepted after {corrections} correction round(s); review={verdict.evidence_ref}",
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
                workspace,
                DeliveryRequest(spec.item.item_id, item_commit, tracker_commit),
            )
            machine.transition(ItemState.FINALISED)
            self._event(record, "item.finalised", spec.item.item_id, finalise.item_commit, tracker_commit)
            self._ports.workspace.release(workspace)
            machine.transition(ItemState.RELEASED)
            return ItemOutcome(
                item_id=spec.item.item_id,
                disposition=ItemDisposition.ACCEPTED,
                states=tuple(machine.history),
                finalise=finalise,
            )
        except Exception as exc:
            structural = isinstance(exc, (EvidenceError, TypeError, ValueError))
            if claimed:
                try:
                    outcome = self._park(spec, machine, record, workspace, str(exc), structural=structural)
                    return outcome
                except Exception:
                    structural = True
            return ItemOutcome(
                item_id=spec.item.item_id,
                disposition=ItemDisposition.FAILED,
                states=tuple(machine.history),
                reason=str(exc),
                structural_failure=structural,
            )
        finally:
            if workspace is not None and machine.current is not ItemState.RELEASED:
                self._ports.workspace.release(workspace)

    def _build_validate_review(
        self,
        spec: ItemExecutionSpec,
        workspace: WorkspaceRef,
        machine: ItemStateMachine,
        record: RunRecordRef,
        context_refs: tuple[str, ...],
    ) -> tuple[ReviewVerdict, DiffEvidence, ValidationEvidence]:
        brief_path = self._workspace_brief(spec, workspace)
        builder_policy = self._workspace_policy(spec.tool_policy, workspace)
        builder = self._ports.harness.invoke(
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
            )
        )
        if builder.outcome is not SeatOutcome.SUCCEEDED:
            raise AdapterError(f"builder ended {builder.outcome.value}")
        if not isinstance(builder.payload, BuilderReport):
            raise EvidenceError("builder result did not contain a typed BuilderReport")
        machine.transition(ItemState.BUILT)
        self._event(record, "build.completed", spec.item.item_id, builder.payload.report_ref)

        diff = self._ports.workspace.diff(workspace)
        command = self._ports.command.run(
            CommandRequest(
                command_id=f"{record.run_id}:{spec.item.item_id}:{spec.validator_id}",
                argv=spec.validator_argv,
                cwd=workspace.root,
                timeout_seconds=spec.command_timeout_seconds,
            )
        )
        if command.exit_code != 0 or command.timed_out or command.cancelled:
            raise AdapterError(f"validator {spec.validator_id} did not pass")
        validation = ValidationEvidence(
            validator_id=spec.validator_id,
            workspace_revision=diff.workspace_revision,
            command=command,
            changed_paths=diff.changed_paths,
        )
        machine.transition(ItemState.VALIDATED)
        self._event(record, "validation.completed", spec.item.item_id, command.stdout_ref)

        reviewer_policy = self._workspace_policy(
            spec.reviewer_tool_policy or self._read_only_policy(spec), workspace
        )
        reviewer = self._ports.harness.invoke(
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
            )
        )
        if reviewer.outcome is not SeatOutcome.SUCCEEDED:
            raise AdapterError(f"reviewer ended {reviewer.outcome.value}")
        if not isinstance(reviewer.payload, ReviewVerdict):
            raise EvidenceError("reviewer result did not contain a typed ReviewVerdict")
        if reviewer.payload.item_id != spec.item.item_id:
            raise EvidenceError("review verdict item does not match the selected item")
        machine.transition(ItemState.REVIEWED)
        self._event(record, "review.completed", spec.item.item_id, reviewer.payload.evidence_ref)
        return reviewer.payload, diff, validation

    def _specialist_review(
        self,
        spec: ItemExecutionSpec,
        workspace: WorkspaceRef,
        triggering: ReviewVerdict,
        record: RunRecordRef,
    ) -> ReviewVerdict:
        brief_path = self._workspace_brief(spec, workspace)
        reviewer_policy = self._workspace_policy(
            spec.reviewer_tool_policy or self._read_only_policy(spec), workspace
        )
        result = self._ports.harness.invoke(
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
            )
        )
        if result.outcome is not SeatOutcome.SUCCEEDED or not isinstance(result.payload, ReviewVerdict):
            raise EvidenceError("specialist result did not contain a typed ReviewVerdict")
        if result.payload.item_id != spec.item.item_id:
            raise EvidenceError("specialist verdict item does not match the selected item")
        self._event(record, "specialist.completed", spec.item.item_id, result.payload.evidence_ref)
        return result.payload

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

    def _park(
        self,
        spec: ItemExecutionSpec,
        machine: ItemStateMachine,
        record: RunRecordRef,
        workspace: WorkspaceRef | None,
        reason: str,
        structural: bool = False,
    ) -> ItemOutcome:
        if machine.current not in {ItemState.PARKED, ItemState.RELEASED}:
            machine.transition(ItemState.PARKED)
        self._ports.tracker.park(spec.item.item_id, reason, actor="coordinator", workspace=workspace)
        self._event(record, "item.parked", spec.item.item_id)
        if workspace is not None and machine.current is not ItemState.RELEASED:
            tracker_commit = self._ports.workspace.commit_tracker(
                workspace, spec.item.item_id, item_commit=None
            )
            finalise = self._ports.workspace.deliver(
                workspace,
                DeliveryRequest(spec.item.item_id, item_commit=None, tracker_commit=tracker_commit),
            )
            self._ports.workspace.release(workspace)
            machine.transition(ItemState.RELEASED)
        else:
            finalise = None
        return ItemOutcome(
            item_id=spec.item.item_id,
            disposition=ItemDisposition.PARKED,
            states=tuple(machine.history),
            reason=reason,
            finalise=finalise,
            structural_failure=structural,
        )

    def _event(self, record: RunRecordRef, event_type: str, item_id: str, *refs: str) -> None:
        self._ports.records.append(
            record,
            RunEvent(event_type=event_type, occurred_at="adapter-time", item_id=item_id, evidence_refs=tuple(refs)),
        )
