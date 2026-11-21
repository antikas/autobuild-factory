---
name: autobuild-coordinated
description: Run an approved Pinax or BACKLOG.md queue as a coordinator-run campaign inside the current coding-assistant session, with fresh builder seats, blind reviewer seats, isolated lanes, a deterministic validator and evidence-based acceptance. Use instead of the AutoBuild application when the owner chooses the coordinated mode at launch, or asks to "run the queue in this session", "coordinate the build" or "run it with subagents".
user-invocable: true
---

# AutoBuild, coordinated mode

This skill runs the AutoBuild method without the Python application. The session that loads it is the coordinator: it holds scope, decisions, tracker writes and acceptance, and dispatches fresh seats for building and reviewing. The application's project profile, tracker and validator conventions apply unchanged; only the runner differs.

Read this file completely, then `rules.md`, then the adapter for the runtime you are in (`adapters/claude-code.md`, `adapters/github-copilot.md` or `adapters/codex.md`). Templates and scripts under this directory are the shipped mechanics; use them rather than re-deriving them.

## When to choose this mode

- The application is not installed, or the owner wants to watch and steer the campaign as it runs.
- Items are design-heavy and the owner wants fresh judgment seats at a pinned tier (a build tier for builders, a review tier for reviewers) chosen per item.
- The campaign is expected to surface stops that become new items and need the owner's ruling mid-campaign.

Choose the application when the queue is mechanical, unattended and long, or when the campaign must run without a session open.

## Four roles, one session

1. The owner approves the queue, rules on stops, decisions and scope changes, and performs any release or publication.
2. The coordinator (this session) claims items, classifies their lanes, writes briefs, dispatches seats, adjudicates findings, integrates, records and closes. It changes an artefact itself only in the Direct lane, and never lets a builder write the tracker.
3. A builder is a fresh seat per item. It reads the brief and the repository, changes the artefact in its lane, runs only the focused tests that cover its change, and returns a bounded report. It never commits and never runs the validator; the coordinator runs the validator once on the reviewed candidate.
4. A reviewer is a fresh seat per review pass. It reads the committed or frozen artefact and the evidence, never the builder's reasoning, and follows the [evidence rules](rules.md#evidence) and [findings and disposition rules](rules.md#findings-and-disposition) when it returns a verdict.

## Preconditions

- An approved queue, frozen at launch: Pinax (`pinax status` shows ready items with briefs) or a `BACKLOG.md` in the AutoBuild format. Nothing joins the running campaign after launch; new work goes to the backlog.
- A project profile `.autobuild.toml` naming the builder, reviewer and specialist model tiers, with optional effort values beside those tiers, the validator command (`[validator] argv`), the tool policy and the boundary map described under [Lanes](#lanes). `rules.md` resolves the effort and each runtime adapter renders its own option. If it is absent, ask the owner for those facts once and write the profile before the first claim.
- A campaign environment file, written once from `templates/env.sh`: it points every temporary and cache path at the scratch root and sets whatever the validator needs. Every seat runs project commands through `scripts/run.py <lane> <env-file> -- <command>`, one prompt-free call that applies it.
- A design-invariant checklist file for the boundary being built, seeded from the plan and grown when a review finds a new failure class. Builder and reviewer briefs both cite it. The right-first-time rules govern the campaign: the reproduced-blocker rubric in `templates/reviewer-brief.md`, class-level fixes, the lane's round limit, and pipelined independent items.
- The inputs the briefs name, resolved once per campaign and written into the campaign record: the constraints file (the project's constraints document, or the brief's own Constraints section when the project has none); the validator command from the profile; the map probe, when the project has a path-mapped validator, otherwise the validator command itself; the records directory (`docs/campaigns/` unless the project names another); and the builder's report line budget (40 lines unless the item needs more); a reviewer's report lists every finding with its severity and confidence and is kept short enough to read in one pass.
- A scratch root on the build drive, given to every seat. Nothing is written to the operating system's temporary directory. The `scripts/audit_scratch.sh` check runs after every item.
- Lanes: one reusable Git worktree per parallel lane (`scripts/lane.sh new <lane> <branch>`), each with its own bootstrapped environment, and one detached worktree for the post-merge master lane (`scripts/lane.sh master-lane`). The main tree is never a lane.
- The subscription or usage meter, when the runtime has one: read before every seat and after every item, warn at the owner's warning level, stop at the owner's stop level.

## Lanes

Every item runs in one of three lanes. Direct covers answers, documents, configuration, and a fix inside one file that already has a test. Code work beyond that is Small, or Material when it touches a named boundary.

| Lane | Steps | Budget for the whole item |
|---|---|---|
| Direct | The coordinator does the work, runs the covering test and commits | Minutes |
| Small | Brief, one builder, covering tests once, one blind reviewer, one fix round | 30 minutes |
| Material | Brief with the boundary's invariants, one builder, covering tests plus the changed journey's check, one blind reviewer plus one specialist per boundary touched, rounds as below | 90 minutes |

The project profile (`.autobuild.toml`, or `AGENTS.md` when the project keeps its profile there) maps each named boundary to path patterns and an invariant list. The coordinator matches the item's declared paths against that map before dispatch and the changed paths after the build. A match makes the item Material and adds that boundary's specialist. In the Direct lane the coordinator may change the artefact itself; in the other lanes every change goes through a builder.

Rounds:

- Small: one review round and one fix round, with the new test seen red and then green.
- Material: the second round verifies the fixes and checks for regressions without exploring further. A blocker that survives it gets one final correction round.

After the final round, in both lanes and without asking the owner: when the deterministic validation is green, each open finding is registered as a follow-up with its landing point (before the live deploy, before production, or ordinary), the item merges and the campaign continues. When it is red, the item never merges; it is parked with its evidence and the campaign continues without it.

## The item cycle

1. Claim. `pinax claim <id>`: with Pinax 0.2.1 or later the command commits its own change, and publishes it when the remote default branch is checked out, so the coordinator never commits `.ergon` by hand. On a branch other than the remote default branch, Pinax commits the claim and does not publish it (exit 4, status `committed_local`): when the campaign delivers through a pull request from that branch, the claim is accepted and published with the pull request, so other machines do not see it until the pull request merges; in any other mode it is a stop. On `BACKLOG.md`, mark the item in progress and commit the change. Builders never touch the tracker.
2. Classify. Choose the lane by the [lane rule](#lanes), using the profile's boundary map and the item's declared paths, and walk each acceptance clause to the files it touches. Repeat the match on the changed paths after the build; a new boundary match makes the item Material and adds that boundary's specialist. A Direct item is done by the coordinator, which runs the covering test and commits, then continues at step 9.
3. Brief. Write the builder brief from `templates/builder-brief.md`: the item brief, the constraints file, the paths the item owns, the shell rules, the scratch path, the pre-review checklist and the return contract. A Material brief carries the invariant list of each boundary it touches. Name the model tier and effort from the profile.
4. Build. Dispatch the builder in its lane through the runtime adapter. Wait for its report. Treat its focused test run as pre-review evidence, never as acceptance; the coordinator runs the validator once on the reviewed candidate.
5. Freeze. Nothing runs in the lane while a reviewer reads it. A reviewer's report ends with the line "nothing running"; no fold or correction starts before that line arrives.
6. Review. The floor is one blind reviewer per item plus one specialist per boundary the changed paths touch, each specialist reviewing its boundary only. Dispatch each as a fresh seat with `templates/reviewer-brief.md` against the frozen lane, its FOCUS line naming the whole item or the specialist's boundary and invariant list. Each reviewer applies the [evidence rules](rules.md#evidence) and [findings and disposition rules](rules.md#findings-and-disposition) to the frozen candidate.
7. Adjudicate. The coordinator applies the [findings and disposition rules](rules.md#findings-and-disposition). A blocker returns to the builder as a correction round with the reviewer's reproducing command quoted and its root-cause class named. Small gets one fix round. Material gets a fresh second-round re-check on the frozen tree and, for a blocker that survives it, one final correction round. After the final round the [round limit](#lanes) applies. Register the reviewers' SCOPE findings with `scripts/register_findings.py <repository> <review file> --source "<what was reviewed>"`, which adds each finding to the tracker and then blocks it with gate `scope`.
8. Integrate. Commit in the lane by staging the item's owned paths explicitly (a lane with no ignore file carries bytecode and build output that a whole-tree add would sweep in); merge the base branch into the lane; the builder resolves code conflicts keeping both sides; apply the [evidence rules](rules.md#evidence) to revalidate only evidence affected by merge changes on the merged candidate. Green: fast-forward the base branch, or merge the lane into it when the base moved. Red: park the item with its evidence and continue. If another item changed every generated file (a header, a marker), re-emit the lane's generated trees through every emitter before the lane run and confirm the diff is header-only.
9. Post-merge lane. The coordinator runs the single declared acceptance validation required by the [evidence rules](rules.md#evidence) on the exact delivery candidate at the required boundary. Read the elapsed time against the load at the time; the ceiling is evidence, not a gate.
10. Close the item. `pinax done <id> --briefing <file>` with an acceptance briefing from `templates/item-briefing.md`; Pinax commits the close itself. On `BACKLOG.md`, set the row to `Done (<commit>)` and commit the tracker change. Add the campaign record row.
11. Audit. Run `scripts/audit_scratch.sh`; read the meter; record both.

## Stops become items

A builder that meets a Stop condition returns evidence and a reproducing command and stops. A gap that blocks a queued item's acceptance is registered as a new item with typed edges to the items it blocks (`pinax dep add <id> --to <id> --type blocks`), and the stopped item is re-dispatched after the gap closes. Any other gap or outside defect goes to the backlog with gate `scope`; the frozen queue does not grow. Nothing is deferred in silence; every item is shipped, parked with a stated reason, or registered blocked with a typed gate.

## Tracker commands

The coordinator alone writes the tracker, always with an actor in the form `role@host`, and tracker changes are committed separately from product changes. Pinax 0.2.1 or later commits every mutating command itself (claim, done, note add, block, park, dep add, add) and pushes it when the remote default branch is checked out; the coordinator never stages or commits `.ergon` by hand, and an earlier Pinax is not supported. Read Pinax's `--json` result on every mutating command; a nonzero exit is read, not retried. 3 on a claim means another writer holds the item, so select the next one. 4 with status `committed_local` on a claim from a branch other than the remote default branch is accepted only when the work is delivered through a pull request from that branch; the claim is published with the pull request and is not visible to other machines until it merges. Any other 4 (the remote unreachable or unreadable, a foreign repository, or that claim in any other mode) is a stop that states the cause Pinax reports. 1, 2, 5, 6 and 7 are stops to report with Pinax's message (7 leaves the event uncommitted for the owner to resolve). A write that exits 0 without being pushed is recorded as unpublished and goes out with the next push of its branch. On Pinax: `pinax claim <id> --actor <role@handle>`, `pinax done <id> --briefing <file> --actor ...`, `pinax note add <id> --ref <file> --caption <text> --actor ...` (a caption is at most 200 characters; longer evidence goes in the referenced record), `pinax dep add <id> --to <id> --type blocks`, `pinax block <id> --gate decision|scope|destructive|proposal`, `pinax add` for a stop turned into an item. On `BACKLOG.md`: edit the row's Status cell to `Claimed by <actor>`, `Done (<commit>)`, `Parked: <reason>` or `Blocked: <gate>`, and the coordinator commits the change.

## Records

- Campaign record: one file per campaign under the project's records directory, with the table from `templates/campaign-record.md` and dated entries for every dispatch, verdict, Direct-lane fix, incident and ruling. It links the delivery candidate, review and validator evidence, and lists the non-blocking findings registered with gate `scope` under the [findings and disposition rules](rules.md#findings-and-disposition).
- Item briefing: one file per accepted item, committed with the tracker close. It records the findings disposition under the [findings and disposition rules](rules.md#findings-and-disposition).
- Process record: the method lessons of the campaign, written as they happen, harvested at close into the project's method documentation.
- Change log: a line for a release, an evidence-boundary change or a material capability change, wherever the project records its changes.

## Direct-lane fixes

The coordinator fixes a defect itself only when the fix is in the Direct lane: a red-to-green proof first, then the touched-path validator before anything depends on the fix, committed on the base branch with a message that names the defect, and recorded. A defect outside that lane is registered under [Stops become items](#stops-become-items).

## Close

Close when the queue is dry or the owner stops the campaign: a final post-merge lane on the last merge commit, the campaign totals, the owner-facing follow-ups, the harvest of the process record, and the plan outcome against its success criteria. Release and publication are the owner's act and start only on the owner's explicit instruction.
