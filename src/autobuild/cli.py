"""Thin command-line entry point for the production AutoBuild workflow."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from autobuild.bootstrap.composition import run_campaign
from autobuild.bootstrap.profile import ProfileOverrides, load_settings
from autobuild.domain import ItemDisposition


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
    run.add_argument("--tracker", choices=("auto", "pinax", "backlog"))
    run.add_argument("--backlog")
    run.add_argument("--refill-plan")
    run.add_argument("--allow-delivery", action="store_true")
    run.add_argument("--output", type=Path)
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
        tracker_kind=args.tracker,
        backlog_path=args.backlog,
        refill_plan=args.refill_plan,
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = load_settings(args.repository, args.profile, _overrides(args))
        result = run_campaign(
            settings,
            campaign_id=args.campaign_id,
            allow_delivery=args.allow_delivery,
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
