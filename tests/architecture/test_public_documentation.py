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


def test_operator_guide_covers_the_complete_public_setup() -> None:
    guide = (ROOT / "docs" / "running-autobuild.md").read_text(encoding="utf-8")

    for heading in (
        "## Install AutoBuild",
        "## Set up AutoBuild on macOS",
        "## Install and authenticate one coding assistant",
        "### GitHub Copilot CLI",
        "## Write an approved item brief",
        "## Set up Pinax",
        "## Set up BACKLOG.md",
        "## Create the project profile",
        "## Run a campaign",
        "## Read the result",
        "## Supply proposal-only refill",
        "## If a run is interrupted",
        "## Common startup failures",
        "## Technical documentation",
    ):
        assert heading in guide
    assert "| Item | Title | Status | Brief |" in guide
    assert 'kind = "auto"' in guide
    assert "--allow-delivery" in guide
    assert "Platform and coding assistant are separate choices" in (
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
        "## Tests that enforce the design",
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
    for path in (ROOT / "README.md", *(ROOT / "docs").glob("*.md")):
        content = path.read_text(encoding="utf-8")
        assert "\u2013" not in content
        assert "\u2014" not in content
