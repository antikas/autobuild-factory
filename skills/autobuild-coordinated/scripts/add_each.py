"""Stage files one at a time, retrying a write an on-access scanner refuses.

Usage: python add_each.py <worktree> <path> [<path> ...]

Each path is a file or a directory. Directories are resolved through git itself
(modified and untracked, not ignored), so an ignored tree such as a virtual
environment is never walked. A `git add` refused with a permission error is
retried up to eight times with a growing pause; any other failure stops the run.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ATTEMPTS = 8
LOCK_MARKER = "Permission denied"


def add_one(worktree: Path, rel: str) -> bool:
    for attempt in range(1, ATTEMPTS + 1):
        result = subprocess.run(
            ["git", "-C", str(worktree), "add", "--", rel], capture_output=True, text=True
        )
        if result.returncode == 0:
            return True
        if LOCK_MARKER not in result.stderr:
            sys.stderr.write(f"{rel}: {result.stderr.strip()}\n")
            return False
        time.sleep(0.5 * attempt)
    sys.stderr.write(f"{rel}: still refused after {ATTEMPTS} attempts\n")
    return False


def main() -> int:
    if len(sys.argv) < 3:
        sys.stderr.write(__doc__)
        return 2
    worktree = Path(sys.argv[1]).resolve()
    files: list[str] = []
    for raw in sys.argv[2:]:
        listing = subprocess.run(
            ["git", "-C", str(worktree), "ls-files", "--modified", "--others",
             "--exclude-standard", "--", raw],
            capture_output=True, text=True,
        )
        if listing.returncode != 0:
            sys.stderr.write(f"{raw}: {listing.stderr.strip()}" + chr(10))
            return 1
        matched = [line.strip() for line in listing.stdout.splitlines() if line.strip()]
        if not matched:
            sys.stderr.write(f"{raw}: no modified or untracked file matches this path" + chr(10))
            return 1
        files.extend(matched)
    added = failed = 0
    for rel in dict.fromkeys(files):
        if add_one(worktree, rel):
            added += 1
        else:
            failed += 1
    print(f"added={added} failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
