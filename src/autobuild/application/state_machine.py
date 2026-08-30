"""Pure item lifecycle transitions."""

from __future__ import annotations

from dataclasses import dataclass, field

from autobuild.domain import ItemState

ALLOWED_TRANSITIONS: dict[ItemState, frozenset[ItemState]] = {
    ItemState.READY: frozenset({ItemState.VERIFIED}),
    ItemState.VERIFIED: frozenset({ItemState.CLAIMED}),
    ItemState.CLAIMED: frozenset({ItemState.ISOLATED, ItemState.PARKED}),
    ItemState.ISOLATED: frozenset({ItemState.BUILT, ItemState.PARKED}),
    ItemState.BUILT: frozenset({ItemState.VALIDATED, ItemState.PARKED}),
    ItemState.VALIDATED: frozenset({ItemState.REVIEWED, ItemState.PARKED}),
    ItemState.REVIEWED: frozenset(
        {ItemState.CORRECTING, ItemState.ESCALATED, ItemState.FINALISED, ItemState.PARKED}
    ),
    ItemState.CORRECTING: frozenset({ItemState.BUILT, ItemState.PARKED}),
    ItemState.ESCALATED: frozenset(
        {ItemState.CORRECTING, ItemState.FINALISED, ItemState.PARKED}
    ),
    ItemState.FINALISED: frozenset({ItemState.RELEASED}),
    ItemState.PARKED: frozenset({ItemState.RELEASED}),
    ItemState.RELEASED: frozenset(),
}


@dataclass(slots=True)
class ItemStateMachine:
    current: ItemState = ItemState.READY
    history: list[ItemState] = field(default_factory=lambda: [ItemState.READY])

    def transition(self, target: ItemState) -> None:
        if target not in ALLOWED_TRANSITIONS[self.current]:
            raise ValueError(f"invalid item transition: {self.current.value} -> {target.value}")
        self.current = target
        self.history.append(target)
