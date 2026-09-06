#!/usr/bin/env bash
# Lane helpers for a coordinated campaign. Every command is plain and idempotent.
#
#   lane.sh new <lane-dir> <branch> [<base>]   create or reuse a worktree on <branch> cut from <base> (default: the current branch)
#   lane.sh take-base <lane-dir> <base>        merge <base> into the lane (no rebase, no rewrite)
#   lane.sh master-lane <lane-dir> <base>      create or re-detach a worktree at <base> for the post-merge full run
#   lane.sh validate <lane-dir> <env-file> <argv...>   run the project validator inside the lane after sourcing the environment
#
# The script never moves the base branch, never deletes a branch, and never runs a rewrite.
set -euo pipefail

cmd="${1:-}"
case "$cmd" in
  new)
    lane="$2"; branch="$3"; base="${4:-$(git rev-parse --abbrev-ref HEAD)}"
    if [ -d "$lane/.git" ] || [ -f "$lane/.git" ]; then
      echo "lane exists: $lane ($(git -C "$lane" rev-parse --abbrev-ref HEAD))"
    elif git show-ref --verify -q "refs/heads/$branch"; then
      git worktree add -q "$lane" "$branch"
    else
      git worktree add -q -b "$branch" "$lane" "$base"
    fi
    git -C "$lane" rev-parse --short HEAD
    ;;
  take-base)
    lane="$2"; base="$3"
    git -C "$lane" merge -q --no-edit "$base"
    git -C "$lane" log --oneline -1
    ;;
  master-lane)
    lane="$2"; base="$3"
    if [ -d "$lane/.git" ] || [ -f "$lane/.git" ]; then
      git -C "$lane" checkout -q --detach "$base"
    else
      git worktree add -q --detach "$lane" "$base"
    fi
    git -C "$lane" merge-base --is-ancestor HEAD "$base"
    echo "master lane at $(git -C "$lane" rev-parse --short HEAD), clean entries: $(git -C "$lane" status --porcelain | wc -l)"
    ;;
  validate)
    lane="$2"; envfile="$3"; shift 3
    cd "$lane"
    # shellcheck disable=SC1090
    source "$envfile"
    "$@"
    ;;
  *)
    sed -n '2,9p' "$0" >&2
    exit 2
    ;;
esac
