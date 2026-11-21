"""A follow-up is triaged from a fresh clone alone: real Git, real Pinax, a bare
origin, and the whole campaign run through ``autobuild run``.

The reviewer passes the item with one long non-blocking finding. After the
campaign, the scratch root is deleted and the origin is cloned afresh; the
clone must carry the follow-up in the tracker, blocked behind ``proposal``,
its brief note pointing to a report file in the clone, and the full finding
text and evidence reference in both the report and the parent's item page."""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

import pytest
from scanner_retry import run_retrying
from test_serial_delivery import git

from autobuild.cli import main as campaign_main

pytestmark = pytest.mark.usefixtures("self_committing_pinax")

EVIDENCE_REF = "src/cache.py:41-58"

FAKE_HARNESS = """\
import json
import os
import sys
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
    finding = json.loads(Path(os.environ["AUTOBUILD_TEST_FINDING"]).read_text(encoding="utf-8"))
    payload = {"decision": "pass", "findings": [finding], "evidence_ref": "fake-review", "inspected": [], "acceptance": []}
else:
    Path("out.txt").write_text("built\\n", encoding="utf-8")
    payload = {"summary": "fake harness built out.txt", "report_ref": "fake-build"}

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


def _pinax(root: Path, *args: str) -> str:
    return _run("pinax", "--root", str(root), *args, cwd=root)


def _fixture(root: Path) -> tuple[Path, Path, str]:
    remote = root / "remote.git"
    repository = root / "repository"
    _run("git", "init", "--bare", str(remote))
    _run("git", "init", "-b", "main", str(repository))
    git(repository, "config", "user.name", "AutoBuild Durability Proof")
    git(repository, "config", "user.email", "autobuild-durability@example.invalid")
    (repository / "README.md").write_text("# Durability proof\n", encoding="utf-8")
    (repository / "docs").mkdir()
    (repository / "docs" / "brief.md").write_text(
        "# Brief\n\nWrite out.txt.\n\n## Declared paths\n\n`out.txt`\n", encoding="utf-8"
    )
    _pinax(repository, "init", "--actor", "coordinator@proof")
    created = json.loads(
        _pinax(
            repository, "add", "--title", "Build out.txt", "--prefix", "dur",
            "--actor", "coordinator@proof", "--json",
        )
    )
    item_id = str(created.get("item_id") or created["id"])
    _pinax(
        repository, "note", "add", item_id, "--ref", "docs/brief.md", "--caption",
        "out.txt reaches the default branch.", "--actor", "coordinator@proof", "--json",
    )
    git(repository, "add", ".")
    git(repository, "commit", "-m", "ADDED: initialise the durability proof")
    git(repository, "remote", "add", "origin", str(remote))
    git(repository, "push", "-u", "origin", "main")
    git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    return repository, remote, item_id


def _profile(tmp_path: Path, executable: Path) -> Path:
    command = [sys.executable, str(executable), "claude-code"]
    validator = [sys.executable, "-c", "pass"]
    profile = tmp_path / "autobuild.toml"
    profile.write_text(
        "[run]\n"
        'harness = "claude-code"\n'
        "max_items = 1\n"
        "concurrency = 1\n\n"
        "[models]\n"
        'builder = "builder-model"\n'
        'reviewer = "reviewer-model"\n\n'
        "[validator]\n"
        'id = "durability-tests"\n'
        f"argv = {json.dumps(validator)}\n\n"
        "[harness]\n"
        f"command = {json.dumps(command)}\n\n"
        "[preflight]\n"
        'accepted_environment = ["NODE_EXTRA_CA_CERTS", "SSLKEYLOGFILE", "HTTP_PROXY", '
        '"HTTPS_PROXY", "ALL_PROXY", "AUTOBUILD_TEST_FINDING"]\n\n'
        "[pressure]\n"
        "high_cpu_percent = 100\ncalm_cpu_percent = 100\n"
        "high_memory_percent = 0\nhigh_memory_gb = 0\n"
        "critical_memory_percent = 0\ncritical_memory_gb = 0\nhigh_commit_percent = 0\n",
        encoding="utf-8",
    )
    return profile


def _consequence() -> str:
    paragraph = (
        "The cache key is built from the raw path | not the normalised one, so "
        "`key(a)` and ``key(b)`` collide on a case-folding file system.\n"
    )
    text = "".join(f"{index}: {paragraph}" for index in range(20)) + "## Edges\nEnd."
    assert len(text) >= 1500
    return text


def _events(clone: Path) -> list[dict]:
    events: list[dict] = []
    for log in sorted((clone / ".ergon" / "log").glob("*.jsonl")):
        for line in log.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(json.loads(line))
    return events


def test_a_fresh_clone_holds_the_full_text_of_every_follow_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, remote, parent = _fixture(tmp_path / "fixture")
    consequence = _consequence()
    finding = tmp_path / "finding.json"
    finding.write_text(
        json.dumps(
            {
                "code": "CACHE",
                "consequence": consequence,
                "evidence_ref": EVIDENCE_REF,
                "blocking": False,
                "specialist_boundary": None,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AUTOBUILD_TEST_FINDING", str(finding))
    executable = tmp_path / "fake durability harness.py"
    executable.write_text(FAKE_HARNESS, encoding="utf-8")
    output = tmp_path / "campaign-result.json"
    scratch = tmp_path / "scratch"

    exit_code = campaign_main(
        [
            "run",
            "--repository", str(repository),
            "--profile", str(_profile(tmp_path, executable)),
            "--scratch-root", str(scratch),
            "--allow-delivery",
            "--output", str(output),
        ]
    )

    result = json.loads(output.read_text(encoding="utf-8"))
    assert [item["disposition"] for item in result["items"]] == ["accepted"], result
    assert exit_code == 0
    shutil.rmtree(scratch, ignore_errors=True)
    shutil.rmtree(repository)
    clone = tmp_path / "fresh" / "clone"
    _run("git", "clone", "--quiet", str(remote), str(clone))

    events = _events(clone)
    title = f"Follow-up: {parent} CACHE"
    (proposal_id,) = [
        event["payload"]["item_id"]
        for event in events
        if event["type"] == "item.created" and event["payload"].get("title") == title
    ]
    assert any(
        event["type"] == "item.blocked"
        and event["payload"].get("item_id") == proposal_id
        and event["payload"].get("gate") == "proposal"
        for event in events
    ), [event for event in events if event["payload"].get("item_id") == proposal_id]
    (note,) = [
        event["payload"]
        for event in events
        if event["type"] == "note.added" and event["payload"].get("item_id") == proposal_id
    ]
    report = clone / note["ref"]
    assert re.fullmatch(r"docs/campaigns/[^/]+\.md", note["ref"]), note
    assert report.is_file()
    assert proposal_id in note["caption"] and parent in note["caption"]
    assert len(note["caption"]) <= 200

    content = report.read_text(encoding="utf-8")
    section = content[content.index(f"### {proposal_id} {title}") :]
    assert f"- Evidence reference: {EVIDENCE_REF}" in section
    assert f"- Parent item: {parent}" in section
    assert f"\n```text\n{consequence}\n```\n" in section

    page = (clone / ".ergon" / "items" / f"{parent}.md").read_text(encoding="utf-8")
    findings = page[page.index("## Review findings") :]
    assert f"- Evidence reference: {EVIDENCE_REF}" in findings
    assert f"\n```text\n{consequence}\n```\n" in findings
