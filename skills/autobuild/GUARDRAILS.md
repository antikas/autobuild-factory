# Autobuild guardrails

This file is the guardrails SSOT for every agent in an autobuild run. The workflow injects its path into every agent prompt; agents read it, they do not restate it. If any instruction in a run conflicts with this file, this file wins and the agent stops and reports.

## The four gates

**Scope gate.** An agent builds only the item it was dispatched, from the tracker, inside the run worktree. No silent scope expansion: new scope is proposed (to `docs/PROPOSED-BACKLOG.md`), never built uninvited. Touching any path or repo outside the run worktree is a breach: stop and report, do not proceed.

**Decision gate.** A genuinely open decision, or novel-architecture work with no precedent in the repo, is never taken by an agent: park the item with the decision it needs, stated precisely. A precedent-determined REVERSIBLE call is different: take the precedent, build, and surface the decision in the report. Owner-gated items (deploys, publishes, anything the config names as gated) are never selected, no matter how ready they look.

**Destructive gate.** Irreversible or destructive actions are never executed by a run: no deletions of user data, no force-pushes, no production deploys, no external publishing. Where an item genuinely requires one, the run produces a propose-only manifest describing the action and parks; the owner executes it.

**Observability gate.** A run that cannot be observed does not run. Before the first dispatch, arm a monitor on the evidence stream (new commits on the run branch). Liveness is judged by real evidence (journal result events, commit activity), never by output-file mtime alone. Stall deadlines must tolerate long single turns: a high-effort judgment-tier turn legitimately runs many minutes.

## Branch and commit discipline

- All work happens on a dedicated run branch (`autobuild/<date>`) in an isolated worktree. Agents never switch branches, never work on the default branch directly, never force-push.
- One commit per verified cycle (build passed audit and adjudication), with EXPLICIT paths (`git add <paths> && git commit -- <paths>`), never a pathless commit: a shared index must not sweep foreign work.
- Commit messages use delta notation: state what was ADDED / MODIFIED / REMOVED.
- Single-writer discipline: every agent operates only in the run worktree, so a concurrent session in the repo's main tree can never clobber an uncommitted cycle.
- Never revert or clean a tree that may hold live or protected work. If an external build process may still be running in the worktree, or an item is marked protected, leave the tree exactly as it is and report.

## Verification economy

- The builder runs ONLY the item's scoped validator (fast static gates plus the touched packages' tests), plus any determinism or isolation tests the item's acceptance names. Never the full suite, never integration/e2e/recording suites the item did not name: they are slow, environment-dependent, and not the item's gate.
- Tests run in the foreground with the exit code read. Never a background test polled for a done-marker.
- Auditors verify by READING the changed files and the builder's evidence, not by re-executing. At most ONE load-bearing claim may be re-derived with ONE cheap targeted command, and only if reading leaves genuine doubt.
- Missing, thin, or inconsistent validator evidence is itself a defect: fail the audit citing it.
- Over-verification is also a citable defect: running suites beyond the scoped validator, or re-running an already-passed command, is waste that stalls runs and falsely fails good work.
- The full suite runs ONCE per run, at accept/merge time for the whole branch, never per item or per fold.
- Thrash floor: if the SAME error survives TWO materially different fix attempts, stop and return failed-with-evidence (both attempts and their outputs). Never a third iteration: a clean failure with evidence is the correct outcome.

## The report

Every run ends with a morning report the owner can act on cold:

- **Shipped** — merged/committed items, with commits.
- **Parked** — each with the structured reason (phase that failed, failing command, error signature, evidence pointer) and the decision it needs.
- **Proposed** — refill candidates landed propose-only, with a link.
- **Failed** — anything that died without a clean park.
- **Next** — what the queue holds now.

Silent deferral is forbidden: every dispatched item ends the run as exactly one of shipped, parked, or failed.
