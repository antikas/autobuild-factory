"""Adapter registration and entry-point discovery."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import metadata
from typing import Callable, Iterable, Mapping

from autobuild.domain import AdapterError, PortKind

AdapterFactory = Callable[[Mapping[str, object]], object]


@dataclass(frozen=True, slots=True)
class AdapterSelection:
    kind: PortKind
    name: str
    config: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class AdapterRegistration:
    kind: PortKind
    name: str
    factory: AdapterFactory


class AdapterRegistry:
    """Registry populated by built-ins or third-party Python entry points."""

    ENTRY_POINT_GROUP = "autobuild.adapters"

    def __init__(self) -> None:
        self._registrations: dict[tuple[PortKind, str], AdapterRegistration] = {}

    def register(self, kind: PortKind, name: str, factory: AdapterFactory) -> None:
        key = (kind, name.strip())
        if not key[1]:
            raise ValueError("adapter registration name must not be empty")
        if key in self._registrations:
            raise AdapterError(f"adapter already registered: {kind.value}:{name}")
        self._registrations[key] = AdapterRegistration(kind, key[1], factory)

    def names(self, kind: PortKind) -> tuple[str, ...]:
        return tuple(sorted(name for registered_kind, name in self._registrations if registered_kind is kind))

    def create(self, kind: PortKind, name: str, config: Mapping[str, object] | None = None) -> object:
        registration = self._registrations.get((kind, name))
        if registration is None:
            raise AdapterError(f"adapter not registered: {kind.value}:{name}")
        return registration.factory(config or {})

    def registrations(self, kind: PortKind) -> tuple[AdapterRegistration, ...]:
        return tuple(
            registration
            for (registered_kind, _), registration in sorted(
                self._registrations.items(), key=lambda entry: (entry[0][0].value, entry[0][1])
            )
            if registered_kind is kind
        )

    def load_entry_points(self, entry_points: Iterable[object] | None = None) -> tuple[str, ...]:
        discovered = tuple(entry_points) if entry_points is not None else self._system_entry_points()
        loaded: list[str] = []
        for entry_point in sorted(discovered, key=lambda candidate: str(getattr(candidate, "name", ""))):
            plugin = entry_point.load()
            if hasattr(plugin, "register"):
                plugin.register(self)
            elif callable(plugin):
                plugin(self)
            else:
                raise AdapterError(f"entry point {entry_point.name!r} is not an adapter registrar")
            loaded.append(str(entry_point.name))
        return tuple(loaded)

    def _system_entry_points(self) -> tuple[object, ...]:
        available = metadata.entry_points()
        if hasattr(available, "select"):
            return tuple(available.select(group=self.ENTRY_POINT_GROUP))
        return tuple(available.get(self.ENTRY_POINT_GROUP, ()))
