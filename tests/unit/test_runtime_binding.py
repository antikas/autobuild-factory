from __future__ import annotations

import pytest

from autobuild.bootstrap import AdapterRegistry, AdapterSelection, RuntimeResolver
from autobuild.domain import (
    AdapterIdentity,
    AmbiguousBindingError,
    MissingBindingError,
    PortKind,
)
from autobuild.testing import FakeAdapter


def factory(name: str, *, available: bool = True, capabilities: frozenset[str] = frozenset()):
    return lambda config: FakeAdapter(
        AdapterIdentity(name, str(config.get("version", "1")), capabilities),
        available=available,
        diagnostics=("offline",) if not available else (),
    )


def test_explicit_selection_wins_and_binding_is_frozen() -> None:
    registry = AdapterRegistry()
    registry.register(PortKind.HARNESS, "first", factory("first", capabilities=frozenset({"invoke"})))
    registry.register(PortKind.HARNESS, "second", factory("second", capabilities=frozenset({"invoke"})))

    binding = RuntimeResolver(registry).resolve(
        [PortKind.HARNESS],
        [AdapterSelection(PortKind.HARNESS, "second", {"version": "9"})],
        {PortKind.HARNESS: frozenset({"invoke"})},
    )

    assert binding.manifest == (("harness", "second", "9"),)
    with pytest.raises(AttributeError):
        binding.bindings += ()


def test_discovery_refuses_ambiguity() -> None:
    registry = AdapterRegistry()
    registry.register(PortKind.HARNESS, "first", factory("first"))
    registry.register(PortKind.HARNESS, "second", factory("second"))

    with pytest.raises(AmbiguousBindingError, match="multiple harness adapters"):
        RuntimeResolver(registry).resolve([PortKind.HARNESS])


def test_discovery_reports_missing_capability() -> None:
    registry = AdapterRegistry()
    registry.register(PortKind.HARNESS, "limited", factory("limited", capabilities=frozenset({"probe"})))

    with pytest.raises(MissingBindingError, match="no harness adapter available"):
        RuntimeResolver(registry).resolve(
            [PortKind.HARNESS], required_capabilities={PortKind.HARNESS: frozenset({"invoke"})}
        )


def test_configured_unavailable_adapter_fails_before_claim() -> None:
    registry = AdapterRegistry()
    registry.register(PortKind.HARNESS, "offline", factory("offline", available=False))

    with pytest.raises(MissingBindingError, match="configured harness adapter.*unavailable"):
        RuntimeResolver(registry).resolve(
            [PortKind.HARNESS], [AdapterSelection(PortKind.HARNESS, "offline", {})]
        )
