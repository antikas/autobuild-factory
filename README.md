# autobuild

You run an AI coding agent on a project that has a real backlog. Every item needs the same ritual: pick the next piece of work, brief the agent, wait, review the diff, run the tests, commit, and update the tracker. Doing that by hand costs you the evening. Letting one agent run unattended costs you differently: it trusts its own work, wanders off scope, burns an hour rerunning test suites, or reverts something half-finished that you wanted to keep.

Autobuild is a pair of Claude Code skills that run a project's backlog for you, with the controls built in. A fresh agent builds each item, then two independent reviewers who did not see the build judge it from the artefacts alone. A coordinator accepts the item, sends it back for a bounded fix, or parks it with a written reason. Every accepted item becomes a commit on a dedicated branch plus a status event on the tracker, so the morning after an overnight run you read a short report instead of a transcript. When the queue runs dry, the system reads the project's own docs, proposes new work, and leaves the proposals for you to curate. Nothing destructive, nothing irreversible, and nothing outside the repo ever runs without you.

## What is in the box

- **`skills/autobuild/`** runs the backlog. It holds the skill contract ([SKILL.md](skills/autobuild/SKILL.md)), the orchestration engine ([autobuild.workflow.js](skills/autobuild/autobuild.workflow.js)), the safety rules every agent must obey ([GUARDRAILS.md](skills/autobuild/GUARDRAILS.md)), and a small probe that reads your live Claude subscription usage ([quota.py](skills/autobuild/quota.py)).
- **`skills/autobuild-plan/`** prepares the backlog. It turns a one-line ask ("make X buildable end to end") into a researched plan, has four independent reviewers attack that plan, folds their findings in, registers the resulting queue on the tracker, and stops one command short of launching the run ([SKILL.md](skills/autobuild-plan/SKILL.md)).
- **A tracker (yours, optional):** every cycle writes its status back to a tracker, so the run leaves a replayable trail. Autobuild pairs naturally with [pinax](https://github.com/antikas/pinax), a git-native build tracker published separately; a plain `BACKLOG.md` ready set works too.

The two compose: `/autobuild-plan` produces the queue, you review it and pull the trigger, `/autobuild` executes it.

## What you can watch it do

1. In a Claude Code session inside your repo, you type `/autobuild`.
2. The session checks for an interrupted earlier run first. If one exists, it resumes it from the evidence in git. Otherwise it creates a dated run branch and an isolated working copy of the repo, so nothing it does can collide with your open editor.
3. It reads the ready items from your tracker, arms a monitor on the run branch, and starts the loop.
4. For each item you see commits appear on the run branch, one per accepted item, each merged back to your default branch as it lands. Items that fail their audit get up to two bounded fix rounds. Items that need a human decision get parked with the reason recorded on the tracker.
5. When the queue is empty the run proposes follow-on work into `docs/PROPOSED-BACKLOG.md` and stops.
6. You read the closing report: shipped, parked, proposed, failed, next.

## How a single item travels

A scout agent reads the tracker and picks the next ready item in dependency order. It classifies the item as trivial, standard, or hard, checks it against the eligibility filter, and writes a full brief. The scout only selects work; it never builds. (Seat name in the code: Proskopos.)

The brief is enriched with known traps. If you have connected a memory tool, the system queries it for past errors on this repo so the builder does not re-solve a solved problem. If you have not, this step quietly does nothing.

A router assigns the item to a model by its class: a cheap model for trivial mechanical work, a standard model for normal work, the strongest available model for hard work. Each build starts in a fresh context with no memory of earlier items. The builder implements the full brief, runs only the narrow validation the item names (its type checks plus the tests its change touches), and reports its evidence. It does not commit. (Seat name: Chiron.)

Two auditors then judge the work independently. Each gets the spec and the changed files, and neither saw the build happen, so the builder's claims carry no weight; only the artefacts do. They read rather than re-run, and either can fail the item. (Seat name: Kritos.)

A coordinator holds the full picture and decides: accept, fold (send back with a bounded fix list, at most twice), or park. Parking restores the working tree to clean and writes a structured reason to the tracker: which phase failed, which command, which error, where the evidence lives. (Seat name: Epistates.)

On accept, a mechanical close agent commits exactly the item's files, appends a completion event to the tracker, merges the run branch into your default branch, and pushes. Then the loop picks the next item.

When the queue drops below a threshold, a refill pass reads what the project is from its own README and docs, proposes candidate features, and has a blind critic score them against the project's thesis. Clear, small, reversible candidates can be promoted into the live queue if you allow it; everything else stays in `docs/PROPOSED-BACKLOG.md` for you. Ideas that are still a direction rather than a precise question go to a separate fog ledger so they are captured without being built.

## People set the rules

Every control is a plain value you pass when you invoke the workflow, and you can change any of it per repo, per run:

- **Which model fills which seat** (`modelPolicy`). The defaults put the strongest tier on judgment and routine tiers on routine work. If your plan has no premium tier, set the coordinator to `opus` and everything still works.
- **What counts as validation** for this repo (`validatorCmd`), written once into your own config table and reused.
- **Which items the run may never pick**: deploys, publishes, and anything else you gate stay blocked on the tracker by name.
- **How hard to push**: items per run (`maxItems`), fix rounds per item (`maxFolds`), whether an empty queue triggers proposals (`proposeWhenDry`), and whether any proposal may auto-promote (`promotePolicy`).
- **When to stop for quota**: unsupervised runs stop cleanly at a weekly Claude usage ceiling (88% by default, `weeklyCeilingPercent`) so they never eat the window you wanted for your own sessions.

## Building on two subscriptions at once

Optionally, the run can spread builds across Claude and a ChatGPT subscription. You give each item class an ordered ladder of model names, for example `buildStandard: ['gpt-5.5','sonnet']`. Claude models run as normal subagents; OpenAI models run through the `codex` CLI signed into your ChatGPT account, with no API key anywhere. When one provider hits a usage limit, that lane cools down and the router moves to the next rung, and a shared state file lets concurrent runs on other repos see the same signal. When every build lane is cooling, the run stops cleanly and stays resumable. Review quality does not bend: judgment seats only ever run on a capable tier, in a fresh context, whichever provider that is. This whole mode is off unless you pass `lanePolicy`.

## What keeps it honest

- Builders never audit and auditors never build. Every audit starts blind, in a fresh context, from the artefacts.
- The working copy is isolated. Agents operate only in the run worktree, on the run branch. They never touch your default branch directly and never force-push.
- The scope fence is mechanical. When you hand the run a fixed list of items, an item outside that list stops the run, even if the scout liked it.
- Destructive and irreversible actions are never executed. They come back as written proposals. Deploys and publishes stay parked behind named gates.
- Verification is budgeted. Scoped checks per item, the full suite once per run, and an auditor who catches over-verification as a defect, the same as under-verification.
- A run that cannot be observed does not run: a monitor is armed on the evidence stream before the first item is dispatched.
- Interruption loses nothing. A killed session is resumed from git evidence in the same branch and worktree, and a half-finished build is snapshotted before anything else may touch the tree.

The checks do the trusting. No agent's self-report is taken on faith, including the models' own claims about their work.

## What this is not

- It does not deploy, publish, or touch production. Those steps are registered on the tracker as blocked and wait for you.
- It does not attempt novel architecture. An item with no precedent to follow gets parked with the decision it needs.
- It is not for single items. If you already know the one thing you want built, brief a builder directly.
- The quota probe reads an undocumented endpoint that the Claude client itself uses. It can stop working at any time, so it is wired as a soft signal only.
- The dual-subscription mode needs your own ChatGPT subscription with the `codex` CLI signed in, and is off by default.

## For engineers

Layout:

```
skills/
  autobuild/            the execution skill
    SKILL.md            contract: seats, lanes, invocation, recovery
    autobuild.workflow.js   the engine (runs under Claude Code's Workflow tool)
    GUARDRAILS.md       the rules injected into every agent prompt
    quota.py            live subscription-usage probe (optional, soft signal)
  autobuild-plan/       the planning skill
    SKILL.md            contract: research, plan shape, blind review, registration
```

Install: copy both skill folders into `~/.claude/skills/` (so the workflow lands at `~/.claude/skills/autobuild/autobuild.workflow.js`, the path the skill invokes). Requirements: Claude Code with subagents and the Workflow tool, and a repo with a tracker. The tracker is either [pinax](https://github.com/antikas/pinax) (a git-native build tracker; autobuild reads `pinax next` and writes `pinax done` / `pinax block` events) or a plain `BACKLOG.md` with a ready set.

First run: do one supervised single-item run end to end before any unattended use, and read [GUARDRAILS.md](skills/autobuild/GUARDRAILS.md) first; it is the SSOT the skills point at. Details, the full config reference, and recovery procedure live in [skills/autobuild/SKILL.md](skills/autobuild/SKILL.md).

License: MIT.
