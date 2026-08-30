from __future__ import annotations

from dataclasses import dataclass

import pytest

from autobuild.bootstrap import AdapterRegistry, register_first_party_harnesses
from autobuild.domain import AdapterError, AdapterIdentity, PortKind
from autobuild.testing import FakeAdapter


@dataclass
class FakeEntryPoint:
    name: str
    plugin: object

    def load(self) -> object:
        return self.plugin


def test_fourth_harness_registers_through_entry_point() -> None:
    registry = AdapterRegistry()

    def register(registry_to_extend: AdapterRegistry) -> None:
        registry_to_extend.register(
            PortKind.HARNESS,
            "test-fourth-harness",
            lambda config: FakeAdapter(
                AdapterIdentity("test-fourth-harness", str(config.get("version", "1")), frozenset({"invoke"}))
            ),
        )

    loaded = registry.load_entry_points([FakeEntryPoint("test-plugin", register)])

    assert loaded == ("test-plugin",)
    adapter = registry.create(PortKind.HARNESS, "test-fourth-harness", {"version": "2"})
    assert adapter.probe().identity == AdapterIdentity("test-fourth-harness", "2", frozenset({"invoke"}))


def test_duplicate_registration_is_rejected() -> None:
    registry = AdapterRegistry()
    registry.register(PortKind.HARNESS, "same", lambda config: object())
    with pytest.raises(AdapterError, match="already registered"):
        registry.register(PortKind.HARNESS, "same", lambda config: object())


def test_first_party_harnesses_share_the_same_registration_surface(tmp_path) -> None:
    registry = AdapterRegistry()
    command = FakeAdapter(AdapterIdentity("command", "1"))

    register_first_party_harnesses(registry)

    assert registry.names(PortKind.HARNESS) == ("claude-code", "codex", "github-copilot")
    created = registry.create(
        PortKind.HARNESS,
        "codex",
        {"command_port": command, "output_root": tmp_path, "command": "codex"},
    )
    assert created.adapter_name == "codex"
