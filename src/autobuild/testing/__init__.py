"""Reusable deterministic fakes for workflow and adapter contract tests."""

from autobuild.testing.fakes import (
    FakeAdapter,
    FakeAgentSdkClient,
    FakeCommandAdapter,
    FakeEnvironmentProbe,
    FakeFilesystemProbe,
    FakeHarnessAdapter,
    FakeKnowledgeAdapter,
    FakeLaneStateAdapter,
    FakeLeaseAdapter,
    FakeNetworkProbe,
    FakeProgressPort,
    FakeRunRecordAdapter,
    FakeTrackerAdapter,
    FakeUsageMeter,
    FakeWorkspaceAdapter,
)

__all__ = [
    "FakeAdapter",
    "FakeAgentSdkClient",
    "FakeCommandAdapter",
    "FakeEnvironmentProbe",
    "FakeFilesystemProbe",
    "FakeHarnessAdapter",
    "FakeKnowledgeAdapter",
    "FakeLaneStateAdapter",
    "FakeLeaseAdapter",
    "FakeNetworkProbe",
    "FakeProgressPort",
    "FakeRunRecordAdapter",
    "FakeTrackerAdapter",
    "FakeUsageMeter",
    "FakeWorkspaceAdapter",
]
