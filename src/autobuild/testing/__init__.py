"""Reusable deterministic fakes for workflow and adapter contract tests."""

from autobuild.testing.fakes import (
    FakeAdapter,
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
    FakeWorkspaceAdapter,
)

__all__ = [
    "FakeAdapter",
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
    "FakeWorkspaceAdapter",
]
