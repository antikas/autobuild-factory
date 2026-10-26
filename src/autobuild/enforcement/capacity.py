"""The capacity rule: item and heavy-command concurrency from one host reading."""

from __future__ import annotations

import math

from autobuild.domain import CapacityPolicy, HostCapacity, HostReading, bytes_to_gib


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(value, high))


def derive_capacity(reading: HostReading, policy: CapacityPolicy) -> HostCapacity:
    """The single owner of the capacity formula.

    ``items = clamp(min(logical // 4, floor((available - reserve) / per_item)),
    1, max_concurrency)`` with memory in GiB, and ``heavy = clamp(logical // 12,
    1, items)``. An unmeasured available-memory field drops the memory term; an
    unmeasured CPU count gives 1 for both. An explicit profile integer replaces
    the automatic value; heavy never exceeds items, because each item runs at
    most one command at a time."""

    cpu_term = None if reading.logical_cpus is None else reading.logical_cpus // 4
    available_gib = bytes_to_gib(reading.memory_available_bytes)
    memory_term = (
        None
        if available_gib is None
        else math.floor((available_gib - policy.reserve_gib) / policy.per_item_gib)
    )
    if policy.concurrency is not None:
        items, items_source = policy.concurrency, "profile"
    elif cpu_term is None:
        items, items_source = 1, "auto"
    else:
        terms = [cpu_term] if memory_term is None else [cpu_term, memory_term]
        items, items_source = _clamp(min(terms), 1, policy.max_concurrency), "auto"
    if policy.heavy_concurrency is not None:
        heavy, heavy_source = min(policy.heavy_concurrency, items), "profile"
    elif reading.logical_cpus is None:
        heavy, heavy_source = 1, "auto"
    else:
        heavy, heavy_source = _clamp(reading.logical_cpus // 12, 1, items), "auto"
    return HostCapacity(items, heavy, items_source, heavy_source, cpu_term, memory_term)
