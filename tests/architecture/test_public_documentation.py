from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import inspect
import re
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path

import pytest

from autobuild.adapters.claude_harness import ClaudeCodeHarnessAdapter
from autobuild.adapters.codex_harness import CodexHarnessAdapter
from autobuild.adapters.copilot_harness import CopilotCliHarnessAdapter
from autobuild.adapters.tracker_commits import SELF_COMMITTING_TRACKER_REQUIRED
from autobuild.application.campaign import CampaignRunner
from autobuild.bootstrap.profile import _BOUNDARY_KEYS, _PROFILE_ALLOWED_KEYS
from autobuild.cli import _parser
from autobuild.domain import CampaignStopReason


ROOT = Path(__file__).resolve().parents[2]

PAGES = {
    "README.md": "aa3a2848614377d6947e0ad74f5d60e7ab51af9db751c82ce8191f139204cad3",
    "docs/architecture.md": "192cf8f44fc33d342c8293dedfb256161afd22aa006c914d8df6e1b9c4dc5f84",
    "docs/getting-started.md": "d665bf418b6334cd3c498030fa4d1274d8bf91543b12d6361a1a7c7dd401cedf",
    "docs/lanes-and-usage.md": "00043e48c64d8521143342af6fe63b3e787672c6043270b7f4cab7470d5c986f",
    "docs/platforms.md": "e9223fe76cdb8d68b131fad51f5319085d6ef4e6633d9554bd9937aa36e8d0da",
    "docs/reference/commands.md": "2c9df29c4e8c7ce67d08ce1bd9e023285539ac76afea27ffacebd1a6c67c7851",
    "docs/reference/events-and-records.md": "3e04b5c04cb01834673f5e4983903fccaefaa272042de746e9a77de117f3461a",
    "docs/reference/harness-adapters.md": "7d9a0f68f7756f361bdcdaef11b2ee07ddd8d8684604006645e5ebbd1afdf801",
    "docs/reference/profile.md": "ad03b54bf53eb9c733ff921991241cc2875fbf5c2aab4be2046ea4b797e1264d",
    "docs/running-campaigns.md": "648ed822a066acc13186e3b0caed3c7cd223e966f6629033edca89e7ca9e5912",
    "docs/trackers.md": "a8fccdec54d198dad51a8e1934086797f04dd5285f8746afd56501d9e4904b8e",
    "docs/writing-briefs.md": "d824da8ec47485d544bc349e0c0257e11764fc86b71a9435063742edc750325f",
}
REMOVED_PAGES = tuple(f"docs/{name}.md" for name in ("running-" "autobuild", "harness-adapters"))
FENCE = re.compile(r"^(```+)[^\n]*\n.*?^\1[ \t]*$\n?", re.MULTILINE | re.DOTALL)


def page(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def outside_fences(markdown: str) -> str:
    return FENCE.sub("", markdown)


def section(markdown: str, heading: str) -> str:
    """The body under ``heading`` up to the next heading of its level or above."""

    level = len(heading) - len(heading.lstrip("#"))
    _, found, body = f"\n{markdown}".partition(f"\n{heading}\n")
    assert found, f"no heading {heading}"
    # Headings inside a code fence are comments, so they must not end a section.
    masked = FENCE.sub(lambda match: re.sub(r"[^\n]", "x", match.group()), body)
    end = re.search(rf"^#{{1,{level}}} ", masked, re.MULTILINE)
    return body[: end.start()] if end else body


def table_column(markdown: str) -> list[str]:
    """The first cell of every table row whose first cell opens with code."""

    return re.findall(r"^\| `([^`]+)`[^|]*\|", markdown, re.MULTILINE)


def anchors(markdown: str) -> set[str]:
    found: set[str] = set()
    counts: dict[str, int] = {}
    for heading in re.findall(r"^#{1,6} (.*)$", outside_fences(markdown), re.MULTILINE):
        slug = re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")
        count = counts.get(slug, 0)
        counts[slug] = count + 1
        found.add(f"{slug}-{count}" if count else slug)
    return found


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
    """Every Markdown file of the public selection; without the manifest, of the public tree."""

    selected = public_paths()
    if selected is not None:
        return tuple(ROOT / name for name in sorted(selected) if name.endswith(".md"))
    return (
        ROOT / "README.md",
        *(
            path
            for tree in ("docs", "skills", "src", "tests")
            for path in sorted((ROOT / tree).rglob("*.md"))
        ),
    )


def test_the_documentation_tests_read_every_page() -> None:
    read = {path.relative_to(ROOT).as_posix() for path in public_markdown()}
    assert set(PAGES) <= read
    assert {name for name in read if name == "README.md" or name.startswith("docs/")} == set(PAGES)


def test_the_pages_are_the_reviewed_pages() -> None:
    if public_paths() is None:
        pytest.skip("the reviewed digests bind the source tree only")
    actual = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in PAGES}
    assert actual == PAGES
    for name in REMOVED_PAGES:
        assert not (ROOT / name).exists(), name


def test_nothing_public_names_a_removed_page() -> None:
    names = public_paths()
    if names is None:
        names = frozenset(
            path.relative_to(ROOT).as_posix()
            for tree in ("docs", "skills", "src", "tests")
            for path in (ROOT / tree).rglob("*")
            if path.is_file()
        ) | {"README.md"}
    removed = re.compile("running-" "autobuild|docs/" "harness-adapters")
    offenders = [
        name
        for name in sorted(names)
        if "__pycache__" not in name
        and removed.search((ROOT / name).read_bytes().decode("utf-8", "replace"))
    ]
    assert offenders == []


def test_the_export_selects_the_pages_and_classifies_every_path() -> None:
    exporter = ROOT / "release" / "export_public.py"
    if not exporter.exists():
        pytest.skip("the exporter is private and absent from the public tree")
    spec = importlib.util.spec_from_file_location("export_public", exporter)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        del sys.modules[spec.name]

    def listed(*arguments: str) -> set[str]:
        raw = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "-z", *arguments], capture_output=True, check=True
        ).stdout
        return {name for name in raw.decode("utf-8").split("\0") if name}

    # A commit of the checkout holds its untracked sources and lacks its deleted files.
    untracked = listed(
        "--others", "--exclude-standard", "--", "README.md", "docs", "skills", "src", "tests"
    )
    entries = tuple(
        module.TreeEntry("100644", "blob", "", name)
        for name in sorted((listed() | untracked) - listed("--deleted"))
    )
    manifest = tomllib.loads(page(module.MANIFEST_PATH))

    selected = {entry.path for entry in module._selected_entries(entries, manifest)}

    assert {name for name in selected if name == "README.md" or name.startswith("docs/")} == set(PAGES)
    assert not selected & set(REMOVED_PAGES)
    assert {
        "docs/macos-github-copilot-proof.md",
        "docs/platform-proof.md",
        "docs/platform-proof-results.md",
    } <= set(manifest["exclude_files"])


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


def test_public_markdown_holds_no_private_path() -> None:
    for path in public_markdown():
        content = path.read_text(encoding="utf-8")
        assert "~/knowledge/" not in content
        assert "~/.claude/" not in content


def test_public_markdown_links_and_anchors_resolve() -> None:
    selected = public_paths()
    checked = 0
    for path in public_markdown():
        source = path.relative_to(ROOT).as_posix()
        prose = re.sub(r"`[^`\n]*`", "", outside_fences(path.read_text(encoding="utf-8")))
        for target in re.findall(r"\]\(([^)\s]+)\)", prose):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            checked += 1
            name, _, anchor = target.partition("#")
            local = (path.parent / name).resolve(strict=False) if name else path
            assert local.exists(), f"{source} has missing link {target}"
            if selected is not None:
                projected = local.relative_to(ROOT).as_posix()
                assert projected in selected or any(
                    chosen.startswith(f"{projected}/") for chosen in selected
                ), f"{source} links to excluded public path {projected}"
            if anchor and local.suffix == ".md":
                assert anchor in anchors(local.read_text(encoding="utf-8")), (
                    f"{source} links to missing anchor {target}"
                )
    assert checked


def test_public_documentation_uses_plain_ascii_punctuation() -> None:
    for path in public_markdown():
        content = path.read_text(encoding="utf-8")
        for dash in ("\u2013", "\u2014"):
            assert dash not in content, f"{path.relative_to(ROOT)} holds {ascii(dash)}"


def test_public_documentation_uses_direct_headings() -> None:
    for path in public_markdown():
        for line in outside_fences(path.read_text(encoding="utf-8")).splitlines():
            assert re.match(r"^#{1,6} (What|How|For)\b", line) is None, (
                f"{path.relative_to(ROOT)} has a canned heading: {line}"
            )


def test_pinax_mentions_point_to_the_public_repository() -> None:
    public_url = "https://github.com/antikas/pinax-tracker"
    mentions = {name for name in PAGES if re.search(r"\bpinax\b", page(name), re.IGNORECASE)}
    linked = {name for name in mentions if public_url in page(name)}

    assert mentions == {
        "README.md",
        "docs/architecture.md",
        "docs/getting-started.md",
        "docs/platforms.md",
        "docs/reference/commands.md",
        "docs/reference/events-and-records.md",
        "docs/reference/profile.md",
        "docs/running-campaigns.md",
        "docs/trackers.md",
        "docs/writing-briefs.md",
    }
    assert {"README.md", "docs/architecture.md", "docs/trackers.md"} <= linked
    for name in ("skills/autobuild/SKILL.md", "skills/autobuild-plan/SKILL.md"):
        assert public_url in page(name), name


def test_the_tracker_guide_states_the_pinax_version_the_code_requires() -> None:
    version = re.compile(r"pinax-tracker (\d+\.\d+\.\d+)")
    required = set(version.findall(SELF_COMMITTING_TRACKER_REQUIRED))
    assert len(required) == 1
    assert set(version.findall(page("docs/trackers.md"))) == required


def test_tracker_commit_hand_over_is_documented_where_coordinators_read() -> None:
    skill_root = ROOT / "skills" / "autobuild-coordinated"
    skill = (skill_root / "SKILL.md").read_text(encoding="utf-8")
    rules = (skill_root / "rules.md").read_text(encoding="utf-8")
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


def test_test_commands_use_the_current_uv_certificate_flag() -> None:
    offending = [
        f"{name}:{number}"
        for name in ("AGENTS.md", *PAGES)
        # AGENTS.md is private context and absent from the public tree.
        if (ROOT / name).is_file()
        for number, line in enumerate(
            (ROOT / name).read_text(encoding="utf-8").splitlines(), start=1
        )
        if "--native-tls" in line
    ]
    assert offending == [], f"deprecated uv flag --native-tls in: {', '.join(offending)}"


def test_the_pages_list_every_unbuilt_reason_the_report_can_print() -> None:
    source, first_line = inspect.getsourcelines(CampaignRunner._unbuilt_reason)
    tree = ast.parse(textwrap.dedent("".join(source)))
    returns = [node for node in ast.walk(tree) if isinstance(node, ast.Return)]
    # Only a literal return can be checked against the guide, so any other
    # return is refused rather than traced.
    not_literal = [
        f"line {first_line + node.lineno - 1}"
        for node in returns
        if not (isinstance(node.value, ast.Constant) and isinstance(node.value.value, str))
    ]
    assert not_literal == [], (
        "CampaignRunner._unbuilt_reason returns a reason that is not a string literal "
        f"at {', '.join(not_literal)} of {inspect.getsourcefile(CampaignRunner)}"
    )
    reasons = {node.value.value for node in returns}
    assert reasons, "no string literal returned by CampaignRunner._unbuilt_reason"

    sentences = {
        "docs/running-campaigns.md": "The reason for an allowed item left unbuilt is one of ",
        "docs/reference/events-and-records.md": "for each allowed item that was not built, or `none`. The reason is one of ",
    }
    for name, opening in sentences.items():
        _, found, rest = page(name).partition(opening)
        assert found, f"{name} no longer lists the unbuilt reasons"
        documented = set(re.findall(r"`([^`]+)`", rest[: rest.index(".")]))
        missing = sorted(reasons - documented)
        assert missing == [], f"unbuilt reasons missing from {name}: {', '.join(missing)}"
        stale = sorted(documented - reasons)
        assert stale == [], f"{name} lists unbuilt reasons no report prints: {', '.join(stale)}"


def campaign_stop_reasons() -> set[str]:
    tree = ast.parse(Path(inspect.getsourcefile(CampaignRunner)).read_text(encoding="utf-8"))
    reasons = set()
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "observe_stop"
        ):
            continue
        # Only a literal member can be compared with the pages.
        argument = node.args[0] if len(node.args) == 1 else None
        assert (
            isinstance(argument, ast.Attribute)
            and isinstance(argument.value, ast.Name)
            and argument.value.id == "CampaignStopReason"
        ), f"observe_stop without a literal CampaignStopReason at line {node.lineno}"
        reasons.add(CampaignStopReason[argument.attr].value)
    return reasons


def test_the_pages_list_every_stop_reason_a_campaign_stops_with() -> None:
    reasons = campaign_stop_reasons()
    assert reasons
    row = re.search(
        r"^\| `stop_reason` \| (`queue_dry`.*) \|$",
        page("docs/reference/events-and-records.md"),
        re.MULTILINE,
    )
    assert row, "no stop_reason row in docs/reference/events-and-records.md"
    table = section(page("docs/running-campaigns.md"), "### Stop reasons").split("\n| Hold |", 1)[0]
    architecture = re.search(r"A campaign ends with one of ([^.]*)\.", page("docs/architecture.md"))
    assert architecture, "docs/architecture.md no longer lists the stop reasons"

    for name, listed in {
        "docs/running-campaigns.md": table_column(table),
        "docs/reference/events-and-records.md": re.findall(r"`([a-z_]+)`", row.group(1)),
        "docs/architecture.md": re.findall(r"`([a-z_]+)`", architecture.group(1)),
    }.items():
        missing = sorted(reasons - set(listed))
        assert missing == [], f"stop reasons missing from {name}: {', '.join(missing)}"
        stale = sorted(set(listed) - reasons)
        assert stale == [], f"{name} lists stop reasons no campaign ends with: {', '.join(stale)}"


def command_options() -> dict[str, set[str]]:
    subcommands = next(
        action for action in _parser()._actions if isinstance(action, argparse._SubParsersAction)
    )
    return {
        name: {
            option
            for action in command._actions
            for option in action.option_strings
            if option not in {"-h", "--help"}
        }
        for name, command in subcommands.choices.items()
    }


OPTION = re.compile(r"(?<![\w-])--?[a-z][a-z-]*")


def test_the_command_reference_matches_the_command_line_parser() -> None:
    reference = page("docs/reference/commands.md")
    commands = command_options()
    headings = re.findall(r"^#+ (.*)$", outside_fences(reference), re.MULTILINE)
    sections = [match[1] for heading in headings if (match := re.fullmatch(r"`autobuild ([\w-]+)`", heading))]
    indexed = re.findall(r"^- \[`autobuild ([\w-]+)`\]", reference, re.MULTILINE)
    assert sorted(sections) == sorted(commands)
    assert sorted(indexed) == sorted(commands)
    sentence = re.search(r"it takes exactly one command: ([^.]*)\.", reference)
    assert sentence and set(re.findall(r"`([\w-]+)`", sentence[1])) == set(commands)
    for heading in headings:
        named = re.match(r"`?autobuild ([\w-]+)|.*`autobuild ([\w-]+)", heading)
        if named:
            assert (named[1] or named[2]) in commands, heading
        assert set(OPTION.findall(heading)) <= set().union(*commands.values()), heading

    profile_options = set(table_option_names(section(reference, "### Profile options")))
    for name, options in commands.items():
        body = section(reference, f"## `autobuild {name}`")
        synopsis = re.search(rf"^```\n(autobuild {name} .*?)^```$", body, re.MULTILINE | re.DOTALL)
        assert synopsis, f"no synopsis for autobuild {name}"
        listed = set(OPTION.findall(synopsis[1]))
        if "[profile options]" in synopsis[1]:
            listed |= profile_options
        assert listed == options, f"synopsis of autobuild {name}: {sorted(listed ^ options)}"
        rows = set(table_option_names(body))
        if rows:
            if "[profile options]" in synopsis[1]:
                rows |= profile_options
            assert rows == options, f"option table of autobuild {name}: {sorted(rows ^ options)}"
        else:
            prose = set(OPTION.findall(outside_fences(body)))
            assert options <= prose, f"autobuild {name} never explains {sorted(options - prose)}"
    for call, rest in re.findall(r"^autobuild ([\w-]+)(.*)$", reference, re.MULTILINE):
        assert call in commands, f"unknown command autobuild {call}"
        assert set(OPTION.findall(rest)) <= commands[call], f"autobuild {call}{rest}"
    assert "`-h` or `--help`" in reference


def table_option_names(markdown: str) -> list[str]:
    return [cell.split()[0] for cell in table_column(markdown) if cell.startswith("--")]


def test_the_profile_reference_matches_the_profile_loader() -> None:
    reference = page("docs/reference/profile.md")
    tables = {
        heading: section(reference, f"## `[{heading}]`")
        for heading in re.findall(r"^## `\[([^`]+)\]`$", outside_fences(reference), re.MULTILINE)
    }
    assert {name.split(".")[0] for name in tables} == _PROFILE_ALLOWED_KEYS["profile"]
    documented = {
        ("lanes.*" if name == "lanes.<name>" else name): set(table_column(body))
        for name, body in tables.items()
    }
    # The class names of this table are the profile author's own.
    assert documented.pop("run.item_classes") == {"<class name>"}
    accepted = {name: keys for name, keys in _PROFILE_ALLOWED_KEYS.items() if name != "profile"}
    assert set(documented) == set(accepted)
    for name, keys in accepted.items():
        missing = sorted(keys - documented[name])
        assert missing == [], f"profile keys missing from [{name}]: {', '.join(missing)}"
        stale = sorted(documented[name] - keys)
        assert stale == [], f"[{name}] documents keys the loader refuses: {', '.join(stale)}"
    sentence = re.search(
        r"Each `\[process\.boundaries\.<name>\]` table holds ([^.]*)\.", tables["process"]
    )
    assert sentence and set(re.findall(r"`([a-z_]+)`", sentence[1])) == _BOUNDARY_KEYS


def disclaimer_candidates() -> tuple[str, ...]:
    selected = public_paths()
    if selected is None:
        selected = frozenset(
            path.relative_to(ROOT).as_posix()
            for path in (
                ROOT / "README.md",
                *(ROOT / "docs").rglob("*.md"),
                *(ROOT / "skills").rglob("*.md"),
                *(ROOT / "src").rglob("*.py"),
            )
            if path.is_file()
        )
    return tuple(
        sorted(
            path
            for path in selected
            if path.endswith((".md", ".py")) and not path.startswith("tests/")
        )
    )


def test_every_public_file_on_the_usage_meter_says_the_endpoint_is_undocumented() -> None:
    offenders = []
    described = set()
    assert set(PAGES) <= set(disclaimer_candidates())
    for name in disclaimer_candidates():
        text = (ROOT / name).read_text(encoding="utf-8").lower()
        if name in PAGES:
            if not re.search(r"(claude|usage|subscription) meter|oauth/usage", text):
                continue
            described.add(name)
            # The Codex surface has a disclaimer of its own, which must not stand in for this one.
            disclaimed = any(
                re.search(r"\bendpoint\b.*undocumented.*without notice", line)
                for line in text.splitlines()
            )
        else:
            if "oauth/usage" not in text and "subscription meter" not in text:
                continue
            disclaimed = "undocumented" in text and "without notice" in text
        if not disclaimed:
            offenders.append(name)
    assert described == {
        "README.md",
        "docs/architecture.md",
        "docs/lanes-and-usage.md",
        "docs/platforms.md",
        "docs/reference/commands.md",
        "docs/reference/events-and-records.md",
        "docs/reference/harness-adapters.md",
        "docs/reference/profile.md",
    }
    assert offenders == [], (
        f"usage meter documented without the disclaimer in: {', '.join(offenders)}"
    )


def test_private_brief_tests_stay_out_of_the_projection() -> None:
    selected = public_paths()
    if selected is None:
        pytest.skip("the release manifest is private and absent from the public tree")
    module = "tests/unit/test_private_briefs.py"
    assert (ROOT / module).is_file()
    assert module not in selected
    skip_reason = "item briefs are private " + "and absent from the public tree"
    this_file = Path(__file__).resolve().relative_to(ROOT).as_posix()
    holders = sorted(
        name
        for name in selected
        if name.startswith("tests/")
        and name.endswith(".py")
        and name != this_file
        and skip_reason in (ROOT / name).read_text(encoding="utf-8")
    )
    assert holders == [], f"projected tests hold the private-brief skip: {', '.join(holders)}"
