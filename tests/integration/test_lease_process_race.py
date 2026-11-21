from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

import pytest

from autobuild.adapters import FilesystemLeaseAdapter
from autobuild.adapters import lease as lease_module
from autobuild.domain import AdapterError, LeaseRequest, LeaseSurface, SurfaceKind

CHILD = Path(__file__).with_name("lease_race_child.py")
_POLL_SECONDS = 0.02


def _wait_for(path: Path, limit: float) -> None:
    deadline = time.monotonic() + limit
    while not path.exists():
        if time.monotonic() >= deadline:
            pytest.fail(f"{path.name} did not appear within {limit} seconds")
        time.sleep(_POLL_SECONDS)


def _digest(surface: Path) -> str:
    return hashlib.sha256(
        os.path.normcase(str(surface.resolve(strict=False))).encode("utf-8")
    ).hexdigest()


def _child(*arguments: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen([sys.executable, str(CHILD), *arguments])


def _stop(children: list[subprocess.Popen[bytes]]) -> None:
    for child in children:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)


def test_two_processes_racing_for_one_surface_see_exactly_one_win(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    signals = tmp_path / "signals"
    signals.mkdir()
    release = signals / "release"
    finish = signals / "finish"
    surfaces = [tmp_path / f"repo-{index}" / ".ergon" for index in range(5)]
    children: list[subprocess.Popen[bytes]] = []
    results: dict[int, list[Path]] = {}
    try:
        for index, surface in enumerate(surfaces):
            results[index] = []
            for side, rival in (("a", "b"), ("b", "a")):
                result = signals / f"result-{index}-{side}.json"
                results[index].append(result)
                children.append(
                    _child(
                        "race",
                        "--scratch", str(scratch),
                        "--surface", str(surface),
                        "--marker", str(signals / f"read-{index}-{side}"),
                        "--rival", str(signals / f"read-{index}-{rival}"),
                        "--release", str(release),
                        "--result", str(result),
                        "--finish", str(finish),
                        "--campaign", f"campaign-{index}-{side}",
                    )
                )
        release.write_text("go", encoding="utf-8")
        for paths in results.values():
            for path in paths:
                _wait_for(path, 30.0)
        finish.write_text("done", encoding="utf-8")
        for child in children:
            assert child.wait(timeout=30) == 0
    finally:
        finish.write_text("done", encoding="utf-8")
        _stop(children)

    for index, surface in enumerate(surfaces):
        outcomes = [json.loads(path.read_text(encoding="utf-8")) for path in results[index]]
        assert all(outcome["wrapped_read"] for outcome in outcomes)
        assert sorted(outcome["outcome"] for outcome in outcomes) == ["granted", "held"]
        winner = next(outcome for outcome in outcomes if outcome["outcome"] == "granted")
        record = json.loads(
            (scratch / "leases" / f"{_digest(surface)}.json").read_text(encoding="utf-8")
        )
        assert record["process_id"] == winner["process_id"]


def _hold_lock(tmp_path: Path, surface: Path) -> tuple[subprocess.Popen[bytes], Path]:
    signals = tmp_path / "signals"
    signals.mkdir()
    holding = signals / "holding"
    finish = signals / "finish"
    child = _child(
        "hold",
        "--scratch", str(tmp_path / "scratch"),
        "--surface", str(surface),
        "--marker", str(holding),
        "--finish", str(finish),
    )
    _wait_for(holding, 30.0)
    return child, finish


def test_a_killed_lock_holder_never_blocks_the_surface(tmp_path: Path) -> None:
    surface = tmp_path / "repo" / ".ergon"
    child, _ = _hold_lock(tmp_path, surface)
    try:
        child.kill()
        child.wait(timeout=10)
    finally:
        _stop([child])

    adapter = FilesystemLeaseAdapter(tmp_path / "scratch", lock_wait_seconds=5.0)
    grant = adapter.acquire(LeaseRequest(LeaseSurface(surface, SurfaceKind.TRACKER), "campaign-b"))

    assert grant.record.campaign_id == "campaign-b"
    assert (tmp_path / "scratch" / "leases" / f"{_digest(surface)}.lock").is_file()


def test_acquire_refuses_when_the_lock_stays_held_past_the_bound(tmp_path: Path) -> None:
    surface = tmp_path / "repo" / ".ergon"
    child, finish = _hold_lock(tmp_path, surface)
    try:
        adapter = FilesystemLeaseAdapter(
            tmp_path / "scratch", lock_wait_seconds=0.5, lock_poll_seconds=0.05
        )
        with pytest.raises(AdapterError) as excinfo:
            adapter.acquire(
                LeaseRequest(LeaseSurface(surface, SurfaceKind.TRACKER), "campaign-b")
            )
        assert f"{_digest(surface)}.lock" in str(excinfo.value)
        assert not (tmp_path / "scratch" / "leases" / f"{_digest(surface)}.json").exists()
    finally:
        _finish_and_stop(child, finish)


def _finish_and_stop(child: subprocess.Popen[bytes], finish: Path) -> None:
    try:
        finish.write_text("done", encoding="utf-8")
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    finally:
        _stop([child])


def _lease_file(tmp_path: Path, surface: Path) -> Path:
    return tmp_path / "scratch" / "leases" / f"{_digest(surface)}.json"


def test_renew_refuses_while_another_process_holds_the_lock(tmp_path: Path) -> None:
    surface = tmp_path / "repo" / ".ergon"
    adapter = FilesystemLeaseAdapter(
        tmp_path / "scratch", lock_wait_seconds=0.5, lock_poll_seconds=0.05
    )
    grant = adapter.acquire(
        LeaseRequest(LeaseSurface(surface, SurfaceKind.TRACKER), "campaign-a")
    )
    before = json.loads(_lease_file(tmp_path, surface).read_text(encoding="utf-8"))
    child, finish = _hold_lock(tmp_path, surface)
    try:
        with pytest.raises(AdapterError) as excinfo:
            adapter.renew(grant)
        assert f"{_digest(surface)}.lock" in str(excinfo.value)
        after = json.loads(_lease_file(tmp_path, surface).read_text(encoding="utf-8"))
        assert after["heartbeat_at"] == before["heartbeat_at"]
    finally:
        _finish_and_stop(child, finish)


def test_release_refuses_while_another_process_holds_the_lock(tmp_path: Path) -> None:
    surface = tmp_path / "repo" / ".ergon"
    adapter = FilesystemLeaseAdapter(
        tmp_path / "scratch", lock_wait_seconds=0.5, lock_poll_seconds=0.05
    )
    grant = adapter.acquire(
        LeaseRequest(LeaseSurface(surface, SurfaceKind.TRACKER), "campaign-a")
    )
    child, finish = _hold_lock(tmp_path, surface)
    try:
        with pytest.raises(AdapterError) as excinfo:
            adapter.release(grant)
        assert f"{_digest(surface)}.lock" in str(excinfo.value)
        assert _lease_file(tmp_path, surface).is_file()
    finally:
        _finish_and_stop(child, finish)


def test_the_lock_wait_is_bounded_even_when_the_injected_clock_is_frozen(
    tmp_path: Path,
) -> None:
    surface = tmp_path / "repo" / ".ergon"
    frozen = datetime(2026, 11, 19, tzinfo=UTC)
    child, finish = _hold_lock(tmp_path, surface)
    errors: list[BaseException] = []
    try:
        adapter = FilesystemLeaseAdapter(
            tmp_path / "scratch",
            now=lambda: frozen,
            lock_wait_seconds=0.5,
            lock_poll_seconds=0.05,
        )

        def attempt() -> None:
            try:
                adapter.acquire(
                    LeaseRequest(LeaseSurface(surface, SurfaceKind.TRACKER), "campaign-b")
                )
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        worker = threading.Thread(target=attempt, daemon=True)
        worker.start()
        worker.join(timeout=10)
        assert not worker.is_alive(), "the lock wait did not end within 10 seconds"
        assert len(errors) == 1 and isinstance(errors[0], AdapterError)
        assert f"{_digest(surface)}.lock" in str(errors[0])
    finally:
        _finish_and_stop(child, finish)


def test_a_contended_surface_never_delays_a_step_on_another_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    surface_a = tmp_path / "repo-a" / ".ergon"
    surface_b = tmp_path / "repo-b" / ".ergon"
    child, finish = _hold_lock(tmp_path, surface_a)
    contended = threading.Event()
    original = lease_module._try_lock

    def observed(descriptor: int) -> bool:
        taken = original(descriptor)
        if not taken:
            contended.set()
        return taken

    monkeypatch.setattr(lease_module, "_try_lock", observed)
    adapter = FilesystemLeaseAdapter(
        tmp_path / "scratch", lock_wait_seconds=60.0, lock_poll_seconds=0.05
    )
    waiter = threading.Thread(
        target=lambda: _swallow(
            lambda: adapter.acquire(
                LeaseRequest(LeaseSurface(surface_a, SurfaceKind.TRACKER), "campaign-a")
            )
        ),
        daemon=True,
    )
    released: list[bool] = []

    def steps_on_b() -> None:
        other = LeaseSurface(surface_b, SurfaceKind.WORKTREE)
        grant = adapter.acquire(LeaseRequest(other, "campaign-b"))
        grant = adapter.renew(grant)
        released.append(adapter.release(grant).released)

    stepper = threading.Thread(target=lambda: _swallow(steps_on_b), daemon=True)
    try:
        waiter.start()
        assert contended.wait(timeout=10), "surface A was never contended"
        stepper.start()
        stepper.join(timeout=30)
        assert not stepper.is_alive(), "a contended surface A delayed a step on surface B"
        assert released == [True]
        assert waiter.is_alive()
    finally:
        _finish_and_stop(child, finish)
        waiter.join(timeout=10)
        stepper.join(timeout=70)


def _swallow(step: Callable[[], object]) -> None:
    try:
        step()
    except AdapterError:
        pass
