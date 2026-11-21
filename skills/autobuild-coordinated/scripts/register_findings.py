"""Register a review's non-blocking findings in the owning tracker, blocked with gate scope.

A reviewer's report lists blockers and, under a heading that starts with SCOPE,
the findings that do not block. Blockers go back to the builder. Every other
finding becomes a tracker item that waits for the owner to rank it; none joins
the running item or campaign.

Usage:
    python register_findings.py <repository> <review file> --source "<what was reviewed>" [--prefix PFX] [--dry-run]

The review file is plain text or markdown. The script reads the lines under the
first heading or label whose text starts with "SCOPE" (any case, with or without
markdown marks), up to the next heading or label in capitals. Each list line or
non-empty line there is one finding. A line of "none" registers nothing.

Each finding is added with `pinax add` in <repository> and then blocked with
`pinax block --gate scope`. Titles carry the source so the item reads on its own.
Exit code 0 when every finding was registered (or none was found), 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

MARKS = r"^\s*(?:#{1,6}\s*)?(?:\*\*)?"
SCOPE = re.compile(MARKS + r"SCOPE\b(.*)$")
# Another section starts at a markdown heading, or at a label in capitals that ends the line or is followed by a colon.
OTHER = re.compile(r"^\s*#{1,6}\s|" + MARKS + r"[A-Z][A-Z ,/&-]{2,}(?:\*\*)?\s*(?::|$)")
BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
NOTHING = {"none", "none.", "nothing", "nothing.", "n/a", "-"}
TITLE_LIMIT = 300


def scope_findings(text: str) -> list[str]:
    """The findings under the SCOPE heading, one per list line or non-empty line."""
    findings: list[str] = []
    inside = False
    for raw in text.splitlines():
        scope = SCOPE.match(raw)
        if scope and not inside:
            inside = True
            # The label may carry its first finding on the same line, after a colon.
            rest = scope.group(1)
            after = rest.split(":", 1)[1].strip(" *") if ":" in rest else ""
            if after:
                findings.append(after)
            continue
        if not inside:
            continue
        if OTHER.match(raw):
            break
        line = BULLET.sub("", raw).strip()
        if line:
            findings.append(line)
    return [f for f in findings if f.lower() not in NOTHING]


def title_for(finding: str, source: str) -> str:
    clean = " ".join(finding.split())
    title = f"{clean} (review finding outside acceptance: {source})"
    return title if len(title) <= TITLE_LIMIT else title[: TITLE_LIMIT - 3].rstrip() + "..."


def pinax_path() -> str:
    found = shutil.which("pinax")
    if found:
        return found
    local = Path.home() / ".local" / "bin" / "pinax"
    if local.exists():
        return str(local)
    raise FileNotFoundError("pinax is not on PATH or in ~/.local/bin")


LOCK_TRIES = 4
LOCK_PAUSE_SECONDS = 3.0


def _tracker(args: list[str], repo: Path, run, pause) -> subprocess.CompletedProcess:
    """One tracker call. Another git process holding the repository's lock is waited out, a few times."""
    for attempt in range(1, LOCK_TRIES + 1):
        done = run(args, cwd=repo, capture_output=True, text=True, timeout=120)
        if done.returncode == 0 or ".lock" not in (done.stdout + done.stderr) or attempt == LOCK_TRIES:
            return done
        pause(LOCK_PAUSE_SECONDS)
    return done


def register(repo: Path, findings: list[str], source: str, prefix: str | None, run=subprocess.run, pause=time.sleep) -> list[str]:
    """Add and block each finding. Returns the new item ids. Raises on the first failure."""
    pinax = pinax_path()
    ids: list[str] = []
    for finding in findings:
        add = [pinax, "add", "--title", title_for(finding, source), "--json"]
        if prefix:
            add += ["--prefix", prefix]
        done = _tracker(add, repo, run, pause)
        if done.returncode != 0:
            raise RuntimeError(f"pinax add failed: {(done.stdout + done.stderr).strip()[-300:]}")
        item = json.loads(done.stdout)["item_id"]
        blocked = _tracker([pinax, "block", item, "--gate", "scope", "--json"], repo, run, pause)
        if blocked.returncode != 0:
            raise RuntimeError(f"pinax block {item} failed (the item exists and is not blocked): {(blocked.stdout + blocked.stderr).strip()[-300:]}")
        ids.append(item)
    return ids


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Register a review's non-blocking findings, blocked with gate scope.")
    parser.add_argument("repository", type=Path)
    parser.add_argument("review", type=Path)
    parser.add_argument("--source", required=True, help="what was reviewed, in a few plain words")
    parser.add_argument("--prefix", help="tracker id prefix, when the repository has more than one")
    parser.add_argument("--dry-run", action="store_true", help="print the titles and register nothing")
    args = parser.parse_args(argv)
    findings = scope_findings(args.review.read_text(encoding="utf-8", errors="replace"))
    if not findings:
        print("no findings under SCOPE; nothing registered")
        return 0
    if args.dry_run:
        for finding in findings:
            print("would register:", title_for(finding, args.source))
        return 0
    try:
        ids = register(args.repository, findings, args.source, args.prefix)
    except (RuntimeError, FileNotFoundError, KeyError, ValueError, OSError) as error:
        sys.stderr.write(f"error: {error}\n")
        return 1
    for item, finding in zip(ids, findings):
        print(f"{item}  {finding[:100]}")
    print(f"registered {len(ids)} finding(s), each blocked with gate scope, in {os.fspath(args.repository)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
