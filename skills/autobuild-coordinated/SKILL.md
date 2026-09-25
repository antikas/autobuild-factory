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
2. The coordinator (this session) claims items, writes briefs, dispatches seats, adjudicates findings, integrates, records and closes. It never builds product code inside an item and never lets a builder write the tracker.
3. A builder is a fresh seat per item. It reads the brief and the repository, changes the artefact in its lane, runs the scoped validator, and returns a bounded report. It never commits.
4. A reviewer is a fresh seat per review pass. It reads the committed or frozen artefact and the evidence, never the builder's reasoning, tests every claim itself, drives every gate red, and returns a verdict with reproducing commands.

## Preconditions

- An approved queue: Pinax (`pinax status` shows ready items with briefs) or a `BACKLOG.md` in the AutoBuild format.
- A project profile `.autobuild.toml` naming the builder, reviewer and specialist model tiers, optionally an effort beside each tier (the Claude Code adapter starts each seat at it; the Codex and GitHub Copilot adapters run seats at the assistant's own configured effort), the validator command (`[validator] argv`) and the tool policy. If it is absent, ask the owner for those facts once and write the profile before the first claim.
- A campaign environment file, written once from `templates/env.sh`: it points every temporary and cache path at the scratch root and sets whatever the validator needs. Every seat sources it before every command.
- The inputs the briefs name, resolved once per campaign and written into the campaign record: the constraints file (the project's constraints document, or the brief's own Constraints section when the project has none); the validator command from the profile; the map probe, when the project has a path-mapped validator, otherwise the validator command itself; the records directory (`docs/campaigns/` unless the project names another); and the builder's report line budget (40 lines unless the item needs more); a reviewer's report lists every finding with its severity and confidence and is kept short enough to read in one pass.
- A scratch root on the build drive, given to every seat. Nothing is written to the operating system's temporary directory. The `scripts/audit_scratch.sh` check runs after every item.
- Lanes: one reusable Git worktree per parallel lane (`scripts/lane.sh new <lane> <branch>`), each with its own bootstrapped environment, and one detached worktree for the post-merge master lane (`scripts/lane.sh master-lane`). The main tree is never a lane.
- The subscription or usage meter, when the runtime has one: read before every seat and after every item, warn at the owner's warning level, stop at the owner's stop level.

## The item cycle

1. Claim. `pinax claim <id>` (or mark the BACKLOG item in progress) and commit the tracker change. The coordinator commits; builders never touch the tracker.
2. Brief. Write the builder brief from `templates/builder-brief.md`: the item brief, the constraints file, the paths the item owns, the shell rules, the scratch path, the pre-review checklist and the return contract. Name the model tier and effort from the profile.
3. Build. Dispatch the builder in its lane through the runtime adapter. Wait for its report. Treat its validator run as pre-review evidence, never as acceptance.
4. Freeze. Nothing runs in the lane while a reviewer reads it. A reviewer's report ends with the line "nothing running"; no fold or correction starts before that line arrives.
5. Review. Dispatch a fresh reviewer with `templates/reviewer-brief.md` against the frozen lane. The reviewer re-drives red proofs on scratch copies, rebuilds what the builder claims to have built, and compares generated trees against the base branch.
6. Adjudicate. A blocker goes back to the same builder as a correction round with the reviewer's reproducing command quoted; then a fresh re-check on the frozen tree. Non-blocking findings are folded (corrections, proofs, hygiene) or recorded. A fold that adds a capability, a gate or a declaration key is a build and gets its own review pass.
7. Integrate. Commit in the lane by staging the item's owned paths explicitly (a lane with no ignore file carries bytecode and build output that a whole-tree add would sweep in); merge the base branch into the lane; the builder resolves code conflicts keeping both sides; rerun the lane's suites on the merged tree; fast-forward the base branch, or merge the lane into it when the base moved. If another item changed every generated file (a header, a marker), re-emit the lane's generated trees through every emitter before the lane run and confirm the diff is header-only.
8. Post-merge lane. Run the full validator in the detached master lane at the merge commit. Read the elapsed time against the load at the time; the ceiling is evidence, not a gate.
9. Close the item. `pinax done <id> --briefing <file>` (or set the BACKLOG row to `Done (<commit>)`) with an acceptance briefing from `templates/item-briefing.md`; commit the tracker; add the campaign record row.
10. Audit. Run `scripts/audit_scratch.sh`; read the meter; record both.

## Stops become items

A builder that meets a Stop condition returns evidence and a reproducing command and stops. The coordinator registers the gap as a new item with typed edges to the items it blocks, records the ruling in the plan and the campaign record, and re-dispatches the stopped item after the gap closes. Nothing is deferred in silence; every item is shipped, parked with a stated reason, or registered blocked with a typed gate.

## Tracker commands

The coordinator alone writes the tracker, always with an actor, and commits tracker changes separately from product changes. On Pinax: `pinax claim <id> --actor <role@handle>`, `pinax done <id> --briefing <file> --actor ...`, `pinax note add <id> --ref <file> --caption <text> --actor ...` (a caption is at most 200 characters; longer evidence goes in the referenced record), `pinax dep add <id> --to <id> --type blocks`, `pinax block <id> --gate decision|scope|destructive|proposal`, `pinax add` for a stop turned into an item. On `BACKLOG.md`: edit the row's Status cell to `Claimed by <actor>`, `Done (<commit>)`, `Parked: <reason>` or `Blocked: <gate>`, and commit.

## Records

- Campaign record: one file per campaign under the project's records directory, with the table from `templates/campaign-record.md` and dated entries for every dispatch, verdict, fix-forward, incident and ruling.
- Item briefing: one file per accepted item, committed with the tracker close.
- Process record: the method lessons of the campaign, written as they happen, harvested at close into the project's method documentation.
- Change log: a line for a release, an evidence-boundary change or a material capability change, wherever the project records its changes.

## Fix-forwards

A defect found outside an item's scope is a coordinator fix-forward: a small, reversible change with its own deterministic proof (a suite, a probe, a red check turned green), committed on the base branch with a message that names the defect, and recorded. A fix-forward that touches engine behaviour gets the touched-path validator before anything depends on it.

## Close

Close when the queue is dry or the owner stops the campaign: a final post-merge lane on the last merge commit, the campaign totals, the owner-facing follow-ups, the harvest of the process record, and the plan outcome against its success criteria. Release and publication are the owner's act and start only on the owner's explicit instruction.
