# Set up and run AutoBuild

AutoBuild has two separate stages. The planning stage turns a build request into a reviewed plan and a registered work queue. The execution stage builds that approved queue one item at a time.

The owner controls the boundary between them. Planning stops after queue registration. Execution starts only after the owner gives a separate launch instruction.

This guide covers both stages and a new repository setup. The [architecture guide](architecture.md) explains the internal design and extension points.

## Stage 1: plan and register the work

Use [`autobuild-plan`](../skills/autobuild-plan/SKILL.md) when the request still needs research, decisions, an executable plan, or tracker registration. A typical request is "create a plan to autobuild the account dashboard."

The planning stage:

1. Finds the existing specifications, decisions, plans, and live tracker state.
2. Inspects the code, tests, deployment path, documentation, and publication surfaces affected by the request.
3. Writes an end-to-end plan with acceptance criteria, dependencies, owner gates, and the project validator.
4. Runs four independent reviews covering scope, execution readiness, architecture, and failure risks.
5. Applies accepted corrections and records the review result beside the plan.
6. Registers ready items in [Pinax](https://github.com/antikas/pinax-tracker) or `BACKLOG.md`.
7. Registers production deployment, publication, and unresolved owner decisions as blocked items.
8. Reports the plan, queue, decisions, gates, and launch command, then stops.

The owner reviews the plan and queue before execution. The planning stage can write planning documents and tracker records. It does not start a builder or change product code.

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
9. Deliver the item branch through the selected mode. Local PR delivery merges into the invoking branch and pushes only when separately authorised. Protected delivery merges into the default branch, pushes it, and verifies the remote revision.
10. Continue until the queue is dry, the item limit is reached, or a structural evidence failure stops the campaign.

The reviewer does not receive the builder transcript. Accepted work must still match the diff that passed validation and review.

Every review finding carries a blocking flag. The reviewer blocks only for a concrete consequence it would not merge under its own name, and records every other reservation as a non-blocking finding on a passing verdict. A `correct` or `escalate` needs at least one blocking finding, a `park` needs at least one finding, and a `pass` carries non-blocking findings only. When a passing review carries non-blocking findings, AutoBuild accepts and delivers the item and records one non-runnable follow-up proposal per finding for the owner. The campaign report lists these proposals under Follow-ups.

The `autobuild` skill reads the project configuration and launches the Python command. You can also call the command directly without installing either skill.

The repository and source archive contain both skill folders. The Python wheel installs the `autobuild` command only. Install the skills through the coding assistant's normal skill method, or use their `SKILL.md` files from a checkout.

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

Release 0.4.0 is published to PyPI as `autobuild-factory` and provides a Python wheel and source archive on the [GitHub release page](https://github.com/antikas/autobuild-factory/releases/tag/autobuild-factory-0.4.0). Install the released command from PyPI:

```text
uv tool install autobuild-factory==0.4.0
```

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
uv tool install autobuild-factory==0.4.0
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

Every brief carries two triage lines that AutoBuild reads before it claims the item, without a model call. The `Item nature` line names the class: `repository` work builds inside the isolated worktree, while `machine`, `cross-repository`, and `owner-gated` work cannot and is parked at once with reason `nature:<class>`. An absent line means `repository`. The `## Declared paths` section lists every path the item edits; any path that resolves outside the repository and the configured `allowed_roots` also marks the item `cross-repository`. Register `machine` and `cross-repository` items as blocked, not ready, so a person handles them outside the fence.

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

Install it:

```text
pipx install pinax-tracker
pinax --help
```

The [Pinax repository](https://github.com/antikas/pinax-tracker) also documents installation with `pip`.

Initialise the target repository and add an item:

```text
pinax init --actor your-name@your-machine
pinax add --title "Add the account summary" --prefix app --allow-new-prefix --actor your-name@your-machine --json
```

The add command returns an item identifier. Attach the approved brief to that item:

```text
pinax note add <item-id> --ref docs/items/APP-001.md --caption "The dashboard shows the account name and formatted balance." --actor your-name@your-machine --json
```

Commit and push the initial tracker state:

```text
git add .ergon docs/items/APP-001.md
git commit -m "ADDED: register account summary work"
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

`command_timeout_seconds` limits the validator process. The default is 600 seconds.

### Model settings

`builder` and `reviewer` are required. `specialist` is optional and defaults to the reviewer model.

Use model names accepted by the selected harness. AutoBuild records the configured names and passes them to the harness command.

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

The validator is the deterministic acceptance authority. The model cannot replace or change it during a run.

### Tool and path policy

`allowed_tools` uses the semantic names `read`, `write`, `shell`, `python`, and `git`. Each harness adapter maps those names to its own permission flags.

`allowed_roots` adds approved directories outside the repository. Use it for an external brief or shared read-only reference. Paths can be absolute or relative to the profile.

The builder receives the isolated worktree plus the configured external roots. The reviewer receives read access to the same roots.

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
builder = "claude-opus-4-8"
reviewer = "claude-opus-4-8"
specialist = "claude-opus-4-8"

[lanes.codex]
builder = "gpt-5.6-sol"
reviewer = "gpt-5.6-sol"
specialist = "gpt-5.6-sol"

[lanes.github-copilot]
builder = "gpt-5.6"
reviewer = "gpt-5.6"
specialist = "gpt-5.6"
```

`run.lanes` names the lanes in order of preference. Each name must have a `[lanes.<harness>]` table with `builder` and `reviewer` model names; `specialist` is optional and defaults to the reviewer model. Lane choice is made at launch: preflight probes every listed lane, cools any lane whose executable is missing, unauthenticated or lacking a required capability with the `probe` signature, and starts the first capable lane. Pass `--harness <name>` to move a listed lane to the front for one launch.

When a seat hits a structural limit on the active lane, that lane cools and the seat re-runs on the next capable lane. A limit is read only from the harness CLI's exit code and structured error fields, never from words in the event stream, so a report that mentions "rate limit" in prose with a clean exit and a valid result never cools a lane. When the vendor supplies a reset time the lane cools until then; otherwise it cools for `run.lane_cool_seconds` (default 3600). When no capable lane remains, the item parks with the lane signature, its worktree evidence is kept, and the campaign stops with the `lanes_exhausted` reason.

`run.lane_state_root` is where the machine-local `lanes.json` cooling file lives. It defaults to the scratch root. Concurrent campaigns on one machine share this file under a lock, so a lane one campaign cooled is skipped by the others until it recovers. Pass `--lane-state-root` to override it for one launch.

The active lane is recorded on tracker and run events: the claim actor is `builder@<lane>`, a park reason is prefixed `<lane>:`, the close briefing names the lane per seat, and run event payloads carry the lane. The manifest records the lane order and the cooling state.

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

The allow-list is a hard fence. If the tracker offers an item outside the allow-list as its next item, AutoBuild stops the campaign with the `scope_fence_violation` stop reason and makes no claim, rather than build out-of-scope work.

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
- `validator-runnable`: confirms the validator executable resolves, reports a version, and that a repository-relative script path exists.
- `validator-offline`: runs the validator once in the primary checkout with proxies pointed at a closed port and offline flags set, so any dependency download fails at once.
- `validator-budget`: confirms `command_timeout_seconds` is not below the declared validator budget.
- `transport`: confirms the harness streams a large instruction through stdin or a prompt file and keeps every argv element small.
- `briefs`: confirms the next ready item's brief file exists and is under 1 MiB.

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

## Run a campaign

Review the queue, briefs, profile, current branch, and remote. Local PR delivery is the usual choice when a person will review the branch before it reaches the default branch:

```text
autobuild run --repository . --delivery-mode current-branch-pr --allow-delivery
```

`--allow-delivery` is the human gate for repository changes. AutoBuild stops before adapter preflight when the flag is absent. It does not decide where the result is delivered.

`current-branch-pr` captures the branch and revision of the checkout that starts the campaign. Accepted product and tracker commits merge into that branch. AutoBuild does not check out, merge into, or push the default branch. It also does not push the current branch unless you add `--push-current-branch`. The mode refuses a current branch that is the detected default branch unless you also pass `--allow-current-branch-default`.

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

At the end of every campaign AutoBuild commits a report to `docs/campaigns/<campaign-id>.md` in the repository. The report lists Shipped items with their item and merged commits, Parked items with reasons, Failed items with errors, Follow-ups created during the campaign, the Next ready item, and a per-item table of seat durations and token usage. When the campaign runs with an allow-list, the report also lists every allowed item it left unbuilt with a reason: not ready, blocked, excluded, item bound, or lanes exhausted. The report commit is a tracker-class commit that touches only that path and is delivered through the selected delivery mode: pushed in `protected-default`, kept local in `current-branch-pr`.

Every run event carries a real UTC timestamp and a JSON payload. The `campaign.started` payload names the harness, models, item bound, delivery mode, validator id, and manifest path. A `seat.completed` payload is written for each builder, reviewer, and specialist invocation with the seat, model class, resolved model, outcome, exit code, start and end times, duration, input and output tokens, cost, and raw output and stderr references. The `validation.completed`, `review.completed`, `specialist.completed`, `item.parked`, `item.finalised`, and `campaign.completed` events carry their own payload fields. An `item.correcting` event is appended when an item enters a correction round, with its `round` and the `triggering_evidence_ref` of the review that asked for the change.

Each run directory also holds `progress.log`, the plain-language progress lines rendered from those same events, one per line and prefixed with the event UTC timestamp. The lines cover the campaign start, each item claim, seat completion, validation, review decision, correction round, park, delivery, and the campaign completion counts and report path.

The stop reason is one of:

- `queue_dry`: no runnable item remains
- `item_bound`: the campaign reached `max_items`
- `structural_failure`: required evidence or a contract was invalid
- `scope_fence_violation`: the tracker offered an item outside the allow-list or inside the exclude-list; the campaign stopped without a claim
- `lanes_exhausted`: every configured harness lane cooled on a subscription limit or spawn failure; the current item parked with the lane signature and its worktree evidence was kept

The process exit code is:

- `0` when no item ran or every item was accepted
- `1` when an item parked or failed
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

- `0` when the campaign-completion line has been printed
- `2` when the run does not exist, or no run exists for `--latest`
- `3` when `--timeout-seconds` elapses before the completion line arrives

## Changes made by accepted and parked outcomes

An accepted item creates separate commits for product files and tracker state. In `current-branch-pr` mode, AutoBuild merges the item branch into the invoking branch and reports the resulting local commit with `pushed: false`. A human can inspect that branch and create a pull request. Add `--push-current-branch` only when the current branch should be pushed and verified.

In `protected-default` mode, AutoBuild merges the item branch into the default branch with a merge commit, pushes the default branch, and checks that the remote points at that commit.

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

Normal failures park the claimed item and release the isolated worktree. A killed process or machine restart can leave a claimed item, an AutoBuild branch, or a worktree under the scratch root.

At the start of every campaign, before it selects the first item, AutoBuild asks the tracker for items its builder actor still holds that are neither done nor parked, and reads each item's phase marker from the most recent run under the scratch root. An interrupted item resumes automatically when its state is good: the marker's worktree still exists as a registered worktree of the repository, the worktree head matches the marker, the product status digest still matches a `built`, `validated` or `reviewed` marker, the tracker still shows the item claimed, and no live lease from another campaign holds the worktree or the tracker root. A resumed item restarts from its marker: a build that is not yet trusted re-runs the builder, a `built` marker re-runs validation and review, a `validated` marker re-runs review, and a `reviewed` correction starts the next round with the same correction count. The run record notes `item.resumed` with the prior run id and marker state, and the campaign then continues to the rest of the queue.

When the state is not good, AutoBuild parks the item with a stated reason and moves on rather than attaching to it. The reason is one of `resume:missing-worktree`, `resume:head-moved`, `resume:revision-changed`, `resume:lease-held`, `resume:tracker-mismatch` or `resume:missing-marker`. A parked item's worktree is snapshotted into the run record and left in place, so nothing is lost. A worktree under the scratch root that no claimed item owns is never attached; the campaign report lists it under Parked with reason `resume:orphan-worktree` and leaves it in place. These park cases still need a person: inspect the named worktree and reconcile it before you re-queue the item. Taking over a worktree that another live campaign holds is never automatic, so stop the named holder first.

Preserve a parked or orphan worktree until you have inspected it.

Every park preserves the parked work under the run record at `runs/<run-id>/evidence/<item-id>-park/`. That directory holds `snapshot.json` with the paths, digests, and park reason, `changes.patch` with the tracked changes as a binary patch against the start commit, and a `files/` folder with each untracked product file copied verbatim. Apply `changes.patch` to the start commit and restore the copied files to reproduce the parked tree. AutoBuild also refuses to remove a worktree while it still holds uncommitted tracker files, so a killed run leaves the tracker log in place rather than discarding it.

### Single-writer leases

Every campaign holds a lease on each surface it writes. There is one lease for the tracker root (the repository's `.ergon` directory or the backlog file's parent) and one lease for each isolated worktree. The campaign takes the tracker-root lease before the first claim and renews it at every item boundary. Each item takes its worktree lease when the worktree is created and drops it when the worktree is released.

Lease records live under `<scratch-root>/leases/`, one JSON file per surface named by the SHA-256 of the surface path. Each record names the holder campaign id, the process id, the host, the start time, the last heartbeat, and the surface it guards. Read these files to see which campaign owns a surface after an interrupted run.

A lease is live when its process id is still running on the recorded host and its heartbeat is younger than `run.lease_stale_seconds` (default 1800). A second campaign that starts against a live tracker lease stops with exit code 2 before any claim and names the holder campaign id and process id. There is no automatic take-over: stop the named holder first. A lease whose process is gone or whose heartbeat is older than the stale window is stale. The next campaign reclaims a stale lease, records the previous holder in the run record and the campaign report, and continues. Releasing a lease is idempotent, and releasing a lease this process does not hold is a recorded no-op.

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

`briefs preflight failed` names a ready item whose brief file is missing or larger than 1 MiB. Fix the brief path or trim the brief.

`primary checkout must be clean` means the main repository has tracked or untracked changes. Commit, move, or intentionally resolve them before retrying.

`required git remote is missing: origin` means the repository has no `origin` remote. Add the correct writable remote before the run.

`authentication is unavailable` means the selected coding assistant command was found but its authentication probe failed. Run the harness-specific checks in this guide.

`missing required run configuration` names the profile facts that are absent.

`validator ... did not pass` parks the item and records the validator output under the run directory.

## Technical documentation

- [Architecture](architecture.md)
- [Harness adapter contract](harness-adapters.md)
