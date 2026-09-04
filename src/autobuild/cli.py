"""Thin command-line entry point for the production AutoBuild workflow."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from autobuild.adapters.progress import ProgressLogReader, is_completion_line
from autobuild.bootstrap.composition import run_campaign
from autobuild.bootstrap.environment import resolve_runs_root
from autobuild.bootstrap.profile import ProfileOverrides, load_settings
from autobuild.domain import DeliveryMode, ItemDisposition


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autobuild",
        description="Run the portable AutoBuild campaign workflow.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    run = subcommands.add_parser("run", help="Run ready tracker items until a bound or stop condition")
    run.add_argument("--repository", default=".")
    run.add_argument("--profile")
    run.add_argument("--harness")
    run.add_argument("--harness-arg", action="append")
    run.add_argument("--builder-model")
    run.add_argument("--reviewer-model")
    run.add_argument("--specialist-model")
    run.add_argument("--validator-id")
    run.add_argument("--validator-argv-json")
    run.add_argument("--allowed-tool", action="append")
    run.add_argument("--allowed-root", action="append")
    run.add_argument("--max-items", type=int)
    run.add_argument("--campaign-id")
    run.add_argument("--scratch-root")
    run.add_argument(
        "--lane-state-root",
        help="Directory for the machine-local shared lane cooling file (lanes.json).",
    )
    run.add_argument("--tracker", choices=("auto", "pinax", "backlog"))
    run.add_argument("--backlog")
    run.add_argument(
        "--allow-item",
        action="append",
        help="Add an item id to the campaign allow-list (repeatable).",
    )
    run.add_argument(
        "--exclude-item",
        action="append",
        help="Add an item id to the campaign exclude-list (repeatable).",
    )
    run.add_argument("--refill-plan")
    run.add_argument("--allow-delivery", action="store_true")
    run.add_argument(
        "--delivery-mode",
        choices=tuple(mode.value for mode in DeliveryMode),
        default=DeliveryMode.PROTECTED_DEFAULT.value,
        help="Delivery target: protected default branch or the invoking PR branch.",
    )
    run.add_argument(
        "--push-current-branch",
        action="store_true",
        help="Push and verify the invoking branch in current-branch-pr mode.",
    )
    run.add_argument(
        "--allow-current-branch-default",
        action="store_true",
        help="Permit current-branch-pr mode when the invoking branch is the default branch.",
    )
    run.add_argument("--output", type=Path)

    watch = subcommands.add_parser(
        "watch",
        help="Follow the progress lines of a running or finished campaign",
    )
    selector = watch.add_mutually_exclusive_group(required=True)
    selector.add_argument("--run", help="Run id to follow under the runs root")
    selector.add_argument(
        "--latest",
        action="store_true",
        help="Follow the most recently modified run under the runs root",
    )
    watch.add_argument("--repository", default=".")
    watch.add_argument("--profile")
    watch.add_argument("--scratch-root")
    watch.add_argument(
        "--timeout-seconds",
        type=float,
        help="Stop with exit 3 if no completion line arrives within this many seconds.",
    )
    return parser


def _overrides(args: argparse.Namespace) -> ProfileOverrides:
    return ProfileOverrides(
        harness=args.harness,
        harness_command=tuple(args.harness_arg or ()),
        builder_model=args.builder_model,
        reviewer_model=args.reviewer_model,
        specialist_model=args.specialist_model,
        validator_id=args.validator_id,
        validator_argv_json=args.validator_argv_json,
        allowed_tools=tuple(args.allowed_tool or ()),
        allowed_roots=tuple(args.allowed_root or ()),
        max_items=args.max_items,
        scratch_root=args.scratch_root,
        lane_state_root=args.lane_state_root,
        tracker_kind=args.tracker,
        backlog_path=args.backlog,
        refill_plan=args.refill_plan,
        allow_items=tuple(args.allow_item or ()),
        exclude_items=tuple(args.exclude_item or ()),
    )


def _latest_run_dir(runs_root: Path) -> Path | None:
    """The most recently modified run directory under the runs root, or None."""

    try:
        candidates = [entry for entry in runs_root.iterdir() if entry.is_dir()]
    except OSError:
        return None
    if not candidates:
        return None
    return max(candidates, key=lambda entry: entry.stat().st_mtime)


def _watch(
    args: argparse.Namespace,
    *,
    clock=time.monotonic,
    sleep=time.sleep,
    poll_interval: float = 0.1,
) -> int:
    """Print a run's progress lines as they land and choose the exit code.

    The runs root is resolved without a complete profile. Stdout carries the
    progress lines only; every diagnostic goes to stderr. The command exits 0 once
    the campaign-completion line has been printed, 2 when the run cannot be found,
    and 3 when ``--timeout-seconds`` elapses first."""

    runs_root = resolve_runs_root(args.repository, args.profile, args.scratch_root)
    if args.latest:
        run_dir = _latest_run_dir(runs_root)
        if run_dir is None:
            print(f"no run exists under {runs_root}", file=sys.stderr)
            return 2
    else:
        run_dir = runs_root / args.run
        if run_dir.parent != runs_root or not run_dir.is_dir():
            print(f"run does not exist: {args.run}", file=sys.stderr)
            return 2
    reader = ProgressLogReader(run_dir / "progress.log")
    deadline = None if args.timeout_seconds is None else clock() + args.timeout_seconds
    while True:
        for line in reader.poll():
            print(line, flush=True)
            if is_completion_line(line):
                return 0
        if deadline is not None and clock() >= deadline:
            print(
                f"timed out after {args.timeout_seconds:g}s "
                "waiting for the campaign to complete",
                file=sys.stderr,
            )
            return 3
        sleep(poll_interval)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "watch":
        return _watch(args)
    try:
        settings = load_settings(args.repository, args.profile, _overrides(args))
        result = run_campaign(
            settings,
            campaign_id=args.campaign_id,
            allow_delivery=args.allow_delivery,
            delivery_mode=DeliveryMode(args.delivery_mode),
            push_current_branch=args.push_current_branch,
            allow_current_branch_default=args.allow_current_branch_default,
        )
    except Exception as exc:
        print(f"AutoBuild failed before completion: {exc}", file=sys.stderr)
        return 2
    rendered = json.dumps(result, indent=2, sort_keys=True)
    if args.output is not None:
        destination = args.output.expanduser().resolve(strict=False)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    dispositions = {item["disposition"] for item in result["items"]}
    return 0 if not dispositions or dispositions == {ItemDisposition.ACCEPTED.value} else 1
