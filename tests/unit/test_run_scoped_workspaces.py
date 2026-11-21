"""Every campaign run gets its own batch and item temporary directories, so two
campaigns on one host, on the same or different repositories, never share or
clear each other's pytest base temporary directory."""

from __future__ import annotations

import shlex
import shutil
import threading
from dataclasses import dataclass, replace
from pathlib import Path

from test_item_scratch import basetemp, message, result
from test_test_economy import _batch_context, _batch_ports, _Deliveries
from test_workflows import builder, command, make_ports, review, spec

from autobuild.adapters import CodexHarnessAdapter
from autobuild.adapters.harness_cli import item_scratch_environment
from autobuild.application import CampaignRunner, ItemWorkflow
from autobuild.bootstrap.composition import _item_environment
from autobuild.domain import (
    AdapterIdentity,
    CampaignRef,
    CommandRequest,
    CommandResult,
    ItemDisposition,
    ReviewDecision,
    RunRecordRef,
    Seat,
    SeatRequest,
    ToolPolicy,
    WorkspaceRef,
)
from autobuild.testing import FakeCommandAdapter, FakeRunRecordAdapter

WAIT_SECONDS = 5
MARKER_SUFFIX = ".campaign"


def identity(name: str) -> AdapterIdentity:
    return AdapterIdentity(name, "1", frozenset({"test"}))


@dataclass
class _RunNamed(FakeRunRecordAdapter):
    """A run record whose ``create`` returns a fixed run id, as two launches of
    one campaign id get different run ids from the real run record."""

    run_id: str = "run"

    def create(self, campaign: CampaignRef) -> RunRecordRef:
        return RunRecordRef(self.run_id, Path("/fake/record") / self.run_id)


@dataclass
class _PytestLikeValidator(FakeCommandAdapter):
    """A batch validator that, like pytest, clears and recreates its basetemp,
    then checks after both campaigns have done so that its folder holds only
    its own marker."""

    name: str = ""
    started: threading.Barrier | None = None
    written: threading.Barrier | None = None

    def run(self, request: CommandRequest) -> CommandResult:
        with self._guard:
            self.requests.append(request)
        try:
            assert self.started is not None and self.written is not None
            self.started.wait()
            folder = Path(basetemp(dict(request.environment)))
            if folder.exists():
                shutil.rmtree(folder)
            folder.mkdir(parents=True)
            (folder / f"{self.name}{MARKER_SUFFIX}").write_text(self.name, encoding="utf-8")
            self.written.wait()
            markers = sorted(path.name for path in folder.glob(f"*{MARKER_SUFFIX}"))
            exit_code = 0 if markers == [f"{self.name}{MARKER_SUFFIX}"] else 1
        except Exception:
            exit_code = 1
        return CommandResult(request.command_id, exit_code, f"stdout:{self.name}", f"stderr:{self.name}", "start", "end")


def test_two_campaigns_on_two_repositories_validating_at_once_never_share_a_folder(
    tmp_path: Path,
) -> None:
    scratch = tmp_path / "scratch"
    started = threading.Barrier(2, timeout=WAIT_SECONDS)
    written = threading.Barrier(2, timeout=WAIT_SECONDS)
    runs = {"a": "run-a", "b": "run-b"}
    ports_by_name = {}
    for name, run_id in runs.items():
        base = _batch_ports(1, [])
        base.workspace.validation_revisions = [f"head-{name}"]
        validator = _PytestLikeValidator(identity("command"), name=name, started=started, written=written)
        ports_by_name[name] = replace(
            base,
            command=validator,
            records=_RunNamed(identity("records"), run_id=run_id),
            item_environment=_item_environment(scratch),
        )
    outcomes: dict[str, object] = {}
    errors: list[BaseException] = []

    def run(name: str) -> None:
        try:
            outcomes[name] = CampaignRunner(ports_by_name[name], items=_Deliveries()).run(
                CampaignRef("autobuild-20261119T180035Z", tmp_path / f"repository-{name}", max_items=1),
                lambda selected: spec(selected),
                context=_batch_context(every=1),
            )
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(name,)) for name in runs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=4 * WAIT_SECONDS)

    assert errors == []
    for name in runs:
        ports = ports_by_name[name]
        assert [entry.disposition for entry in outcomes[name].items] == [ItemDisposition.ACCEPTED]
        assert [event.event_type for event in ports.records.events if event.event_type.startswith("campaign.batch_valid")] == [
            "campaign.batch_validated"
        ], name
    environments = {name: dict(ports_by_name[name].command.requests[0].environment) for name in runs}
    basetemps = {name: Path(basetemp(environments[name])) for name in runs}
    temps = {name: Path(environments[name]["TMPDIR"]) for name in runs}
    assert basetemps["a"] != basetemps["b"]
    assert temps["a"] != temps["b"]
    for one, other in (("a", "b"), ("b", "a")):
        for mine in (basetemps[one], temps[one]):
            for theirs in (basetemps[other], temps[other]):
                assert mine.resolve() not in theirs.resolve().parents
                assert mine.resolve() != theirs.resolve()


def test_the_scratch_directory_depends_on_the_run_and_the_name(tmp_path: Path) -> None:
    root = tmp_path / "items"
    first = dict(item_scratch_environment(root, "run-a", "batch-1"))
    second = dict(item_scratch_environment(root, "run-b", "batch-1"))

    assert first["TMPDIR"] != second["TMPDIR"]
    assert basetemp(first) != basetemp(second)
    assert dict(item_scratch_environment(root, "run-a", "batch-1")) == first
    other = dict(item_scratch_environment(root, "run-a", "batch-2"))
    assert other["TMPDIR"] != first["TMPDIR"]
    assert basetemp(other) != basetemp(first)
    directory = Path(first["TMPDIR"]).parent
    assert directory.parent == root
    assert directory.name.startswith("batch-1-")


def _seat_request(run_id: str, root: Path) -> SeatRequest:
    return SeatRequest(
        run_id,
        "item-1",
        Seat.BUILDER,
        "builder-class",
        root / "brief.md",
        WorkspaceRef(root, "branch", "base", "lease"),
        ToolPolicy(frozenset({"python"}), (root,)),
        "build",
        "builder-report-v1",
        60.0,
    )


def test_seats_of_one_item_in_two_runs_get_different_temporary_trees(tmp_path: Path) -> None:
    outputs = tmp_path / "outputs"
    commands = FakeCommandAdapter(
        identity("command"),
        scripted_results=[
            result(outputs, f"seat-{n}", message({"summary": "built"})) for n in range(2)
        ],
    )
    harness = CodexHarnessAdapter(commands, tmp_path / "harness" / "codex", command=("codex",))

    for run_id in ("run-a", "run-b"):
        harness.invoke(_seat_request(run_id, tmp_path / "worktree"))

    first, second = (dict(request.environment) for request in commands.requests)
    assert first["TMPDIR"] != second["TMPDIR"]
    assert basetemp(first) != basetemp(second)
    assert shlex.split(first["PYTEST_ADDOPTS"]) != shlex.split(second["PYTEST_ADDOPTS"])


def test_an_items_validator_basetemp_is_derived_from_its_run(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    basetemps = []
    for run_id in ("run-a", "run-b"):
        ports = replace(
            make_ports([builder(), review(ReviewDecision.PASS)], [command()], diff_count=1),
            item_environment=_item_environment(scratch),
        )
        record = RunRecordRef(run_id, Path("/fake/record"))
        outcome = ItemWorkflow(ports).run(
            CampaignRef("campaign", Path("/repo")), spec(), record
        )
        assert outcome.disposition is ItemDisposition.ACCEPTED
        environment = dict(ports.command.requests[0].environment)
        expected = dict(item_scratch_environment(scratch / "items", run_id, spec().item.item_id))
        assert basetemp(environment) == basetemp(expected)
        basetemps.append(basetemp(environment))

    assert basetemps[0] != basetemps[1]
