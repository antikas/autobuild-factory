"""A registered follow-up keeps its full finding text in the repository: in the
parent's close briefing and in the campaign report, never cut to a caption."""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import pytest
from test_workflows import _review_result, builder, campaign, command, item, make_ports, spec

from autobuild.adapters import BacklogTrackerAdapter, PinaxTrackerAdapter
from autobuild.application import CampaignRunner
from autobuild.domain import (
    CloseEvidence,
    CommandResult,
    DiffEvidence,
    EvidenceError,
    Proposal,
    ReviewDecision,
    ReviewFinding,
    ReviewVerdict,
    TrackerPublication,
    ValidationEvidence,
    WorkspaceRef,
    fenced_block,
)


def long_consequence() -> str:
    paragraph = (
        "The reviewer saw the cache key built from the raw path | the normalised "
        "path, so `key(a)` and ``key(b)`` collide on a case-folding file system.\n"
    )
    text = "".join(f"{index}: {paragraph}" for index in range(20))
    assert len(text) >= 1500
    return text + "Last line without a newline"


EDGES = "First line.\n## Edges\nA run of four backticks: ```` stays inside.\n## Next\nEnd."


def _report(
    consequence: str, evidence_ref: str = "src/cache.py:41-58", code: str = "CACHE"
) -> str:
    verdict = ReviewVerdict(
        "item-1",
        ReviewDecision.PASS,
        (ReviewFinding(code, consequence, evidence_ref, blocking=False),),
        "verdict:pass",
    )
    ports = make_ports([builder(), _review_result(verdict)], [command()], diff_count=1)
    ports.tracker.queue.append(item())
    CampaignRunner(ports).run(campaign(), lambda selected: spec(selected))
    assert ports.tracker.proposals[0].question == consequence
    return ports.workspace.reports[0].content


_LINE = re.compile(r"[^\r\n]*")
_LINE_ENDING = re.compile(r"\r\n|\r|\n")


def _lines(text: str) -> list[tuple[str, int, int]]:
    """Each line of ``text`` as (content, start, content end), ending a line at
    ``\\r\\n``, a lone ``\\r`` or a lone ``\\n`` the way CommonMark does."""

    lines: list[tuple[str, int, int]] = []
    position = 0
    while True:
        end = _LINE.match(text, position).end()
        lines.append((text[position:end], position, end))
        ending = _LINE_ENDING.match(text, end)
        if ending is None:
            return lines
        position = ending.end()


def _is_closing(line: str, fence: str) -> bool:
    return re.fullmatch(rf"`{{{len(fence)},}}", line) is not None


def _subsection(report: str, heading: str) -> str:
    """From ``heading`` to the next section heading outside a fence."""

    text = report[report.index(heading) :]
    inside = ""
    for index, (line, start, _) in enumerate(_lines(text)):
        fence = re.match(r"^(`{3,})", line)
        if fence and not inside:
            inside = fence.group(1)
        elif inside and _is_closing(line, inside):
            inside = ""
        elif not inside and (line.startswith("## ") or (index and line.startswith("### "))):
            return text[:start]
    return text


def _fenced_bodies(text: str) -> list[str]:
    """The body of every fenced block in ``text``, read the way a Markdown
    reader closes a fence: on a line of at least as many backticks. A body is
    the exact characters between the opening and the closing fence line."""

    bodies: list[str] = []
    lines = _lines(text)
    index = 0
    while index < len(lines):
        opening = re.match(r"^(`{3,})", lines[index][0])
        if opening is None:
            index += 1
            continue
        fence = opening.group(1)
        first = index + 1
        index = first
        while not _is_closing(lines[index][0], fence):
            index += 1
        bodies.append(text[lines[first][1] : lines[index - 1][2]] if index > first else "")
        index += 1
    return bodies


def _outside_fences(text: str) -> list[str]:
    inside = ""
    kept: list[str] = []
    for line, _, _ in _lines(text):
        fence = re.match(r"^(`{3,})", line)
        if fence and not inside:
            inside = fence.group(1)
        elif inside and _is_closing(line, inside):
            inside = ""
        elif not inside:
            kept.append(line)
    return kept


def _sections(text: str) -> list[str]:
    return [line for line in _outside_fences(text) if line.startswith("## ")]


def test_the_report_keeps_a_long_finding_and_its_evidence_reference_verbatim() -> None:
    consequence = long_consequence()

    report = _report(consequence)

    section = _subsection(report, "### proposal-1 Follow-up: item-1 CACHE")
    assert "- Parent item: item-1" in section
    assert "- Finding code: CACHE" in section
    assert "- Blocking: no" in section
    assert "- Evidence reference: src/cache.py:41-58" in section
    question, rationale = _fenced_bodies(section)
    assert question == consequence
    assert question[:-1] != consequence
    assert "non-blocking reviewer finding CACHE" in rationale


def test_a_refill_proposal_writes_none_for_its_finding_fields() -> None:
    from autobuild.domain import RefillPlan

    ports = make_ports([], [], diff_count=0)
    refill = RefillPlan(proposals=(Proposal("candidate", "sharp question?", "reason", "brief"),))
    CampaignRunner(ports).run(campaign(refill_enabled=True), lambda selected: spec(selected), refill)

    section = _subsection(ports.workspace.reports[0].content, "### proposal-1 candidate")
    for field in ("Parent item", "Finding code", "Blocking", "Evidence reference"):
        assert f"- {field}: none" in section
    assert _fenced_bodies(section) == ["sharp question?", "reason"]


def _evidence(findings: tuple[ReviewFinding, ...]) -> CloseEvidence:
    workspace = WorkspaceRef(Path("/worktree"), "run", "base", "lease")
    diff = DiffEvidence(workspace, "revision", (), "patch")
    result = CommandResult("command", 0, "stdout", "stderr", "start", "end")
    return CloseEvidence(
        "item-1",
        "revision",
        diff,
        ValidationEvidence("tests", "revision", result, ()),
        ReviewVerdict("item-1", ReviewDecision.PASS, findings, "verdict:pass"),
        "trajectory",
    )


def _review_findings(briefing: str) -> str:
    start = briefing.index("## Review findings")
    return briefing[start : briefing.index("\n## Changed paths", start)]


def test_a_trailing_line_break_survives_the_fence() -> None:
    assert _fenced_bodies(fenced_block("text\n")) == ["text\n"]
    assert _fenced_bodies(fenced_block("text")) == ["text"]


def test_the_close_briefing_writes_every_finding_in_full() -> None:
    first = long_consequence()
    second = "A second, blocking consequence.\nWith a line | break."
    briefing = PinaxTrackerAdapter._briefing(
        _evidence(
            (
                ReviewFinding("CACHE", first, "src/cache.py:41-58", blocking=False),
                ReviewFinding("EDGE", second, "src/edge.py:7", blocking=True),
            )
        ),
        "commit",
    )

    assert _sections(briefing) == [
        "## Outcome", "## Evidence", "## Review findings", "## Changed paths"
    ]
    findings = _review_findings(briefing)
    assert "### CACHE\n\n- Blocking: no\n- Evidence reference: src/cache.py:41-58" in findings
    assert "### EDGE\n\n- Blocking: yes\n- Evidence reference: src/edge.py:7" in findings
    assert _fenced_bodies(findings) == [first, second]


def test_a_close_briefing_without_findings_writes_none() -> None:
    briefing = PinaxTrackerAdapter._briefing(_evidence(()), "commit")

    assert "## Review findings\n\nnone\n\n## Changed paths" in briefing


def test_the_close_briefing_names_the_builder_lane_when_it_is_known() -> None:
    briefing = PinaxTrackerAdapter._briefing(replace(_evidence(()), builder_lane="two"), "commit")

    evidence = briefing[briefing.index("## Evidence") : briefing.index("## Review findings")]
    assert "- Builder lane: `two`" in evidence.splitlines()
    assert evidence.endswith("- Trajectory: `trajectory`\n- Builder lane: `two`\n\n")


def test_a_close_briefing_without_a_builder_lane_has_no_lane_line() -> None:
    briefing = PinaxTrackerAdapter._briefing(_evidence(()), "commit")

    assert "Builder lane" not in briefing
    assert "- Trajectory: `trajectory`\n\n## Review findings" in briefing


def test_a_heading_and_backticks_in_the_text_stay_inside_one_fenced_block() -> None:
    report = _report(EDGES)
    briefing = PinaxTrackerAdapter._briefing(
        _evidence((ReviewFinding("EDGE", EDGES, "src/edge.py:7", blocking=False),)), "commit"
    )
    plain_report = _report("plain text")

    assert _sections(report) == _sections(plain_report)
    assert _sections(briefing) == [
        "## Outcome", "## Evidence", "## Review findings", "## Changed paths"
    ]
    assert f"`````text\n{EDGES}\n`````" in report
    assert f"`````text\n{EDGES}\n`````" in briefing
    assert _fenced_bodies(_subsection(report, "### proposal-1"))[0] == EDGES
    assert _fenced_bodies(_review_findings(briefing)) == [EDGES]


def test_a_fence_is_never_shorter_than_three_backticks() -> None:
    assert fenced_block("no ticks") == "```text\nno ticks\n```"
    assert fenced_block("a ` b") == "```text\na ` b\n```"
    assert fenced_block("a ``` b") == "````text\na ``` b\n````"


class _RecordingPinax(PinaxTrackerAdapter):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.commands: list[tuple[str, ...]] = []

    def _on_primary(self, check, *args: str):  # type: ignore[override]
        self.commands.append(args)
        return {"item_id": "abp-ab12"}, TrackerPublication(True, "")


def _proposal(brief_ref: str, parent: str = "abd-zz01") -> Proposal:
    return Proposal(
        "Follow-up: abd-zz01 CACHE",
        long_consequence(),
        "non-blocking reviewer finding CACHE",
        brief_ref,
        parent_item=parent,
        evidence_ref="src/cache.py:41-58",
        finding_code="CACHE",
        blocking=False,
    )


def _caption(adapter: _RecordingPinax) -> str:
    note = next(args for args in adapter.commands if args[:2] == ("note", "add"))
    return note[note.index("--caption") + 1]


def test_the_caption_points_to_the_report_section_and_the_parent(tmp_path: Path) -> None:
    adapter = _RecordingPinax(tmp_path)

    adapter.propose(_proposal("docs/campaigns/campaign-1.md"), actor="coordinator")

    caption = _caption(adapter)
    assert len(caption) <= 200
    assert caption == (
        "Full text: docs/campaigns/campaign-1.md, section abp-ab12; "
        "parent abd-zz01 close briefing"
    )
    note = next(args for args in adapter.commands if args[:2] == ("note", "add"))
    assert note[note.index("--ref") + 1] == "docs/campaigns/campaign-1.md"


def test_a_long_brief_ref_drops_the_parent_clause(tmp_path: Path) -> None:
    adapter = _RecordingPinax(tmp_path)
    brief_ref = "docs/campaigns/" + "c" * 140 + ".md"

    adapter.propose(_proposal(brief_ref), actor="coordinator")

    caption = _caption(adapter)
    assert len(caption) <= 200
    assert caption == f"Full text: {brief_ref}, section abp-ab12"


def test_a_brief_ref_too_long_for_any_pointer_runs_no_pinax_command(tmp_path: Path) -> None:
    adapter = _RecordingPinax(tmp_path)

    with pytest.raises(EvidenceError):
        adapter.propose(_proposal("docs/campaigns/" + "c" * 190 + ".md"), actor="coordinator")

    assert adapter.commands == []


def test_the_backlog_row_keeps_a_long_question_whole(tmp_path: Path) -> None:
    (tmp_path / "BACKLOG.md").write_text(
        "# Backlog\n\n| Item | Title | Status | Brief |\n|---|---|---|---|\n"
        "| TST-001 | First | Ready | docs/brief.md |\n",
        encoding="utf-8",
    )
    adapter = BacklogTrackerAdapter(tmp_path)
    adapter._commit_primary = lambda message: "revision"  # type: ignore[method-assign]
    adapter._publish_primary = lambda revision: None  # type: ignore[method-assign]
    question = " ".join(f"word{index}" for index in range(300))
    assert len(question) >= 1500

    adapter.propose(
        Proposal(
            "Follow-up: TST-001 CACHE",
            question,
            "non-blocking reviewer finding CACHE",
            "docs/campaigns/campaign.md",
            evidence_ref="src/cache.py:41-58",
        ),
        actor="coordinator",
    )

    row = (tmp_path / "BACKLOG.md").read_text(encoding="utf-8").splitlines()[-1]
    assert f"Proposed: {question} non-blocking reviewer finding CACHE" in row
    assert "Evidence: src/cache.py:41-58" in row


def test_the_proposed_event_keeps_the_evidence_reference_and_the_full_consequence() -> None:
    consequence = long_consequence()
    verdict = ReviewVerdict(
        "item-1",
        ReviewDecision.PASS,
        (ReviewFinding("CACHE", consequence, "src/cache.py:41-58", blocking=False),),
        "verdict:pass",
    )
    ports = make_ports([builder(), _review_result(verdict)], [command()], diff_count=1)
    ports.tracker.queue.append(item())

    CampaignRunner(ports).run(campaign(), lambda selected: spec(selected))

    (event,) = [e for e in ports.records.events if e.event_type == "item.follow_up_proposed"]
    assert event.payload["evidence_ref"] == "src/cache.py:41-58"
    assert event.payload["consequence"] == consequence


def _brief_ref_for_pointer(adapter: _RecordingPinax, length: int) -> str:
    """A brief_ref whose pointer, with an eight-character id suffix, is ``length``
    characters long."""

    fixed = len("Full text: , section ") + len(adapter._proposal_prefix) + 1 + 8
    brief_ref = "docs/items/" + "b" * (length - fixed - len("docs/items/.md")) + ".md"
    assert len(f"Full text: {brief_ref}, section {adapter._proposal_prefix}-abcdefgh") == length
    return brief_ref


def test_the_caption_check_accepts_a_pointer_that_fits_an_eight_character_suffix(
    tmp_path: Path,
) -> None:
    adapter = _RecordingPinax(tmp_path)
    brief_ref = _brief_ref_for_pointer(adapter, 200)
    proposal = _proposal(brief_ref, parent="")
    assert proposal.report_ref == ""

    adapter.propose(proposal, actor="coordinator")

    assert adapter.commands[0][0] == "add"
    assert _caption(adapter) == f"Full text: {brief_ref}, section abp-ab12"


def test_the_caption_check_refuses_a_pointer_one_past_an_eight_character_suffix(
    tmp_path: Path,
) -> None:
    adapter = _RecordingPinax(tmp_path)
    proposal = _proposal(_brief_ref_for_pointer(adapter, 201), parent="")
    assert proposal.report_ref == ""

    with pytest.raises(EvidenceError):
        adapter.propose(proposal, actor="coordinator")

    assert adapter.commands == []


def test_a_refill_caption_points_to_the_report_and_its_note_links_its_brief(
    tmp_path: Path,
) -> None:
    from autobuild.domain import RefillPlan

    recorder = _RecordingPinax(tmp_path)
    ports = make_ports([], [], diff_count=0)
    ports.tracker.propose = recorder.propose  # type: ignore[method-assign]
    refill = RefillPlan(
        proposals=(Proposal("candidate", "sharp question?", "reason", "docs/items/refill-x.md"),)
    )

    CampaignRunner(ports).run(campaign(refill_enabled=True), lambda selected: spec(selected), refill)

    assert _caption(recorder) == "Full text: docs/campaigns/campaign.md, section abp-ab12"
    note = next(args for args in recorder.commands if args[:2] == ("note", "add"))
    assert note[note.index("--ref") + 1] == "docs/items/refill-x.md"
    assert "### abp-ab12 candidate" in ports.workspace.reports[0].content


def test_follow_ups_registered_before_a_failed_registration_keep_their_section() -> None:
    findings = tuple(
        ReviewFinding(code, f"{code} consequence", f"src/{code}.py:1", blocking=False)
        for code in ("FIRST", "SECOND", "THIRD")
    )
    verdict = ReviewVerdict("item-1", ReviewDecision.PASS, findings, "verdict:pass")
    ports = make_ports([builder(), _review_result(verdict)], [command()], diff_count=1)
    ports.tracker.queue.append(item())
    registered = ports.tracker.propose
    attempts: list[str] = []

    def propose(proposal: Proposal, actor: str):
        attempts.append(proposal.finding_code)
        if proposal.finding_code == "SECOND":
            raise RuntimeError("pinax note add failed")
        return registered(proposal, actor)

    ports.tracker.propose = propose  # type: ignore[method-assign]

    outcome = CampaignRunner(ports).run(campaign(), lambda selected: spec(selected))

    (accepted,) = outcome.items
    assert accepted.disposition.value == "accepted"
    assert [f.proposal.finding_code for f in accepted.follow_ups] == ["FIRST"]
    assert attempts == ["FIRST", "SECOND"]
    failed = [e for e in ports.records.events if e.event_type == "item.follow_up_failed"]
    assert len(failed) == 1
    report = ports.workspace.reports[0].content
    follow_ups = report[report.index("## Follow-ups") :]
    follow_ups = follow_ups[: follow_ups.index("\n## ", 1)]
    assert "### proposal-1 Follow-up: item-1 FIRST" in follow_ups
    assert "SECOND" not in follow_ups and "THIRD" not in follow_ups


@pytest.mark.parametrize("separator", ["\n", "\r", "\r\n"], ids=["lf", "cr", "crlf"])
def test_a_line_break_in_a_code_or_evidence_reference_cannot_open_a_section(
    separator: str,
) -> None:
    code = f"CACHE{separator}## Edges"
    evidence_ref = f"src/cache.py:41{separator}## Edges"

    report = _report("plain text", evidence_ref=evidence_ref, code=code)
    plain_report = _report("plain text")
    briefing = PinaxTrackerAdapter._briefing(
        _evidence((ReviewFinding(code, "plain text", evidence_ref, blocking=False),)), "commit"
    )

    assert _sections(report) == _sections(plain_report)
    assert _sections(briefing) == [
        "## Outcome", "## Evidence", "## Review findings", "## Changed paths"
    ]
    for document in (report, briefing):
        assert not [line for line in _outside_fences(document) if line.startswith("## Edges")]
        bodies = _fenced_bodies(document)
        assert evidence_ref in bodies
        assert code in bodies
    heading = "### proposal-1 Follow-up: item-1 CACHE ## Edges"
    assert heading in [line for line, _, _ in _lines(report)]
    assert f"{heading}\n" in report
    section = _subsection(report, heading)
    assert "- Parent item: item-1" in section
    assert f"- Finding code:\n\n```text\n{code}\n```" in section
    assert f"- Evidence reference:\n\n```text\n{evidence_ref}\n```" in section
    findings = _review_findings(briefing)
    assert findings.startswith("## Review findings\n\n### CACHE ## Edges\n")
    assert f"- Finding code:\n\n```text\n{code}\n```" in findings
    assert f"- Evidence reference:\n\n```text\n{evidence_ref}\n```" in findings
