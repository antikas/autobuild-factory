"""A serial campaign whose second ready item has an unusable brief.

Real Git with a bare local origin, a ``BACKLOG.md`` tracker, the production
command and a fake harness that runs as a real child process."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_git_workspace import git
from test_launch_branch import Launch
from test_pinax_tracker import pinax

from autobuild.adapters.harness_cli import CliHarnessAdapter

HARNESS = """\
import json
import sys
from pathlib import Path

args = sys.argv[2:]
if args == ["--version"]:
    print("claude-code fake 1.0")
    raise SystemExit(0)
if args[:2] == ["auth", "status"]:
    print(json.dumps({"loggedIn": True}))
    raise SystemExit(0)
if "Review the actual brief" in sys.stdin.read():
    payload = {"decision": "pass", "findings": [], "evidence_ref": "r", "inspected": [], "acceptance": []}
else:
    target = Path("product.txt")
    built = target.read_text(encoding="utf-8") if target.exists() else ""
    target.write_text(built + "built\\n", encoding="utf-8")
    payload = {"summary": "built", "report_ref": "b"}
print(json.dumps({
    "result": "complete",
    "structured_output": payload,
    "usage": {"input_tokens": 9, "output_tokens": 3},
}))
"""

SECOND = "docs/second.md"
BRIEF = "# Brief\n\nAdd a line to product.txt.\n\n## Declared paths\n\n`product.txt`\n"


@pytest.mark.parametrize(
    "content, reason",
    [
        (None, f"brief:missing {SECOND}"),
        (b"", f"brief:empty {SECOND}"),
        (b" \n\t\n", f"brief:empty {SECOND}"),
        (b"x" * 1_048_577, f"brief:too-large 1048577 bytes, over 1048576 {SECOND}"),
        (b"# Brief \xff\xfe\n", f"brief:unreadable {SECOND}"),
    ],
    ids=["missing", "empty", "whitespace", "too-large", "not-utf8"],
)
def test_the_second_item_parks_before_its_claim_and_the_others_are_delivered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: bytes | None,
    reason: str,
) -> None:
    launch = Launch(tmp_path)
    (tmp_path / "harness.py").write_text(HARNESS, encoding="utf-8")
    profile = tmp_path / "autobuild.toml"
    profile.write_text(
        profile.read_text(encoding="utf-8").replace("max_items = 1", "max_items = 3"),
        encoding="utf-8",
    )
    repository = launch.repository
    (repository / "docs").mkdir()
    (repository / "docs" / "first.md").write_text(BRIEF, encoding="utf-8")
    (repository / "docs" / "third.md").write_text(BRIEF, encoding="utf-8")
    if content is not None:
        (repository / SECOND).write_bytes(content)
    (repository / "BACKLOG.md").write_text(
        "| Item | Title | Status | Brief |\n|---|---|---|---|\n"
        "| MB-1 | First | Ready | docs/first.md |\n"
        f"| MB-2 | Second | Ready | {SECOND} |\n"
        "| MB-3 | Third | Ready | docs/third.md |\n",
        encoding="utf-8",
    )
    launch._commit_all("queue three items")
    git(repository, "push", "origin", "main")
    assembled: list[str] = []
    materialise = CliHarnessAdapter._materialise_evidence

    def recording(self, request):
        assembled.append(request.item_id)
        return materialise(self, request)

    monkeypatch.setattr(CliHarnessAdapter, "_materialise_evidence", recording)

    assert launch.run("--campaign-id", "brief-run") == 1

    backlog = (repository / "BACKLOG.md").read_text(encoding="utf-8")
    assert f"| MB-2 | Second | Parked: {reason} | {SECOND} |" in backlog
    assert "| MB-1 | First | Done (" in backlog
    assert "| MB-3 | Third | Done (" in backlog
    assert str(tmp_path) not in backlog
    subjects = git(launch.remote, "log", "--format=%s", "main")
    assert "MODIFIED: claim MB-2 in BACKLOG" not in subjects
    assert "MODIFIED: park MB-2 in BACKLOG" in subjects
    assert git(launch.remote, "show", "main:product.txt") == "built\nbuilt"
    assert "MB-2" not in git(repository, "for-each-ref")
    assert "MB-2" not in git(repository, "worktree", "list", "--porcelain")
    assert assembled == ["MB-1", "MB-1", "MB-3", "MB-3"]

    (events_path,) = (launch.scratch / "runs").rglob("events.jsonl")
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    second = [event for event in events if event.get("item_id") == "MB-2"]
    kinds = [event["event_type"] for event in second]
    assert kinds.index("item.triaged") < kinds.index("item.parked")
    assert not {"item.claimed", "seat.launched", "seat.completed"} & set(kinds)
    triaged = second[kinds.index("item.triaged")]
    assert triaged["payload"] == {"triage": reason.split()[0], "reason": reason}

    report = (repository / "docs" / "campaigns" / "brief-run.md").read_text(encoding="utf-8")
    parked = report.split("## Parked", 1)[1].split("\n## ", 1)[0]
    assert "MB-2" in parked and reason in parked


def _reasons(value: object) -> list[str]:
    if isinstance(value, dict):
        found = [
            str(entry) for key, entry in value.items() if key in ("reason", "error", "diagnostics")
        ]
        return found + [reason for entry in value.values() for reason in _reasons(entry)]
    if isinstance(value, list):
        return [reason for entry in value for reason in _reasons(entry)]
    return []


@pytest.mark.usefixtures("self_committing_pinax")
@pytest.mark.parametrize("cause", ("missing", "empty"))
def test_a_brief_absent_or_empty_in_the_worktree_parks_by_its_reference(
    tmp_path: Path, cause: str
) -> None:
    launch = Launch(tmp_path)
    repository = launch.repository
    (repository / "docs").mkdir()
    git(repository, "rm", "BACKLOG.md")
    pinax(repository, "init", "--actor", "coordinator@proof")
    created = json.loads(
        pinax(
            repository, "add", "--title", "Second", "--prefix", "mbp",
            "--actor", "coordinator@proof", "--json",
        )
    )
    item_id = str(created.get("item_id") or created["id"])
    pinax(
        repository, "note", "add", item_id, "--ref", SECOND, "--caption", "It is built.",
        "--actor", "coordinator@proof", "--json",
    )
    if cause == "empty":
        (repository / SECOND).write_bytes(b"")
    launch._commit_all("queue one item")
    git(repository, "push", "origin", "main")
    if cause == "empty":
        git(repository, "update-index", "--assume-unchanged", SECOND)
    (repository / SECOND).write_text(BRIEF, encoding="utf-8")

    assert launch.run("--tracker", "pinax", "--campaign-id", "park-run") == 1

    reason = f"seat brief is {cause}: {SECOND}"
    (run_folder,) = (path.parent for path in (launch.scratch / "runs").rglob("events.jsonl"))
    events = [
        json.loads(line)
        for line in (run_folder / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    kinds = [event["event_type"] for event in events if event.get("item_id") == item_id]
    assert "item.claimed" in kinds and "item.parked" in kinds
    assert not {"seat.launched", "seat.completed"} & set(kinds)

    tracker = "".join(
        path.read_text(encoding="utf-8")
        for path in sorted((repository / ".ergon").rglob("*"))
        if path.is_file()
    )
    progress = (run_folder / "progress.log").read_text(encoding="utf-8")
    report = (repository / "docs" / "campaigns" / "park-run.md").read_text(encoding="utf-8")
    parked = report.split("## Parked", 1)[1].split("\n## ", 1)[0]
    for text in (tracker, progress, parked):
        assert reason in text
        assert str(tmp_path) not in text
    assert str(launch.scratch / "workspace") not in report
    assert str(tmp_path) not in git(launch.remote, "log", "--format=%B", "main")

    reasons = _reasons(events)
    for path in run_folder.rglob("*.json"):
        reasons += _reasons(json.loads(path.read_text(encoding="utf-8")))
    assert any(reason in text for text in reasons)
    assert [text for text in reasons if str(tmp_path) in text] == []
    for root in (launch.scratch, repository / ".ergon", repository / "docs"):
        for path in root.rglob("*"):
            if path.is_file():
                text = path.read_text(encoding="utf-8", errors="replace")
                assert f"seat brief is {cause}: {tmp_path}" not in text, path
