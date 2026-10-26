"""Thin command-line entry point for the production AutoBuild workflow."""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

from autobuild.adapters.progress import ProgressLogReader, is_completion_line
from autobuild.bootstrap.composition import (
    host_capacity,
    preflight_report,
    read_usage,
    run_campaign,
)
from autobuild.bootstrap.environment import resolve_runs_root
from autobuild.bootstrap.profile import ConfigurationError, ProfileOverrides, load_settings
from autobuild.domain import (
    CapacityPolicy,
    DeliveryMode,
    HostCapacity,
    HostReading,
    ItemDisposition,
    UsageReading,
    bytes_to_gib,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autobuild",
        description="Run the portable AutoBuild campaign workflow.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    run = subcommands.add_parser("run", help="Run ready tracker items until a bound or stop condition")
    _profile_arguments(run)
    run.add_argument("--campaign-id")
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

    usage = subcommands.add_parser(
        "usage",
        help="Read the subscription usage meter once, read-only",
    )
    usage.add_argument("--json", action="store_true", help="Print the normalised reading as JSON.")
    usage.add_argument("--scratch-root", help="Directory for the meter command's output files.")

    host = subcommands.add_parser(
        "host",
        help="Read the host's CPU and memory once and print the derived concurrency",
    )
    host.add_argument("--repository", default=".")
    host.add_argument("--profile")
    host.add_argument("--json", action="store_true", help="Print the reading and capacity as JSON.")
    host.add_argument(
        "--sample-seconds",
        type=float,
        default=1.0,
        help="Window over which CPU busy is sampled (default 1).",
    )

    preflight = subcommands.add_parser(
        "preflight",
        help="Probe every lane and run the launch preflight without claiming any item",
    )
    _profile_arguments(preflight)
    preflight.add_argument("--json", action="store_true", help="Print the report as JSON.")
    return parser


def _profile_arguments(command: argparse.ArgumentParser) -> None:
    """The profile and override arguments shared by ``run`` and ``preflight``."""

    command.add_argument("--repository", default=".")
    command.add_argument("--profile")
    command.add_argument("--harness")
    command.add_argument("--harness-arg", action="append")
    command.add_argument("--builder-model")
    command.add_argument("--reviewer-model")
    command.add_argument("--specialist-model")
    command.add_argument("--validator-id")
    command.add_argument("--validator-argv-json")
    command.add_argument("--allowed-tool", action="append")
    command.add_argument("--allowed-root", action="append")
    command.add_argument("--max-items", type=int)
    command.add_argument("--scratch-root")
    command.add_argument(
        "--lane-state-root",
        help="Directory for the machine-local shared lane cooling file (lanes.json).",
    )
    command.add_argument("--tracker", choices=("auto", "pinax", "backlog"))
    command.add_argument("--backlog")
    command.add_argument(
        "--allow-item",
        action="append",
        help="Add an item id to the campaign allow-list (repeatable).",
    )
    command.add_argument(
        "--exclude-item",
        action="append",
        help="Add an item id to the campaign exclude-list (repeatable).",
    )
    command.add_argument("--refill-plan")


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


def usage_payload(reading: UsageReading) -> dict[str, object]:
    """The normalised meter reading ``autobuild usage --json`` prints."""

    return {
        "schema": "autobuild.usage.v1",
        "meter_id": reading.meter_id,
        "ok": reading.ok,
        "error": reading.error or None,
        "fetched_at": reading.fetched_at,
        "extra_usage_enabled": reading.extra_usage_enabled,
        "limits": [
            {
                "kind": limit.kind.value,
                "scope": limit.scope,
                "percent": limit.percent,
                "resets_at": limit.resets_at,
            }
            for limit in reading.limits
        ],
    }


def _usage(args: argparse.Namespace, read=read_usage) -> int:
    """Print one meter reading; exit 0 when the meter answered and 1 when not."""

    scratch = Path(args.scratch_root) if args.scratch_root else None
    try:
        reading = read(scratch)
    except Exception as exc:
        print(f"usage meter unavailable: {type(exc).__name__}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(usage_payload(reading), indent=2, sort_keys=True))
    elif not reading.ok:
        print(f"usage meter {reading.meter_id} unavailable: {reading.error}")
    else:
        print(f"usage meter {reading.meter_id} at {reading.fetched_at}:")
        for limit in reading.limits:
            print(f"  {limit.describe()}")
        if reading.extra_usage_enabled is not None:
            state = "enabled" if reading.extra_usage_enabled else "off"
            print(f"  extra usage {state}")
    return 0 if reading.ok else 1


def _gib(value: int | None) -> float | None:
    gib = bytes_to_gib(value)
    return None if gib is None else round(gib, 3)


def host_payload(
    reading: HostReading, policy: CapacityPolicy, capacity: HostCapacity
) -> dict[str, object]:
    """The reading and derived capacity ``autobuild host --json`` prints.
    Memory is in GiB."""

    busy = reading.cpu_busy_percent
    return {
        "schema": "autobuild.host.v1",
        "reading": {
            "logical_cpus": reading.logical_cpus,
            "cpu_busy_percent": None if busy is None else round(busy, 1),
            "sample_seconds": reading.sample_seconds,
            "memory_total_gib": _gib(reading.memory_total_bytes),
            "memory_available_gib": _gib(reading.memory_available_bytes),
            "commit_total_gib": _gib(reading.commit_total_bytes),
            "commit_available_gib": _gib(reading.commit_available_bytes),
            "measured": reading.measured(),
            "diagnostics": list(reading.diagnostics),
        },
        "profile": {
            "concurrency": "auto" if policy.concurrency is None else policy.concurrency,
            "heavy_concurrency": "auto"
            if policy.heavy_concurrency is None
            else policy.heavy_concurrency,
            "reserve_gb": policy.reserve_gib,
            "per_item_gb": policy.per_item_gib,
            "max_concurrency": policy.max_concurrency,
        },
        "capacity": {
            "items": capacity.items,
            "heavy": capacity.heavy,
            "items_source": capacity.items_source,
            "heavy_source": capacity.heavy_source,
            "cpu_term": capacity.cpu_term,
            "memory_term": capacity.memory_term,
        },
    }


def _host(args: argparse.Namespace, read=host_capacity) -> int:
    """Print one host reading and the derived capacity; exit 0, or 2 when the
    profile cannot be read or the sample window is not positive."""

    if not (math.isfinite(args.sample_seconds) and args.sample_seconds > 0):
        print("autobuild host: --sample-seconds must be a positive finite number", file=sys.stderr)
        return 2
    try:
        reading, policy, capacity = read(args.repository, args.profile, args.sample_seconds)
    except ConfigurationError as exc:
        print(f"autobuild host: {exc}", file=sys.stderr)
        return 2
    payload = host_payload(reading, policy, capacity)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    shown = payload["reading"]

    def text(value: object, unit: str = "") -> str:
        return "unmeasured" if value is None else f"{value}{unit}"

    print(f"logical cpus: {text(shown['logical_cpus'])}")
    busy_line = f"cpu busy: {text(shown['cpu_busy_percent'], '%')}"
    if shown["cpu_busy_percent"] is not None:
        busy_line += f" over {reading.sample_seconds:g}s"
    print(busy_line)
    print(
        f"memory available: {text(shown['memory_available_gib'], ' GiB')} "
        f"of {text(shown['memory_total_gib'], ' GiB')}"
    )
    print(
        f"commit available: {text(shown['commit_available_gib'], ' GiB')} "
        f"of {text(shown['commit_total_gib'], ' GiB')}"
    )
    for diagnostic in reading.diagnostics:
        print(f"note: {diagnostic}")
    print(
        f"concurrency: items {capacity.items} ({capacity.items_source}), "
        f"heavy {capacity.heavy} ({capacity.heavy_source})"
    )
    if capacity.items_source == "auto":
        print("run.concurrency is auto, the default; set it to 1 to build one item at a time")
    return 0


def _preflight(args: argparse.Namespace, report=preflight_report) -> int:
    """Print the lane probes and the preflight; exit 0 when the launch would
    pass, 1 when a check fails and 2 when the profile or runtime cannot load."""

    try:
        settings = load_settings(args.repository, args.profile, _overrides(args))
        result = report(settings)
    except Exception as exc:
        print(f"AutoBuild preflight failed before completion: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        for lane in result["lanes"]:
            state = "capable" if lane["capable"] else "not capable"
            cooled = f"; cooled until {lane['cooled_until']}" if lane["cooled_until"] else ""
            print(f"lane {lane['name']}: {state} ({lane['detail'] or lane['version']}){cooled}")
        for probe in result["preflight"]:
            print(f"preflight {probe['name']}: {probe['detail']}")
        if result["error"]:
            print(f"preflight failed: {result['error']}")
    return 0 if result["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "watch":
        return _watch(args)
    if args.command == "usage":
        return _usage(args)
    if args.command == "preflight":
        return _preflight(args)
    if args.command == "host":
        return _host(args)
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
