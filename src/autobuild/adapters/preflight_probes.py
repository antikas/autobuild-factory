"""Concrete network, environment and filesystem mechanisms for the preflight doctor.

These are the only place the doctor's reachability, environment and filesystem
checks touch the operating system. The enforcement layer holds the decisions; each
method here performs one operation with the standard library and lets its native
error surface for the doctor to name.
"""

from __future__ import annotations

import shutil
import socket
import ssl
from collections.abc import Mapping
from pathlib import Path
from uuid import uuid4


class SocketNetworkProbe:
    def __init__(self, timeout_seconds: float = 10.0) -> None:
        self._timeout = timeout_seconds

    def resolve(self, host: str) -> None:
        socket.getaddrinfo(host, None)

    def tls_handshake(self, host: str, port: int) -> None:
        context = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=self._timeout) as raw:
            with context.wrap_socket(raw, server_hostname=host) as secure:
                secure.getpeercert()

    def closed_local_port(self) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]


class ProcessEnvironmentProbe:
    def __init__(self, environment: Mapping[str, str]) -> None:
        self._environment = dict(environment)

    def value(self, name: str) -> str | None:
        return self._environment.get(name)

    def resolve_executable(self, name: str) -> str | None:
        found = shutil.which(name)
        if found is not None:
            return found
        candidate = Path(name)
        return str(candidate) if candidate.is_file() else None


class LocalFilesystemProbe:
    def ensure_directory(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)

    def probe_write(self, path: Path) -> None:
        marker = path / f".preflight-{uuid4().hex}.tmp"
        marker.write_text("preflight", encoding="utf-8")
        marker.unlink()

    def foreign_locks(self, path: Path) -> tuple[str, ...]:
        if not path.is_dir():
            return ()
        return tuple(str(entry) for entry in sorted(path.glob("*.lock")))

    def is_file(self, path: Path) -> bool:
        return path.is_file()

    def size(self, path: Path) -> int:
        return path.stat().st_size
