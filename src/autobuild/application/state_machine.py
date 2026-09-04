"""Pure item lifecycle transitions."""

from __future__ import annotations

from dataclasses import dataclass, field

from autobuild.domain import ItemState

ALLOWED_TRANSITIONS: dict[ItemState, frozenset[ItemState]] = {
    ItemState.READY: frozenset({ItemState.VERIFIED, ItemState.PARKED}),
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


# The canonical route from READY to any resumable state, used to seed a machine
# that resumes an interrupted item so its recorded history reads sensibly.
_CANONICAL_ROUTE: tuple[ItemState, ...] = (
    ItemState.READY,
    ItemState.VERIFIED,
    ItemState.CLAIMED,
    ItemState.ISOLATED,
    ItemState.BUILT,
    ItemState.VALIDATED,
    ItemState.REVIEWED,
)


@dataclass(slots=True)
class ItemStateMachine:
    current: ItemState = ItemState.READY
    history: list[ItemState] = field(default_factory=lambda: [ItemState.READY])

    def transition(self, target: ItemState) -> None:
        if target not in ALLOWED_TRANSITIONS[self.current]:
            raise ValueError(f"invalid item transition: {self.current.value} -> {target.value}")
        self.current = target
        self.history.append(target)

    @classmethod
    def resume_at(cls, state: ItemState) -> "ItemStateMachine":
        """Seed a machine to an interrupted item's phase so it can be resumed.

        The history is the canonical route up to ``state`` (with the two states
        that branch off the main route appended after ``reviewed``), so later
        transitions validate against the real phase without pretending the prior
        seats ran in this process."""

        if state in _CANONICAL_ROUTE:
            history = list(_CANONICAL_ROUTE[: _CANONICAL_ROUTE.index(state) + 1])
        elif state in {ItemState.CORRECTING, ItemState.ESCALATED, ItemState.FINALISED}:
            history = list(_CANONICAL_ROUTE) + [state]
        else:
            raise ValueError(f"{state.value} is not a resumable phase")
        return cls(current=state, history=history)
