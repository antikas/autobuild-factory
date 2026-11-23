"""Every command finds one scratch root from the same repository, profile and option."""

from __future__ import annotations

import ast
import json
import os
import subprocess
from pathlib import Path

import pytest

from autobuild.adapters.claude_harness import ClaudeCodeHarnessAdapter
from autobuild.adapters.local_records import PAUSE_MARKER
from autobuild.bootstrap import composition
from autobuild.bootstrap.profile import ProfileOverrides, load_settings
from autobuild.cli import main
from autobuild.domain import AdapterIdentity, ProbeResult, UsageReading

SOURCE = Path(composition.__file__).resolve().parents[1]
COMPLETION = "campaign completed: shipped 1, parked 0, failed 0; report report.md\n"
PROFILE = """
[run]
lanes = ["claude-code"]
{scratch}
[lanes.claude-code]
builder = "builder"
reviewer = "reviewer"
[validator]
id = "tests"
argv = ["python", "-m", "pytest"]
[subscription_routing]
enabled = true
[usage]
warn_percent = 94
stop_percent = 98
five_hour_stop_percent = 95
"""


class _Meter:
    meter_ids = frozenset({"claude-subscription"})

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def probe(self) -> ProbeResult:
        return ProbeResult.ready(AdapterIdentity("meter", "1"))

    def read(self, meter_id: str) -> UsageReading:
        return UsageReading.failed(meter_id, "offline")


class _Stopped(Exception):
    pass


class _Project:
    def __init__(self, root: Path, monkeypatch, capsys) -> None:
        self.root, self.monkeypatch, self.capsys = root, monkeypatch, capsys
        self.repository = root / "project"
        self.working = root / "working"
        self.profile = root / "settings" / "campaign.toml"
        for folder in (self.repository, self.working, self.profile.parent):
            folder.mkdir()
        (self.repository / "BACKLOG.md").write_text(
            "| Item | Title | Status | Brief |\n|---|---|---|---|\n", encoding="utf-8"
        )
        for arguments in (
            ("init", "-q", "-b", "main"),
            ("add", "BACKLOG.md"),
            ("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "start"),
            ("init", "-q", "--bare", str(root / "origin.git")),
            ("remote", "add", "origin", str(root / "origin.git")),
            ("push", "-q", "-u", "origin", "main"),
            ("remote", "set-head", "origin", "main"),
        ):
            subprocess.run(("git", "-C", str(self.repository), *arguments), check=True)
        self.brief = root / "brief.md"
        self.brief.write_text("brief", encoding="utf-8")
        self.recorded: list[Path] = []

    def configure(self, scratch: str) -> None:
        self.profile.write_text(PROFILE.format(scratch=scratch), encoding="utf-8")

    def _run(self, command: str, *extra: str) -> tuple[int, str, str]:
        code = main(
            [command, "--repository", str(self.repository), "--profile", str(self.profile), *extra]
        )
        captured = self.capsys.readouterr()
        return code, captured.out, captured.err

    def _configured(self, command: str, *extra: str) -> Path | str:
        """The root the command sets up, stopped before it does any work there."""

        real = composition.configure_scratch_environment
        seen: list[Path] = []

        def record(root=None):
            seen.append(real(root))
            raise _Stopped

        with self.monkeypatch.context() as patch:
            patch.setattr(composition, "configure_scratch_environment", record)
            _code, _out, err = self._run(command, *extra)
        return seen[0] if seen else err

    def roots(self, expected: Path, *option: str) -> dict[str, object]:
        """The scratch root each command uses. A command that did not find the
        expected root is given what it printed instead."""

        roots: dict[str, object] = {}
        code, out, err = self._run("preflight", "--json", *option)
        roots["preflight"] = Path(json.loads(out)["scratch_root"]) if code == 1 else err
        roots["run"] = self._configured("run", "--allow-delivery", *option)
        roots["subscription-status"] = self._configured("subscription-status", *option)
        roots["subscription-review"] = self._configured(
            "subscription-review", "--commit", "HEAD", "--brief", str(self.brief), *option
        )

        runs = expected / "runs"
        (runs / "run-1").mkdir(parents=True)
        (runs / "run-1" / "progress.log").write_text("", encoding="utf-8")
        code, out, err = self._run("pause", "--run", "run-1", *option)
        marked = code == 0 and (runs / "run-1" / PAUSE_MARKER).is_file()
        roots["pause"] = expected if marked else err
        (runs / "run-1" / "progress.log").write_text(COMPLETION, encoding="utf-8")
        code, out, err = self._run("watch", "--latest", *option)
        roots["watch"] = expected if code == 0 and out == COMPLETION else err

        with self.monkeypatch.context() as patch:
            patch.setattr(composition, "ClaudeUsageMeterAdapter", _Meter)
            _code, _out, err = self._run("usage", *option)
        written = list(self.root.rglob("usage-meter"))
        roots["usage"] = written[0].parent if written else err
        return roots


@pytest.fixture
def project(tmp_path: Path, monkeypatch, capsys) -> _Project:
    monkeypatch.setattr(os, "environ", os.environ.copy())
    monkeypatch.setattr(
        ClaudeCodeHarnessAdapter, "probe", lambda self: ProbeResult.unavailable("offline")
    )
    default = tmp_path / "default"
    default.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(default))
    project = _Project(tmp_path, monkeypatch, capsys)
    monkeypatch.chdir(project.working)
    return project


COMMANDS = (
    "preflight", "run", "subscription-status", "subscription-review", "watch", "pause", "usage",
)


def test_every_command_resolves_a_relative_option_to_one_root(project: _Project) -> None:
    project.configure('scratch_root = "from-profile"')
    expected = project.profile.parent.resolve() / "scratch"

    roots = project.roots(expected, "--scratch-root", "scratch")

    assert roots == dict.fromkeys(COMMANDS, expected)


def test_every_command_resolves_a_relative_profile_value_to_one_root(project: _Project) -> None:
    project.configure('scratch_root = "from-profile"')
    expected = project.profile.parent.resolve() / "from-profile"

    assert project.roots(expected) == dict.fromkeys(COMMANDS, expected)


def test_every_command_uses_one_default_root(project: _Project) -> None:
    project.configure("")
    expected = project.root.resolve() / "default" / "autobuild"

    assert project.roots(expected) == dict.fromkeys(COMMANDS, expected)


def test_without_a_profile_a_relative_option_resolves_against_the_repository(
    project: _Project,
) -> None:
    settings = load_settings(
        project.repository,
        None,
        ProfileOverrides(
            harness="claude-code", builder_model="b", reviewer_model="r", validator_id="v",
            validator_argv_json='["test"]', scratch_root="scratch", lane_state_root="state",
        ),
    )

    assert settings.scratch_root == project.repository.resolve() / "scratch"
    assert settings.lane_state_root == project.repository.resolve() / "state"


@pytest.mark.parametrize("from_option", [True, False])
def test_the_lane_state_root_shares_the_scratch_roots_base(
    project: _Project, from_option: bool
) -> None:
    project.configure("" if from_option else 'scratch_root = "roots/a"\nlane_state_root = "roots/b"')
    overrides = (
        ProfileOverrides(scratch_root="roots/a", lane_state_root="roots/b")
        if from_option
        else ProfileOverrides()
    )

    settings = load_settings(project.repository, project.profile, overrides)

    base = project.profile.parent.resolve() / "roots"
    assert (settings.scratch_root, settings.lane_state_root) == (base / "a", base / "b")


def test_a_home_path_is_expanded_and_an_absolute_path_is_used_as_given(project: _Project) -> None:
    project.configure("")
    project.monkeypatch.setenv("HOME", str(project.root))
    project.monkeypatch.setenv("USERPROFILE", str(project.root))
    expected = project.root.resolve() / "home-scratch"

    for value in ("~/home-scratch", str(expected)):
        settings = load_settings(
            project.repository, project.profile, ProfileOverrides(scratch_root=value)
        )
        assert settings.scratch_root == expected


def test_a_profile_that_is_a_link_gives_every_command_the_linked_files_folder(
    project: _Project,
) -> None:
    project.configure("")
    link = project.repository / ".autobuild.toml"
    try:
        link.symlink_to(project.profile)
    except OSError:
        pytest.skip("this host cannot create a symbolic link")
    real_folder = project.profile.parent.resolve()
    project.profile = link

    roots = project.roots(real_folder / "scratch", "--scratch-root", "scratch")

    assert roots == dict.fromkeys(COMMANDS, real_folder / "scratch")


@pytest.mark.parametrize("profile", ["[run\n", "[run]\nscratch_root = 5\n", None])
def test_usage_exits_two_when_the_profile_cannot_be_read(
    project: _Project, capsys, profile: str | None
) -> None:
    if profile is not None:
        project.profile.write_text(profile, encoding="utf-8")

    assert main(["usage", "--profile", str(project.profile)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("autobuild usage:")


RESOLVER = {
    ("bootstrap/profile.py", "_scratch_or_lane_state_root"),
    ("bootstrap/profile.py", "resolve_scratch_root"),
    ("bootstrap/environment.py", "default_scratch_root"),
}
# No option or profile value reaches these as text. They are handed a root that is
# already absolute, except configure_scratch_environment, which the proof program
# also calls with its own working-directory option.
GIVEN_A_PATH = {
    ("bootstrap/environment.py", "configure_scratch_environment"),
    ("adapters/git_workspace.py", "__init__"),
    ("adapters/lane_state.py", "__init__"),
    ("adapters/lease.py", "__init__"),
    ("adapters/usage_cache.py", "__init__"),
    ("adapters/git_workspace.py", "_under_scratch"),
    ("adapters/harness_cli.py", "scratch_environment"),
}
CONVERSIONS = {"_resolve_path", "Path", "resolve", "absolute", "abspath", "realpath"}


def _conversions(path: Path) -> set[str]:
    found = set()

    def visit(node: ast.AST, function: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = node.name
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", getattr(node.func, "attr", ""))
            text = f"{function} {ast.unparse(node)}"
            if name in CONVERSIONS and ("scratch" in text or "lane_state" in text):
                found.add(function)
        for child in ast.iter_child_nodes(node):
            visit(child, function)

    visit(ast.parse(path.read_text(encoding="utf-8")), "")
    return found


def test_only_one_function_turns_a_root_value_into_a_path() -> None:
    found = {
        (path.relative_to(SOURCE).as_posix(), function)
        for path in SOURCE.rglob("*.py")
        for function in _conversions(path)
    }

    assert found == RESOLVER | GIVEN_A_PATH
