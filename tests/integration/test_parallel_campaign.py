"""Three independent items built at once by the production command.

Real Git with a bare local origin, real Pinax, and a fake harness that runs as
a real child process. Each builder writes its own file, then waits at a
rendezvous until all three builders are inside their own worktrees, so the
test sees the three worktrees exist at the same time. All three must be
delivered and ``pinax verify`` must pass on the primary checkout."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from scanner_retry import run_retrying

from autobuild.cli import main as campaign_main

pytestmark = pytest.mark.usefixtures("self_committing_pinax")

ITEMS = 3

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
    payload = {"decision": "pass", "findings": [], "evidence_ref": "fake-review"}
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
    seen = len(list(rendezvous.glob("*.started")))
    (rendezvous / f"{target}.seen").write_text(str(seen), encoding="utf-8")
    payload = {"summary": "fake harness built " + target, "report_ref": "fake-build"}

print(json.dumps({
    "result": "complete",
    "structured_output": payload,
    "usage": {"input_tokens": 9, "output_tokens": 3},
}))
"""


def _run(*argv: str, cwd: Path | None = None) -> str:
    completed = run_retrying(argv, cwd=cwd)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"{' '.join(argv)} failed: {detail}")
    return completed.stdout.strip()


def _git(root: Path, *args: str) -> str:
    return _run("git", "-C", str(root), *args)


def _pinax(root: Path, *args: str) -> str:
    return _run("pinax", "--root", str(root), *args, cwd=root)


def _fixture(root: Path) -> tuple[Path, Path]:
    remote = root / "remote.git"
    repository = root / "repository"
    _run("git", "init", "--bare", str(remote))
    _run("git", "init", "-b", "main", str(repository))
    _git(repository, "config", "user.name", "AutoBuild Parallel Proof")
    _git(repository, "config", "user.email", "autobuild-parallel@example.invalid")
    (repository / "README.md").write_text("# Parallel proof\n", encoding="utf-8")
    docs = repository / "docs"
    docs.mkdir()
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
                repository, "add", "--title", f"Build out-{number}", "--prefix", "par",
                "--actor", "coordinator@proof", "--json",
            )
        )
        item_id = str(created.get("item_id") or created["id"])
        _pinax(
            repository, "note", "add", item_id, "--ref", brief, "--caption",
            f"out-{number}.txt reaches the default branch.",
            "--actor", "coordinator@proof", "--json",
        )
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "ADDED: initialise the parallel proof")
    _git(repository, "remote", "add", "origin", str(remote))
    _git(repository, "push", "-u", "origin", "main")
    _git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    return repository, remote


def test_three_independent_items_build_at_once_and_all_deliver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, remote = _fixture(tmp_path / "fixture")
    rendezvous = tmp_path / "rendezvous"
    rendezvous.mkdir()
    monkeypatch.setenv("AUTOBUILD_TEST_RENDEZVOUS", str(rendezvous))
    monkeypatch.setenv("AUTOBUILD_TEST_ITEMS", str(ITEMS))
    executable = tmp_path / "fake parallel harness.py"
    executable.write_text(FAKE_HARNESS, encoding="utf-8")
    command = [sys.executable, str(executable), "claude-code"]
    validator = [sys.executable, "-c", "pass"]
    profile = tmp_path / "autobuild.toml"
    profile.write_text(
        "[run]\n"
        'harness = "claude-code"\n'
        f"max_items = {ITEMS}\n"
        f"concurrency = {ITEMS}\n\n"
        "[models]\n"
        'builder = "builder-model"\n'
        'reviewer = "reviewer-model"\n\n'
        "[validator]\n"
        'id = "parallel-tests"\n'
        f"argv = {json.dumps(validator)}\n\n"
        "[harness]\n"
        f"command = {json.dumps(command)}\n\n"
        "[preflight]\n"
        'accepted_environment = ["NODE_EXTRA_CA_CERTS", "SSLKEYLOGFILE", "HTTP_PROXY", '
        '"HTTPS_PROXY", "ALL_PROXY", "AUTOBUILD_TEST_RENDEZVOUS", "AUTOBUILD_TEST_ITEMS"]\n\n'
        # The real host monitor runs; memory lines of zero and a CPU line of
        # 100 keep a busy test machine from holding admissions. Under load (or
        # with the suite on parallel workers) the run outlasts the CPU window.
        "[pressure]\n"
        "high_cpu_percent = 100\ncalm_cpu_percent = 100\n"
        "high_memory_percent = 0\nhigh_memory_gb = 0\n"
        "critical_memory_percent = 0\ncritical_memory_gb = 0\nhigh_commit_percent = 0\n",
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

    result = json.loads(output.read_text(encoding="utf-8"))
    assert exit_code == 0, result
    assert [item["disposition"] for item in result["items"]] == ["accepted"] * ITEMS
    assert all(item["pushed"] for item in result["items"])
    assert result["schedule"]["concurrency"] == ITEMS
    assert result["schedule"]["peak_concurrency"] == ITEMS
    seen = {path.name: path.read_text(encoding="utf-8") for path in rendezvous.glob("*.seen")}
    assert seen == {f"out-{n}.txt.seen": str(ITEMS) for n in range(1, ITEMS + 1)}
    worktrees = {path.read_text(encoding="utf-8") for path in rendezvous.glob("*.started")}
    assert len(worktrees) == ITEMS
    assert str(repository) not in worktrees
    delivered = _git(remote, "ls-tree", "-r", "--name-only", "main").split()
    assert {f"out-{n}.txt" for n in range(1, ITEMS + 1)} <= set(delivered)
    verify = run_retrying(("pinax", "--root", str(repository), "verify"), cwd=repository)
    assert verify.returncode == 0, verify.stdout + verify.stderr
