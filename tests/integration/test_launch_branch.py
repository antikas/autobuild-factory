"""The launch branch against the delivery mode, through the production command.

Real Git with a bare local origin, a ``BACKLOG.md`` tracker and a fake harness
that runs as a real child process."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from test_git_workspace import git

from autobuild.cli import main as campaign_main

HARNESS = """\
import json
import sys
from pathlib import Path

args = sys.argv[2:]
if args == ["--version"]:
    print("claude-code fake 1.0")
    raise SystemExit(0)
if args[:2] == ["auth", "status"]:
    print(json.dumps({"loggedIn": sys.argv[1] == "signed-in"}))
    raise SystemExit(0)
if "Review the actual brief" in sys.stdin.read():
    payload = {"decision": "pass", "findings": [], "evidence_ref": "r", "inspected": [], "acceptance": []}
else:
    Path("product.txt").write_text("built\\n", encoding="utf-8")
    payload = {"summary": "built", "report_ref": "b"}
print(json.dumps({
    "result": "complete",
    "structured_output": payload,
    "usage": {"input_tokens": 9, "output_tokens": 3},
}))
"""


SWITCHING_HARNESS = """\
import json
import subprocess
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
    subprocess.run(
        ["git", "-C", REMOTE, "symbolic-ref", "HEAD", "refs/heads/other"], check=True
    )
    payload = {"summary": "built", "report_ref": "b"}
print(json.dumps({
    "result": "complete",
    "structured_output": payload,
    "usage": {"input_tokens": 9, "output_tokens": 3},
}))
"""


class Launch:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.remote = root / "remote.git"
        self.repository = root / "repository"
        self.scratch = root / "scratch"
        self.lane_state = root / "lane-state"
        git(root, "init", "--bare", str(self.remote))
        git(root, "init", "-b", "main", str(self.repository))
        git(self.repository, "config", "user.name", "AutoBuild Test")
        git(self.repository, "config", "user.email", "autobuild@example.invalid")
        (self.repository / "brief.md").write_text(
            "# Brief\n\nWrite product.txt.\n\n## Declared paths\n\n`product.txt`\n", encoding="utf-8"
        )
        (self.repository / "BACKLOG.md").write_text(
            "| Item | Title | Status | Brief |\n|---|---|---|---|\n"
            "| LB-1 | Add the product | Ready | brief.md |\n",
            encoding="utf-8",
        )
        self._commit_all("initial")
        git(self.repository, "remote", "add", "origin", str(self.remote))
        git(self.repository, "push", "-u", "origin", "main")
        git(self.remote, "symbolic-ref", "HEAD", "refs/heads/main")
        (root / "harness.py").write_text(HARNESS, encoding="utf-8")
        for folder in (self.scratch / "runs", self.scratch / "leases", self.lane_state):
            folder.mkdir(parents=True)
            (folder / "kept.txt").write_text("kept\n", encoding="utf-8")
        self.profile()

    def profile(self, login: str = "signed-in") -> None:
        command = [sys.executable, str(self.root / "harness.py"), login]
        (self.root / "autobuild.toml").write_text(
            '[run]\nharness = "claude-code"\nmax_items = 1\n\n'
            '[models]\nbuilder = "b"\nreviewer = "r"\n\n'
            f'[validator]\nid = "tests"\nargv = {json.dumps([sys.executable, "-c", "pass"])}\n\n'
            f"[harness]\ncommand = {json.dumps(command)}\n\n"
            '[preflight]\naccepted_environment = ["NODE_EXTRA_CA_CERTS", "SSLKEYLOGFILE", '
            '"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"]\n',
            encoding="utf-8",
        )

    def _commit_all(self, subject: str) -> str:
        git(self.repository, "add", ".")
        git(self.repository, "commit", "-m", subject)
        return git(self.repository, "rev-parse", "HEAD")

    def commit(self, name: str) -> str:
        (self.repository / name).write_text("local\n", encoding="utf-8")
        return self._commit_all(f"ADDED: {name}")

    def feature(self) -> str:
        git(self.repository, "checkout", "-b", "feature/x")
        return self.commit("unreviewed.txt")

    def main(self, command: str, *options: str) -> int:
        return campaign_main(
            [
                command,
                "--repository", str(self.repository),
                "--profile", str(self.root / "autobuild.toml"),
                "--tracker", "backlog",
                "--scratch-root", str(self.scratch),
                "--lane-state-root", str(self.lane_state),
                *options,
            ]
        )

    def run(self, *options: str) -> int:
        return self.main("run", "--allow-delivery", *options)

    def state(self) -> dict[str, object]:
        def files(root: Path) -> dict[str, bytes]:
            return {
                path.relative_to(root).as_posix(): path.read_bytes() if path.is_file() else b""
                for path in root.rglob("*")
            }

        return {
            "refs": git(self.repository, "for-each-ref"),
            "head": git(self.repository, "rev-parse", "HEAD", "--abbrev-ref", "HEAD"),
            "status": git(self.repository, "status", "--porcelain", "--untracked-files=all"),
            "worktrees": git(self.repository, "worktree", "list", "--porcelain"),
            "origin": git(self.remote, "for-each-ref"),
            "backlog": (self.repository / "BACKLOG.md").read_bytes(),
            "lane state": files(self.lane_state),
            "runs": files(self.scratch / "runs"),
            "leases": files(self.scratch / "leases"),
        }


@pytest.fixture
def launch(tmp_path: Path) -> Launch:
    return Launch(tmp_path)


def feature_message() -> str:
    return (
        "protected-default delivery must be launched from the default branch main; "
        "the checkout is on feature/x; switch to main, "
        "or pass --delivery-mode current-branch-pr to deliver to feature/x"
    )


@pytest.mark.parametrize("remote_head_known", (False, True))
def test_a_protected_default_launch_from_a_feature_branch_changes_nothing(
    launch: Launch, capsys: pytest.CaptureFixture[str], remote_head_known: bool
) -> None:
    if remote_head_known:
        git(launch.repository, "remote", "set-head", "origin", "main")
    extra = launch.feature()
    before = launch.state()

    code = launch.run()

    reached = git(launch.remote, "rev-list", "main").split()
    assert extra not in reached, "the feature commit reached origin's default branch"
    assert code == 2
    assert capsys.readouterr().err == f"AutoBuild failed before completion: {feature_message()}\n"
    assert launch.state() == before


def test_a_protected_default_launch_from_a_detached_head_changes_nothing(
    launch: Launch, capsys: pytest.CaptureFixture[str]
) -> None:
    git(launch.repository, "checkout", "--detach")
    extra = launch.commit("unreviewed.txt")
    before = launch.state()

    assert launch.run() == 2

    assert capsys.readouterr().err == (
        "AutoBuild failed before completion: "
        "protected-default delivery must be launched from the default branch main; "
        f"the checkout is on a detached head at {extra[:12]}; switch to main, "
        "or pass --delivery-mode current-branch-pr\n"
    )
    assert launch.state() == before


def test_a_launch_from_the_default_branch_publishes_its_local_commit_with_the_item(
    launch: Launch,
) -> None:
    local = launch.commit("local.txt")

    assert launch.run() == 0

    assert local in git(launch.remote, "rev-list", "main").split()
    subjects = git(launch.remote, "log", "--format=%s", "main").splitlines()
    assert "MODIFIED: claim LB-1 in BACKLOG" in subjects
    assert "MODIFIED: close LB-1 in tracker" in subjects
    assert git(launch.remote, "show", "main:product.txt") == "built"
    assert git(launch.remote, "for-each-ref", "--format=%(refname)") == "refs/heads/main"


@pytest.mark.parametrize(
    ("branch", "options", "message"),
    (
        ("feature/x", (), "must be launched from the default branch main"),
        ("main", ("--delivery-mode", "current-branch-pr"), "refuses the detected default branch"),
        ("", ("--delivery-mode", "current-branch-pr"), "requires a named invoking branch"),
    ),
)
def test_a_refused_branch_cools_no_failing_lane(
    launch: Launch,
    capsys: pytest.CaptureFixture[str],
    branch: str,
    options: tuple[str, ...],
    message: str,
) -> None:
    launch.profile(login="signed-out")
    if branch == "feature/x":
        launch.feature()
    elif not branch:
        git(launch.repository, "checkout", "--detach")
    before = launch.state()

    assert launch.run(*options) == 2

    assert not (launch.lane_state / "lanes.json").exists()
    assert message in capsys.readouterr().err
    assert launch.state() == before


def test_a_failing_lane_is_cooled_when_the_branch_is_accepted(launch: Launch) -> None:
    launch.profile(login="signed-out")

    assert launch.run() == 2

    assert "claude-code" in (launch.lane_state / "lanes.json").read_text(encoding="utf-8")


def test_preflight_gives_the_launch_answer_for_a_feature_branch(
    launch: Launch, capsys: pytest.CaptureFixture[str]
) -> None:
    launch.feature()
    before = launch.state()

    assert launch.main("preflight", "--json") == 1
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is False
    assert report["error"] == feature_message()
    assert (report["lanes"], report["preflight"]) == ([], [])

    assert launch.main("preflight") == 1
    assert capsys.readouterr().out == f"preflight failed: {feature_message()}\n"
    assert launch.state() == before

    assert launch.main("preflight", "--json", "--delivery-mode", "current-branch-pr") == 0
    assert json.loads(capsys.readouterr().out)["error"] is None


def test_preflight_in_current_branch_mode_refuses_only_a_detached_head(
    launch: Launch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert launch.main("preflight") == 0
    assert launch.main("preflight", "--delivery-mode", "current-branch-pr") == 0
    git(launch.repository, "checkout", "--detach")
    capsys.readouterr()

    assert launch.main("preflight", "--json", "--delivery-mode", "current-branch-pr") == 1
    assert json.loads(capsys.readouterr().out)["error"] == (
        "current-branch-pr delivery requires a named invoking branch"
    )


UNESTABLISHED = (
    "the default branch of origin could not be established: this clone records no "
    "remote head and origin did not answer with its head; make origin reachable, "
    "or set the remote head in this clone, then launch again"
)


@pytest.mark.parametrize("checkout", ("feature", "main", "detached"))
def test_a_protected_default_launch_with_a_guessed_default_branch_changes_nothing(
    launch: Launch, capsys: pytest.CaptureFixture[str], checkout: str
) -> None:
    if checkout == "feature":
        launch.feature()
    elif checkout == "detached":
        git(launch.repository, "checkout", "--detach")
    git(launch.repository, "remote", "set-url", "origin", str(launch.root / "unreachable.git"))
    before = launch.state()

    code = launch.run()

    assert capsys.readouterr().err == f"AutoBuild failed before completion: {UNESTABLISHED}\n"
    assert code == 2
    assert launch.state() == before

    assert launch.main("preflight", "--json") == 1
    report = json.loads(capsys.readouterr().out)
    assert (report["ok"], report["error"]) == (False, UNESTABLISHED)
    assert (report["lanes"], report["preflight"]) == ([], [])
    assert launch.state() == before


def test_a_remote_that_answers_without_a_head_leaves_the_default_branch_a_guess(
    launch: Launch, capsys: pytest.CaptureFixture[str]
) -> None:
    git(launch.remote, "symbolic-ref", "HEAD", "refs/heads/nowhere")
    launch.feature()
    before = launch.state()

    assert launch.run() == 2

    assert capsys.readouterr().err == f"AutoBuild failed before completion: {UNESTABLISHED}\n"
    assert launch.state() == before


def test_a_recorded_remote_head_establishes_the_default_branch_of_a_silent_remote(
    launch: Launch, capsys: pytest.CaptureFixture[str]
) -> None:
    git(launch.repository, "remote", "set-head", "origin", "main")
    git(launch.repository, "remote", "set-url", "origin", str(launch.root / "unreachable.git"))

    assert launch.main("preflight", "--json") == 0
    assert json.loads(capsys.readouterr().out)["error"] is None


def test_current_branch_mode_still_accepts_a_silent_remote(
    launch: Launch, capsys: pytest.CaptureFixture[str]
) -> None:
    launch.feature()
    git(launch.repository, "remote", "set-url", "origin", str(launch.root / "unreachable.git"))

    assert launch.main("preflight", "--json", "--delivery-mode", "current-branch-pr") == 0
    assert json.loads(capsys.readouterr().out)["error"] is None


def test_every_item_delivers_to_the_default_branch_of_the_launch(launch: Launch) -> None:
    profile = launch.root / "autobuild.toml"
    profile.write_text(
        profile.read_text(encoding="utf-8").replace("max_items = 1", "max_items = 2"),
        encoding="utf-8",
    )
    (launch.root / "harness.py").write_text(
        SWITCHING_HARNESS.replace("REMOTE", repr(str(launch.remote))), encoding="utf-8"
    )
    with (launch.repository / "BACKLOG.md").open("a", encoding="utf-8") as backlog:
        backlog.write("| LB-2 | Add to the product | Ready | brief.md |\n")
    launch._commit_all("queue a second item")
    git(launch.repository, "push", "origin", "main", "main:refs/heads/other")
    other = git(launch.remote, "rev-parse", "refs/heads/other")
    git(launch.repository, "fetch", "origin")

    code = launch.run("--campaign-id", "switched")

    assert git(launch.remote, "symbolic-ref", "HEAD") == "refs/heads/other"
    assert git(launch.remote, "rev-parse", "refs/heads/other") == other
    subjects = git(launch.remote, "log", "--format=%s", "main").splitlines()
    assert {"MODIFIED: close LB-1 in tracker", "MODIFIED: close LB-2 in tracker"} <= set(subjects)
    assert "ADDED: campaign switched report" in subjects
    assert git(launch.remote, "show", "main:product.txt") == "built\nbuilt"
    assert code == 0
