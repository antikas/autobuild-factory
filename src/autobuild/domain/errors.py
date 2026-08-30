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


class EvidenceError(AutoBuildError):
    """Evidence is stale, incomplete, or inconsistent with the requested action."""
