"""GitHub Copilot CLI adapter."""

from __future__ import annotations

import shutil
from pathlib import Path
from uuid import uuid4

from autobuild.adapters.harness_cli import CliHarnessAdapter
from autobuild.domain import SeatRequest
from autobuild.ports import CommandPort


class CopilotCliHarnessAdapter(CliHarnessAdapter):
    adapter_name = "github-copilot"

    def __init__(
        self,
        command_port: CommandPort,
        output_root: Path,
        command: tuple[str, ...] = ("copilot",),
        auth_command: tuple[str, ...] = ("gh", "auth", "status"),
        model_map=None,
    ) -> None:
        super().__init__(command_port, output_root, command, model_map)
        self._auth_command = auth_command

    def _version_argv(self) -> tuple[str, ...]:
        return (*self._command, "version")

    def _probe_authentication(self) -> tuple[bool, str]:
        if self.environment_has_github_token():
            return True, "GitHub Copilot token environment is available"
        if not self._auth_command:
            return False, "GitHub Copilot authentication source is unavailable"
        auth_executable = self._auth_command[0]
        if shutil.which(auth_executable) is None and not Path(auth_executable).is_file():
            return False, f"{auth_executable} executable was not found"
        result = self._probe_run("auth", self._auth_command)
        authenticated = result.exit_code == 0
        return authenticated, "GitHub CLI authentication fallback is available" if authenticated else "GitHub Copilot is not authenticated"

    def _invocation(self, request: SeatRequest, run_ref: str):
        self._require_known_tools(request.tool_policy.allowed_tools)
        availability = {
            "read": ("view", "glob", "grep"),
            "write": ("apply_patch", "create", "edit"),
            "shell": (
                "bash",
                "powershell",
                "list_bash",
                "list_powershell",
                "read_bash",
                "read_powershell",
                "stop_bash",
                "stop_powershell",
                "write_bash",
                "write_powershell",
            ),
            "python": ("bash", "powershell"),
            "git": ("bash", "powershell"),
        }
        permissions = {
            "read": ("read",),
            "write": ("write",),
            "shell": ("shell",),
            "python": ("shell(python:*)",),
            "git": ("shell(git:*)",),
        }
        available_tools = sorted(
            {
                native
                for tool in request.tool_policy.allowed_tools
                for native in availability[tool]
            }
        )
        allowed_tools = sorted(
            {
                native
                for tool in request.tool_policy.allowed_tools
                for native in permissions[tool]
            }
        )
        log_root = self._output_root / "logs"
        log_root.mkdir(parents=True, exist_ok=True)
        available = ",".join(available_tools)
        allowed = ",".join(allowed_tools)
        argv = (
            *self._command,
            "-C",
            str(request.workspace.root),
            f"--prompt={request.instructions}",
            f"--model={self._model(request.model_class)}",
            "--session-id",
            str(uuid4()),
            f"--available-tools={available}",
            f"--allow-tool={allowed}",
            "--no-ask-user",
            "--no-auto-update",
            "--no-custom-instructions",
            "--no-experimental",
            "--no-remote",
            "--no-remote-export",
            "--disable-builtin-mcps",
            "--disallow-temp-dir",
            "--no-bash-env",
            "--no-color",
            "--stream=off",
            "--output-format=json",
            f"--log-dir={log_root}",
        )
        return argv, None
