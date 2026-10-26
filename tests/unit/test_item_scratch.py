"""Every command and seat of an item gets that item's own temporary directories.

Two items run at once through one workflow with a real command-line harness
adapter over a keyed command fake, so the recorded command requests carry the
environment each seat and each validator would receive."""

from __future__ import annotations

import json
import os
import shlex
import threading
from pathlib import Path

from autobuild.adapters import CodexHarnessAdapter
from autobuild.adapters.harness_cli import item_scratch_environment
from autobuild.application import ItemWorkflow, Lane, WorkflowPorts
from autobuild.bootstrap.composition import _item_environment
from autobuild.domain import (
    AdapterIdentity,
    CampaignRef,
    CommandRequest,
    CommandResult,
    DeliveryMode,
    DiffEvidence,
    ItemDisposition,
    ItemExecutionSpec,
    ToolPolicy,
    WorkItem,
    WorkspaceRef,
)
from autobuild.testing import (
    FakeCommandAdapter,
    FakeKnowledgeAdapter,
    FakeLeaseAdapter,
    FakeRunRecordAdapter,
    FakeTrackerAdapter,
    FakeWorkspaceAdapter,
)

ITEMS = ("item-a", "item-b")
WAIT_SECONDS = 5
TEMP_NAMES = ("TMPDIR", "TEMP", "TMP")
CACHE_NAMES = (
    "UV_CACHE_DIR", "PIP_CACHE_DIR", "NPM_CONFIG_CACHE", "XDG_CACHE_HOME", "PYTHONPYCACHEPREFIX"
)


def identity(name: str) -> AdapterIdentity:
    return AdapterIdentity(name, "1", frozenset({"test"}))


def message(text: dict) -> str:
    return json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(text)}})


def result(root: Path, name: str, stdout: str) -> CommandResult:
    root.mkdir(parents=True, exist_ok=True)
    out, err = root / f"{name}-stdout.txt", root / f"{name}-stderr.txt"
    out.write_text(stdout, encoding="utf-8")
    err.write_text("", encoding="utf-8")
    return CommandResult(name, 0, str(out), str(err), "start", "end")


class _BuildersMeet(FakeCommandAdapter):
    """Holds every builder seat's command until both items have reached it."""

    barrier: threading.Barrier | None = None

    def run(self, request: CommandRequest) -> CommandResult:
        if self.barrier is not None and ":builder:" in request.command_id:
            self.barrier.wait()
        return super().run(request)


def workspace_of(root: Path, item_id: str) -> WorkspaceRef:
    return WorkspaceRef(root / "worktrees" / item_id, f"autobuild/{item_id}", "base", f"lease-{item_id}")


def item_spec(item_id: str) -> ItemExecutionSpec:
    return ItemExecutionSpec(
        WorkItem(item_id, f"title {item_id}", "plan", ("passes",)),
        Path("brief.md"),
        "validator",
        ("python", "-m", "pytest"),
        ToolPolicy(frozenset({"python"}), (Path("/worktree"),)),
        "builder-class",
        "reviewer-class",
        "specialist-class",
        delivery_mode=DeliveryMode.PROTECTED_DEFAULT,
        delivery_target_branch="main",
        delivery_target_revision="base",
    )


def basetemp(environment: dict[str, str]) -> str:
    tokens = shlex.split(environment["PYTEST_ADDOPTS"])
    found = [token.removeprefix("--basetemp=") for token in tokens if token.startswith("--basetemp")]
    assert len(found) == 1
    return found[0]


def under(path: str, root: Path) -> bool:
    return root.resolve() in Path(path).resolve().parents


def run_two_items(tmp_path: Path) -> tuple[Path, CodexHarnessAdapter, dict[str, list[CommandRequest]]]:
    """Run both items at once and return each item's recorded command requests:
    its builder seat, its validator and its reviewer seat."""

    scratch = tmp_path / "scratch"
    outputs = tmp_path / "outputs"
    commands = _BuildersMeet(
        identity("command"),
        results_by_item={
            item_id: [
                result(outputs, f"{item_id}-builder", message({"summary": "built"})),
                result(outputs, f"{item_id}-validator", "passed"),
                result(
                    outputs,
                    f"{item_id}-reviewer",
                    message({"decision": "pass", "findings": [], "evidence_ref": f"verdict:{item_id}"}),
                ),
            ]
            for item_id in ITEMS
        },
    )
    commands.barrier = threading.Barrier(len(ITEMS), timeout=WAIT_SECONDS)
    harness = CodexHarnessAdapter(commands, scratch / "harness" / "codex", command=("codex",))
    workspaces = {item_id: workspace_of(tmp_path, item_id) for item_id in ITEMS}
    ports = WorkflowPorts(
        FakeTrackerAdapter(identity("tracker")),
        FakeWorkspaceAdapter(
            identity("workspace"),
            workspaces_by_item=workspaces,
            diffs_by_item={
                item_id: [DiffEvidence(workspaces[item_id], f"revision:{item_id}", (), f"patch:{item_id}")]
                for item_id in ITEMS
            },
        ),
        harness,
        commands,
        FakeRunRecordAdapter(identity("records")),
        FakeKnowledgeAdapter(identity("knowledge")),
        lease=FakeLeaseAdapter(identity("lease")),
        lanes=(Lane("codex", harness),),
        item_environment=_item_environment(scratch),
    )
    campaign = CampaignRef("campaign", tmp_path, max_items=2)
    workflow = ItemWorkflow(ports)
    record = ports.records.create(campaign)
    before = dict(os.environ)
    outcomes: dict[str, object] = {}
    errors: list[BaseException] = []

    def run(item_id: str) -> None:
        try:
            outcomes[item_id] = workflow.run(campaign, item_spec(item_id), record)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(item_id,)) for item_id in ITEMS]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=4 * WAIT_SECONDS)

    assert dict(os.environ) == before
    assert errors == []
    assert [outcomes[item_id].disposition for item_id in ITEMS] == [ItemDisposition.ACCEPTED] * 2
    requests = {
        item_id: [request for request in commands.requests if f":{item_id}:" in request.command_id]
        for item_id in ITEMS
    }
    assert [len(requests[item_id]) for item_id in ITEMS] == [3, 3]
    return scratch, harness, requests


def test_two_concurrent_items_and_their_seats_get_distinct_temp_and_basetemp_under_scratch(
    tmp_path: Path,
) -> None:
    scratch, _harness, requests = run_two_items(tmp_path)

    temps: dict[str, set[str]] = {}
    basetemps: dict[str, set[str]] = {}
    for item_id in ITEMS:
        for request in requests[item_id]:
            environment = dict(request.environment)
            values = {environment[name] for name in TEMP_NAMES}
            assert len(values) == 1
            temps.setdefault(item_id, set()).update(values)
            basetemps.setdefault(item_id, set()).add(basetemp(environment))
        for path in (*temps[item_id], *basetemps[item_id]):
            assert under(path, scratch)
            assert any(part.startswith(item_id) for part in Path(path).parts)
    assert temps["item-a"].isdisjoint(temps["item-b"])
    assert basetemps["item-a"].isdisjoint(basetemps["item-b"])


def test_two_concurrent_items_share_the_campaign_package_caches(tmp_path: Path) -> None:
    _scratch, harness, requests = run_two_items(tmp_path)
    seat_campaign = dict(harness._scratch_environment())

    effective: dict[str, list[dict[str, str | None]]] = {}
    for item_id in ITEMS:
        for request in requests[item_id]:
            environment = dict(request.environment)
            seat = ":builder:" in request.command_id or ":reviewer:" in request.command_id
            campaign = seat_campaign if seat else dict(os.environ)
            values = {name: environment.get(name, os.environ.get(name)) for name in CACHE_NAMES}
            assert values == {name: campaign.get(name) for name in CACHE_NAMES}, request.command_id
            effective.setdefault(item_id, []).append(values)
            # Positive control: the temporary directory is the item's own.
            assert environment["TMPDIR"] != campaign.get("TMPDIR")

    assert effective["item-a"] == effective["item-b"]
    assert all(seat_campaign[name] for name in CACHE_NAMES)


def test_item_ids_that_clean_to_the_same_name_get_distinct_directories(tmp_path: Path) -> None:
    slashed = dict(item_scratch_environment(tmp_path / "items", "a/b"))
    dashed = dict(item_scratch_environment(tmp_path / "items", "a-b"))

    assert slashed["TMPDIR"] != dashed["TMPDIR"]
    assert basetemp(slashed) != basetemp(dashed)
    assert dict(item_scratch_environment(tmp_path / "items", "a/b")) == slashed


def test_an_inherited_basetemp_is_replaced_and_other_pytest_options_are_kept(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("PYTEST_ADDOPTS", "-x --basetemp=/shared/pytest -p no:cacheprovider --basetemp /other")

    environment = dict(item_scratch_environment(tmp_path / "items", "item-a"))

    tokens = shlex.split(environment["PYTEST_ADDOPTS"])
    expected = (Path(environment["TMPDIR"]).parent / "pytest").as_posix()
    assert tokens == ["-x", "-p", "no:cacheprovider", f"--basetemp={expected}"]
    assert os.environ["PYTEST_ADDOPTS"].startswith("-x --basetemp=/shared/pytest")
