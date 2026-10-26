"""Reusable deterministic fakes for workflow and adapter contract tests."""

from autobuild.testing.fakes import (
    FakeAdapter,
    FakeAgentSdkClient,
    FakeClock,
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
    "FakeClock",
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
