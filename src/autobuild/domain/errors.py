"""Typed failures returned across AutoBuild boundaries."""


class AutoBuildError(Exception):
    """Base class for an expected AutoBuild failure."""


class CapabilityError(AutoBuildError):
    """The runtime cannot provide a capability required by the workflow."""


class MissingBindingError(CapabilityError):
    """No compatible adapter can satisfy a required port."""


class AmbiguousBindingError(CapabilityError):
    """Discovery found more than one adapter and configuration did not choose."""


class AdapterError(AutoBuildError):
    """An adapter failed to honour its port contract."""


class PolicyViolation(AutoBuildError):
    """Deterministic enforcement refused a requested action."""


class ScopeFenceViolation(PolicyViolation):
    """The tracker offered or was asked to claim an item outside the campaign
    selection fence. The campaign stops without a claim."""


class EvidenceError(AutoBuildError):
    """Evidence is stale, incomplete, or inconsistent with the requested action."""


class PreflightError(AutoBuildError):
    """A preflight probe found the environment unfit and stopped the launch."""


class LanesExhausted(AutoBuildError):
    """No capable harness lane remains for a seat.

    The failed seat cooled the last lane in preference order. The item parks with
    the carried signature, its worktree is kept, and the campaign stops with the
    ``lanes_exhausted`` reason. ``lane`` names the last lane that was tried."""

    def __init__(self, signature: str, lane: str) -> None:
        self.signature = signature
        self.lane = lane
        super().__init__(
            f"no capable lane remains; last lane {lane} cooled with signature {signature}"
        )


class LeaseHeld(AutoBuildError):
    """A live single-writer lease names another holder for the same surface.

    The campaign refuses to write into a surface a live campaign owns. There is
    no automatic take-over: the founder rule is that the holder is stopped first.
    The held record is carried so the message names the holder."""

    def __init__(self, record: "LeaseRecord") -> None:
        self.record = record
        super().__init__(
            f"{record.surface_kind.value} surface {record.surface_path} is held by "
            f"campaign {record.campaign_id} (process {record.process_id} on {record.host}); "
            "stop the holder before starting a second campaign"
        )
