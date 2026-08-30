from __future__ import annotations

import re
import tomllib
from pathlib import Path


ROOT = Path(__file__).parents[2]


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
        *(ROOT / "skills").glob("*/SKILL.md"),
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
