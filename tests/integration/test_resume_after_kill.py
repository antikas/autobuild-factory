"""Automatic resume after the campaign process is really killed.

A child process runs the production ``run`` command against a BACKLOG.md
repository with a real remote, the real tracker, workspace, run-record and
lease adapters and a real validator. Only the harness CLI is a fake script. The
fake seat the test holds for writes a marker naming its own process id and
waits; the test kills the campaign process, then the orphaned seat, and runs the
same command again to completion."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from autobuild.adapters.lease import process_alive

DEADLINE = 180.0
TEARDOWN_WAIT = 5.0
# A helper test waits this long for its own child to exit. The test asserts
# that the child stopped, not how fast: on a loaded host a killed or starting
# interpreter can take longer than TEARDOWN_WAIT to be scheduled and reaped.
CHILD_EXIT_WAIT = DEADLINE
STDERR_TAIL = 4000
POLL = 0.05
ITEM = "PRF-001"
RESULT = "portable autobuild proof\n"

FAKE_HARNESS = """\
import json
import os
import sys
import time
from pathlib import Path

CONTROL = Path({control!r})
vendor = sys.argv[1]
args = sys.argv[2:]

if args == ["--version"] or args == ["version"]:
    print(f"{{vendor}} fake 1.0")
    raise SystemExit(0)
if args[:2] == ["auth", "status"]:
    print(json.dumps({{"loggedIn": True}}))
    raise SystemExit(0)

prompt = sys.stdin.read()
seat = "reviewer" if "Review the actual brief" in prompt else "builder"
run = (CONTROL / "run").read_text(encoding="utf-8").strip()
entry = {{"run": run, "seat": seat, "pid": os.getpid(), "cwd": os.getcwd()}}
with (CONTROL / "seats.jsonl").open("a", encoding="utf-8") as log:
    log.write(json.dumps(entry) + "\\n")

hold = CONTROL / "hold"
if hold.exists() and hold.read_text(encoding="utf-8").strip() == seat:
    if seat == "builder":
        Path("proof_result.txt").write_text("partial work\\n", encoding="utf-8")
    pending = CONTROL / "held.json.tmp"
    pending.write_text(json.dumps(entry), encoding="utf-8")
    pending.replace(CONTROL / "held.json")
    deadline = time.monotonic() + 120
    while not (CONTROL / "release").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    raise SystemExit(5)

if seat == "reviewer":
    payload = {{"decision": "pass", "findings": [], "evidence_ref": "fake-review",
               "inspected": [], "acceptance": []}}
else:
    Path("proof_result.txt").write_text({result!r}, encoding="utf-8")
    payload = {{"summary": "fake harness completed the build", "report_ref": "fake-build"}}
print(json.dumps({{
    "result": "complete",
    "structured_output": payload,
    "usage": {{"input_tokens": 9, "output_tokens": 3}},
}}))
"""


def _run(*argv: str, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        list(argv), cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=False
    )
    if completed.returncode != 0:
        raise RuntimeError(f"{' '.join(argv)} failed: {completed.stderr.strip()}")
    return completed.stdout.strip()


def _git(root: Path, *args: str) -> str:
    return _run("git", "-C", str(root), *args)


def _fixture(root: Path) -> tuple[Path, Path]:
    remote = root / "remote.git"
    repository = root / "repository"
    _run("git", "init", "--bare", str(remote))
    _run("git", "init", "-b", "main", str(repository))
    _git(repository, "config", "user.name", "AutoBuild Kill")
    _git(repository, "config", "user.email", "autobuild@example.invalid")
    (repository / "docs").mkdir()
    (repository / "docs" / "brief.md").write_text(
        "# Approved kill brief\n\n"
        "Create `proof_result.txt` at the repository root with exactly this line:\n\n"
        f"```text\n{RESULT}```\n",
        encoding="utf-8",
    )
    (repository / "test_proof.py").write_text(
        "from pathlib import Path\n"
        "import unittest\n\n"
        "class ProofTest(unittest.TestCase):\n"
        "    def test_result(self):\n"
        "        proof = Path('proof_result.txt')\n"
        "        if proof.exists():\n"
        f"            self.assertEqual(proof.read_text(encoding='utf-8'), {RESULT!r})\n",
        encoding="utf-8",
    )
    (repository / "BACKLOG.md").write_text(
        "# Backlog\n\n"
        "| Item | Title | Status | Brief |\n"
        "|---|---|---|---|\n"
        f"| {ITEM} | Create the kill proof result | Ready | docs/brief.md |\n",
        encoding="utf-8",
    )
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "ADDED: initialise kill proof")
    _git(repository, "remote", "add", "origin", str(remote))
    _git(repository, "push", "-u", "origin", "main")
    _git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    return repository, remote


def _harness_script(tmp_path: Path) -> Path:
    return tmp_path / "fake harness.py"


def _profile(tmp_path: Path, control: Path) -> Path:
    script = _harness_script(tmp_path)
    script.write_text(FAKE_HARNESS.format(control=str(control), result=RESULT), encoding="utf-8")
    command = [sys.executable, str(script), "claude-code"]
    validator = [sys.executable, "-m", "unittest", "-q", "test_proof.py"]
    profile = tmp_path / "kill-autobuild.toml"
    profile.write_text(
        "[run]\n"
        'harness = "claude-code"\n'
        "max_items = 1\n\n"
        "[models]\n"
        'builder = "builder-model"\n'
        'reviewer = "reviewer-model"\n\n'
        "[validator]\n"
        'id = "proof-tests"\n'
        f"argv = {json.dumps(validator)}\n\n"
        "[harness]\n"
        f"command = {json.dumps(command)}\n\n"
        "[preflight]\n"
        'accepted_environment = ["NODE_EXTRA_CA_CERTS", "SSLKEYLOGFILE", "HTTP_PROXY", '
        '"HTTPS_PROXY", "ALL_PROXY"]\n',
        encoding="utf-8",
    )
    return profile


class _Children:
    """Every process the test started or must stop, killed at teardown.

    ``pids`` holds only pids of unreaped children of the test process itself,
    which cannot be reused while the test holds them; they are signalled
    unchecked. Any other process, such as an orphaned seat, is stopped only
    through ``seats_log`` and ``harness``, which name the fake harness seat log
    and script: a recorded seat is signalled only while it runs and its command
    line names that script. ``kill_all`` attempts every kill and returns the
    errors it caught instead of raising, so a failure of the test body is never
    replaced by a teardown error."""

    def __init__(self, seats_log: Path | None = None, harness: Path | None = None) -> None:
        self.processes: list[subprocess.Popen[bytes]] = []
        self.pids: list[int] = []
        self.seats_log = seats_log
        self.harness = harness

    def kill_all(self) -> list[str]:
        errors: list[str] = []
        for process in self.processes:
            try:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=TEARDOWN_WAIT)
            except Exception as exc:
                errors.append(f"process {getattr(process, 'pid', '?')}: {exc!r}")
        for pid in self.pids:
            _kill_pid(pid)
        try:
            errors.extend(self._stop_seats())
        except Exception as exc:
            errors.append(f"seats: {exc!r}")
        return errors

    def _stop_seats(self) -> list[str]:
        if self.seats_log is None or self.harness is None or not self.seats_log.exists():
            return []
        errors: list[str] = []
        pids: set[int] = set()
        for line in self.seats_log.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            pid = _seat_pid(line)
            if pid is None:
                # A seat killed mid-write leaves such a line; the seats after it
                # must still be stopped.
                errors.append(f"seats: skipped malformed line {line!r}")
            else:
                pids.add(pid)
        harness = str(self.harness)
        for pid in sorted(pids):
            if _gone(pid):
                continue
            arguments = _cmdline(pid)
            if arguments is None:
                # Without a readable command line the pid may already belong
                # to an unrelated process, so it is never signalled.
                _wait_until(lambda: _gone(pid), TEARDOWN_WAIT)
            elif harness in arguments:
                _kill_pid(pid)
        return errors


def _seat_pid(line: str) -> int | None:
    try:
        entry = json.loads(line)
    except ValueError:
        return None
    pid = entry.get("pid") if isinstance(entry, dict) else None
    if type(pid) is not int or pid <= 0:
        return None
    return pid


def _cmdline(pid: int) -> list[str] | None:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    if not raw:
        return None
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def _wait_until(condition, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(POLL)
    return True


def _kill_pid(pid: int) -> None:
    try:
        os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
    except OSError:
        pass


def _gone(pid: int) -> bool:
    """True once ``pid`` has stopped running. A killed process another parent
    has not reaped yet is a zombie that can no longer write anything."""

    if not process_alive(pid):
        return True
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return False
    return stat.rsplit(")", 1)[-1].split()[0] == "Z"


@dataclass(frozen=True)
class _Campaign:
    """A campaign process together with the log its stderr is written to."""

    process: subprocess.Popen[bytes]
    stderr_log: Path

    def stderr_tail(self) -> str:
        if not self.stderr_log.exists():
            return ""
        return self.stderr_log.read_text(encoding="utf-8", errors="replace")[-STDERR_TAIL:]


def _wait_for(campaign: _Campaign, condition, what: str) -> None:
    deadline = time.monotonic() + DEADLINE
    while not condition():
        if campaign.process.poll() is not None:
            code = campaign.process.returncode
            raise AssertionError(
                f"campaign exited {code} before {what}; stderr tail:\n{campaign.stderr_tail()}"
            )
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(POLL)


def _start_campaign(
    children: _Children, repository: Path, profile: Path, scratch: Path, logs: Path, name: str
) -> _Campaign:
    environment = dict(os.environ)
    source = str(Path(__file__).resolve().parents[2] / "src")
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (source, environment.get("PYTHONPATH", "")) if part
    )
    with (logs / f"{name}.out").open("wb") as stdout, (logs / f"{name}.err").open("wb") as stderr:
        process = subprocess.Popen(
            [
                sys.executable, "-m", "autobuild", "run",
                "--repository", str(repository),
                "--profile", str(profile),
                "--scratch-root", str(scratch),
                "--allow-delivery",
                "--output", str(logs / f"{name}.json"),
            ],
            stdout=stdout,
            stderr=stderr,
            env=environment,
        )
    children.processes.append(process)
    return _Campaign(process, logs / f"{name}.err")


def _run_with_teardown(children: _Children, control: Path, body: Callable[[], None]) -> None:
    """Run ``body``, then always release held seats and kill every child.

    A teardown error fails a passing body; a failing body raises its own
    failure, never the teardown's."""

    body_failed = True
    try:
        body()
        body_failed = False
    finally:
        (control / "release").write_text("release", encoding="utf-8")
        teardown_errors = children.kill_all()
        if not body_failed and teardown_errors:
            pytest.fail(f"teardown failed: {teardown_errors}")


def _runs(scratch: Path) -> dict[str, Path]:
    root = scratch / "runs"
    if not root.is_dir():
        return {}
    return {path.name: path for path in root.iterdir() if (path / "manifest.json").is_file()}


def _events(run_dir: Path) -> list[dict]:
    text = (run_dir / "events.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _campaign_id(run_dir: Path) -> str:
    return json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))["campaign_id"]


def _marker(run_dir: Path) -> dict:
    return json.loads((run_dir / "evidence" / f"{ITEM}-phase.json").read_text(encoding="utf-8"))


def _leases(scratch: Path) -> list[dict]:
    root = scratch / "leases"
    return [json.loads(path.read_text(encoding="utf-8")) for path in root.glob("*.json")]


def _registered(repository: Path) -> set[Path]:
    listing = _git(repository, "worktree", "list", "--porcelain")
    return {
        Path(line.removeprefix("worktree ").strip()).resolve()
        for line in listing.splitlines()
        if line.startswith("worktree ")
    }


def _seats(control: Path, run: str) -> list[dict]:
    log = control / "seats.jsonl"
    if not log.exists():
        return []
    entries = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]
    return [entry for entry in entries if entry["run"] == run]


@pytest.mark.parametrize(
    ("held_seat", "marker_state"), [("reviewer", "validated"), ("builder", "isolated")]
)
def test_a_killed_campaign_is_resumed_by_the_next_run_and_delivered(
    tmp_path: Path, held_seat: str, marker_state: str
) -> None:
    repository, remote = _fixture(tmp_path)
    control = tmp_path / "control"
    control.mkdir()
    logs = tmp_path / "logs"
    logs.mkdir()
    scratch = tmp_path / "scratch"
    profile = _profile(tmp_path, control)
    (control / "run").write_text("first", encoding="utf-8")
    (control / "hold").write_text(held_seat, encoding="utf-8")
    held = control / "held.json"
    children = _Children(control / "seats.jsonl", _harness_script(tmp_path))

    def body() -> None:
        # --- The first run, killed while the held seat runs. ---
        first = _start_campaign(children, repository, profile, scratch, logs, "first")
        _wait_for(first, held.exists, f"the {held_seat} seat to start")
        seat_pid = json.loads(held.read_text(encoding="utf-8"))["pid"]
        first.process.kill()
        first.process.wait(timeout=DEADLINE)
        _kill_pid(seat_pid)
        if not _wait_until(lambda: _gone(seat_pid), DEADLINE):
            raise AssertionError("timed out waiting for the orphaned seat to stop")

        if os.name != "nt":
            assert first.process.returncode == -signal.SIGKILL
        else:
            assert first.process.returncode != 0
        (first_id, first_dir), = _runs(scratch).items()
        marker = _marker(first_dir)
        assert marker["state"] == marker_state
        worktree = Path(marker["worktree_root"]).resolve()
        holders = {
            (lease["surface_kind"], Path(lease["surface_path"]).resolve()): lease["process_id"]
            for lease in _leases(scratch)
        }
        assert holders[("worktree", worktree)] == first.process.pid
        trackers = [(path, pid) for (kind, path), pid in holders.items() if kind == "tracker"]
        assert trackers == [(repository.resolve(), first.process.pid)]
        assert worktree in _registered(repository)
        assert not any(e["event_type"] == "campaign.completed" for e in _events(first_dir))
        assert f"| {ITEM} | Create the kill proof result | Claimed by " in (
            repository / "BACKLOG.md"
        ).read_text(encoding="utf-8")
        if held_seat == "builder":
            assert (worktree / "proof_result.txt").read_text(encoding="utf-8") == "partial work\n"

        # --- The second run, which must resume the killed item. ---
        (control / "hold").unlink()
        (control / "run").write_text("second", encoding="utf-8")
        second = _start_campaign(children, repository, profile, scratch, logs, "second")
        second.process.wait(timeout=DEADLINE)

        assert second.process.returncode == 0, second.stderr_tail()
        (second_dir,) = [path for name, path in _runs(scratch).items() if name != first_id]
        events = _events(second_dir)
        reclaimed = [e for e in events if e["event_type"] == "campaign.lease_reclaimed"]
        previous = [e["payload"]["previous_holder"]["process_id"] for e in reclaimed]
        assert previous == [first.process.pid]
        (resumed,) = [e for e in events if e["event_type"] == "item.resumed"]
        assert resumed["payload"]["prior_run_id"] == first_id
        assert resumed["payload"]["marker_state"] == marker_state
        assert _campaign_id(second_dir) != _campaign_id(first_dir)
        assert _git(remote, "show", "main:proof_result.txt") + "\n" == RESULT
        assert f"| {ITEM} | Create the kill proof result | Done (" in (
            repository / "BACKLOG.md"
        ).read_text(encoding="utf-8")
        assert worktree not in _registered(repository)
        builders = [entry for entry in _seats(control, "second") if entry["seat"] == "builder"]
        if held_seat == "reviewer":
            assert builders == []
        else:
            assert [Path(entry["cwd"]).resolve() for entry in builders] == [worktree]
        assert children.pids == []

    _run_with_teardown(children, control, body)


# --- Test helpers -------------------------------------------------------------

SLEEPER = "import time\ntime.sleep(60)\n"
needs_proc = pytest.mark.skipif(not Path("/proc/self/cmdline").exists(), reason="needs /proc")


class _StuckProcess:
    pid = 101

    def poll(self) -> int | None:
        return None

    def kill(self) -> None:
        pass

    def wait(self, timeout: float | None = None) -> int:
        raise subprocess.TimeoutExpired("stuck", timeout)


class _RecordingProcess:
    pid = 102

    def __init__(self) -> None:
        self.killed = False

    def poll(self) -> int | None:
        return -9 if self.killed else None

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        return -9


def _sleeper(script: Path) -> subprocess.Popen[bytes]:
    script.write_text(SLEEPER, encoding="utf-8")
    return subprocess.Popen([sys.executable, str(script)])


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.kill()
    process.wait(timeout=CHILD_EXIT_WAIT)


def test_kill_all_attempts_every_kill_and_returns_the_errors(tmp_path: Path) -> None:
    child = _sleeper(tmp_path / "sleeper.py")
    try:
        stuck, recording = _StuckProcess(), _RecordingProcess()
        children = _Children()
        children.processes.extend([stuck, recording])  # type: ignore[list-item]
        children.pids.append(child.pid)

        errors = children.kill_all()

        assert recording.killed
        child.wait(timeout=CHILD_EXIT_WAIT)
        assert len(errors) == 1
        assert "TimeoutExpired" in errors[0]
    finally:
        _stop(child)


@needs_proc
def test_kill_all_stops_only_recorded_seats_running_the_fake_harness(tmp_path: Path) -> None:
    seat = _sleeper(_harness_script(tmp_path))
    other = _sleeper(tmp_path / "other.py")
    try:
        log = tmp_path / "seats.jsonl"
        log.write_text(
            "".join(json.dumps({"run": "x", "pid": p.pid}) + "\n" for p in (seat, other)),
            encoding="utf-8",
        )
        children = _Children(log, _harness_script(tmp_path))

        assert children.kill_all() == []

        seat.wait(timeout=CHILD_EXIT_WAIT)
        assert other.poll() is None
    finally:
        _stop(seat)
        _stop(other)


@needs_proc
def test_kill_all_never_signals_a_seat_whose_command_line_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seat = _sleeper(_harness_script(tmp_path))
    try:
        log = tmp_path / "seats.jsonl"
        log.write_text(json.dumps({"run": "x", "pid": seat.pid}) + "\n", encoding="utf-8")
        children = _Children(log, _harness_script(tmp_path))
        module = sys.modules[__name__]
        readable = module._cmdline
        monkeypatch.setattr(
            module, "_cmdline", lambda pid: None if pid == seat.pid else readable(pid)
        )
        monkeypatch.setattr(module, "TEARDOWN_WAIT", 0.5)

        started = time.monotonic()
        children.kill_all()
        elapsed = time.monotonic() - started

        # The sweep must not wait without bound on a seat it cannot identify.
        # The bound is loose so that host load alone cannot fail it.
        assert elapsed < 30.0
        # A signal is delivered asynchronously, so the seat must stay running
        # for a while, not just at the first poll.
        assert not _wait_until(lambda: seat.poll() is not None, 0.5)
    finally:
        _stop(seat)


@needs_proc
@pytest.mark.parametrize(
    "malformed", ['{"run": "x", "pid"', "[1]", '{"run": "x"}', '{"pid": "12"}', '{"pid": true}']
)
def test_kill_all_skips_a_malformed_seat_line_and_stops_the_seats_after_it(
    tmp_path: Path, malformed: str
) -> None:
    seat = _sleeper(_harness_script(tmp_path))
    try:
        log = tmp_path / "seats.jsonl"
        log.write_text(
            malformed + "\n" + json.dumps({"run": "x", "pid": seat.pid}) + "\n", encoding="utf-8"
        )
        children = _Children(log, _harness_script(tmp_path))

        errors = children.kill_all()

        seat.wait(timeout=CHILD_EXIT_WAIT)
        assert any(malformed in error for error in errors), errors
    finally:
        _stop(seat)


class _BodyFailed(Exception):
    pass


def _passes() -> None:
    pass


def _fails() -> None:
    raise _BodyFailed("the body's own failure")


def test_run_with_teardown_fails_a_passing_body_when_teardown_errors(tmp_path: Path) -> None:
    children = _Children()
    children.processes.append(_StuckProcess())  # type: ignore[arg-type]

    with pytest.raises(pytest.fail.Exception) as raised:
        _run_with_teardown(children, tmp_path, _passes)

    assert "TimeoutExpired" in str(raised.value)
    assert (tmp_path / "release").exists()


def test_run_with_teardown_returns_after_a_passing_body_and_a_clean_teardown(
    tmp_path: Path,
) -> None:
    recording = _RecordingProcess()
    children = _Children()
    children.processes.append(recording)  # type: ignore[arg-type]

    _run_with_teardown(children, tmp_path, _passes)

    assert recording.killed


def test_run_with_teardown_raises_the_body_failure_over_teardown_errors(tmp_path: Path) -> None:
    recording = _RecordingProcess()
    children = _Children()
    children.processes.extend([_StuckProcess(), recording])  # type: ignore[list-item]

    with pytest.raises(_BodyFailed):
        _run_with_teardown(children, tmp_path, _fails)

    assert recording.killed


def test_wait_for_an_early_exit_names_the_campaign_stderr(tmp_path: Path) -> None:
    log = tmp_path / "early.err"
    with log.open("wb") as stderr:
        process = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stderr.write('marker-early-death'); sys.exit(3)"],
            stderr=stderr,
        )
    try:
        process.wait(timeout=CHILD_EXIT_WAIT)
        campaign = _Campaign(process, log)
        with pytest.raises(AssertionError) as raised:
            _wait_for(campaign, lambda: False, "the marker")
        assert "exited 3" in str(raised.value)
        assert "marker-early-death" in str(raised.value)
    finally:
        _stop(process)


def test_wait_for_names_the_stderr_of_a_campaign_started_and_exiting_early(
    tmp_path: Path,
) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    missing = tmp_path / "missing-repository"
    children = _Children()
    try:
        campaign = _start_campaign(
            children, missing, tmp_path / "absent.toml", tmp_path / "scratch", logs, "early"
        )
        with pytest.raises(AssertionError) as raised:
            _wait_for(campaign, lambda: False, "the marker")
        message = str(raised.value)
        assert "exited 2" in message
        assert "AutoBuild failed before completion" in message
        assert missing.name in message
    finally:
        assert children.kill_all() == []
