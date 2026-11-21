from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "skills" / "autobuild-coordinated" / "scripts" / "register_findings.py"


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("register_findings", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rf = load_script()

REPORT = """VERDICT FAIL
BLOCKERS
- The cancel overwrites a delivered result (clause A). Reproduced with cancel.py.
SCOPE, the non-blocking findings:
- API replies wait up to 21 seconds for the next poll.
- The journal has no rotation.
2. A stop can take longer than the unit allows.
WHAT YOU RAN
- pytest tests/test_runner.py: 40 passed
nothing running
"""


def test_only_the_lines_under_scope_are_findings() -> None:
    assert rf.scope_findings(REPORT) == [
        "API replies wait up to 21 seconds for the next poll.",
        "The journal has no rotation.",
        "A stop can take longer than the unit allows.",
    ]


def test_a_blocker_is_never_registered() -> None:
    assert not any("cancel overwrites" in finding for finding in rf.scope_findings(REPORT))


def test_markdown_headings_and_a_finding_on_the_label_line() -> None:
    text = "## BLOCKERS\nnone\n## SCOPE: one stale comment in runner.py\n- A second finding.\n## WHAT YOU RAN\n- x\n"
    assert rf.scope_findings(text) == ["one stale comment in runner.py", "A second finding."]


def test_none_registers_nothing() -> None:
    assert rf.scope_findings("VERDICT PASS\nSCOPE: none\nWHAT YOU RAN\n- x\n") == []
    assert rf.scope_findings("SCOPE\n- None.\n") == []


def test_a_report_with_no_scope_section_has_no_findings() -> None:
    assert rf.scope_findings("VERDICT PASS\nBLOCKERS\nnone\n") == []


def test_a_finding_that_starts_with_capitals_does_not_end_the_section() -> None:
    text = "SCOPE\nAPI returns the old value after a reload.\nSQL is built by hand in one place.\nWHAT YOU RAN\n"
    assert len(rf.scope_findings(text)) == 2


def test_a_title_carries_its_source_and_stays_within_the_limit() -> None:
    title = rf.title_for("The journal has   no rotation.", "the runner's service item")
    assert title == "The journal has no rotation. (review finding outside acceptance: the runner's service item)"
    long_title = rf.title_for("x" * 900, "s")
    assert len(long_title) <= rf.TITLE_LIMIT
    assert long_title.isascii()


def test_each_finding_is_added_then_blocked_with_gate_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[1] == "add":
            return SimpleNamespace(returncode=0, stdout=json.dumps({"item_id": "kx-%04d" % len(calls)}), stderr="")
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(rf, "pinax_path", lambda: "pinax")
    ids = rf.register(Path("."), ["first", "second"], "a review", "kx", run=fake_run)

    assert len(ids) == 2
    assert [call[1] for call in calls] == ["add", "block", "add", "block"]
    assert calls[1][2:5] == [ids[0], "--gate", "scope"]
    assert calls[3][2:5] == [ids[1], "--gate", "scope"]
    assert "--prefix" in calls[0]


def test_a_held_git_lock_is_waited_out(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    pauses: list[float] = []

    def fake_run(args, **kwargs):
        calls.append(args[1])
        if args[1] == "block" and calls.count("block") == 1:
            return SimpleNamespace(returncode=1, stdout="", stderr="fatal: Unable to create '.git/index.lock': File exists.")
        if args[1] == "add":
            return SimpleNamespace(returncode=0, stdout=json.dumps({"item_id": "ex-0001"}), stderr="")
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(rf, "pinax_path", lambda: "pinax")
    ids = rf.register(Path("."), ["first"], "a review", None, run=fake_run, pause=pauses.append)

    assert ids == ["ex-0001"]
    assert calls == ["add", "block", "block"]
    assert len(pauses) == 1


def test_a_failed_add_stops_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(args, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="tracker refused")

    monkeypatch.setattr(rf, "pinax_path", lambda: "pinax")
    with pytest.raises(RuntimeError):
        rf.register(Path("."), ["first"], "a review", None, run=fake_run)


def test_a_report_without_findings_registers_nothing_and_never_calls_the_tracker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    review = tmp_path / "review.md"
    review.write_text("VERDICT PASS\nSCOPE: none\nWHAT YOU RAN\n- pytest: 3 passed\n", encoding="utf-8")

    def refuse(*args, **kwargs):
        raise AssertionError("the tracker must not be called")

    monkeypatch.setattr(rf, "register", refuse)
    assert rf.main([str(tmp_path), str(review), "--source", "a review"]) == 0
    assert "nothing registered" in capsys.readouterr().out
