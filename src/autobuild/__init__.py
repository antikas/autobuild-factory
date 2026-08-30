"""Portable, adapter-driven AutoBuild workflow."""

from importlib.metadata import PackageNotFoundError, version

from autobuild.domain import PortKind

__all__ = ["PortKind"]

try:
    __version__ = version("autobuild")
except PackageNotFoundError:
    __version__ = "development"
