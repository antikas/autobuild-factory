---
name: autobuild-plan
description: Turn one build request into a short AutoBuild-ready plan and a registered queue. The skill researches the current system, runs the riskiest real step once in a scratch copy, writes a decision brief with one block of owner inputs and the next slice in full, gets one independent review, registers the queue and stops before launch. Use autobuild for execution. Use a builder directly for one specified item.
user-invocable: true
---

# autobuild-plan

This skill produces the plan that `/autobuild` later executes. It stops before launch unless the owner has already authorised execution.

**Sources this skill points at, never restates:** the sibling AutoBuild skill (`../autobuild/SKILL.md`) for invocation; `../../docs/running-autobuild.md` for briefs, tracker, validator and refill configuration; `../autobuild-coordinated/rules.md` for how a coordinator repairs, releases gates and verifies during a campaign. Read any dispatch rules or agent definitions the environment carries.

## The shape of a plan

A plan is a decision brief, one block of owner inputs, the next slice in full, and one line for each later item. A later item gets its full brief when it becomes the next slice, written from what the earlier slices taught.

A plan fixes outcomes, acceptance criteria and authority. It does not prescribe routine execution choices. Every fixed detail is something that can be wrong and that a reviewer will find, so detail beyond the next slice costs review rounds and buys nothing. Execution choices belong to the campaign coordinator, who repairs inside the approved scope and escalates only a changed outcome, architecture, authority, material cost or scope.

## Phase 0: ground the request

1. **Search memory before files:** use an available memory or recall tool to find the prior specification, plan and decisions; search prior plans and decision documents when recall is unavailable.
2. **Read the scope source yourself.** Treat a specification older than the repository as a hypothesis until the current code confirms it.
3. Read tracker state once in every affected repository (`pinax status --json` on a [Pinax](https://github.com/antikas/pinax-tracker) repository, otherwise the supported backlog file). Confirm the request is still unexecuted and note existing items that affect its order.

## Phase 1: inspect, then probe

**Inspect.** Read small surfaces yourself. Use one scout per surface too wide to read directly, in parallel when the host supports it, with structured output and per-answer file evidence, at the implementation-capable model tier. Cover the build repositories (extension method against the latest implementation, test lanes, the declared validator, deployment code, drift), every publication surface the request implies, and the open decisions and gates touching the ask. Ask scouts to report contradictions in the request's assumptions.

**Probe.** Before writing any detail, run the riskiest real step once: the command, installation, build or user journey the next slice depends on that nobody has seen work in this repository. Probe in a scratch copy, a fresh clone or throwaway worktree under the scratch root. A probe changes nothing shared: no commits, no tracker writes, no sync of a shared environment, no start or stop of a shared service. Record the command, the result and its consequence in the plan. A failing probe is the most useful result planning produces: its fix becomes the first item.

## Phase 2: write the plan

Write `plans/<date>-<slug>/plan.md` in the team's planning repository, or `docs/plans/` in the target repository.

1. **Decision brief**, 500 to 700 words as a usability target: outcome with a concrete example; the current position in plain English; scope; decisions and authority; uncertainty and the probe result; the next slice and its dependencies; acceptance evidence and rollback.
2. **Owner inputs**, in one block: each default the owner may override, with its precedent; each disposition, closure or baseline only the owner can give; each physical act only the owner can perform. The owner answers the block once, with the launch instruction. A missing input ships labelled as missing, with only the claim that depends on it withheld. It never becomes a later question.
3. **Next slice**, every item in full: scope source, lane, dependencies, `Item nature`, `## Declared paths`, acceptance and stop condition. Acceptance makes a wrong answer fail: independent expected values, both case classes, a tolerance fixed now. A measurement names the task, the start and end boundaries and the unit on both sides. Where one item produces a value another consumes, name the connecting piece and its end-to-end proof. Scoped checks run per item; full acceptance runs at the slice boundary and is never repeated on unchanged evidence.
4. **Later items**, one line each, in order, with their dependencies, through go-live preparation, production deployment and publication, so the whole road stays visible.
5. **Configuration:** the profile (models, efforts, validator), the campaign environment, and the coordinator work the slice needs before or between items (environment setup, machine changes, other repositories). Include the seed of the campaign's design-invariant checklist (the failure classes known for the boundaries the slice touches) and the reviewer severity rubric, per the right-first-time rules in the coordinated skill's preconditions (`../autobuild-coordinated/SKILL.md`). Keep acceptance fixtures proportionate: a fixture larger than the code it tests is a cost every later item pays.

Before review, check the plan against itself: the registration list equals the next slice plus the later lines; the order respects every dependency; no item needs something a later item creates; every gate names who releases it and on what evidence; the owner-input block is complete. Cut anything that is not the brief, the inputs, the next slice, the later lines or the configuration.

## Phase 3: one independent review

One fresh reviewer at the review tier reads the plan from disk with its sources, the repositories and the AutoBuild contract by path, without the author's narration. It reviews the next slice for four things: the mandate is covered by the slice or a later line; the slice can run as written (order, gates, validator, item size, environment); repository claims match the code and nothing duplicates an existing source of truth; and how the slice could damage production, a user, another repository or the run. A slice that crosses repositories or touches a specialist boundary may add one named specialist reviewer; that is an exception the plan states, not a default.

The reviewer returns `PASS`, `PASS_WITH_FOLDS` or `FAIL`. A blocker names the acceptance criterion it violates, a defect in the code, or a consequential risk, with evidence. Everything else is advisory; the author records, schedules or dismisses it with a reason. The author verifies each blocker against the code, corrects the plan, and sends only the corrected parts to a fresh reviewer. Three rounds are the limit: after the third, the open blockers go to the owner with the plan. Record each round in `review-verdict.md` beside the plan.

## Phase 4: register and stop

1. Register the next slice in each repository's tracker in dependency order, with briefs as `../../docs/running-autobuild.md` describes. Every brief carries `Item nature` and `## Declared paths`; AutoBuild reads both at claim without a model call.
2. Register each later line as one item, blocked with gate `scope` (or `Blocked` in `BACKLOG.md`), its brief a pointer to its line in the plan.
3. Owner gates (`pinax block <id> --gate decision`, or `Blocked`) are only for genuine owner decisions and owner acts: a strategic or irreversible choice, a public release, a production deployment, a physical act the agent cannot perform.
4. `machine` and `cross-repository` items are the coordinator's work, done in the attended setup window or between items. Register them blocked so the application never claims them, because a fenced worktree cannot reach a machine or another repository, and name the coordinator and the evidence in the release clause. They are never questions for the owner.
5. Registration commits ride the default branch. Index the plan folder where the project indexes plans, and record any durable lesson where the project keeps them.
6. **Report and stop.** Give the owner the brief, the probe result, the verdict, the owner-input block and the launch command. One reply from the owner answers the inputs and gives the launch instruction.

## Acceptance criteria

- Prior material was found and the plan links to its scope source.
- A real probe of the riskiest step ran in a scratch copy, and its result is in the plan.
- The plan is the brief, the owner-input block, the next slice in full and one line per later item, with the road through deployment and publication visible.
- The owner's inputs fit one reply, and no gate asks the owner something the coordinator can establish from evidence.
- Every acceptance criterion in the next slice names the wrong answer it rejects.
- One independent review ran on the plan from disk, blockers were verified against the code, and the verdict trail lives beside the plan.
- The tracker answers "what would the run do?" with one status call, and the owner holds the launch trigger.
