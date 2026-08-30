"""Resolve, probe, and freeze one adapter binding per required port."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, TypeVar, cast

from autobuild.bootstrap.registry import AdapterRegistry, AdapterSelection
from autobuild.domain import (
    AdapterIdentity,
    AmbiguousBindingError,
    MissingBindingError,
    PortKind,
    ProbeResult,
)

PortT = TypeVar("PortT")


@dataclass(frozen=True, slots=True)
class PortBinding:
    kind: PortKind
    adapter_name: str
    adapter: object
    identity: AdapterIdentity


@dataclass(frozen=True, slots=True)
class RuntimeBinding:
    """Immutable adapter set used for the complete campaign."""

    bindings: tuple[PortBinding, ...]

    def __post_init__(self) -> None:
        kinds = tuple(binding.kind for binding in self.bindings)
        if len(kinds) != len(set(kinds)):
            raise ValueError("runtime binding contains duplicate port kinds")

    def get(self, kind: PortKind) -> object:
        for binding in self.bindings:
            if binding.kind is kind:
                return binding.adapter
        raise MissingBindingError(f"runtime has no {kind.value} binding")

    def typed(self, kind: PortKind, protocol: type[PortT]) -> PortT:
        adapter = self.get(kind)
        if not isinstance(adapter, protocol):
            raise TypeError(f"bound adapter {kind.value} does not satisfy {protocol.__name__}")
        return cast(PortT, adapter)

    @property
    def manifest(self) -> tuple[tuple[str, str, str], ...]:
        return tuple(
            (binding.kind.value, binding.identity.name, binding.identity.version)
            for binding in sorted(self.bindings, key=lambda current: current.kind.value)
        )


class RuntimeResolver:
    def __init__(self, registry: AdapterRegistry) -> None:
        self._registry = registry

    def resolve(
        self,
        required: Iterable[PortKind],
        selections: Iterable[AdapterSelection] = (),
        required_capabilities: Mapping[PortKind, frozenset[str]] | None = None,
    ) -> RuntimeBinding:
        selected = {selection.kind: selection for selection in selections}
        capabilities = required_capabilities or {}
        bindings = tuple(
            self._resolve_one(kind, selected.get(kind), capabilities.get(kind, frozenset()))
            for kind in tuple(required)
        )
        return RuntimeBinding(bindings)

    def _resolve_one(
        self,
        kind: PortKind,
        selection: AdapterSelection | None,
        required_capabilities: frozenset[str],
    ) -> PortBinding:
        if selection is not None:
            adapter = self._registry.create(kind, selection.name, selection.config)
            probe = self._probe(adapter)
            return self._admit(kind, selection.name, adapter, probe, required_capabilities)

        compatible: list[PortBinding] = []
        diagnostics: list[str] = []
        for registration in self._registry.registrations(kind):
            adapter = registration.factory({})
            probe = self._probe(adapter)
            if not probe.available:
                diagnostics.extend(probe.diagnostics)
                continue
            assert probe.identity is not None
            if not required_capabilities.issubset(probe.identity.capabilities):
                continue
            compatible.append(PortBinding(kind, registration.name, adapter, probe.identity))

        if not compatible:
            detail = "; ".join(diagnostics) if diagnostics else "no compatible registration"
            raise MissingBindingError(f"no {kind.value} adapter available: {detail}")
        if len(compatible) > 1:
            names = ", ".join(binding.adapter_name for binding in compatible)
            raise AmbiguousBindingError(f"multiple {kind.value} adapters available: {names}")
        return compatible[0]

    @staticmethod
    def _probe(adapter: object) -> ProbeResult:
        probe_method = getattr(adapter, "probe", None)
        if probe_method is None or not callable(probe_method):
            raise TypeError("registered adapter has no callable probe()")
        result = probe_method()
        if not isinstance(result, ProbeResult):
            raise TypeError("adapter probe() must return ProbeResult")
        return result

    @staticmethod
    def _admit(
        kind: PortKind,
        name: str,
        adapter: object,
        probe: ProbeResult,
        required_capabilities: frozenset[str],
    ) -> PortBinding:
        if not probe.available or probe.identity is None:
            detail = "; ".join(probe.diagnostics) or "probe unavailable"
            raise MissingBindingError(f"configured {kind.value} adapter {name!r} unavailable: {detail}")
        missing = required_capabilities - probe.identity.capabilities
        if missing:
            raise MissingBindingError(
                f"configured {kind.value} adapter {name!r} lacks capabilities: {', '.join(sorted(missing))}"
            )
        return PortBinding(kind, name, adapter, probe.identity)
