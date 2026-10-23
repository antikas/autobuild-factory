"""First-party harness registrations for runtime composition."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from autobuild.adapters import (
    ClaudeCodeHarnessAdapter,
    CodexHarnessAdapter,
    CopilotCliHarnessAdapter,
)
from autobuild.adapters.claude_sdk_harness import ClaudeSdkHarnessAdapter
from autobuild.bootstrap.registry import AdapterRegistry
from autobuild.domain import PortKind


def _required(config: Mapping[str, object], key: str):
    if key not in config:
        raise ValueError(f"harness adapter configuration requires {key!r}")
    return config[key]


def _command(config: Mapping[str, object], default: str) -> tuple[str, ...]:
    value = config.get("command", (default,))
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (tuple, list)) and all(isinstance(part, str) for part in value):
        return tuple(value)
    raise TypeError("harness command must be a string or sequence of strings")


def _models(config: Mapping[str, object]) -> Mapping[str, str] | None:
    value = config.get("model_map")
    if value is None:
        return None
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) and isinstance(model, str) for key, model in value.items()
    ):
        raise TypeError("model_map must map model classes to CLI model identifiers")
    return value


def register_first_party_harnesses(registry: AdapterRegistry) -> None:
    def claude(config: Mapping[str, object]):
        return ClaudeCodeHarnessAdapter(
            _required(config, "command_port"),
            Path(_required(config, "output_root")),
            _command(config, "claude"),
            _models(config),
        )

    def codex(config: Mapping[str, object]):
        return CodexHarnessAdapter(
            _required(config, "command_port"),
            Path(_required(config, "output_root")),
            _command(config, "codex"),
            _models(config),
        )

    def claude_sdk(config: Mapping[str, object]):
        key_file = config.get("key_file")
        if key_file is not None and not isinstance(key_file, (str, Path)):
            raise TypeError("key_file must be a path")
        return ClaudeSdkHarnessAdapter(
            _required(config, "command_port"),
            Path(_required(config, "output_root")),
            Path(key_file) if key_file is not None else None,
            _models(config),
        )

    def copilot(config: Mapping[str, object]):
        return CopilotCliHarnessAdapter(
            _required(config, "command_port"),
            Path(_required(config, "output_root")),
            _command(config, "copilot"),
            model_map=_models(config),
        )

    registry.register(PortKind.HARNESS, "claude-code", claude)
    registry.register(PortKind.HARNESS, "claude-sdk", claude_sdk)
    registry.register(PortKind.HARNESS, "codex", codex)
    registry.register(PortKind.HARNESS, "github-copilot", copilot)
