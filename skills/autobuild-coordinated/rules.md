# Standing rules for a coordinated campaign

These rules came from campaigns that ran the method by hand. Each states the failure it prevents. A brief that departs from one names the departure and the reason.

## Commands and history

- No history rewrite anywhere: no rebase, no force push, no branch deletion, no hard reset, no amend. Integration is by merge; a lane that must take the base branch merges it in. Rewrites prompt the harness even in permissive modes, and an unattended run stalls on the prompt.
- Plain commands. One command per shell call, no pipelines that hide an exit code, no shell loops, no subshell retries, no here-document commit messages. A seat that needs a loop writes a script file and runs it.
- A forbidden command is a disclosure mechanism as much as a prohibition. Seats report every departure unprompted; the coordinator records each against the item. The harness-level remedy (a hook that refuses the command) belongs to the application, not the brief.

## Host artefacts

- An on-access scanner can refuse a fresh file for a moment. One shared retry helper owns that retry in the test tree; a seat re-issues a refused command once; a whole-tree `git add` that fails on a different file each pass is replaced by per-file staging (`scripts/add_each.py`).
- Some refusals are content-specific: the same object hash is refused on every attempt while other objects write. Retries never clear it. Reword or reflow the content and commit again.
- A helper covers only the calls routed through it. Before declaring a host artefact handled, grep the file for every remaining direct call.
- A stale index lock left by a crashed hook blocks every later write; a lock older than the last live git process is removed once, deliberately, never in a loop.

## Lanes and runs

- Lanes are reusable worktrees, never moved while a seat or a run is inside them. The base branch never moves while its lane runs; the post-merge lane reads a detached worktree that nothing else touches, so tracker writes and record commits can land at any time.
- A lane run, once started, runs to its end. Stopping the harness task kills the outer shell only; the runner and its children continue and rewrite generated files under any concurrent work. A run that must not count is left to finish and its tree treated as unverified until a clean run ends.
- A lane's time ceiling assumes an idle machine. Two builders running database builds in parallel double it. Read the elapsed figure against the load at the time.
- Fold rounds start only after the reviewer's report says nothing of its own is still running. A fold dispatched into a tree the reviewer is still rebuilding produces a torn tree and a false verdict.
- A change after the final lane re-opens the lane. A path-mapped validator on a clean base branch validates nothing; name the touched paths explicitly and run the acceptance script before the change ships.

## Evidence

- A relationship proof asserts key agreement in the store that owns the entity and is driven red with an identifier that store does not know. Counting rows off the delivering feed proves delivery, not the relationship.
- A gate without a red proof is a finding. Every gate a builder adds or retires is proved red through a scratch copy and the real command, by the builder first and by the reviewer again.
- A validated candidate tree carries a scratch git directory for its byte-stability checks. A projection asserts the source carries no `.git`, copies with the directory excluded, and asserts the target's remote refs still resolve afterwards.
- A new estate, shape, translator or route with its own test needs its own validator-map rule; the first one's rule will not grow to cover it. The acceptance checklist asks for the rule and a probe on a path only that test covers.
- Whole-tree scanning checks are selected by the directory rule, not by the file class, or a new file with a narrow rule of its own escapes them until the full lane.

## Fixtures and scope

- Where the project generates artefacts from declarations: fixtures that dodge a route hide the gap until the first real estate. A generator's fixture set needs one fixture per generation and per translator hand-off, and a root-level estate, before the first real one.
- A fold that adds a capability is a build. Folds are for corrections, proofs and hygiene.
- A stated prohibition on a scratch path lowers the rate of violations; a periodic audit of the forbidden location catches the rest.
- Test runners and build tools write to the operating system's temporary directory unless told otherwise. The campaign environment file sets the temporary and cache variables, and a runner with its own base directory option (for example a test framework's base temporary path) is pointed at the scratch root explicitly; the audit catches the runner that was not.
- A lane with no ignore file carries bytecode and build output; integration stages the item's owned paths by name, never the whole tree.
- Stops are outcomes. An item that meets a Stop condition returns evidence and a reproducing command; the coordinator registers the gap as an item with typed edges and re-dispatches after it closes.

## Seats and tiers

- Fresh seats and a blind reviewer are the mechanism, not the ceremony. Every first review in the reference campaign found a defect the green validator had missed, in more than half the items a contract-level one. The builder's own validator run is pre-review evidence, never acceptance.
- Design-heavy items take the review-tier model for the builder as well; mechanical items take the build tier and are corrected once each. The profile carries one builder, reviewer and specialist tier; the coordinator raises a design-heavy item's builder to the reviewer tier and records the choice in the campaign record.
- The meter is read before every seat and after every item; the gates are recorded even when they never bind.
