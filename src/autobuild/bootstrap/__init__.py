"""Runtime composition: the only layer allowed to select adapters."""

from autobuild.bootstrap.registry import AdapterRegistry, AdapterSelection
from autobuild.bootstrap.runtime import PortBinding, RuntimeBinding, RuntimeResolver
from autobuild.bootstrap.builtins import register_first_party_harnesses
from autobuild.bootstrap.environment import configure_scratch_environment

__all__ = [
    "AdapterRegistry",
    "AdapterSelection",
    "PortBinding",
    "RuntimeBinding",
    "RuntimeResolver",
    "configure_scratch_environment",
    "register_first_party_harnesses",
]
