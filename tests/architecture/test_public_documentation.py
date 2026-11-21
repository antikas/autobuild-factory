from __future__ import annotations

import ast
import inspect
import re
import textwrap
import tomllib
from pathlib import Path

from autobuild.adapters.claude_harness import ClaudeCodeHarnessAdapter
from autobuild.adapters.codex_harness import CodexHarnessAdapter
from autobuild.adapters.copilot_harness import CopilotCliHarnessAdapter
from autobuild.application.campaign import CampaignRunner


ROOT = Path(__file__).resolve().parents[2]


def test_coordinated_adapter_guides_match_harness_effort_options() -> None:
    adapters = {
        "claude-code.md": (ClaudeCodeHarnessAdapter, "<level>"),
        "codex.md": (CodexHarnessAdapter, "<effort>"),
        "github-copilot.md": (CopilotCliHarnessAdapter, "<effort>"),
    }

    for guide_name, (adapter, placeholder) in adapters.items():
        effort_option = " ".join(adapter._effort_option(None, placeholder))
        guide = (ROOT / "skills" / "autobuild-coordinated" / "adapters" / guide_name).read_text(
            encoding="utf-8"
        )
        assert effort_option in guide


def public_paths() -> frozenset[str] | None:
    manifest_path = ROOT / "release" / "public-manifest.toml"
    if not manifest_path.exists():
        return None
    manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    include_files = set(manifest["include_files"])
    include_trees = tuple(manifest["include_trees"])
    exclude_files = set(manifest["exclude_files"])
    exclude_trees = tuple(manifest["exclude_trees"])
    paths = set(include_files)
    for tree in include_trees:
        paths.update(
            path.relative_to(ROOT).as_posix()
            for path in (ROOT / tree).rglob("*")
            if path.is_file()
        )
    paths.difference_update(exclude_files)
    paths = {
        path
        for path in paths
        if not any(path == tree or path.startswith(f"{tree}/") for tree in exclude_trees)
    }
    return frozenset(paths)


def public_markdown() -> tuple[Path, ...]:
    return (
        ROOT / "README.md",
        *(ROOT / "docs").glob("*.md"),
        *(ROOT / "skills").rglob("*.md"),
    )


def projected_markdown() -> tuple[Path, ...]:
    candidates = public_markdown()
    selected = public_paths()
    if selected is None:
        return candidates
    return tuple(
        path for path in candidates if path.relative_to(ROOT).as_posix() in selected
    )


def test_operator_guide_covers_the_complete_public_setup() -> None:
    guide = (ROOT / "docs" / "running-autobuild.md").read_text(encoding="utf-8")

    for heading in (
        "## Install AutoBuild",
        "## Stage 1: plan and register the work",
        "## Stage 2: execute the approved queue",
        "## Set up AutoBuild on macOS",
        "## Install and authenticate one coding assistant",
        "### GitHub Copilot CLI",
        "## Write an approved item brief",
        "## Set up Pinax",
        "## Set up BACKLOG.md",
        "## Create the project profile",
        "## Run a campaign",
        "## Campaign result",
        "## Supply proposal-only refill",
        "## Recover an interrupted run",
        "## Common startup failures",
        "## Technical documentation",
    ):
        assert heading in guide
    assert "| Item | Title | Status | Brief |" in guide
    assert 'kind = "auto"' in guide
    assert "--allow-delivery" in guide
    assert "`autobuild-plan`" in guide
    assert "The Python wheel installs the `autobuild` command only" in guide
    assert guide.index("## Stage 1: plan and register the work") < guide.index(
        "## Stage 2: execute the approved queue"
    )
    assert "It does not start a builder or change product code." in guide
    assert "Platform and coding assistant are separate choices" in (
        ROOT / "README.md"
    ).read_text(encoding="utf-8")
    assert "`skills/autobuild-plan/`" in (
        ROOT / "README.md"
    ).read_text(encoding="utf-8")


def test_architecture_guide_covers_boundaries_state_and_evidence() -> None:
    guide = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")

    for heading in (
        "## Dependency direction",
        "## Runtime composition",
        "## Port contracts",
        "## Campaign state",
        "## Item state",
        "## Evidence chain",
        "## Git delivery model",
        "## Tracker adapters",
        "## Extension points",
        "## Architecture tests",
    ):
        assert heading in guide
    for port in (
        "TrackerPort",
        "WorkspacePort",
        "HarnessPort",
        "CommandPort",
        "RunRecordPort",
        "KnowledgePort",
    ):
        assert port in guide
    assert "## Entry skills and executable" in guide
    assert "`autobuild-plan`" in guide


def test_public_markdown_links_resolve_and_private_paths_do_not_escape() -> None:
    selected = public_paths()
    for path in public_markdown():
        content = path.read_text(encoding="utf-8")
        assert "~/knowledge/" not in content
        assert "~/.claude/" not in content
        for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", content):
            if target.startswith(("http://", "https://", "#")):
                continue
            local = (path.parent / target.split("#", 1)[0]).resolve(strict=False)
            assert local.exists(), f"{path.relative_to(ROOT)} has missing link {target}"
            source = path.relative_to(ROOT).as_posix()
            if selected is not None and source in selected:
                projected = local.relative_to(ROOT).as_posix()
                assert projected in selected, (
                    f"{source} links to excluded public path {projected}"
                )


def test_public_documentation_uses_plain_ascii_punctuation() -> None:
    for path in projected_markdown():
        content = path.read_text(encoding="utf-8")
        assert "\u2013" not in content
        assert "\u2014" not in content


def test_public_documentation_uses_direct_headings() -> None:
    for path in projected_markdown():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("#"):
                assert re.match(r"^#{1,6} (What|How|For)\b", line) is None, (
                    f"{path.relative_to(ROOT)} has a canned heading: {line}"
                )


def test_pinax_mentions_point_to_the_public_repository() -> None:
    public_url = "https://github.com/antikas/pinax-tracker"
    for path in (
        ROOT / "README.md",
        ROOT / "docs" / "architecture.md",
        ROOT / "docs" / "running-autobuild.md",
        ROOT / "skills" / "autobuild" / "SKILL.md",
        ROOT / "skills" / "autobuild-plan" / "SKILL.md",
    ):
        assert public_url in path.read_text(encoding="utf-8"), path.relative_to(ROOT)


def test_tracker_commit_hand_over_is_documented_where_coordinators_and_operators_read() -> None:
    skill_root = ROOT / "skills" / "autobuild-coordinated"
    skill = (skill_root / "SKILL.md").read_text(encoding="utf-8")
    rules = (skill_root / "rules.md").read_text(encoding="utf-8")
    guide = (ROOT / "docs" / "running-autobuild.md").read_text(encoding="utf-8")
    steps = {line.split(".", 1)[0]: line for line in skill.splitlines() if line[:3] in {"1. ", "10."}}
    tracker_section = skill.split("## Tracker commands", 1)[1].split("\n## ", 1)[0]

    assert "never commits `.ergon` by hand" in steps["1"]
    assert "the claim is accepted and published with the pull request" in steps["1"]
    assert "not visible to other machines until it merges" in tracker_section
    assert "states the cause Pinax reports" in tracker_section
    assert "On `BACKLOG.md`, mark the item in progress and commit the change" in steps["1"]
    assert "Pinax commits the close itself" in steps["10"]
    assert "On `BACKLOG.md`, set the row to `Done (<commit>)` and commit" in steps["10"]
    assert "Pinax 0.2.1 or later commits every mutating command itself" in tracker_section
    assert "never stages or commits `.ergon` by hand" in tracker_section
    assert "the coordinator commits the change" in tracker_section
    assert "never stages or commits `.ergon` by hand" in rules
    assert "A `BACKLOG.md` change is still committed by the coordinator" in rules
    for guide_name in ("claude-code.md", "codex.md", "github-copilot.md"):
        adapter = (skill_root / "adapters" / guide_name).read_text(encoding="utf-8")
        assert "never commits `.ergon` by hand" in adapter
    assert "AutoBuild 0.7.0 needs pinax-tracker 0.2.1 or later" in guide
    assert "Do not use AutoBuild 0.6.0 or earlier with pinax-tracker 0.2.1 or later" in guide
    assert "tracker_environment" in guide and "tracker_refused" in guide
    assert "other machines do not see the claim until the pull request merges" in guide
    assert "the message states the cause Pinax reported" in guide


def test_test_commands_use_the_current_uv_certificate_flag() -> None:
    offending = [
        f"{name}:{number}"
        for name in ("AGENTS.md", "README.md", "docs/running-autobuild.md")
        # AGENTS.md is private context and absent from the public tree.
        if (ROOT / name).is_file()
        for number, line in enumerate(
            (ROOT / name).read_text(encoding="utf-8").splitlines(), start=1
        )
        if "--native-tls" in line
    ]
    assert offending == [], f"deprecated uv flag --native-tls in: {', '.join(offending)}"


def test_campaign_result_lists_every_unbuilt_reason_the_report_can_print() -> None:
    tree = ast.parse(textwrap.dedent(inspect.getsource(CampaignRunner._unbuilt_reason)))
    reasons = {
        node.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Return)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    assert reasons, "no string literal returned by CampaignRunner._unbuilt_reason"

    guide = (ROOT / "docs" / "running-autobuild.md").read_text(encoding="utf-8")
    start = guide.index("When the campaign runs with an allow-list")
    sentence = guide[start : guide.index(".", start)]
    reason_list = re.sub(r"\s*\([^)]*\)", "", sentence.split(": ", 1)[1])
    documented = {
        entry.strip().removeprefix("or ").strip() for entry in reason_list.split(",")
    }

    missing = sorted(reasons - documented)
    assert missing == [], f"unbuilt reasons missing from Campaign result: {', '.join(missing)}"
