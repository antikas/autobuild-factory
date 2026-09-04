---
name: autobuild-plan
description: Turn one build request into an AutoBuild-ready plan and registered queue. The skill researches the current system, covers build through publication, runs four independent review lenses, and stops before launch. Use autobuild for execution. Use a builder directly for one specified item.
user-invocable: true
---

# autobuild-plan

This skill produces the plan that `/autobuild` later executes. It researches the request, writes the plan, runs the review gate, and registers the queue. It stops before launch unless the owner has already authorised execution.

**SSOTs this skill points at, never restates:** the sibling AutoBuild skill (`../autobuild/SKILL.md`) for invocation and `../../docs/running-autobuild.md` for tracker, validator and refill configuration. If your environment carries its own dispatch rules or agent definitions, read those directly too.

## Phase 0: ground the request

1. **Search memory before files:** use an available memory or recall tool to find the prior specification, plan and decisions. Search prior plans and decision documents when recall is unavailable. Read harness and dispatch rules directly.
2. **Read the scope source yourself:** follow the prior specification and its decision register. Treat a specification older than the repository as a hypothesis until the current code confirms it.
3. Read tracker state once in every affected repository. Use `pinax status --json` on a [Pinax](https://github.com/antikas/pinax-tracker) repository; otherwise read the supported backlog file. Confirm that the request is still unexecuted and identify existing items that affect its order.

## Phase 1: inspect each affected surface

Use one capable scout per surface, in parallel when the host supports it, with structured output and per-answer file evidence. Pin the implementation-capable model tier available in the current host. Standard surfaces:
- **the build repositories:** compare the documented extension method with the latest implementation. Inspect coupling checks, test lanes, the declared validator, deployment code, repository drift, and pinned upstream data.
- **every publication surface** implied by "end to end": inspect its structure, the files one new entry changes, and its deployment state.
- **the programme docs:** open decisions and named gates touching the ask; what has been executed since the spec was written.

Ask scouts to report contradictions in the request's assumptions. The author reads the pivotal decisions and latest review precedent directly. Scouts may summarise supporting sources.

## Phase 2: write the plan

Create one plan at `plans/<date>-<slug>/plan.md` in the knowledge repository or the target repository's `docs/plans/` directory. Use this structure:

1. **Current position:** give a plain English summary that a new reader can follow.
2. **Mandate:** state the owner's request and the gates that authorise activation and launch.
3. **Changes since the source specification:** number each evidenced change and explain its effect. Link to the specification as the scope source.
4. **Decision table:** give reversible decisions a named default, precedent and override window. Register open or irreversible decisions as blocked owner gates. Keep content that needs a named approver in draft when precedent exists; otherwise park it.
5. **Queue:** give each item its scope source, change notes, lane, dependencies and testable acceptance criteria. Set each item's `Item nature` and `## Declared paths` so triage can classify it before a claim. Cover build, go-live preparation, blocked production deployment, publication updates, and blocked publication. State the park path for any item that needs a person or production access during its cycle.
6. **Dependency graph:** show the item order and how the new queue fits the existing queue.
7. **AutoBuild configuration:** name the invocation, builder and reviewer models, validator, gate types and observation settings.
8. **Constraints and measurable success criteria:** include the live result, publication surface, visible deferrals, and the record for each autonomous decision.

## Phase 3: run the independent review

Use four fresh reviewer sessions and run them in parallel when the host supports it. Give them the plan, sources, repositories and AutoBuild contract by file path. Keep the author's narration outside the review.

1. **Coverage and dependencies:** check the complete mandate and the real item order.
2. **Autobuildability:** check unattended execution, gate types, validator choice, lanes and item size.
3. **Architecture and source ownership:** check repository claims against code, reuse existing patterns and prevent duplicate sources of truth.
4. **Premortem:** describe how the plan could cause production damage, user harm, cross-repository failure, a costly decision or a failed run.

Each seat returns `PASS`, `PASS_WITH_FOLDS`, or `FAIL`. It lists file-backed blockers, direct folds and the claims it checked. Verify every blocker against the code, apply accepted folds, and repeat failed reviews with fresh seats. Record every round in `review-verdict.md` beside the plan.

## Phase 4: register the queue and stop

Only after PASS / PASS_WITH_FOLDS-with-folds-applied:
1. Register the queue in each repo's tracker in dependency order. Use `pinax add` on a Pinax repo. Otherwise add rows to the supported `BACKLOG.md` table with a `Ready` status and a brief reference, following `../../docs/running-autobuild.md`. Acceptance belongs in the brief, with the plan and spec named as its sources. Every brief carries an `Item nature` line (`repository`, `machine`, `cross-repository`, or `owner-gated`) and a `## Declared paths` section listing every path the item edits; AutoBuild reads both at claim without a model call. Owner-gated Pinax items get their gate typed **at registration** (`pinax block <id> --gate decision`); owner-gated backlog rows use `Blocked`, never `Ready`. Register `machine` and `cross-repository` items as blocked with a decision gate the same way, because a fenced builder cannot touch a machine or a path outside the repository; a person handles them outside the run. Registration commits ride the default branch (register-before-branch invariant).
2. Hygiene: index the plan folder wherever plans are indexed; log the day's delta; update memory if a durable lesson surfaced.
3. **Report and stop.** Give the owner the plain English plan summary, verdict, decisions and override windows, open gates, and launch command. Wait for the owner's launch instruction.

## Acceptance criteria

- Memory or focused search found the request's prior material. The plan links to the scope source and keeps each fact there.
- Every open decision has a precedent-backed default and override window, or a registered blocked gate. The plan contains no silent defaults or omissions.
- The queue covers build, live operation and publication. Owner-gated steps remain visible as blocked items.
- A four-lens blind review ran on the artefact from disk; blockers were code-verified by the adjudicator; the verdict trail lives beside the plan.
- The tracker answers "what would the run do?" with one status call; the owner holds the launch trigger.
