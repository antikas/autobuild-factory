# Set up and run AutoBuild

AutoBuild has two separate stages. The planning stage turns a build request into a reviewed plan and a registered work queue. The execution stage builds that approved queue, several independent items at once when the host allows.

The owner controls the boundary between them. Planning stops after queue registration. Execution starts only after the owner gives a separate launch instruction.

This guide covers both stages and a new repository setup. The [architecture guide](architecture.md) explains the internal design and extension points.

## Stage 1: plan and register the work

Use [`autobuild-plan`](../skills/autobuild-plan/SKILL.md) when the request still needs research, decisions, an executable plan, or tracker registration. A typical request is "create a plan to autobuild the account dashboard."

The planning stage:

1. Finds the existing specifications, decisions, plans, and live tracker state.
2. Inspects the code, tests, deployment path, documentation, and publication surfaces affected by the request.
3. Runs the riskiest real step once in a scratch copy, so the plan starts from what actually happens.
4. Writes a decision brief, one block of owner inputs, the next slice of work in full, and one line for each later item through deployment and publication.
5. Has one independent reviewer check the next slice, applies accepted corrections, and records the review result beside the plan.
6. Registers the next slice in [Pinax](https://github.com/antikas/pinax-tracker) or `BACKLOG.md`, and each later item as a blocked one-line item.
7. Registers owner decisions, production deployment, and publication as blocked owner gates. Machine and other-repository work is registered for the coordinator, not the owner.
8. Reports the brief, the probe result, the review result, the owner inputs, and the launch command, then stops.

The owner answers the inputs and approves the launch in one reply. The planning stage can write planning documents and tracker records. It does not start a builder or change product code. Its probe runs in a scratch copy and changes nothing shared.

If the repository already has an approved queue and complete briefs, start with stage 2. If the coding assistant cannot load `SKILL.md` files, follow the same planning steps manually and register the queue before execution.

## Stage 2: execute the approved queue

Use [`autobuild`](../skills/autobuild/SKILL.md) after the owner approves the plan and gives the launch instruction. Requests such as "run the backlog" or "autobuild this project" start this stage.

For each ready item, AutoBuild follows this sequence:

1. Check the coding assistant, authentication, tracker, Git repository, validator policy, and temporary work area.
2. Claim the next ready item and commit that tracker change. Protected delivery pushes it to the default branch; local PR delivery keeps it local until an optional current-branch push is authorised.
3. Create an isolated Git worktree from the claimed revision.
4. Start a fresh builder session with the approved brief and allowed tools.
5. Calculate the changed files and run the declared validator.
6. Start a fresh read-only reviewer with the brief, diff, and validator evidence.
7. Correct a blocking finding, ask a specialist when the finding names a specialist boundary, or park the item.
8. Create a product commit and a separate tracker commit for accepted work.
9. Deliver the item branch through the selected mode, one delivery at a time. Delivery fetches the target branch, merges it into the item branch, and runs the validator again only when the merge brought a change that overlaps the item. A pushing delivery pushes that exact commit and only then moves the local target branch to it. Local PR delivery updates the invoking branch and pushes only when separately authorised. Protected delivery updates the default branch, pushes it, and verifies the remote revision.
10. Continue until the queue is dry, the item limit is reached, or one hold on new admissions lasts `run.hold_limit_seconds`. An error in one item ends only that item.

The reviewer does not receive the builder transcript. The reviewer and boundary specialists receive the latest builder report as an evidence file in the run record, holding the builder's summary and report reference. It is the builder's claim, checked against the diff and the validator output. Each builder seat writes its own report file; a resume from a marker past the build hands no builder report. Accepted work must still match the diff that passed validation and review.

Every review finding carries a blocking flag. The reviewer blocks only for a concrete consequence it would not merge under its own name, and records every other reservation as a non-blocking finding on a passing verdict. A `correct` or `escalate` needs at least one blocking finding, a `park` needs at least one finding, and a `pass` carries non-blocking findings only. When a passing review carries non-blocking findings, AutoBuild accepts and delivers the item and records one non-runnable follow-up proposal per finding for the owner. The campaign report lists these proposals under Follow-ups. A follow-up's full text is kept in the repository, never only in the scratch root: the parent item's close briefing on its Pinax item page has a Review findings section with each finding's code, blocking flag, evidence reference and full consequence, and the campaign report has one subsection per proposal with the same text. The proposal's Pinax note points to that report and section, because a note caption holds at most 200 characters; with a BACKLOG.md tracker the full text and evidence reference are kept in the row. A refill proposal's note links its own brief, and its caption points to its section of the campaign report, where its full question and rationale are kept. A finding code or evidence reference that holds a line break is shown after its label in a fenced block, and the section heading that names it shows that line break as a space, so no reviewer text can split a section. When registering one follow-up fails, registration stops there, and the follow-ups registered before the failure keep their report section. A triage of a campaign's follow-ups therefore needs only a clone of the repository. A follow-up is never built by the campaign that registers it: it waits for a later campaign (see [Restrict a campaign to selected items](#restrict-a-campaign-to-selected-items)).

An item gets at most `max_corrections` correction rounds, and an escalation that the specialist answers with `correct` gets one more round only while the item is below that limit. In strict mode with a boundary map the review lanes replace `max_corrections`: a Small item gets one correction round and a Material item two, and the last one is judged by the validator without a review (see [Review that follows what a change touches](#review-that-follows-what-a-change-touches)). When the final round still ends on `correct`, AutoBuild does not park the item and does not ask the owner. The validator passed on the reviewed candidate, or on the final correction that it judged without a review, so the item is accepted and delivered as a passing item is, and every finding of the final verdict, blocking ones included, becomes a non-runnable follow-up proposal. A follow-up from a blocking finding says in its rationale that the finding was open at the round limit, and the item's trajectory names the round limit and the number of open blocking findings. The close gate accepts a `correct` verdict only with this round-limit mark and a validator that passed on the same revision as the diff. A validator that fails on any round, the final one included, still parks the item, and a `park` verdict, or an `escalate` that the specialist does not settle, also parks it.

The `autobuild` skill reads the project configuration and launches the Python command. You can also call the command directly without installing either skill.

The repository and source archive contain both skill folders. The Python wheel installs the `autobuild` command only. Install the skills through the coding assistant's normal skill method, or use their `SKILL.md` files from a checkout.

## Coordinated mode: run the queue inside the coding assistant

The `autobuild` skill offers a second mode at launch. In coordinated mode the coding-assistant session itself is the coordinator: it claims items, writes the briefs, dispatches a fresh builder seat and a fresh blind reviewer seat per item, adjudicates findings, integrates by merge, runs the post-merge validator in a detached worktree, closes the item in the tracker with an acceptance briefing, and keeps the campaign record. The project profile, tracker and validator are the same as in application mode; only the runner differs.

Choose coordinated mode when the owner wants to watch and steer, when items are design-heavy and need judgment-tier seats, when stops are expected to become new items mid-campaign, or when the application is not installed. Choose the application for a long unattended mechanical queue.

The mode is documented in [`skills/autobuild-coordinated/SKILL.md`](../skills/autobuild-coordinated/SKILL.md): the item cycle, the standing rules distilled from coordinated campaigns (`rules.md`), one adapter per runtime (`adapters/`), brief and record templates (`templates/`), the per-file staging, lane and scratch-audit scripts (`scripts/`), and the seat agent definitions (`agents/`). `agents/` holds the two custom agents a GitHub Copilot seat runs under and, in `agents/claude-code/`, the Claude Code seat definitions, one per effort level, which start each Claude Code seat at the profile's effort for that seat, or at `high` when the profile sets none. Install the skill folder through the coding assistant's normal skill method; for GitHub Copilot also install the two agent files to `~/.copilot/agents/` or a repository's `.github/agents/`. The [Claude Code adapter](../skills/autobuild-coordinated/adapters/claude-code.md) describes where the Claude Code seat definitions install.

## Project inputs for execution

Stage 2 needs these project facts:

- a Git repository with an `origin` remote; it must be writable for protected delivery or `--push-current-branch`
- an approved work queue in Pinax or `BACKLOG.md`
- one written brief for each runnable item
- one validator command that decides whether the change works
- an installed and authenticated coding assistant
- model names that the selected coding assistant can use

AutoBuild provides the campaign sequence, isolated Git worktree, fresh builder and reviewer sessions, evidence checks, tracker updates, commits, delivery through the selected mode, and a run record.

The coding assistant is called a harness in configuration. AutoBuild includes harness adapters for Claude Code, Codex, and GitHub Copilot CLI.

## Prerequisites

You need:

- Python 3.11 or later
- Git
- `uv`
- a supported coding assistant command
- Pinax only if you choose the Pinax tracker

The target repository must have at least one commit, a detectable default branch, and a remote called `origin`. Local PR delivery requires a named invoking branch. Protected delivery and an explicitly pushed current branch require a writable remote; local PR delivery without `--push-current-branch` commits tracker claims locally.

Check the repository from its root:

```text
git status --short
git branch --show-current
git remote get-url origin
```

Start with a clean working tree. AutoBuild refuses to claim an item from a dirty primary checkout.

## Install AutoBuild

Release 0.11.0 is published to PyPI as `autobuild-factory` and provides a Python wheel and source archive on the [GitHub release page](https://github.com/antikas/autobuild-factory/releases/tag/autobuild-factory-0.11.0). Install the released command from PyPI:

```text
uv tool install autobuild-factory==0.11.0
```

AutoBuild 0.7.0 needs pinax-tracker 0.2.1 or later when the project uses Pinax. From 0.7.0 Pinax commits every tracker change itself and AutoBuild only verifies that commit; an earlier Pinax leaves the change uncommitted and AutoBuild stops at the first claim. Do not use AutoBuild 0.6.0 or earlier with pinax-tracker 0.2.1 or later: those versions commit the tracker change themselves and refuse a tracker that has already committed it.

Check the command:

```text
autobuild --help
autobuild run --help
```

For development from a clone, run:

```text
uv sync
uv run autobuild --help
```

## Set up AutoBuild on macOS

macOS is a platform choice. It does not determine which coding assistant you use. After this setup, choose Claude Code, Codex, or GitHub Copilot in the next section.

Install Apple's command-line tools if Git is not already available:

```text
xcode-select --install
git --version
```

Install `uv` with Homebrew, then install AutoBuild:

```text
brew install uv
uv tool install autobuild-factory==0.11.0
autobuild --help
```

If you use Pinax, install it as well:

```text
brew install pipx
pipx ensurepath
pipx install pinax-tracker
pinax --help
```

You do not need Pinax when the project uses `BACKLOG.md`. Continue with the coding assistant and tracker sections below. AutoBuild uses the macOS system temporary directory unless you supply `--scratch-root` or set `scratch_root` in the project profile.

## Install and authenticate one coding assistant

The coding assistant is independent of the operating system. Choose one harness for a campaign and install it using its official instructions:

- [Claude Code setup](https://docs.anthropic.com/en/docs/claude-code/getting-started)
- [Codex CLI](https://github.com/openai/codex)
- [GitHub Copilot CLI installation](https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/install-copilot-cli)

Check the command and the authentication path that AutoBuild probes.

Claude Code:

```text
claude --version
claude auth status --json
```

Codex:

```text
codex --version
codex login status
```

GitHub Copilot CLI:

```text
copilot version
gh auth status
```

### GitHub Copilot CLI

Install GitHub Copilot CLI on the platform where AutoBuild will run. GitHub supports WinGet on Windows, Homebrew on macOS and Linux, and npm on all three platforms.

Windows:

```text
winget install GitHub.Copilot
```

macOS or Linux with Homebrew:

```text
brew install --cask copilot-cli
```

Any platform with Node.js 22 or later:

```text
npm install -g @github/copilot
```

See [GitHub's installation guide](https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/install-copilot-cli) for other supported methods and current prerequisites.

AutoBuild needs a non-interactive authentication source. The Copilot adapter accepts `COPILOT_GITHUB_TOKEN`, `GH_TOKEN`, or `GITHUB_TOKEN`. It can also use a successful GitHub CLI login:

```text
gh auth login
gh auth status
copilot version
```

The GitHub account must have an active Copilot entitlement and permission to use Copilot CLI. This setup works independently of the platform instructions. For example, you can use it on Windows without following the macOS section.

AutoBuild records the observed command version. The chosen builder, reviewer, and specialist model names must exist in that harness.

## Write an approved item brief

Each runnable item needs a brief that a builder and reviewer can follow without a conversation. Keep the brief in the repository when possible.

Example `docs/items/APP-001.md`:

```markdown
# Add the account summary

Item nature: repository
Item class: default

## Outcome

Show the signed-in customer their account name and current balance on the dashboard.

## Scope

- Use the existing account service.
- Follow the dashboard component pattern already in the repository.
- Keep the current currency formatting rules.

## Acceptance

- The dashboard shows the account name and formatted balance.
- The empty and error states remain usable.
- The declared project validator passes.

## Declared paths

`src/dashboard/account_summary.tsx` (new), `src/dashboard/index.ts`.

## Outside scope

- No account editing.
- No deployment or publication.
```

Every brief carries two triage lines that AutoBuild reads before it claims the item, without a model call. The `Item nature` line names the class: `repository` work builds inside the isolated worktree, while `machine`, `cross-repository`, and `owner-gated` work cannot and is parked at once with reason `nature:<class>`. An absent line means `repository`. The `## Declared paths` section lists every path the item edits; any path that resolves outside the repository and the configured `allowed_roots` also marks the item `cross-repository`. A token that starts with `/`, has no file extension in its last segment, and names nothing that exists, such as the route `/account/home`, is not a path: AutoBuild ignores it for triage, path overlap, review scope and test selection. Register `machine` and `cross-repository` items as blocked, not ready, so a person handles them outside the fence. The preflight `briefs` probe applies the same rule, with the same repository root and allowed roots, before any claim: a brief whose `Item nature` line is absent or names `repository` and whose declared paths would mark it `cross-repository` stops the launch.

An item whose brief orders the builder to run a git staging command (see the `briefs` probe under [Preflight](#preflight)) is parked at claim with a reason that starts `brief:git-staging` and names the first refused line and its invocation, without a claim, a worktree or a seat; the run record's `item.triaged` event lists every refused line. The campaign goes on with the next item and ends with exit code 1, as after any parked item. To fix the brief, describe a git command only in a clause that forbids it, such as "never run `git commit`", in a report of what was done, or in a quotation line that starts with `>`.

The tracker stores the path to this file. AutoBuild passes the same brief to the builder and reviewer.

An absolute brief path is allowed only when its parent directory appears in `policy.allowed_roots`. The harness receives read access to that directory. Repository briefs need no extra root.

## Choose the tracker

AutoBuild supports Pinax and a Markdown backlog. Both adapters implement the same queue, claim, close, park, and proposal operations.

Use Pinax when the repository needs dependencies, event history, concurrent writers, and generated status views. Use `BACKLOG.md` for a small queue that must work without another tracker command.

The default tracker setting is `auto`:

1. AutoBuild selects Pinax when `.ergon/` exists and the `pinax` startup probe passes.
2. It selects `BACKLOG.md` when Pinax is unavailable and a supported backlog file exists.
3. It checks `docs/BACKLOG.md` when the root backlog file is absent.
4. It stops before a claim when neither tracker is usable.

If both trackers are usable, Pinax wins. Selection happens during startup and stays fixed for the campaign. AutoBuild does not switch trackers after a claim.

## Set up Pinax

Pinax is a Git-native tracker. Its event log and generated views live in `.ergon/` inside the repository.

Install pinax-tracker 0.2.1 or later. AutoBuild 0.7.0 and later need it, and earlier AutoBuild versions must not be used with it:

```text
pipx install "pinax-tracker>=0.2.1"
pinax --help
```

The [Pinax repository](https://github.com/antikas/pinax-tracker) also documents installation with `pip`.

Initialise the target repository and add an item. `pinax init` creates `.ergon/` without committing it, so commit that once; every later Pinax command commits itself:

```text
pinax init --actor your-name@your-machine
git add .ergon
git commit -m "ADDED: initialise Pinax"
pinax add --title "Add the account summary" --prefix app --allow-new-prefix --actor your-name@your-machine --json
```

The add command returns an item identifier. Attach the approved brief to that item:

```text
pinax note add <item-id> --ref docs/items/APP-001.md --caption "The dashboard shows the account name and formatted balance." --actor your-name@your-machine --json
```

Every mutating Pinax command (add, note add, claim, done, block, park, dep add) commits its own change, and pushes it when the remote default branch is checked out. After the initial commit, never stage or commit `.ergon` by hand. Commit and push the brief:

```text
git add docs/items/APP-001.md
git commit -m "ADDED: brief for the account summary"
git push origin HEAD
pinax status
```

AutoBuild uses `pinax next` ordering. A runnable item needs an approved brief note. Pinax dependencies and gates decide whether the item is ready.

Select Pinax explicitly when a missing or broken Pinax command should stop the run:

```toml
[tracker]
kind = "pinax"
```

## Set up BACKLOG.md

Create `BACKLOG.md` at the repository root. You can use another path through configuration.

The file needs one Markdown table with these four columns:

```markdown
# Backlog

| Item | Title | Status | Brief |
|---|---|---|---|
| APP-001 | Add the account summary | Ready | docs/items/APP-001.md |
```

Table order is queue order. AutoBuild selects the first runnable row.

These status values are runnable:

- `Ready`
- `Approved`
- `Queued`
- `Todo` or `To do`
- `Not started`

AutoBuild writes these status families:

- `Claimed by <actor>` after it claims an item
- `Done (<commit>)` after accepted delivery
- `Parked: <reason>` after a parked outcome
- `Proposed: <question> <rationale>` for refill proposals

Claimed, done, parked, blocked, and proposed rows are not runnable. Item identifiers must be unique. Required cells cannot be empty. Keep table cell text on one line and avoid the `|` character inside a cell.

Commit and push the backlog and briefs before the run:

```text
git add BACKLOG.md docs/items/APP-001.md
git commit -m "ADDED: register account summary work"
git push origin HEAD
```

Select the backlog explicitly:

```toml
[tracker]
kind = "backlog"
path = "BACKLOG.md"
```

For a one-run local PR override, use:

```text
autobuild run --repository . --tracker backlog --backlog docs/QUEUE.md --delivery-mode current-branch-pr --allow-delivery
```

## Create the project profile

Create `.autobuild.toml` at the repository root. This file supplies facts that AutoBuild must not guess.

AutoBuild refuses unknown top-level profile fields and unknown fields in every profile table. It reports every unknown field in one error. `[run.item_classes]` class names, `[lanes]` lane names and `[process.test_map]` patterns remain open names; a class value must be a positive number of seconds, and a lane table must use the documented fields.

```toml
[run]
harness = "codex"
max_items = 10
seat_timeout_seconds = 900
seat_stall_seconds = 900
command_timeout_seconds = 600

[run.item_classes]
large = 7200

[tracker]
kind = "auto"

[models]
builder = "gpt-5.6-sol"
reviewer = "gpt-5.6-sol"
specialist = "gpt-5.6-sol"

[validator]
id = "tests"
argv = ["uv", "run", "pytest", "-q"]

[policy]
allowed_tools = ["read", "write", "shell", "python", "git"]
allowed_roots = []
```

### Run settings

`harness` is `claude-code`, `codex`, or `github-copilot`. For more than one lane with automatic failover, use `run.lanes` and `[lanes.*]` tables instead, described in "Configure harness lanes and failover".

`max_items` limits accepted or parked item cycles in one campaign. The default is 20.

`seat_timeout_seconds` limits each builder or reviewer process. The default is 900 seconds. It is the wall-clock cap for the `default` item class.

`seat_stall_seconds` is the progress deadline. AutoBuild kills a seat only when its output, its worktree, and its direct child process all show no progress for this long. A slow but working seat is never killed for being quiet, and a seat whose output alone is silent keeps running. The sampling interval never exceeds 60 seconds. The default is 900 seconds.

`[run.item_classes]` maps a class name to its expected wall-clock cap in seconds, for example `large = 7200`. A brief selects its class with an `Item class: <class>` line; an undeclared or unknown class means `default`, which uses `seat_timeout_seconds`. AutoBuild acts at the class cap and raises the seat-timeout ceiling to the largest declared class so a class cap is never refused.

While the host pressure level is `high` or `critical`, a running seat's cap grows by every second it spends there, up to twice its class cap. A `default` seat with a 3600-second cap that runs 1800 seconds at `high` has a cap of 5400 seconds; under pressure for its whole run its cap stops at 7200 seconds. The `seat.completed` event records the added seconds as `timeout_extension_seconds` when there are any. A serial campaign runs no pressure monitor, so its caps never stretch.

`command_timeout_seconds` limits the validator process. The default is 600 seconds.

`batch_validate_every` sets how many deliveries pass between batch validations. The default is 5. After that many deliveries, and once at campaign end when anything was delivered since the last one, AutoBuild runs the validator once on the delivery target head in a detached scratch worktree. See "Batch validation" under "Changes made by accepted and parked outcomes".

`hold_limit_seconds` limits one hold on admissions. A hold starts when a condition blocks all new work, for example every lane is cooling, a tracker write is not committed yet, or the tracker does not answer; "Holds" lists every condition. When one hold lasts this long, the campaign ends with the `hold_limit` stop reason. The default is 21600 seconds (six hours). See "Holds" under "Campaign result".

### Concurrency settings

`concurrency` sets how many items a campaign builds at once, and `heavy_concurrency` how many heavy commands, such as full validators, may run at once. Each is `"auto"` or a positive integer, and both default to `"auto"`, so a profile that does not name them builds as many items at once as the host allows. Set `concurrency = 1` to build one item at a time: that is the serial setting, and it writes no scheduling progress lines or events. An integer replaces the automatic value, and an explicit `concurrency` above 64 is refused when the profile loads. The heavy value never exceeds the item value. A `BACKLOG.md` campaign builds one item at a time unless the profile names an integer, because the backlog cannot express dependencies. The optional `[capacity]` table sets the terms of the automatic formula:

```toml
[run]
concurrency = "auto"
heavy_concurrency = "auto"

[capacity]
reserve_gb = 8
per_item_gb = 3
max_concurrency = 8
```

Memory values are GiB (2^30 bytes). With `"auto"`, AutoBuild reads the host once and derives:

- items: the smaller of `logical CPUs // 4` and `floor((available memory - reserve_gb) / per_item_gb)`, then held between 1 and `max_concurrency`;
- heavy commands: `logical CPUs // 12`, held between 1 and the item value.

When the host cannot measure available memory, the memory term is dropped. When it cannot count its CPUs, both values are 1. An explicit `concurrency` is not bounded by `max_concurrency`. AutoBuild refuses unknown `[capacity]` fields like any other unknown field.

The default `reserve_gb` and `per_item_gb` are starting values, not measurements. Watch your own items' memory use and set `[capacity]` to match.

With a concurrency above 1, every validator run that AutoBuild starts, for the reviewed candidate or for an integrated one, takes a heavy slot, and at most the heavy value run at once; the others wait for a slot. Commands a builder runs inside its own seat are outside AutoBuild's control; the pressure monitor below covers them, because its readings are system-wide.

#### Host pressure

A campaign with a concurrency above 1 watches system-wide CPU and memory while it runs, so the owner's other work and another campaign count. Every 10 seconds it reads the host, measuring CPU busy over 1 second. A window at time t holds the samples taken in [t - 60 s, t]. The levels are:

- `high`: a mean CPU busy of at least 90% over a full 60-second window, available memory below the larger of 10% of total memory and 4 GiB, or commit headroom below 10% of the commit limit;
- `critical`: available memory below the larger of 5% of total memory and 2 GiB;
- `normal`: every CPU sample of the last 120 seconds below 75%, and available memory at least 2 GiB above the high line;
- `recovering`: the span between leaving `high` or `critical` and reaching `normal`.

On a 32 GiB host the high line is 4 GiB and the critical line 2 GiB; on a 64 GiB host they are 6.4 GiB and 3.2 GiB.

Only `normal` admits new items. At `high` and `critical` no new heavy command starts either, and each further 5 minutes there lowers the effective item cap by one, down to 1. At `recovering` heavy commands start again but items are still not admitted. Back at `normal` the cap rises by one every 60 seconds, up to the concurrency. Back-off never stops anything that has started: a running seat, validator or reviewer runs to its own end or its own timeout. With nothing running, a pressure wait lasts until the level is back at `normal`; it is not a hold, and `run.hold_limit_seconds` does not bound it.

Pressure never takes the last slot. With nothing of its own running, a campaign always admits one item, whatever the level, so load from other work on the host cannot leave it idle; the hold applies only to admissions beyond the first. In the same way, with no heavy command of the campaign running, one heavy command starts at any level, in its patient form.

A validator run that times out while the level was `high` or `critical` at any point of its run is suspected of failing from load. It is run once more, alone: it waits until no other heavy command runs, and no other starts until it ends. The rerun's verdict stands. A timeout without recorded pressure, or any other failure, is not run again.

Every validator command id ends with the validator id, which is how the policy admits it: `<run>:<item>:<validator>` for an item, `<run>:<item>:rerun:<validator>` for the load rerun and `<run>:batch:<N>:<validator>` for a batch validation. The profile's `patient_argv` is approved with the validator.

When the first reading cannot measure CPU busy or memory, the campaign records one warning and keeps its static concurrency without pressure control. After a measured reading, a read that fails or lacks a field is skipped: the level, the cap, the heavy-command hold and the window stay as they were, and the first failure of a run records one `campaign.pressure_read_failed` warning. After three failures in a row the campaign records one `campaign.pressure_fallback` event and keeps the current level and cap; it never raises the cap or lifts a hold on a failed read, and only measured readings move them again. A run record that cannot be written does not stop the monitor. A serial campaign does not run the monitor.

The optional `[pressure]` table sets every threshold. These are the defaults; memory values are GiB:

```toml
[pressure]
sample_seconds = 10
read_seconds = 1
window_seconds = 60
calm_seconds = 120
high_cpu_percent = 90
calm_cpu_percent = 75
high_memory_percent = 10
high_memory_gb = 4
critical_memory_percent = 5
critical_memory_gb = 2
high_commit_percent = 10
calm_memory_gb = 2
decrease_seconds = 300
increase_seconds = 60
```

AutoBuild refuses unknown `[pressure]` fields, a non-number, a share outside 0 to 100, a non-positive interval, a `read_seconds` above `sample_seconds`, a `calm_cpu_percent` above `high_cpu_percent` and a negative memory value.

With a concurrency above 1, the campaign admits every ready item it can, up to the concurrency, and builds each in its own worktree on its own worker. An item is admissible when the tracker lists it as ready (no unmet dependency, no live claim), it passes the allow and exclude lists, and it declares no exact file path under `## Declared paths` that an item in flight also declares. Only exact paths count: two items that declare only a glob such as `tests/**` run together. After every delivery or park the campaign reads the ready items again, so a dependant starts once its blocker has merged. `max_items` counts every item the campaign runs, including resumed ones. Deliveries go through one queue, one at a time; an item that reaches delivery while another delivers waits its turn.

While the shared usage reading of the first lane is at or above `usage.warn_percent`, the campaign admits a new item only when nothing is running, so a run at the warning level behaves as a serial run. A lane without a meter, or one with no reading yet, holds nothing back. The stop level still applies before every seat.

No item error, exhausted lanes or tracker failure stops the campaign: running items go on to their own end, and a condition that blocks all work holds new admissions instead (see "Holds" under "Campaign result"). A stop (the item bound, a dry queue or the hold limit) withholds new admissions, and items already running continue to their own end (delivered, parked, or kept with their worktree and evidence) before the campaign ends. The tracker lease is renewed every `lease_stale_seconds / 3` while the campaign runs, not only between items. A failed renewal is retried on the next beat. After three failures in a row the campaign records one `campaign.lease_withheld` event and admits nothing new until a renewal succeeds; running items continue. If nothing is running while admissions are withheld, the campaign renews once itself; when that fails it starts a `lease_renew_failed` hold, which the hold limit bounds.

The progress log adds lines for each admission, for each wait with its reason (the concurrency cap, a declared path an item in flight also declares, the usage warning level, host pressure, or a tracker lease that is not renewing), for a stop that waits for running items, and for a delivery that waits behind others. A `pressure` wait is recorded once for each pressure episode in which a slot is free and an admissible item waits, however many items are queued; `high` and the `recovering` span after it are separate episodes. The log also has one line for each pressure level change with the CPU window mean and available memory, one for each change of the effective cap, one for an unmeasured host and one for each load-suspected validator rerun. A serial campaign writes none of these. Every campaign, serial or not, records `concurrency` (the item value, its source and the heavy value) in the run manifest, and its report adds a Schedule section with the concurrency, the most items that ran at once, the makespan from the first admission to the last completion, and the sum of every item's duration. With the pressure monitor running the section also gives the seconds spent at each level. The campaign result carries the schedule values under `schedule`, including `heavy_concurrency`.

Read the host and the derived values once with:

```text
autobuild host --json --repository /path/to/repository
```

The command is read-only and needs only the concurrency settings, so it works with a profile that has no models or validator. It prints the logical CPU count, CPU busy percent over `--sample-seconds` (default 1), total and available memory, the commit limit and the commit available under it, a `measured` flag for each field, any diagnostics, the profile values, and the derived `items` and `heavy` with their source (`auto` or `profile`) and the CPU and memory terms. A profile that does not name `concurrency` shows `auto`, the default, and the text output then reminds you that `concurrency = 1` builds one item at a time. An unmeasured field prints as `null`. It exits 0, or 2 when the profile cannot be read or `--sample-seconds` is not a positive finite number. On Windows the readings come from the kernel's system times and memory status. On Linux they come from `/proc/stat` and `/proc/meminfo`; the commit fields are reported only when `vm.overcommit_memory` is 2, because only then does the kernel enforce the commit limit, and available memory needs the kernel's `MemAvailable` field. On other hosts only the CPU count is measured.

### Model settings

`builder` and `reviewer` are required. `specialist` is optional and defaults to the reviewer model.

Use model names accepted by the selected harness. AutoBuild records the configured names and passes them to the harness command.

`builder_effort`, `reviewer_effort`, and `specialist_effort` are optional. Each one sets how much that seat's model thinks before and between actions, which is the main control over the seat's cost and time. The accepted values are `low`, `medium`, `high`, `xhigh`, and `max`. Any other value stops the launch with an error that names the key. `specialist_effort` defaults to `reviewer_effort`. When a key is absent, AutoBuild passes no effort for that seat and the harness applies its own configured default. In this single-lane form the effort keys belong in `[models]`; an effort key in a `[lanes.<harness>]` table stops the launch with an error that names it, because lane tables are read only when `run.lanes` is set.

```toml
[models]
builder = "gpt-5.6-sol"
reviewer = "gpt-5.6-sol"
builder_effort = "medium"
reviewer_effort = "high"
```

In this example the specialist runs at `high`, the reviewer effort. Each harness adapter passes the level through its own command option, listed in [Harness adapters](harness-adapters.md#seat-effort). The `seat.completed` run event records the effort AutoBuild requested for each seat. AutoBuild checks the level only against the harness adapter; whether the model applies it is decided by the harness when the seat starts. GitHub Copilot CLI 1.0.88 or later reports a level the model does not offer and leaves it unapplied, and Codex passes any level to its service, so choose a level the selected model offers.

When the validator does not pass on a builder's candidate, the first build or a correction round, the item does not park at once. A fresh builder seat continues in the same worktree one builder effort level higher, with the failed validator output as context, and the validator runs again on its candidate. The run record gains an `item.effort_raised` event, and the new seat's `seat.started` and `seat.completed` events and its start line carry the raised effort. Each red validator raises the level once more, never past `high`, and a raised level holds for every later builder seat of the item, so no attempt runs again at a setting that already failed. The raise starts from the effort the builder's current lane sets, so a lane at `xhigh` or `max` gets no raise and keeps its own level. The item parks with the validator reason when the builder already ran at `high` or above, when its lane sets no builder effort, or when the attempt at `high` also fails. A retry is not a correction round and does not count toward the correction limit. A red validator on resumed work is retried the same way: work resumed past the build is taken as built at the builder effort its current lane sets, so a red validator on it starts a fresh builder one level higher unless that lane sets no builder effort or is already at `high` or above. A builder continuation after a timeout keeps the raised level and the failed validator output of the seat it continues. An item resumed after a crash starts again from its lane's level, because its phase marker records no raises. The `effort` of `item.effort_raised` is the effort the next builder seat asks for on the lane it would start on, and `from_effort` is the effort of the seat that failed.

### Validator settings

`id` is a stable name for the validator evidence.

`argv` is the exact argument list to execute inside the isolated worktree. AutoBuild does not parse shell operators such as `&&`. Put multi-step validation behind a repository script, then name that script here.

Examples:

```toml
[validator]
id = "python-tests"
argv = ["python", "-m", "pytest", "-q"]
```

```toml
[validator]
id = "project-check"
argv = ["npm", "run", "check"]
```

The validator is the deterministic acceptance authority. The model cannot replace or change it during a run. The builder prompt therefore tells the builder, as a constraint, to run only the tests for the files it changed and never the full suite or the validator command. The opt-in [strict build process](#build-process) names those tests in the brief and forbids a whole-suite run.

`patient_argv` is optional. A validator run that starts while the host pressure level is `high` or `critical`, or was at that level during the previous 60 seconds, is patient. With `patient_argv` set it runs that argument list instead of `argv`:

```toml
[validator]
id = "tests"
argv = ["npm", "test"]
patient_argv = ["npm", "test", "--", "--runInBand", "--testTimeout=30000"]
```

Without `patient_argv` it runs `argv` with `AUTOBUILD_HOST_PRESSURE=1` added to its environment, so a project validator script can choose a serial run or longer timeouts itself. At `normal` neither form applies. The `validation.completed` event carries `patient: true` for a patient run.

### Tool and path policy

`allowed_tools` uses the semantic names `read`, `write`, `shell`, `python`, and `git`. Each harness adapter maps those names to its own permission flags.

`allowed_roots` adds approved directories outside the repository. Use it for an external brief or shared read-only reference. Paths can be absolute or relative to the profile.

The builder receives the isolated worktree plus the configured external roots. The reviewer receives read access to the same roots.

### Build process

The optional `[process]` table selects the build process. `mode` is `flow` or `strict`. A profile without the table, or a table without `mode`, runs `flow`, which builds and reviews an existing profile's items as 0.9.4 did; the one change in both modes is the seat environment variable described at the end of this section. Any other value is refused when the profile loads.

`strict` limits each builder seat to the tests that cover what it changed. It needs a test map:

```toml
[process]
mode = "strict"
test_command = ["uv", "run", "pytest"]

[process.test_map]
"src/app/profile/**" = ["tests/unit/test_profile.py"]
"src/app/api/**" = ["tests/unit/api", "tests/contract/test_api.py"]
"web/**" = "npm test -- web"
```

`test_command` is the argument list that starts the project's test runner. Each `test_map` key is a path pattern for source files: `**` spans directories, while `*` and `?` stay inside one path segment. Each value names the tests that cover matching files. An array lists test paths, which run after `test_command`; an array that is empty or holds anything but non-empty strings is refused when the profile loads, with one sentence that names the key. A string is one complete test command line, split by shell quoting rules, so `"npm test -- --grep 'profile page'"` keeps `profile page` as one argument; a string with an unclosed quote is refused. A map with test paths needs `test_command`. Strict mode without a test map is refused.

In strict mode the builder brief names the covering tests for the paths listed under the item's `## Declared paths` heading, and says a whole-suite run is not allowed. When no declared path matches an entry, the brief lists the whole map.

The rule is also checked where the builder's commands run. A builder seat in strict mode gets a pre-command check that passes every shell command line it runs to the refusal rule. The rule splits the line into its commands at `&&`, `||`, `;`, pipes, new lines and `( )` groups, also when a redirection follows with no space, as in `echo hi;>x pytest`, and ignores leading variable assignments. Only an unquoted `<` or `>` is a redirection; the rule drops it and its target. A quoted or escaped one, as in `-k "a>b"`, is part of an argument.

The rule looks past leading words that run the command after them: `env` with its options and assignments, `time`, `timeout` with its options and duration, `nice`, `nohup`, `command` and `exec`, and the reserved words `{`, `!`, `if`, `then`, `else`, `elif`, `do`, `while` and `until`. It reads the command string of `bash -c`, `sh -c` and their option clusters such as `bash -lc`, and judges each command inside it. So `timeout 600 uv run pytest`, `env CI=1 pytest`, `bash -lc "uv run pytest"` and `{ pytest; }` are judged like `pytest`.

A test run of the project's runner is any command that starts it. AutoBuild reads the runner from `test_command`, past any `uv run` and `python -m` in front of it. With `test_command = ["uv", "run", "pytest"]` the runner is `pytest`, so `pytest`, `python -m pytest`, `python3 -m pytest`, `uv run pytest`, `uv run python -m pytest`, `uv run --system-certs python -m pytest`, `uv -q run pytest` with a `uv` option before `run`, and a path ending in `/pytest` such as `.venv/bin/pytest` are all test runs. Such a run is admitted only when every test it selects is a covering test. A path is judged after its `.` and `..` segments are resolved, so `tests/unit/api/../..` is the whole `tests` directory.

A mapped command line, such as `"web/**" = "npm test -- web"`, is admitted only exactly as written and only when its entry covers the change. Any other command that starts the same program and the words before its first option, here `npm test`, is a run of that runner and is refused: `npm test`, `npm test -- api` and `npm test -- web --watch` are refused, and `npm install` is not a test run.

These are whole-suite runs and are refused with one sentence that names the covering tests:

- a run of the runner that selects no test path;
- a run that names a directory, including a bare `tests` or `.` with no slash, even alongside a covering test;
- a run that names a test outside the covering tests;
- a run of a mapped command line's runner that is not a covering line exactly as written.

Every bare word in a runner's arguments selects a test, also straight after an option such as `-q`, and every word after `--`. The one exception is the value of a runner option that takes one. AutoBuild keeps a small table of these per runner; for `pytest` it holds options such as `-k`, `-m`, `-p`, `--maxfail` and `--tb`, so the `profile` in `-k profile` selects no test. For a runner without a table, give an option's value as `--option=value`. A command that is not a test run, such as `git status`, always runs.

The check is a net for an ordinary mistake by a builder. It is not a barrier against a builder set on getting round it, and the reviewer and the validator remain the checks on the result. It does not see a test run inside a script or a Make target, behind an alias, a shell function, `eval`, `xargs`, `sudo` or a command substitution such as `$(echo pytest)`, through another spelling of a mapped command such as `npm run test`, through a runner the configured test command does not name such as `py.test` or `uvx pytest`, or in Python code such as `python -c "import pytest; pytest.main()"`.

How each lane carries the check:

| Lane | Pre-command check |
|---|---|
| `claude-code` | A `PreToolUse` hook on the Bash tool, passed in the seat's `--settings` launch argument. The hook denies a refused command, and the seat sees the refusal sentence. `--safe-mode` would switch the hook off, so this seat starts with `--setting-sources ""`, `--strict-mcp-config` and `--disable-slash-commands` instead; every other seat keeps `--safe-mode`. |
| `claude-sdk` | The same hook in the seat's flag settings, with the same options in place of `--safe-mode`. For a strict builder seat the lane lifts `disableAllHooks`; the seat loads no setting sources, so this hook is the only one it runs. |
| `codex` | None. The lane has no pre-command hook AutoBuild can install, so the brief alone carries the rule. |
| `github-copilot` | None. The lane has no pre-command hook AutoBuild can install, so the brief alone carries the rule. |

On the Claude Code lanes a managed policy can switch the hook off, for example with `disableAllHooks` or `allowManagedHooksOnly`, and AutoBuild is not told; the seat then runs with the brief alone carrying the rule.

The check is installed for builder seats in strict mode only. Flow mode, reviewer and specialist seats get no check. The validator that runs after the builder, the delivery revalidation and the batch validation are run by AutoBuild, not by a seat. They carry no check and still run the full `[validator] argv`.

#### Review that follows what a change touches

The optional boundary map names sensitive areas of the project. Each boundary is a table under `[process.boundaries]`: its key is the boundary's name, `paths` is a non-empty list of path patterns, matched like test-map keys, and `invariants` is the optional repository path of a file that lists what a change there must preserve.

```toml
[process.boundaries.sign-in]
paths = ["src/app/auth/**", "src/app/session.py"]
invariants = "docs/invariants/sign-in.md"

[process.boundaries.payments]
paths = ["src/app/billing/**"]
invariants = "docs/invariants/payments.md"

[process.boundaries.personal-data]
paths = ["src/app/profile/**", "src/app/export/**"]

[process.boundaries.schema]
paths = ["migrations/**", "src/app/models.py"]
invariants = "docs/invariants/schema.md"
```

In strict mode each review round runs the one blind reviewer, as in flow. When the item's paths touch a boundary, AutoBuild adds one specialist seat per boundary touched, in map order. The item's paths are the paths under its `## Declared paths` heading and the paths its diff changed. Each specialist's brief names its boundary and its invariants file, or says that none is named, and it reviews the change for that boundary only. A specialist finding that names no boundary is recorded under its seat's boundary. An item that touches no boundary gets the one blind reviewer only.

The round's verdict is the most severe of all its seats' verdicts: park, then escalate, then correct, then pass. It carries every seat's findings and is checked by the same verdict rule as each seat's own verdict. AutoBuild writes the seats' verdicts to one evidence file, named `<item>-review-round-<n>` by review round (round 1 is the first review, round n the review after correction n-1, and a resumed item keeps the count its phase marker carries), and records a `review.combined` event; a correction round receives that file. Each boundary seat records a `specialist.completed` event that names its boundary. A specialist that escalation calls in is a separate seat and works as it does in flow.

In strict mode with a boundary map, the map also sets the item's review lane and with it the number of review rounds. An item whose paths touch no boundary is Small; an item whose paths touch one is Material. The lane is chosen from the declared paths and the first diff before the first review, and again from the declared paths and each correction's diff once that correction's validation is green. Once an item is Material it stays Material: a correction that touches a boundary makes a Small item Material, and one that stops touching it does not make the item Small again.

- A Small item gets one review. A `pass` is accepted. A `correct`, or a specialist `correct` after an `escalate`, gets one correction round, and the validator judges that correction without a further review. A green validator accepts the item at the round limit with the verdict that asked for the correction, and every finding of that verdict becomes a follow-up whose rationale says the correction was judged by the validator and not reviewed again. A red validator parks the item. When that correction first touches a boundary, the item becomes Material and its next step is the second review, with that boundary's specialist.
- A Material item gets at most two reviews and two correction rounds. A `correct` at the second review, or a specialist `correct` after it, gets one final correction round, which the validator judges without a review, as for a Small item.

`item.finalised` carries `review_lane` as `"small"`, `"material"` or `null`, and the trajectory has the line `review lane: small`, `review lane: material` or `review lane: none`. A resumed item chooses its lane again from its declared paths and its current diff. When its marker is `built` or `validated` and its correction count has reached the lane's limit, 1 for Small and 2 for Material, it gets one review on resume, and that review is its final round: a `pass`, or a `correct` on a green validator with its findings as follow-ups, is accepted, and no further correction runs. Flow mode, and strict mode without a boundary map, have no review lane and keep `max_corrections` with a review after every correction.

A blank boundary name, an unknown key in a boundary, a boundary that is not a table, a missing or empty `paths`, a path that is not a string, or an `invariants` value that is not a file path string is refused when the profile loads, with one sentence that names the boundary when it has a name. The map is checked in both modes.

In `flow` none of this applies. The test map and the boundary map may stay in the profile, so switching modes is one key. Flow runs the one blind reviewer whatever the change touches.

#### Reviewer evidence and review depth

Every blind reviewer and boundary specialist gets the brief, the review patch and the validator output. The review patch holds the tracked diff and then the full text of every added text file, as a new-file hunk in the form `git diff` writes for a new file, so new tests reach the reviewer without it having to open them. Each added file also keeps its `# untracked <path> sha256=<digest>` line. A binary added file (one that holds a NUL byte or is not valid UTF-8) keeps the hash line only. When the patch would exceed 1 MiB, the largest added files fall back to the hash line and the line `# content omitted: <bytes> bytes, read <path> in the workspace` until it fits; a tracked diff that alone exceeds the limit is refused. The patch is written byte for byte with LF line ends on every platform, so its size on disk is the size held under the limit.

Both seats get the brief's acceptance clauses as a numbered checklist: the bullets under its `## Acceptance` heading, numbered `A1`, `A2` and so on. A brief without that heading has the tracker's acceptance lines numbered the same way. The seat is asked to read every changed file, new tests included, to judge each clause against the diff and the validator output, and to treat a clause the change does not deliver as a blocking finding. A specialist that escalation calls in is given no checklist.

The `review-verdict-v1` result carries two more required arrays. `inspected` lists each path the seat read with the line ranges it read, as `{"path": "src/app.py", "lines": "1-120,300-340"}` or `"lines": "all"`. `acceptance` lists each clause it judged, as `{"clause": "A1", "outcome": "met", "evidence_ref": "src/app.py:42"}`, where `outcome` is `met`, `unmet` or `not_checked`. A `pass` verdict that reports any clause `unmet` breaks the verdict rule and is refused with one sentence that names the clause, so it is never accepted.

Each blind reviewer's `review.completed` event and each boundary specialist's `specialist.completed` event carry `duration_seconds`, `inspected`, `acceptance` and a `depth` object: `changed_paths` (the paths the diff changed, deleted paths excluded), `inspected_changed_paths` (how many of those `inspected` names, after both are read as paths relative to the workspace, so `./src/app.py` and the absolute workspace path both count), `clauses` (the clauses given), `clauses_checked` (the given clause numbers judged `met` or `unmet`), `shallow` and `reasons`. A review is shallow when it inspected fewer changed paths than the diff touches, or checked no clause. An escalation specialist's `specialist.completed` carries `duration_seconds` and the two arrays, without `depth`. The depth is the seat's own report checked against the diff; AutoBuild does not read the harness transcript. The campaign report's "Review depth" section shows one row per review seat.

The live planted-defect check guards that the reviewer reads the change against the brief. It builds a small Git fixture whose brief states an exact outcome, whose change delivers another and whose own test asserts the wrong one, so its validator is green. It runs the blind reviewer alone and the blind reviewer with one boundary specialist through the Claude Code harness, with model `claude-opus-5-5` at effort `high`, and fails when either case's `pass` is accepted. It calls the model, so it is skipped unless you ask for it, and it is not part of the validator lane:

```
AUTOBUILD_LIVE_REVIEW=claude-code uv run python -m pytest tests/integration/test_planted_defect_review.py
```

AutoBuild sets `CLAUDE_UNATTENDED=1` in the environment of every seat in both modes: builder, reviewer and specialist, on every harness lane. The variable does nothing by itself. A hook or tool of your own may read it to refuse to ask a question in a seat nobody is watching.

### Custom harness command

Use a command override when the harness executable is outside `PATH` or needs a wrapper:

```toml
[harness]
command = ["/absolute/path/to/codex"]
```

The command remains behind the selected harness adapter. It must support the flags and result format expected by that adapter.

### Progress reporting

AutoBuild renders one plain-language progress line per campaign and item event and streams it live, so the owner learns what the campaign is doing without watching the run record. The optional `[progress]` table selects the sinks:

```toml
[progress]
file = true
stderr = true
command = ["/absolute/path/to/notify", "--stdin"]
command_timeout_seconds = 5
```

`file` and `stderr` are booleans that default to true. The file sink appends every line to `progress.log` under the run record. The stderr sink writes and flushes each line, so a detached launch with redirected stderr keeps every line even if the process is killed.

`command` is an optional non-empty string array. When set, AutoBuild runs the command once per line with the line on standard input, for a push notification or a chat message. The hook is a human-approved profile field, not a workflow command: it runs outside the validator policy. Each call has a hard ceiling of `command_timeout_seconds`, which defaults to 5. A missing executable, a non-zero exit, a timeout, or an encoding error is swallowed and counted, and the total is reported once on the `campaign completed` line as `progress hook failures: N`.

## Configure harness lanes and failover

A campaign can list more than one harness lane and flip to the next lane when a model reaches its subscription limit. The single-lane form above (`run.harness` plus `[models]`) stays valid and means one lane.

Declare a tier map with a `[lanes.<harness>]` table per harness and a `run.lanes` order of preference:

```toml
[run]
lanes = ["claude-code", "codex", "github-copilot"]
lane_cool_seconds = 3600
lane_state_root = "/path/to/local/lane-state"

[lanes.claude-code]
builder = "claude-opus-5-5"
reviewer = "claude-opus-5-5"
specialist = "claude-opus-5-5"
builder_effort = "medium"
reviewer_effort = "high"

[lanes.codex]
builder = "gpt-5.6-sol"
reviewer = "gpt-5.6-sol"
specialist = "gpt-5.6-sol"
builder_effort = "high"
reviewer_effort = "high"

[lanes.github-copilot]
builder = "gpt-5.6"
reviewer = "gpt-5.6"
specialist = "gpt-5.6"
```

`run.lanes` names the lanes in order of preference. Each name must have a `[lanes.<harness>]` table with `builder` and `reviewer` model names; `specialist` is optional and defaults to the reviewer model. A lane table whose name is absent from `run.lanes` is parked: its present model names and effort values are checked, but its missing model keys are allowed. A parked lane is not resolved, probed, or selected by `--harness`, so it has no effect on the active lane order. Each lane table also accepts the optional `builder_effort`, `reviewer_effort`, and `specialist_effort` keys, with the values and defaults described in "Model settings". Effort belongs to the lane: a seat runs at the effort its current lane sets, so a seat that moves to the next lane after a limit takes that lane's effort, and a lane without the key passes no effort and uses its harness default. In the example the `github-copilot` lane sets no effort. A lane table also accepts `key_file`, which only the `claude-sdk` lane reads, as described in "Run overflow seats on API credit". `[models]` is not read when `run.lanes` is set, so an effort key there stops the launch with an error that names the key and points to the lane tables. Before any active lane is probed, AutoBuild refuses to launch when it sets an effort its harness adapter cannot pass, and the error names the lane and the seat. A name listed twice in `run.lanes` also stops the launch, with an error that names it. Lane choice is made at launch: preflight probes every listed lane, cools any lane whose executable is missing, unauthenticated or lacking a required capability with the `probe` signature, and starts the first capable lane. Pass `--harness <name>` to move a listed lane to the front for one launch.

When a seat hits a structural limit on the active lane, that lane cools and the seat re-runs on the next capable lane. A limit is read only from the harness CLI's exit code and structured error fields, read from standard output and standard error, never from words in the event stream, so a report that mentions "rate limit" in prose with a clean exit and a valid result never cools a lane. When the vendor supplies a reset time the lane cools until then; otherwise it cools for `run.lane_cool_seconds` (default 3600). When no capable lane remains, the item parks with the lane signature, its worktree evidence is kept, and the campaign holds new admissions until the earliest lane cooling resets, then probes the lanes again. Running items go on.

`run.lane_state_root` is where the machine-local `lanes.json` cooling file lives. It defaults to the scratch root. Concurrent campaigns on one machine share this file under a lock, so a lane one campaign cooled is skipped by the others until it recovers. Pass `--lane-state-root` to override it for one launch.

The active lane is recorded on tracker and run events: the claim actor is `builder@<lane>`, a park reason is prefixed `<lane>:`, and run event payloads carry the lane. The claim keeps the starting lane: when a limit moves the builder to another lane, the flip is recorded as `item.lane_changed` (with `lane` and `from_lane`) in the run record and as the builder lane in the tracker close briefing, and the item is never claimed again. The manifest records the lane order and the cooling state.

## Stop seats at a usage level

A subscription lane can stop taking seats before its plan limit is spent. A harness adapter may declare a usage meter for its lanes; the `claude-code` adapter declares the Claude subscription meter, and the `codex` and `github-copilot` adapters declare none. The optional `[usage]` table sets the levels:

```toml
[usage]
warn_percent = 80
stop_percent = 90
five_hour_stop_percent = 95
on_probe_failure = "allow"
```

Every key is optional, and the values above are the defaults. `stop_percent` binds the weekly limits; `five_hour_stop_percent` binds the five-hour session limit. A limit binds at or above its stop level. `warn_percent` must not exceed `stop_percent`, and every level is above 0 and at most 100. `on_probe_failure` is `allow` or `refuse`.

Without a `[usage]` table AutoBuild never reads a meter.

With the table, AutoBuild reads the meter of every metered lane:

- At launch, before any claim. The launch is refused, with a message that names the limit, its percent and its reset time, only when every configured lane is metered and at its stop level. Otherwise each metered lane at its stop level is cooled until its reset, and the campaign starts on the next lane. An unmetered lane is never cooled by the meter. The `usage` preflight probe records the readings and any warning.
- Before every seat on a metered lane. A session or weekly limit at its stop level cools the lane in the shared lane state until the latest reset among the binding limits, and the seat runs on the next capable lane. When none remains, the item parks with the `usage` signature and the campaign holds new admissions with the `lanes_exhausted` condition until the earliest reset.
- A weekly limit scoped to one model (for example a limit labelled `Fable`) never cools the lane. Only a seat whose model the label covers moves to the next lane; the other seats stay on the lane. The harness adapter decides whether a label covers a seat's model. When no other lane remains, the item parks with the `usage` signature.

A limit at or above `warn_percent` records one `usage.warning` run event when it crosses the level, and another only after it falls below and crosses again. The other run events are `usage.stop`, `usage.seat_rerouted` and `usage.meter_failed`.

The `claude-sdk` lane declares no meter. See "Run overflow seats on API credit" for a lane that takes over when the subscription lane stops.

The meter reads an undocumented, unsupported endpoint that the Claude Code client uses for its own usage view; it can change without notice and can fail. AutoBuild treats it as a soft signal. With `on_probe_failure = "allow"` a failed reading records `usage.meter_failed`, cools nothing, and the seat proceeds. With `"refuse"` a failed reading at launch refuses the run; before a seat, AutoBuild waits 300 seconds and reads the meter again, up to three times, and then parks the item with the `usage_meter_unavailable` signature. A meter failure never cools a lane for `run.lane_cool_seconds`. A malformed meter response (a limit whose kind is not text, a percent that is not a finite number, or an unreadable reset time) is a meter failure, never an error that stops the run. A binding limit whose reset time is missing or not after the reading counts as a meter failure too.

Read the meter once from a shell with:

```text
autobuild usage --json --scratch-root /path/to/local/scratch
```

The command is read-only. It prints the meter id, `ok`, an error when the meter failed, `extra_usage_enabled`, and each limit with its `kind` (`session`, `weekly_all` or `weekly_scoped`), `percent`, `resets_at` as a UTC time, and `scope` for a model-scoped limit. It exits 0 when the meter answered and 1 when it did not. AutoBuild reads the OAuth token from the Claude Code credentials file on each call and passes it to `curl -q --ssl-no-revoke` on standard input, without `SSLKEYLOGFILE`; `-q` stops curl reading a user `.curlrc`. The child's captured output is redacted before it is written. The token never appears on a command line, in a file AutoBuild writes, in a run record, or in an error message.

The meter needs `curl` on `PATH` and Claude Code's `.credentials.json` file in its default configuration folder in your home directory. Where Claude Code keeps its credentials elsewhere, for example in the macOS Keychain or under a custom `CLAUDE_CONFIG_DIR`, every reading fails; with the default `allow` the stop levels then never act, so check `autobuild usage --json` once before relying on them. `--ssl-no-revoke` turns off certificate revocation checks in curl's Windows TLS backend, which on-access TLS inspection otherwise breaks; it has no effect on other platforms.

## Run overflow seats on API credit

The `claude-sdk` lane runs Claude Code seats through the Claude Agent SDK on an API key, so a campaign can keep building when the subscription lane is cooled. Install AutoBuild with the optional extra:

```text
uv tool install "autobuild-factory[sdk]==0.11.0"
```

Create an API key in the Console organisation whose credit should pay for the seats, save it alone in a file, and name the file in the lane table. Put the lane last so it is used only when the earlier lanes are cooled:

```toml
[run]
lanes = ["claude-code", "claude-sdk"]

[lanes.claude-code]
builder = "claude-opus-5-5"
reviewer = "fable"

[lanes.claude-sdk]
builder = "claude-opus-5-5"
reviewer = "fable"
key_file = "/path/to/secrets/anthropic-sdk.key"
```

`key_file` is read only by the `claude-sdk` lane. A relative path resolves against the profile's folder, but keep the key outside the repository and out of version control: an absolute path to a folder only you can read is the safe choice. Give the key its own Console workspace with a monthly spend limit, so the limit caps what the lane can spend. AutoBuild never reads the key into the profile, a run record or an environment: the SDK's CLI reads the file through an `apiKeyHelper` command, so commands inside the seat do not inherit the key. The seat loads no user settings, hooks or skills, and the variables `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL` and `CLAUDE_CODE_OAUTH_TOKEN` are empty in its child. A deny rule stops the seat's Read tool opening the key file, but the seat runs as your user, so a shell command inside it could still read the file; treat the key as available to any seat on that lane.

A missing or empty key file, or an installation without the extra, makes the lane's probe fail; the lane is cooled and never falls back to subscription credentials. When the API reports that the credit balance is too low, or that the workspace spend limit is reached, the lane cools with the `quota` signal and the seat moves to the next lane, or the item parks with `lanes_exhausted` and the campaign holds new admissions. Neither counts as a builder failure.

## Choose the temporary work location

The scratch setting is optional. With no override, AutoBuild uses the operating system temporary directory under an `autobuild` folder.

To use another location, add this setting:

```toml
[run]
scratch_root = "/path/to/local/scratch"
```

You can also pass `--scratch-root` for one run.

AutoBuild places worktrees, run records, command output, evidence, and package caches below the selected root. It also sets `TMPDIR`, `TEMP`, `TMP`, `UV_CACHE_DIR`, `PIP_CACHE_DIR`, `PYTHONPYCACHEPREFIX`, `XDG_CACHE_HOME`, and `NPM_CONFIG_CACHE` for child processes.

## Restrict a campaign to selected items

The `[selection]` table restricts a campaign to a chosen set of items. Both keys are optional:

```toml
[selection]
allow = ["APP-001", "APP-004", "APP-002"]
exclude = ["APP-007"]
```

`allow` is the closed universe for the campaign. When it is present AutoBuild builds only these items, and its order is the dispatch order regardless of the tracker's own ranking. AutoBuild selects the first allowed, non-excluded item that the tracker reports ready, then the next, until the allow-list is exhausted. An item that is not in the allow-list is never claimed, even if the tracker offers it first.

`exclude` is checked at every selection, including after a refill attempt. An excluded item is skipped; a campaign with only excluded work ready ends `queue_dry`.

The allow-list is a hard fence. If the tracker offers an item outside the allow-list as its next item, AutoBuild records a `campaign.scope_fence_violation` event and makes no claim, rather than build out-of-scope work. The offered item is left alone for the rest of the campaign; when no allowed item is ready, the campaign ends with `queue_dry`.

Every campaign also has a launch set, with or without a `[selection]` table. Once AutoBuild holds the tracker lease, before it lifts a pause or resumes an interrupted item, it reads the id of every item the tracker holds, in any status, and records a `campaign.launch_set_fixed` event with their count. The launch set is fixed at launch and never read again. AutoBuild selects and claims only items in the launch set, so the follow-ups the campaign registers, and any item created by anyone while it runs, wait for a later campaign and are not listed as skipped in its report. An item that exists at launch and becomes ready during the run, such as a dependant whose blocker was delivered, is still built. If the tracker cannot list its items at launch, AutoBuild records `campaign.launch_set_failed` with the error, admits nothing, releases the tracker lease, writes the report and ends with `launch_set_unreadable`.

Extend the profile lists for a single run with repeatable command-line flags:

```text
autobuild run --repository . --allow-item APP-001 --allow-item APP-004 --exclude-item APP-007 --delivery-mode current-branch-pr --allow-delivery
```

`--allow-item` and `--exclude-item` add to the profile lists for that run. AutoBuild records the final allow-list, exclude-list and their sources in `manifest.json` and in the campaign result under `selection`.

## Preflight

Before it claims the first item, AutoBuild runs a preflight doctor. The doctor runs
after the adapter probes and before any tracker change, so a launch that cannot work
on this environment stops before it touches the queue. Every probe that fails stops
the launch with exit code 2 and a message that names the probe and the cause. A full
pass is recorded in `manifest.json` under a `preflight` block with each probe, its
result, and its detail.

The probes are:

- `dns-tls`: resolves DNS and completes a TLS handshake to every declared target.
- `interception`: refuses an unlisted proxy or certificate environment variable.
- `scratch`: confirms the scratch root exists or can be created, is writable, holds no foreign lock, and is not held by a live tracker-root lease from another campaign.
- `telemetry`: confirms the selected harness disables its telemetry in the child environment.
- `removed-environment`: never stops the launch. When `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN` is set in the parent environment, its detail is a warning that names the variable, never its value. The harness removes both from every seat and probe child, so a subscription seat does not bill the key.
- `validator-runnable`: confirms the validator executable resolves, reports a version, and that a repository-relative script path exists.
- `validator-offline`: runs the validator once in the primary checkout with proxies pointed at a closed port and offline flags set, so any dependency download fails at once.
- `validator-budget`: confirms `command_timeout_seconds` is not below the declared validator budget.
- `transport`: confirms the harness streams a large instruction through stdin or a prompt file and keeps every argv element small. An in-process harness such as `claude-sdk` declares its own transport and child environment.
- `briefs`: confirms the next ready item's brief file exists and is under 1 MiB, then reads its text and runs two checks. The nature check refuses a brief whose `Item nature` line is absent or names `repository` when a path under `## Declared paths` resolves outside the repository and the configured `allowed_roots`, because the campaign would park the item as `cross-repository` at claim; the message names every such token. The git staging check finds a brief line that orders the builder to stage, commit or remove through git: `git add`, `git rm`, `git mv`, `git commit` or `git stash` with any arguments, `git apply` with `--index`, `--cached`, `-3` or `--3way` (a three-way apply is allowed when the same line unstages with `git reset -q`, whether that reset is ordered or reported), and any `git reset` other than `git reset`, `git reset -q` or `git reset -- <paths>`. It does not refuse the launch: the probe passes and its detail adds a warning that names the item, each refused line by number with the invocations found and the line text, and that the item will be parked `brief:git-staging` at claim, so the launch goes on with the rest. The builder leaves product changes uncommitted and delivery stages them. Outside a fenced code block, an invocation counts only when its part of the clause (the text between clause ends and commas) gives an order: its first word, after list markers and connectives such as then, and or afterwards, is an imperative verb such as run, apply, stage, remove, make, remember, ensure or be, so "Make sure you run", "Remember to run" and "Be sure to commit with" are orders; a must, should, shall, need or have to comes before it in the clause; the clause is a present-tense passive, with is, are or be before the command and with, by, using, via or through directly before it, such as "the old files are removed with `git rm`"; the part holds nothing but the command; or a connective such as then leads straight into the command. A line that reports a command run earlier, such as "the patch applied cleanly with `git apply --index`" or "the deletions were staged by `git rm`", is not an order. A clause ends at `;`, `:`, `!`, `?`, and at a `.` followed by a space or the line end that does not close e.g., i.e., cf., vs. or etc., so `9.2` and `../repo` stay whole. An invocation is exempt when an earlier word in its clause is never, not, no, nor, without or ends in n't, and a line that starts with `>` is a quotation and is exempt; inside a fenced code block every invocation counts and no word exempts a line. With a concurrency above 1 the probe checks every ready item inside the selection fence, because any of them can be admitted at launch.
- `usage`: runs only when the profile has a `[usage]` table. It reads every metered lane's meter, cools a metered lane at its stop level, and refuses the launch when every lane is at its stop level, as described in "Stop seats at a usage level".

Configure the doctor in the project profile:

```toml
[preflight]
tls_targets = ["registry.example.com:443", "api.example.com:8443"]
accepted_environment = ["NODE_EXTRA_CA_CERTS", "SSLKEYLOGFILE"]

[validator]
id = "tests"
argv = ["uv", "run", "pytest", "-q"]
budget_seconds = 600
```

`tls_targets` lists `host:port` endpoints the campaign must reach. The doctor also
checks the host of the `origin` remote when its URL uses `https`. `accepted_environment`
names the interception variables this environment is allowed to carry; the doctor
records the accepted names and their presence and refuses any other interception
variable. `budget_seconds` is the lane's expected wall-clock; the doctor refuses when
`command_timeout_seconds` is below it and records the measured offline duration.

Run the lane probes and the doctor without a campaign:

```text
autobuild preflight --json --repository .
```

The command takes the same profile and override options as `autobuild run`. It claims
nothing, cools no lane in the shared lane state, and writes no tracker change. It reports
each lane's probe result (`capable`, `version`, `detail`) and any current cooling, the
active lane, and every preflight probe. A usage stop is reported but not recorded as a
cooling. It exits 0 when the launch would pass, 1 when no lane is capable or a probe
fails, and 2 when the profile or an adapter cannot load.

## Run a campaign

Review the queue, briefs, profile, current branch, and remote. Local PR delivery is the usual choice when a person will review the branch before it reaches the default branch:

```text
autobuild run --repository . --delivery-mode current-branch-pr --allow-delivery
```

`--allow-delivery` is the human gate for repository changes. AutoBuild stops before adapter preflight when the flag is absent. It does not decide where the result is delivered.

`current-branch-pr` captures the branch and revision of the checkout that starts the campaign. Accepted product and tracker commits merge into that branch. With Pinax, a claim made on that branch is committed there and not published, because Pinax publishes only from the remote default branch: AutoBuild accepts it in this mode, the claim is published with the pull request, and other machines do not see the claim until the pull request merges. In `protected-default`, the same unpublished claim fails the item as a `tracker_environment` failure, and the campaign goes on. AutoBuild does not check out, merge into, or push the default branch. It also does not push the current branch unless you add `--push-current-branch`. The mode refuses a current branch that is the detected default branch unless you also pass `--allow-current-branch-default`.

Use protected delivery only after a human has approved a merge and push to the default branch:

```text
autobuild run --repository . --delivery-mode protected-default --allow-delivery
```

`protected-default` remains the default when `--delivery-mode` is omitted. It merges accepted work into the detected default branch, pushes that branch, and verifies the remote revision. `--push-current-branch` and `--allow-current-branch-default` work only with `current-branch-pr`.

Write the result to a file when another tool or person needs it:

```text
autobuild run --repository . --delivery-mode current-branch-pr --allow-delivery --output autobuild-result.json
```

Useful one-run overrides include:

```text
autobuild run \
  --repository . \
  --harness codex \
  --builder-model gpt-5.6-sol \
  --reviewer-model gpt-5.6-sol \
  --max-items 1 \
  --scratch-root /path/to/scratch \
  --delivery-mode current-branch-pr \
  --allow-delivery
```

Run `autobuild run --help` for the complete option list.

## Campaign result

The command prints `autobuild.campaign-result.v1` JSON. It includes:

- AutoBuild version and campaign identifier
- repository and scratch paths
- selected adapter names and versions
- stop reason
- the `selection` block with the allow-list, exclude-list and their sources
- refill counts
- each item disposition and state history
- product, tracker, and merge commit identifiers
- remote push result
- final run report path
- the committed repository report path
- `progress_ref`, the absolute path of the plain-language progress log
- the `batch_validation` block: `verdict` and `runs`. The verdict is `failed` when the last batch validation of the campaign failed, so the delivery target head is not proven, `passed` when the last one passed, and `none` when no batch ran. A failure followed by a later pass is `passed`, and the failure stays in `runs`. Each run has `batch`, `final`, `revision` (`unknown` when the validation checkout could not open), `passed`, `exit_code`, `stdout_ref` and `error` (`null` unless the checkout or the validator command raised)

At the end of every campaign AutoBuild commits a report to `docs/campaigns/<campaign-id>.md` in the repository. The report lists Shipped items with their item and merged commits, Parked items with reasons, Failed items with errors, a "Batch validation" section with one line per batch run (its number, `(final)` for the run at campaign end, the revision, `passed` or `failed`, the validator exit code, the validator output reference, and the error when the command raised, or `none` when no batch ran) that opens with a sentence naming the delivery target head that failed validation at campaign end and is not proven when the verdict is `failed`, Unfinished items with their error and kept worktree when a park failed, Holds with the condition, duration and end of each hold when one occurred, Skipped ready items with their reason, Follow-ups created during the campaign (including the findings still open on items accepted at the round limit), each under a subsection headed by its proposal id and title with its parent item, finding code, blocking flag, evidence reference, and its full question and rationale in fenced blocks (`none` for the finding fields of a refill proposal), the Next ready item, a per-item table of each seat's lane, resolved model, requested effort, duration, token usage, and estimated cost, and a "Review depth" table with one row per blind reviewer and boundary specialist: its item, seat (a specialist with its boundary), duration, changed paths inspected out of changed, clauses checked out of given, `shallow` or `ok`, and the shallow reasons (`none` when the campaign ran no review). When the campaign runs with an allow-list, the report also lists every allowed item it left unbuilt with a reason: not ready, blocked (the tracker holds it behind a gate or an unfinished dependency), excluded, outside the launch set (an item created after the campaign started), item bound, hold limit, paused, launch set unreadable, claim taken (another writer claimed it), or ready after the last selection. Every selection mode takes items in the tracker's dispatch order, with the allow-list order first when an allow-list is set. The report commit is a tracker-class commit that touches only that path and is delivered through the selected delivery mode: pushed in `protected-default`, kept local in `current-branch-pr`.

Every run event carries a real UTC timestamp and a JSON payload. The `campaign.started` payload names the harness, models, requested efforts, item bound, delivery mode, validator id, and manifest path. The harness, models and efforts name the lane the campaign starts on: the first lane in the resolved lane order that is capable and not cooling once preflight, including the usage preflight, has run. When every lane is cooling they name the first configured lane. The lane each seat actually ran on is in its `seat.completed` payload. The payload carries `null` for a seat that does not request an effort. A `seat.completed` payload is written for each builder, reviewer, and specialist invocation with the seat, model class, requested effort (`null` when the seat requested none), resolved model, outcome, exit code, start and end times, duration, input and output tokens, cost, and raw output and stderr references. The `validation.completed`, `review.completed`, `specialist.completed`, `item.parked`, `item.finalised`, and `campaign.completed` events carry their own payload fields. An `item.correcting` event is appended when an item enters a correction round, with its `round` and the `triggering_evidence_ref` of the review that asked for the change.

Each run directory also holds `progress.log`, the plain-language progress lines rendered from those same events, one per line and prefixed with the event UTC timestamp. The lines cover the campaign start, each item claim, seat start and completion, validation start and result, review decision, correction round, park, delivery start and result, batch validation start and result, and the campaign completion counts and report path.

The completion line reads `campaign completed: shipped N, parked N, failed N; report R; stop S`. When the batch verdict is `failed` it adds `batch validation failed on <revision>; output <stdout_ref>;` before the report part, with `unknown` for a head the validation checkout could not open and `error <error>` in place of the output when the validator command raised. With neither an output reference nor an error it writes `output none`. The output reference or error is written as one ASCII line: each run of whitespace becomes one space and each non-ASCII character is written as its backslash escape. The `campaign.completed` event carries `batch_validation` with the verdict and, when it is `failed`, `batch_revision`, `batch_stdout_ref` and, when the command raised, `batch_error`. The stop reason does not change: it says why admissions ended, and the verdict says whether the head is proven.

Each long stage of an item writes one line when it starts, so a silent log after a start line means that stage is still running:

- `item <id> seat <seat> started: model <model>, effort <effort>`, for every builder, reviewer, specialist, continuation and correction seat and for every attempt after a lane failover. `<model>` is the model the seat's lane sets for it, on every lane. `effort default` means the harness's own effort is in force. The line ends `, lane <name>`, including on the first lane.
- `item <id> validation started: validator <id>`, written when the command starts, after any wait for a heavy slot, so a validator waiting for a slot has no start line. A run on the integrated candidate adds `(revalidation at delivery)` and a rerun after a suspected load failure adds `(load rerun)`.
- `item <id> delivery started: target branch <branch>`, written when the delivery takes its turn in the queue. The delivery that records a parked item writes it too.

A batch validation writes one such line too: `batch validation <n> started: validator <id> on <revision>`, written when the command starts, after any wait for a heavy slot.

A seat completion line that has a cost ends `, estimated cost 0.47`. The figure is the harness's API-price estimate, rounded to two decimal places; it is not a charge on a subscription lane. The `seat.completed` event keeps the exact number, and the report's `estimated cost` column uses the same rounding. A seat with no reported cost shows none on its line and `-` in the report.

A start event (`seat.started`, `seat.launched`, `validation.started`, `delivery.started`, `campaign.batch_started`) records no cost and no outcome, and the campaign report does not read it. A `seat.launched` event follows each `seat.started` once the harness has built the seat's launch: it records the seat, the lane, the argument vector the seat runs with, the names of the environment variables the launch sets and removes, and whether the seat runs in process. It records environment variable names only, never their values. A killed run can leave a start event with no completion; neither `autobuild watch` nor resume treats that as an error.

No error in an item ends a campaign. Apart from an interrupt or an internal scheduler error, it ends only for one of these stop reasons:

- `queue_dry`: no runnable item remains
- `item_bound`: the campaign reached `max_items`
- `hold_limit`: one hold on admissions lasted `run.hold_limit_seconds`; the `campaign.hold_limit` event and the report name the condition it waited for
- `launch_set_unreadable`: the tracker could not list its items at launch, so nothing was admitted; the `campaign.launch_set_failed` event names the error

A stopped process (an interrupt or a kill) ends the campaign too; see "Recover an interrupted run". Campaign records written by AutoBuild 0.8.0 or earlier can also carry `structural_failure`, `scope_fence_violation`, `lanes_exhausted`, `tracker_environment` or `tracker_refused`; AutoBuild 0.9.0 and later never stop with them.

### Item errors

An error that belongs to one item ends that item and the campaign keeps admitting other items. Such errors are invalid evidence or a broken contract, a tracker refusal or environment error during that item's own write, an adapter error, and any delivery error.

- A claimed item parks with the error as its reason and its evidence. Before the item's product commit exists, the park is recorded in its worktree as usual. Once the item branch holds its product commit, the park is recorded from the primary checkout and the worktree and branch are kept as evidence, so none of the item's commits reach the remote.
- A park recorded in the worktree whose own delivery fails (the worktree tracker write, its tracker commit, or the delivery of that commit, for example an integration merge that git refuses because the worktree holds staged changes) is recorded from the primary checkout instead. Nothing from the worktree reaches the remote or the local target; the worktree, its branch and its uncommitted state are kept and its worktree lease is released. The run record gains an `item.park_delivery_failed` event with the error and the kept worktree, and the park reason reads `<reason>; park delivery failed: <error>`, where the error keeps git's own message. The item ends `parked`, so the next campaign reads it as parked rather than as a claim to resume.
- When the park itself fails, including a fallback park from the primary checkout after a failed worktree park delivery, the item is unfinished: its disposition is `failed`, its worktree and evidence are kept, its worktree lease is released, the `item.park_failed` event names the error (for a failed fallback, the worktree error and then the primary park error) and the kept worktree, and the report lists it under Unfinished. An accepted item in this state keeps its `finalised` phase marker, so the next campaign resumes it by delivering it (see "Recover an interrupted run").
- A builder seat that times out is not an item error. The item keeps its claim and its worktree, nothing is parked or snapshotted, and the run record gains an `item.builder_continued` event with the continuation number. The campaign admits the item again ahead of new work, and a fresh builder seat continues in the same worktree; its prompt says that a previous seat's partial work is present and must be completed, not restarted. Each item gets at most 2 continuations; a third timeout parks it `timeout:builder` with its snapshot. A continuation the campaign stops before admitting stays claimed with its worktree and marker, and the report lists it under Unfinished.
- A validator that does not pass on a builder's candidate is retried one builder effort level higher, up to `high`, before the item parks with the validator reason (see "Model settings").
- An item that fails before its claim is not picked again in the same campaign.
- A failure while recording the follow-ups of a delivered item never undoes the delivery: the item stays accepted and the run record gains an `item.follow_up_failed` event.

A tracker failure on an item's write is classified from the Pinax exit code. `tracker_environment` is exit 4, and the message states the cause Pinax reported (the remote could not be reached or read, the repository is inside another repository, or a claim was committed on a branch Pinax does not publish outside `current-branch-pr`), or an installed Pinax that left its change uncommitted (pinax-tracker 0.2.1 or later is required). `tracker_refused` is exit 1 (unknown item), 2 (invalid actor), 5 (push rejected; outside a claim it is an unpublished write, described at the end of "Holds"), 6 (clock behind the newest published event) or 7 (commit refused). The run record gains an `item.tracker_failed` event with the class, the exit code and Pinax's message. AutoBuild then asks the tracker to recover and holds admissions while the tracker is not clear.

### Partial tracker writes

Pinax exit 7 with the status `commit_refused` or `stage_failed` means Pinax appended its event and its commit was refused, so the event is left uncommitted and Pinax asks the caller to commit the shard and the projection. AutoBuild finishes that write itself, at once, before the item goes on:

1. Under the primary-checkout lock, it commits only the leftover files under `.ergon/`, with the commit subject Pinax chose. When `pinax verify` reports that the projection no longer matches the log, it runs `pinax verify --fix` first. The commit must touch tracker paths only.
2. A refused commit is tried again up to three times, after waits of 2, 5 and 10 seconds. The lock is released during each wait.
3. Outside the lock, it publishes as Pinax would have: when the remote default branch is checked out, it pushes that branch, trying a failed push again after the same waits. A push that still fails leaves the commit unpublished; it goes out with the next push of its branch.

AutoBuild never leaves the primary checkout dirty with a tracker write. A claim, a delivery or a campaign report that finds the primary checkout dirty with tracker files only runs the same recovery first. When the recovery still fails, the item's write fails as `tracker_refused` and the campaign holds admissions with the `tracker_pending` condition.

### Holds

A condition that blocks all work holds new admissions instead of stopping the campaign. Running items go on to their own end. Each hold is probed again on its own schedule:

- `lanes_exhausted`: every lane is cooling. The hold waits until the earliest cooling reset, at least 1 and at most 600 seconds, then probes the lanes again.
- `tracker_pending`: a partial tracker write is still uncommitted. The recovery is tried again every 60 seconds.
- `tracker_unreachable`: the tracker's remote does not answer. The check is repeated with a backoff of 30, 60, 120, 240 and 480 seconds, then every 600 seconds, and the hold is released when the remote answers.
- `tracker_read_failed`: reading the ready queue raised, for example Pinax exit 4 or an operating system error. The queue is read again with the same backoff, and the hold is released when it answers.
- `batch_validation_failed`: the validator failed on the delivery target head. Every 600 seconds the hold fetches the target branch, checks whether the head moved and validates it if so; a passing batch releases the hold. See "Batch validation".

A tracker-lease renewal that fails between items is counted as a heartbeat failure is: the third in a row records `campaign.lease_withheld` and withholds admissions until a renewal succeeds. When nothing is running, a renewal that fails starts a `lease_renew_failed` hold: the renewal is tried again every 30 seconds, the hold is released when one succeeds, and the hold limit applies. A lease, lane state or usage reading that cannot be read while deciding an admission is no signal: admission goes on, and each item and seat reads it again for itself. A refill that fails records `campaign.refill_failed` and the campaign ends with `queue_dry` as it would have.

A ready item without an approved brief is never claimed. The run record gains one `item.skipped` event per item and campaign with the reason `no_brief`, and the report lists it under Skipped.

The run record gains `campaign.hold_started` with the condition and the next check, `campaign.hold_retry` after each failed check, and `campaign.hold_released` with the hold's duration when it clears. A hold that lasts `run.hold_limit_seconds` records `campaign.hold_limit` and ends the campaign with `hold_limit`. The report lists each hold under Holds with its duration and how it ended.

A claim that Pinax reports superseded (exit 3) is not a stop: another writer holds the item, the run record gains an `item.claim_taken` event, and the campaign selects the next item. A claim, park or proposal that Pinax committed without pushing is not a stop either: the run record gains a `tracker.unpublished` event, the progress log says so, and the report lists it under Unpublished tracker writes; it goes out with the next push of its branch. Pinax exit 5 with the status `push_rejected`, meaning a concurrent push won the race and the event stayed committed on the local branch, is handled the same way for a park, a proposal and its block and note: AutoBuild checks the commit and records it as unpublished. For a claim, which Pinax annuls when it cannot publish it, exit 5 still fails as `tracker_refused`. A park or proposal from the primary checkout is refused while that checkout holds staged or modified non-tracker files, and a tracker commit that touches any non-tracker path is refused.

The process exit code is:

- `0` when no item ran or every item was accepted
- `1` when an item parked or failed, or when the batch verdict is `failed`
- `2` when configuration, preflight, or the campaign command raised an error

The scratch root contains `runs/<run-id>/`. Each run directory contains:

- `manifest.json` with configuration and adapter identities
- `events.jsonl` with campaign and item events
- `progress.log` with one plain-language progress line per event
- `evidence/` with workflow-authored evidence such as a per-item trajectory for every disposition
- `report.txt` with the final summary and the committed repository report path

Command output, harness output, and diff patches live under their own scratch subdirectories. Events point to those files. The brief, validator result, diff, review verdict, and commit identities form the acceptance record.

## Watch a campaign

`autobuild watch` follows the progress lines of a running or finished campaign from its run record. It reads only the run's `progress.log` and, to locate the runs root, the profile; it never reads the tracker and never writes anything.

```text
autobuild watch --run <run-id>
autobuild watch --latest
```

Pass exactly one of `--run <run-id>` or `--latest`. The runs root is `--scratch-root` when given, otherwise `[run] scratch_root` from the profile named by `--profile` or found next to `--repository`, otherwise the same default the campaign uses. Resolving it reads only `[run] scratch_root`, so a watcher finds the runs without a complete profile.

The command tracks a byte offset in `progress.log` and prints each newline-terminated line as it lands; a trailing partial line waits for its newline. Only the progress lines reach stdout; diagnostics go to stderr. Without `--timeout-seconds` the command polls until the campaign-completion line arrives. Pass `--timeout-seconds <n>` so a run whose process died never blocks the terminal.

The process exit code is:

- `0` when the campaign-completion line has been printed. That is the line rendered from the campaign's completion event, starting `campaign completed: shipped` after its timestamp; the same words inside an item title, a reason or any other line do not end the watch
- `2` when the profile cannot be read, the run does not exist, or no run exists for `--latest`; the profile error is one line on stderr starting `autobuild watch:`
- `3` when `--timeout-seconds` elapses before the completion line arrives

## Pause a campaign

`autobuild pause` asks a running campaign to stop cleanly. It finds the run the same way `autobuild watch` does and writes a pause marker file, `pause-requested.json`, into that run's directory. The campaign reads the marker from disk, so the command works from any terminal or process.

```text
autobuild pause --run <run-id>
autobuild pause --latest
```

Pass exactly one of `--run <run-id>` or `--latest`, plus `--repository`, `--profile` and `--scratch-root` as for `watch`. The command prints one line naming the run and the marker path. Asking again on a run that is already marked succeeds and leaves one marker.

The campaign checks for the marker before every admission decision, and the lease heartbeat checks it at every beat, so a pause is seen during a hold on admissions too. A heartbeat check that cannot read the marker records `campaign.pause_check_failed` with the error and the number of failed checks in a row, and the first in a row shows one progress line; the heartbeat still renews the lease, and a pause request is then seen at the next admission decision. Once it sees the marker, the campaign records `campaign.pause_requested` with the items still running and admits nothing more. Each running item finishes its current cycle to its normal outcome: delivered, parked, failed, or kept with its claim and worktree for the next campaign. Batch validation and the report then run as usual, and the campaign ends with stop reason `paused`. The report's Next section says the campaign was paused and that `autobuild run` with the same profile resumes it, and an allowed item it did not build is listed with the reason `paused`. A pause seen with nothing running ends the campaign at once.

To resume, run `autobuild run` again on the same repository with the same profile. Before its first admission the new campaign removes the marker from the newest earlier run of the same repository, records `campaign.resumed` with that run's id, resumes any item still claimed as described in "Recover an interrupted run", and then builds the remaining ready items.

The process exit code is:

- `0` when the marker has been written
- `2` when the profile cannot be read, the run does not exist, no run exists for `--latest`, or the run's `progress.log` holds the campaign-completion line; nothing is written then

## Changes made by accepted and parked outcomes

An accepted item creates separate commits for product files and tracker state. In `current-branch-pr` mode, AutoBuild delivers the item branch to the invoking branch and reports the resulting local commit with `pushed: false`. A human can inspect that branch and create a pull request. Add `--push-current-branch` only when the current branch should be pushed and verified.

In `protected-default` mode, AutoBuild delivers the item branch to the default branch, pushes the default branch, and checks that the remote points at that commit.

Both modes deliver one item at a time in the same way:

1. Fetch the target branch from `origin`. When `origin` has no such branch (a new `current-branch-pr` branch), AutoBuild integrates against the local branch only, and a pushing delivery creates the remote branch.
2. In the item worktree, merge the fetched target and then the local target branch into the item branch. The local branch can hold tracker claims that are not yet published; they go out with this push. Integration never rebases. Each merge is a commit whose subject starts `MERGED: integrate`. A conflict only in `.ergon/board.md` or `.ergon/items/*.md` is settled by regenerating the Pinax projection from the merged log. A conflict in any other file stops the merge.
3. When the merge brought a change to a path that intersects the item's own changed paths or its declared paths, run the item's validator again on the merged commit in the worktree. Two paths intersect when they are the same file or sit in the same top-level package: the first two path segments under `apps/` and `packages/`, the first segment otherwise. A declared path also covers every path its glob matches (`**` spans directories, and a glob that starts with a pattern, such as `**/*.ts`, covers everything) and every path under a declared directory. A changed file at the repository root, such as `pyproject.toml`, a lock file or `package.json`, overlaps every item. Changes confined to `.ergon/`, `docs/` or `*.md` files never revalidate. Each decision is recorded as a `delivery.revalidation` event with `run`, `reason` (`overlap`, `no_overlap`, `docs_only` or `unknown_scope`), the incoming paths and the overlapping ones.
4. Under the primary-checkout lock, check that the local target branch is still part of the merged commit. When only tracker files moved there in the meantime, merge them in too, without another validator run. The local target branch does not move.
5. Push that exact commit from the item worktree and check that the remote points at it.
6. Only then, under the lock again, fast-forward the local target branch to the pushed commit, first merging in any tracker-only commits that landed on it meanwhile.

A source change on the local target branch during step 4, or a rejected push, starts again at step 1. Delivery makes at most three such attempts. Because the local target branch moves only onto a commit the remote has accepted, it never holds an undelivered item commit. Without `--push-current-branch`, `current-branch-pr` has no push; the fast-forward in step 6 is then the delivery.

The evidence rule is that the tree the validator passed equals the pushed commit's tree in every path that intersects the item. The pre-push check refuses a candidate that differs from the validated tree in such a path. An unchanged candidate is never validated twice.

### Batch validation

Changes from other packages reach the target without the item validating them, so the integration is checked in batches. After every `run.batch_validate_every` deliveries (default 5), and once at campaign end when anything was delivered since the last batch, AutoBuild runs the validator once on the current head of the delivery target branch in a detached scratch worktree, in a heavy slot and on its own thread, so admissions go on while it runs. It records `campaign.batch_started` when the command starts, after any wait for a heavy slot. A pass records `campaign.batch_validated`. A failure records `campaign.batch_validation_failed` with the head, the validator exit code and the commits delivered since the last batch that passed, and holds new admissions with the `batch_validation_failed` condition. Nothing is reverted and the campaign does not end. Every 600 seconds the hold fetches the target branch from `origin`, fast-forwards the local branch to it when the local head is part of it, and checks the head. An operator clears the hold by pushing a fix to the target branch; the moved head is then validated, and the hold is released only when a later batch passes. The hold limit applies as to any hold. With 6 deliveries the head is validated twice: after the fifth and at the end.

The last batch run decides whether the head is proven at campaign end, whether or not it was the run at campaign end. When it failed, the campaign result's `batch_validation` verdict is `failed`, the completion line and the report's "Batch validation" section name the failing head and the validator output reference, and the process exits 1, as for a parked item. The hold for a failure is ended when the campaign ends; nothing is reverted.

Only staged or modified tracked files, and untracked tracker files, make the primary checkout dirty for a claim, a delivery, a park or a campaign report. An untracked product file, such as a brief written by hand during a campaign, stays where it is and does not block them. It still blocks a delivery that brings in a file at its path, or a folder that holds it: the delivery refuses before the push with `untracked files in the primary checkout are at paths the delivery brings in`, names them, and the item parks from the primary checkout.

A delivery that cannot finish parks the item from the primary checkout and keeps the item worktree and branch as evidence. Any other error after the item's product commit, such as a delivery check that refuses or a tracker close that fails, parks the same way. The park's own tracker commit is checked on its own: the item's product commit before it is outside that check, and a park commit that touches a non-tracker path is still refused. For every delivery park reason, none of the item's commits reach the remote. The park snapshot records only the item's own change, from the start commit to the item's tracker commit, never the target branch changes an integration merge brought in. The park reason starts with one of:

- `delivery_conflict`: merging the target branch conflicted in a file other than the Pinax projection; the merge is aborted and the reason names the files
- `delivery_revalidation_failed`: the merged commit failed the validator; the item's close never reaches the remote
- `delivery_contended`: the target branch gained a source change, or the push was rejected, on all three attempts

A pushed campaign report follows the same rule: it is committed in a detached worktree under the scratch root, pushed from there, and the local branch is fast-forwarded to it only after the remote accepted it.

A parked item writes the reason to the selected tracker and delivers that tracker-only result through the selected delivery mode. AutoBuild does not merge the unaccepted product changes. Before it releases the worktree, AutoBuild snapshots the parked work into the run record so nothing is lost: the tracked changes as a binary patch against the start commit, every untracked product file copied verbatim, and a `snapshot.json` that lists the paths, their digests, and the park reason. The park reason and the `item.parked` event both name that snapshot directory. The isolated worktree is released only after the park record is delivered and the snapshot is written.

A preflight failure occurs before a tracker claim. The primary repository remains unchanged.

## Supply proposal-only refill

Refill is optional. It accepts a reviewed JSON file and runs when the live queue becomes dry. A refill proposal cannot become ready work during that campaign.

Example `refill.json`:

```json
{
  "schema": "autobuild.refill-plan.v1",
  "proposals": [
    {
      "title": "Candidate title",
      "question": "What exact user problem should this solve?",
      "rationale": "Why this belongs in the project",
      "brief_ref": "docs/items/candidate.md"
    }
  ],
  "fog": []
}
```

Reference it in the profile:

```toml
[refill]
plan = "refill.json"
```

You can also pass `--refill-plan refill.json`.

Pinax stores each proposal behind a proposal gate. The backlog adapter adds a non-runnable `Proposed` row and retains the supplied question and rationale in its status cell.

Every proposal needs `title`, `question`, `rationale`, and `brief_ref`. AutoBuild rejects unknown fields, empty fields, an unknown schema, and an empty plan before it touches the tracker.

The optional `fog` array records directions whose question is still unclear. Fog requires the Koine knowledge adapter in this release:

```toml
[knowledge]
command = ["koine-memory"]
fog_ledger = "/path/to/fog-ledger.md"
```

The command must answer `--version`, and the fog ledger must already exist. Public users without that adapter should leave `fog` empty.

## Recover an interrupted run

Normal failures park the claimed item and release the isolated worktree. A killed process or machine restart can leave a claimed item, an AutoBuild branch, or a worktree under the scratch root. A tracker write that Pinax appended and could not commit is recovered by AutoBuild while the campaign runs (see "Partial tracker writes"); a killed run can leave one behind on the primary checkout, and the next claim, delivery or report commits it the same way.

At the start of every campaign, before it selects the first item, AutoBuild asks the tracker for items its builder actor still holds that are neither done nor parked, and reads each item's phase marker from the most recent run under the scratch root. An interrupted item resumes automatically when its state is good: the marker's worktree still exists as a registered worktree of the repository, the worktree head matches the marker (a delivery integration merge on top of the recorded commit still matches), the product status digest still matches a `built`, `validated` or `reviewed` marker, the tracker still shows the item claimed, and no live lease from another campaign holds the worktree or the tracker root. A resumed item restarts from its marker: a build that is not yet trusted re-runs the builder, a `built` marker re-runs validation and review, a `validated` marker re-runs review, and a `reviewed` correction starts the next round with the same correction count. An accepted item writes its `finalised` marker as soon as its product and tracker commits exist, before delivery, and keeps it until the tracker records a park. So an item whose delivery failed and that stayed claimed resumes by delivering: its worktree's item commit is integrated, validated again when the target moved in a path that overlaps it, pushed, and the item ends `done`. The run record notes `item.resumed` with the prior run id and marker state, and the campaign then continues to the rest of the queue.

A paused run resumes the same way: the next `autobuild run` on the repository lifts the pause marker of the paused run, resumes the items it kept claimed, and continues with the ready queue (see "Pause a campaign").

When the state is not good, AutoBuild parks the item with a stated reason and moves on rather than attaching to it. The reason is one of `resume:missing-worktree`, `resume:head-moved`, `resume:revision-changed`, `resume:lease-held`, `resume:tracker-mismatch` or `resume:missing-marker`. A parked item's own worktrees are named with its park: the `item.resume_parked` event lists them in `worktree_roots`. They are the worktree its marker names and every worktree AutoBuild created for that item, so an item with no marker, or with a marker written before its worktree was recorded, still names its own worktree, and that worktree is not also listed as an orphan. Every resume park except `resume:lease-held` snapshots the item's worktree into the run record and leaves it in place, so nothing is lost: `resume:head-moved`, `resume:revision-changed` and `resume:tracker-mismatch` snapshot the marker's worktree, and `resume:missing-marker` and `resume:missing-worktree` snapshot the item's worktree when exactly one belongs to it. When several belong to it, none is snapshotted, because nothing says which one holds the work; all are named and left in place. No worktree is snapshotted while a live lease from another campaign holds it or the tracker root, and the event's `snapshot_path` is then empty. A worktree under the scratch root that no claimed item owns is never attached; the campaign report lists it under Parked with reason `resume:orphan-worktree` and leaves it in place. These park cases still need a person: inspect the named worktree and reconcile it before you re-queue the item. Taking over a worktree that another live campaign holds is never automatic, so stop the named holder first.

Preserve a parked or orphan worktree until you have inspected it.

Every park preserves the parked work under the run record at `runs/<run-id>/evidence/<item-id>-park/`. That directory holds `snapshot.json` with the paths, digests, and park reason, `changes.patch` with the tracked changes as a binary patch against the start commit, and a `files/` folder with each untracked product file copied verbatim. Apply `changes.patch` to the start commit and restore the copied files to reproduce the parked tree. AutoBuild also refuses to remove a worktree while it still holds uncommitted tracker files, so a killed run leaves the tracker log in place rather than discarding it.

### Single-writer leases

Every campaign holds a lease on each surface it writes. There is one lease for the tracker root (the repository's `.ergon` directory or the backlog file's parent) and one lease for each isolated worktree. The campaign takes the tracker-root lease before the first claim and renews it at every item boundary. Each item takes its worktree lease when the worktree is created and drops it when the worktree is released.

Lease records live under `<scratch-root>/leases/`, one JSON file per surface named by the SHA-256 of the surface path. Each record names the holder campaign id, the process id, the host, the start time, the last heartbeat, and the surface it guards. Read these files to see which campaign owns a surface after an interrupted run.

A lease is live when its process id is still running on the recorded host and its heartbeat is younger than `run.lease_stale_seconds` (default 1800). A second campaign that starts against a live tracker lease stops with exit code 2 before any claim and names the holder campaign id and process id. There is no automatic take-over: stop the named holder first. A lease whose process is gone or whose heartbeat is older than the stale window is stale. The next campaign reclaims a stale lease, records the previous holder in the run record and the campaign report, and continues. Releasing a lease is idempotent, and releasing a lease this process does not hold is a recorded no-op.

Taking, renewing and releasing a lease is atomic across processes on one host. Each step reads the record, decides and writes under an operating-system lock on a `<digest>.lock` file beside the lease file, so two campaigns that start together cannot both take the same surface. The lock is held only for that step, and the operating system drops it when its process exits, so a crashed campaign never leaves a surface locked. A step that cannot take the lock within 30 seconds fails with an error that names the lock file. A lock call that fails for any reason other than another holder, for example a lock table that is full, fails at once with an error that names the lock file and the operating-system error, without waiting. Each surface has its own lock within a process as well, so a step that waits for one surface never delays a step on another surface, such as the tracker heartbeat while a worktree is contended. The lock does not reach other hosts: a holder on another host is still judged by its heartbeat age only.

Set the stale window in the profile when a longer or shorter reclaim wait fits the project:

```toml
[run]
lease_stale_seconds = 1800
```

Check the repository and tracker:

```text
git status --short
git worktree list
git branch --list "autobuild/*"
```

Use `pinax status` for Pinax. For a Markdown backlog, inspect the claimed row. Read the latest run record under the scratch root before deciding whether to continue the branch manually or park the item.

AutoBuild resumes a good-state item on its own at the next launch. Reconcile the tracker and worktree by hand only for an item it parked with a `resume:` reason or an orphan worktree it reported.

## Common startup failures

`tracker preflight failed` means Pinax and the configured backlog path were unavailable or invalid. Check `[tracker]`, `.ergon/`, the `pinax` command, and the backlog table.

`dns-tls preflight failed` names a declared TLS target that did not resolve or complete a handshake. Check the network path, the `origin` remote URL, and `[preflight] tls_targets`.

`interception preflight failed` names a proxy or certificate environment variable that is set but not listed. Remove it, or add its name to `[preflight] accepted_environment` when this environment must carry it.

`scratch preflight failed` means the scratch root could not be created, was not writable, or held another process lock. Choose a writable `scratch_root` and clear stale locks.

`telemetry preflight failed` means the selected harness did not disable its telemetry in the child environment. Update the harness adapter so it sets the disabling variables.

`validator-runnable preflight failed` means the validator executable was not found, its version probe failed, or a repository-relative validator script is missing. Check `[validator] argv`.

`validator-offline preflight failed` names the failing validator output line from an offline run. The validator downloaded a dependency or otherwise failed without the network. Vendor the dependency or fix the failing test.

`validator-budget preflight failed` means `command_timeout_seconds` is below `[validator] budget_seconds`, or the offline run exceeded the budget. Raise the timeout or the budget.

`transport preflight failed` means the harness would place the instructions on argv or in an oversized argv element. This is an adapter defect.

`briefs preflight failed` names a ready item whose brief file is missing or larger than 1 MiB, or whose declared paths resolve outside the repository and the allowed roots while its nature is `repository`. Fix the brief path or trim the brief. Make every declared path repository-relative, or name the class on the `Item nature:` line.

`primary checkout must be clean` means the main repository has staged or modified tracked files, or untracked files under the tracker paths. Commit, move, or intentionally resolve them before retrying. Untracked product files do not cause it.

`required git remote is missing: origin` means the repository has no `origin` remote. Add the correct writable remote before the run.

`authentication is unavailable` means the selected coding assistant command was found but its authentication probe failed. Run the harness-specific checks in this guide.

`run profile is not a file` means `--profile` names a directory or another path that is not a file. Pass the path of the TOML profile file.

`missing required run configuration` names the profile facts that are absent.

`validator ... did not pass` parks the item and records the validator output under the run directory.

## Technical documentation

- [Architecture](architecture.md)
- [Harness adapter contract](harness-adapters.md)
