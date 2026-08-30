"""Port bundle injected into the application layer by the composition root."""

from dataclasses import dataclass

from autobuild.ports import CommandPort, HarnessPort, KnowledgePort, RunRecordPort, TrackerPort, WorkspacePort


@dataclass(frozen=True, slots=True)
class WorkflowPorts:
    tracker: TrackerPort
    workspace: WorkspacePort
    harness: HarnessPort
    command: CommandPort
    records: RunRecordPort
    knowledge: KnowledgePort
