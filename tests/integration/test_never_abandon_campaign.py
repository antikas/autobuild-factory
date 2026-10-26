"""No single error abandons a campaign: real Git, real Pinax, a bare origin.

The replay reproduces a live failure. Four items run at a concurrency of
three; every reviewer reports a non-blocking finding, so each accepted item
records a follow-up proposal on the primary checkout. A commit hook refuses
the first follow-up ``pinax note`` commit once, so Pinax exits 7 with its event
appended and not committed, while the other items still run. The campaign
must recover the leftover, keep the primary clean and deliver every item.

The park tests prove that an item whose branch already holds its product
commit parks from the primary checkout after a delivery error."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest
from scanner_retry import run_retrying
from test_serial_delivery import (
    ValidatorRuns,
    default_adapters,
    git,
    item_status,
    pinax_origin,
    run_item,
    scripted_harness,
)

from autobuild.adapters import PinaxTrackerAdapter
from autobuild.cli import main as campaign_main
from autobuild.domain import CampaignRef, ItemDisposition, ItemState, Proposal, TrackerStop
from autobuild.domain.models import HoldCondition

pytestmark = pytest.mark.usefixtures("self_committing_pinax")

ITEMS = 4
CONCURRENCY = 3

FAKE_HARNESS = """\
import json
import os
import re
import sys
import time
from pathlib import Path

args = sys.argv[2:]
if args == ["--version"]:
    print("claude-code fake 1.0")
    raise SystemExit(0)
if args[:2] == ["auth", "status"]:
    print(json.dumps({"loggedIn": True}))
    raise SystemExit(0)

prompt = sys.stdin.read()
if "Review the actual brief" in prompt:
    findings = [{
        "code": "follow-up-wanted",
        "consequence": "a later item should tidy this",
        "evidence_ref": "fake-review",
        "blocking": False,
        "specialist_boundary": None,
    }]
    payload = {"decision": "pass", "findings": findings, "evidence_ref": "fake-review"}
else:
    match = re.search(r"WRITE-TARGET: (out-[0-9]+[.]txt)", prompt)
    if match is None:
        print("no write target in the brief", file=sys.stderr)
        raise SystemExit(3)
    target = match.group(1)
    Path(target).write_text(f"built {target}\\n", encoding="utf-8")
    rendezvous = Path(os.environ["AUTOBUILD_TEST_RENDEZVOUS"])
    (rendezvous / f"{target}.started").write_text(os.getcwd(), encoding="utf-8")
    deadline = time.monotonic() + 60
    while len(list(rendezvous.glob("*.started"))) < int(os.environ["AUTOBUILD_TEST_ITEMS"]):
        if time.monotonic() > deadline:
            break
        time.sleep(0.05)
    payload = {"summary": "fake harness built " + target, "report_ref": "fake-build"}

print(json.dumps({
    "result": "complete",
    "structured_output": payload,
    "usage": {"input_tokens": 9, "output_tokens": 3},
}))
"""

# Refuses the first follow-up note commit, once, as a transient object-write
# refusal did in the live run. Pinax runs commits with hooks.
REFUSE_ONCE_HOOK = """#!/bin/sh
marker="{marker}"
if [ -f "$marker" ] && grep -q "note.added" "$1"; then
  rm -f "$marker"
  echo "error: refused once by the test hook" >&2
  exit 1
fi
exit 0
"""


def _run(*argv: str, cwd: Path | None = None) -> str:
    completed = run_retrying(argv, cwd=cwd)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"{' '.join(argv)} failed: {detail}")
    return completed.stdout.strip()


def _pinax(root: Path, *args: str) -> str:
    return _run("pinax", "--root", str(root), *args, cwd=root)


def _fixture(root: Path) -> tuple[Path, Path]:
    remote = root / "remote.git"
    repository = root / "repository"
    _run("git", "init", "--bare", str(remote))
    _run("git", "init", "-b", "main", str(repository))
    git(repository, "config", "user.name", "AutoBuild Replay Proof")
    git(repository, "config", "user.email", "autobuild-replay@example.invalid")
    (repository / "README.md").write_text("# Replay proof\n", encoding="utf-8")
    (repository / "docs").mkdir()
    _pinax(repository, "init", "--actor", "coordinator@proof")
    for number in range(1, ITEMS + 1):
        brief = f"docs/brief-{number}.md"
        (repository / brief).write_text(
            f"# Brief {number}\n\nWRITE-TARGET: out-{number}.txt\n\n"
            f"## Declared paths\n\n`out-{number}.txt`\n",
            encoding="utf-8",
        )
        created = json.loads(
            _pinax(
                repository, "add", "--title", f"Build out-{number}", "--prefix", "rpl",
                "--actor", "coordinator@proof", "--json",
            )
        )
        item_id = str(created.get("item_id") or created["id"])
        _pinax(
            repository, "note", "add", item_id, "--ref", brief, "--caption",
            f"out-{number}.txt reaches the default branch.",
            "--actor", "coordinator@proof", "--json",
        )
    git(repository, "add", ".")
    git(repository, "commit", "-m", "ADDED: initialise the replay proof")
    git(repository, "remote", "add", "origin", str(remote))
    git(repository, "push", "-u", "origin", "main")
    git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    return repository, remote


def _install_refusal(repository: Path, marker: Path) -> None:
    marker.write_text("refuse the next note commit\n", encoding="utf-8")
    hook = repository / ".git" / "hooks" / "commit-msg"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(REFUSE_ONCE_HOOK.format(marker=marker.as_posix()), encoding="utf-8", newline="\n")
    os.chmod(hook, 0o755)


def _profile(tmp_path: Path, executable: Path) -> Path:
    command = [sys.executable, str(executable), "claude-code"]
    validator = [sys.executable, "-c", "pass"]
    profile = tmp_path / "autobuild.toml"
    profile.write_text(
        "[run]\n"
        'harness = "claude-code"\n'
        f"max_items = {ITEMS + 1}\n"
        f"concurrency = {CONCURRENCY}\n\n"
        "[models]\n"
        'builder = "builder-model"\n'
        'reviewer = "reviewer-model"\n\n'
        "[validator]\n"
        'id = "replay-tests"\n'
        f"argv = {json.dumps(validator)}\n\n"
        "[harness]\n"
        f"command = {json.dumps(command)}\n\n"
        "[preflight]\n"
        'accepted_environment = ["NODE_EXTRA_CA_CERTS", "SSLKEYLOGFILE", "HTTP_PROXY", '
        '"HTTPS_PROXY", "ALL_PROXY", "AUTOBUILD_TEST_RENDEZVOUS", "AUTOBUILD_TEST_ITEMS"]\n\n'
        # Host pressure must never hold this campaign: on a busy host (or with
        # the suite on parallel workers) a CPU-held admission waits for hours.
        "[pressure]\n"
        "high_cpu_percent = 100\ncalm_cpu_percent = 100\n"
        "high_memory_percent = 0\nhigh_memory_gb = 0\n"
        "critical_memory_percent = 0\ncritical_memory_gb = 0\nhigh_commit_percent = 0\n",
        encoding="utf-8",
    )
    return profile


def test_a_refused_follow_up_commit_is_recovered_and_every_item_delivers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, remote = _fixture(tmp_path / "fixture")
    marker = tmp_path / "refuse-once"
    _install_refusal(repository, marker)
    rendezvous = tmp_path / "rendezvous"
    rendezvous.mkdir()
    monkeypatch.setenv("AUTOBUILD_TEST_RENDEZVOUS", str(rendezvous))
    monkeypatch.setenv("AUTOBUILD_TEST_ITEMS", str(CONCURRENCY))
    executable = tmp_path / "fake replay harness.py"
    executable.write_text(FAKE_HARNESS, encoding="utf-8")
    output = tmp_path / "campaign-result.json"

    exit_code = campaign_main(
        [
            "run",
            "--repository", str(repository),
            "--profile", str(_profile(tmp_path, executable)),
            "--scratch-root", str(tmp_path / "scratch"),
            "--allow-delivery",
            "--output", str(output),
        ]
    )

    result = json.loads(output.read_text(encoding="utf-8"))
    assert not marker.exists(), "the hook never refused the follow-up note commit"
    assert result["stop_reason"] == "queue_dry", result
    assert [item["disposition"] for item in result["items"]] == ["accepted"] * ITEMS, result
    assert exit_code == 0
    assert git(repository, "status", "--porcelain", "--untracked-files=all") == ""
    subjects = git(repository, "log", "--format=%s", "main").splitlines()
    assert any(re.match(r"pinax: note\.added abp-", subject) for subject in subjects), subjects
    delivered = git(remote, "ls-tree", "-r", "--name-only", "main").split()
    assert {f"out-{n}.txt" for n in range(1, ITEMS + 1)} <= set(delivered)
    verify = run_retrying(("pinax", "--root", str(repository), "verify"), cwd=repository)
    assert verify.returncode == 0, verify.stdout + verify.stderr


REFUSE_COUNT_HOOK = """#!/bin/sh
counter="{counter}"
if grep -q "note.added" "$1"; then
  left=$(cat "$counter")
  if [ "$left" -gt 0 ]; then
    echo $((left - 1)) > "$counter"
    echo "error: refused by the test hook" >&2
    exit 1
  fi
fi
exit 0
"""


def _refuse_notes(repository: Path, counter: Path, times: int) -> None:
    counter.write_text(f"{times}\n", encoding="utf-8")
    hook = repository / ".git" / "hooks" / "commit-msg"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(
        REFUSE_COUNT_HOOK.format(counter=counter.as_posix()), encoding="utf-8", newline="\n"
    )
    os.chmod(hook, 0o755)


def _proposal() -> Proposal:
    return Proposal("Follow-up", "What should be tidied?", "reviewer reservation", "docs/brief.md")


def test_a_partial_write_is_committed_with_pinax_subject_after_the_stated_waits(
    tmp_path: Path,
) -> None:
    origin = pinax_origin(tmp_path)
    counter = tmp_path / "refusals"
    # Pinax's own commit and the first two recovery commits are refused.
    _refuse_notes(origin.repo, counter, 3)
    sleeps: list[float] = []
    tracker = PinaxTrackerAdapter(origin.repo, sleep=sleeps.append)

    ref = tracker.propose(_proposal(), "coordinator@proof")

    assert sleeps == [2.0, 5.0]
    assert ref.published is True
    assert git(origin.repo, "status", "--porcelain", "--untracked-files=all") == ""
    assert git(origin.repo, "log", "-1", "--format=%s") == f"pinax: note.added {ref.proposal_id}"
    changed = git(origin.repo, "show", "--name-only", "--format=", "HEAD").split()
    assert changed and all(path.startswith(".ergon/") for path in changed)
    assert origin.head() == git(origin.repo, "rev-parse", "HEAD")
    verify = run_retrying(("pinax", "--root", str(origin.repo), "verify"), cwd=origin.repo)
    assert verify.returncode == 0, verify.stdout + verify.stderr


def test_a_partial_write_that_stays_refused_is_pending_until_recover_succeeds(
    tmp_path: Path,
) -> None:
    origin = pinax_origin(tmp_path)
    counter = tmp_path / "refusals"
    _refuse_notes(origin.repo, counter, 99)
    sleeps: list[float] = []
    tracker = PinaxTrackerAdapter(origin.repo, sleep=sleeps.append)

    with pytest.raises(TrackerStop) as raised:
        tracker.propose(_proposal(), "coordinator@proof")

    assert raised.value.code == 7
    assert "recovery of the appended event failed" in str(raised.value)
    assert sleeps == [2.0, 5.0, 10.0]
    leftover = git(origin.repo, "status", "--porcelain", "--untracked-files=all")
    assert leftover and all(line[3:].startswith(".ergon/") for line in leftover.splitlines())
    still = tracker.recover()
    assert (still.clear, still.condition) == (False, HoldCondition.TRACKER_PENDING)

    counter.write_text("0\n", encoding="utf-8")
    recovered = tracker.recover()

    assert recovered.clear is True
    assert git(origin.repo, "status", "--porcelain", "--untracked-files=all") == ""
    assert git(origin.repo, "log", "-1", "--format=%s").startswith("pinax: note.added ")
    assert origin.head() == git(origin.repo, "rev-parse", "HEAD")


def _fenced_fixture(root: Path) -> tuple[Path, dict[str, str]]:
    """Four ready items: ``a`` and ``b`` with briefs, ``c`` and ``d`` without."""

    remote = root / "remote.git"
    repository = root / "repository"
    _run("git", "init", "--bare", str(remote))
    _run("git", "init", "-b", "main", str(repository))
    git(repository, "config", "user.name", "AutoBuild Fence Proof")
    git(repository, "config", "user.email", "autobuild-fence@example.invalid")
    (repository / "README.md").write_text("# Fence proof\n", encoding="utf-8")
    (repository / "docs").mkdir()
    _pinax(repository, "init", "--actor", "coordinator@proof")
    ids: dict[str, str] = {}
    for number, name in enumerate(("a", "b", "c", "d"), start=1):
        created = json.loads(
            _pinax(
                repository, "add", "--title", f"Build out-{number}", "--prefix", "fnc",
                "--actor", "coordinator@proof", "--json",
            )
        )
        ids[name] = str(created.get("item_id") or created["id"])
        if name in {"a", "b"}:
            brief = f"docs/brief-{number}.md"
            (repository / brief).write_text(
                f"# Brief {number}\n\nWRITE-TARGET: out-{number}.txt\n\n"
                f"## Declared paths\n\n`out-{number}.txt`\n",
                encoding="utf-8",
            )
            _pinax(
                repository, "note", "add", ids[name], "--ref", brief, "--caption",
                f"out-{number}.txt reaches the default branch.",
                "--actor", "coordinator@proof", "--json",
            )
    git(repository, "add", ".")
    git(repository, "commit", "-m", "ADDED: initialise the fence proof")
    git(repository, "remote", "add", "origin", str(remote))
    git(repository, "push", "-u", "origin", "main")
    git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    return repository, ids


def test_briefless_items_outside_the_fence_never_stop_the_launch_or_the_campaign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, ids = _fenced_fixture(tmp_path / "fixture")
    rendezvous = tmp_path / "rendezvous"
    rendezvous.mkdir()
    monkeypatch.setenv("AUTOBUILD_TEST_RENDEZVOUS", str(rendezvous))
    monkeypatch.setenv("AUTOBUILD_TEST_ITEMS", "2")
    executable = tmp_path / "fake fence harness.py"
    executable.write_text(FAKE_HARNESS, encoding="utf-8")
    profile = _profile(tmp_path, executable)
    profile.write_text(
        profile.read_text(encoding="utf-8").replace(f"concurrency = {CONCURRENCY}", "concurrency = 2")
        + f"\n[selection]\nallow = {json.dumps([ids['a'], ids['b']])}\n"
        f"exclude = {json.dumps([ids['c']])}\n",
        encoding="utf-8",
    )
    output = tmp_path / "campaign-result.json"

    exit_code = campaign_main(
        [
            "run",
            "--repository", str(repository),
            "--profile", str(profile),
            "--scratch-root", str(tmp_path / "scratch"),
            "--allow-delivery",
            "--output", str(output),
        ]
    )

    assert output.exists(), "the launch failed before the campaign result was written"
    result = json.loads(output.read_text(encoding="utf-8"))
    assert exit_code == 0, result
    assert result["stop_reason"] == "queue_dry"
    assert sorted(item["item_id"] for item in result["items"]) == sorted([ids["a"], ids["b"]])
    assert {item["disposition"] for item in result["items"]} == {"accepted"}
    events_log = Path(result["report_ref"]).parent / "events.jsonl"
    skipped = [
        json.loads(line)
        for line in events_log.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["event_type"] == "item.skipped"
    ]
    assert {event["item_id"] for event in skipped}.isdisjoint({ids["c"], ids["d"]})


def test_a_ready_item_without_a_brief_is_skipped_and_named(tmp_path: Path) -> None:
    origin = pinax_origin(tmp_path, count=1)
    created = json.loads(
        _pinax(
            origin.repo, "add", "--title", "No brief yet", "--prefix", "tst",
            "--actor", "coordinator@proof", "--json",
        )
    )
    briefless = str(created.get("item_id") or created["id"])
    tracker = PinaxTrackerAdapter(origin.repo)
    campaign = CampaignRef("skips", origin.repo)

    ready = [item.item_id for item in tracker.ready_items(campaign)]

    assert ready == origin.items
    assert [(entry.item_id, entry.reason) for entry in tracker.skipped_items()] == [
        (briefless, "no_brief")
    ]


def test_a_delivery_error_after_the_item_commit_parks_from_the_primary(tmp_path: Path) -> None:
    """The primary holds a stray untracked file at the path the item adds, so
    delivery refuses. The item's branch already holds its product commit; the
    park must succeed from the primary checkout, keep the worktree and publish
    none of the item's commits."""

    origin = pinax_origin(tmp_path)
    tracker, workspace = default_adapters(origin)
    stray = origin.repo / "feature.txt"
    harness = scripted_harness(
        origin.items[0],
        after_review=lambda: stray.write_text("left by hand\n", encoding="utf-8"),
    )

    outcome, ports = run_item(origin, workspace, tracker, harness, ValidatorRuns())

    assert outcome.disposition is ItemDisposition.PARKED, outcome.reason
    assert "untracked files in the primary checkout are at paths the delivery brings in" in (
        outcome.reason or ""
    )
    assert outcome.states[-1] is ItemState.PARKED
    assert item_status(origin, origin.items[0]) == "parked"
    assert "feature.txt" not in git(origin.repo, "ls-tree", "-r", "--name-only", origin.head())
    created = next(e for e in ports.records.events if e.event_type == "workspace.created")
    assert Path(created.payload["worktree_root"]).is_dir()
    assert not [e for e in ports.records.events if e.event_type == "item.park_failed"]
