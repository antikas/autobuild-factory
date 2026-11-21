# Standing rules for a coordinated campaign

Each rule states the failure it prevents. A brief that departs from one names the departure and the reason.

## Commands and history

- No history rewrite anywhere: no rebase, no force push, no branch deletion, no hard reset, no amend. Integration is by merge; a lane that must take the base branch merges it in. Rewrites prompt the harness even in permissive modes, and an unattended run stalls on the prompt.
- Plain commands. One command per shell call, no pipelines that hide an exit code, no shell loops, no subshell retries, no here-document commit messages. A seat that needs a loop writes a script file and runs it.
- The tracker commits itself. Pinax 0.2.1 or later commits every mutating command, so a coordinator never stages or commits `.ergon` by hand; a hand commit beside Pinax's own splits one event across two commits and breaks the close evidence chain. An earlier Pinax leaves its change uncommitted and is not supported. A `BACKLOG.md` change is still committed by the coordinator.
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
- A builder runs focused checks for the changed behavior. For a gate the item adds or retires, the builder preserves red evidence from a scratch copy and the real command. A changed candidate invalidates evidence for the affected claim or gate; retain the earlier red evidence and revalidate the affected evidence on the exact candidate.
- A fresh reviewer inspects the frozen candidate and its evidence. The reviewer selectively reproduces an unresolved claim when inspection, the acceptance mapping, or the evidence leaves a material question. The reviewer does not repeat every proof, rebuild every artefact, or run the declared acceptance validation by default.
- The coordinator runs the declared acceptance validation at the required boundary on the actual delivery candidate. This is the acceptance evidence, not a reviewer substitute.
- A validated candidate tree carries a scratch git directory for its byte-stability checks. A projection asserts the source carries no `.git`, copies with the directory excluded, and asserts the target's remote refs still resolve afterwards.
- A new estate, shape, translator or route with its own test needs its own validator-map rule; the first one's rule will not grow to cover it. The acceptance checklist asks for the rule and a probe on a path only that test covers.
- Whole-tree scanning checks are selected by the directory rule, not by the file class, or a new file with a narrow rule of its own escapes them until the full lane.
- Verification economy: scoped checks per item, targeted live proofs where behaviour changes, full deep, live and browser acceptance at integration boundaries, and no repeat on unchanged evidence.

## Findings and disposition

- A finding blocks only under the severity rubric in [the reviewer brief](templates/reviewer-brief.md); this file does not restate it. The coordinator directs or dispatches a blocker's correction through a builder and obtains a fresh review of the affected claim before accepting that claim.
- Every non-blocking finding is registered in the backlog with gate `scope` (`scripts/register_findings.py`). It is never fixed in the running campaign and does not reopen acceptance.
- After the final round the lane's round limit in `SKILL.md` applies: green validation merges with open findings registered as follow-ups; red validation parks the item with its evidence and the campaign continues. No question goes to the owner.
- A capability-sized follow-up is a separately authorised item. A coordinator does not fold it into the delivery candidate.

## Fixtures and scope

- Where the project generates artefacts from declarations: fixtures that dodge a route hide the gap until the first real estate. A generator's fixture set needs one fixture per generation and per translator hand-off, and a root-level estate, before the first real one.
- A fold that adds a capability is a build. Folds are for corrections, proofs and hygiene.
- A stated prohibition on a scratch path lowers the rate of violations; a periodic audit of the forbidden location catches the rest.
- Test runners and build tools write to the operating system's temporary directory unless told otherwise. The campaign environment file sets the temporary and cache variables, and a runner with its own base directory option (for example a test framework's base temporary path) is pointed at the scratch root explicitly; the audit catches the runner that was not.
- A lane with no ignore file carries bytecode and build output; integration stages the item's owned paths by name, never the whole tree.
- Stops are outcomes. An item that meets a Stop condition returns evidence and a reproducing command; the coordinator registers the gap as an item with typed edges and re-dispatches after it closes.
- A gate with a release clause is released by the coordinator on the clause's evidence and recorded in the campaign record; it is never re-asked of the owner. The owner's inputs were collected at launch; a missing one ships labelled as missing with only the dependent claim withheld.
- A `machine` or `cross-repository` item registered for the coordinator is done by the coordinator in the setup window or between items, recorded with its evidence, and closed; it never waits on the owner unless its release clause names an owner act.
- Adjacent-path repair is the coordinator's inside the approved scope: amend a brief's neighbouring test or configuration paths and dispatch the fix an acceptance criterion needs, recorded against the item. A changed outcome, architecture, authority, material cost or scope escalates; nothing else does.

## Seats and tiers

- Fresh seats and a blind reviewer are the mechanism, not the ceremony. A first blind review regularly finds a defect the green validator missed, often at the level of the contract. The builder's focused test run is pre-review evidence, never acceptance. Builders never run the full suite or the validator. The coordinator runs it once per item, and again when integration overlaps.
- Design-heavy items take the review-tier model for the builder as well; mechanical items take the build tier and are corrected once each. The profile table that supplies a seat's model also supplies its optional effort: `builder_effort` for a builder, `reviewer_effort` for a reviewer, and `specialist_effort` for a specialist. A specialist without `specialist_effort` uses `reviewer_effort`; any other unset effort remains unset. A campaign record may name an item-level effort that overrides the profile value, and records that choice. The coordinator raises a design-heavy item's builder to the reviewer tier while keeping the resolved builder effort unless the campaign record overrides it.
- The meter is read before every seat and after every item; the gates are recorded even when they never bind.
