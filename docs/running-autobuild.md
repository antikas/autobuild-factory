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
2. Claim the next ready item and push that tracker change.
3. Create an isolated Git worktree from the claimed revision.
4. Start a fresh builder session with the approved brief and allowed tools.
5. Calculate the changed files and run the declared validator.
6. Start a fresh read-only reviewer with the brief, diff, and validator evidence.
7. Correct a material finding, ask a specialist when the finding names a specialist boundary, or park the item.
8. Create a product commit and a separate tracker commit for accepted work.
9. Merge the item branch into the default branch, push it, and verify the remote revision.
10. Continue until the queue is dry, the item limit is reached, or a structural evidence failure stops the campaign.

The reviewer does not receive the builder transcript. Accepted work must still match the diff that passed validation and review.

The `autobuild` skill reads the project configuration and launches the Python command. You can also call the command directly without installing either skill.

The repository and source archive contain both skill folders. The Python wheel installs the `autobuild` command only. Install the skills through the coding assistant's normal skill method, or use their `SKILL.md` files from a checkout.

## Project inputs for execution

Stage 2 needs these project facts:

- a Git repository with a writable `origin` remote
- an approved work queue in Pinax or `BACKLOG.md`
- one written brief for each runnable item
- one validator command that decides whether the change works
- an installed and authenticated coding assistant
- model names that the selected coding assistant can use

AutoBuild provides the campaign sequence, isolated Git worktree, fresh builder and reviewer sessions, evidence checks, tracker updates, commits, merge, push, and run record.

The coding assistant is called a harness in configuration. Version 0.2.0 includes harness adapters for Claude Code, Codex, and GitHub Copilot CLI.

## Prerequisites

You need:

- Python 3.11 or later
- Git
- `uv`
- a supported coding assistant command
- Pinax only if you choose the Pinax tracker

The target repository must have at least one commit, a current default branch, and a remote called `origin`. AutoBuild pushes tracker claims before it starts a builder, so the remote must be writable.

Check the repository from its root:

```text
git status --short
git branch --show-current
git remote get-url origin
```

Start with a clean working tree. AutoBuild refuses to claim an item from a dirty primary checkout.

## Install AutoBuild

Release 0.2.0 provides a Python wheel and source archive on the [GitHub release page](https://github.com/antikas/autobuild/releases/tag/v0.2.0). Install the released command directly from its tag:

```text
uv tool install git+https://github.com/antikas/autobuild.git@v0.2.0
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
uv tool install git+https://github.com/antikas/autobuild.git@v0.2.0
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

## Outside scope

- No account editing.
- No deployment or publication.
```

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

For a one-run override, use:

```text
autobuild run --repository . --tracker backlog --backlog docs/QUEUE.md --allow-delivery
```

## Create the project profile

Create `.autobuild.toml` at the repository root. This file supplies facts that AutoBuild must not guess.

```toml
[run]
harness = "codex"
max_items = 10
seat_timeout_seconds = 900
command_timeout_seconds = 600

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

`harness` is `claude-code`, `codex`, or `github-copilot`.

`max_items` limits accepted or parked item cycles in one campaign. The default is 20.

`seat_timeout_seconds` limits each builder or reviewer process. The default is 900 seconds.

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

## Choose the temporary work location

The scratch setting is optional. With no override, AutoBuild uses the operating system temporary directory under an `autobuild` folder.

To use another location, add this setting:

```toml
[run]
scratch_root = "/path/to/local/scratch"
```

You can also pass `--scratch-root` for one run.

AutoBuild places worktrees, run records, command output, evidence, and package caches below the selected root. It also sets `TMPDIR`, `TEMP`, `TMP`, `UV_CACHE_DIR`, `PIP_CACHE_DIR`, `PYTHONPYCACHEPREFIX`, `XDG_CACHE_HOME`, and `NPM_CONFIG_CACHE` for child processes.

## Run a campaign

Review the queue, briefs, profile, current branch, and remote. Then run:

```text
autobuild run --repository . --allow-delivery
```

`--allow-delivery` is the human gate for tracker claims, commits, merges, and pushes. AutoBuild stops before adapter preflight when the flag is absent.

Write the result to a file when another tool or person needs it:

```text
autobuild run --repository . --allow-delivery --output autobuild-result.json
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
  --allow-delivery
```

Run `autobuild run --help` for the complete option list.

## Campaign result

The command prints `autobuild.campaign-result.v1` JSON. It includes:

- AutoBuild version and campaign identifier
- repository and scratch paths
- selected adapter names and versions
- stop reason
- refill counts
- each item disposition and state history
- product, tracker, and merge commit identifiers
- remote push result
- final run report path

The stop reason is one of:

- `queue_dry`: no runnable item remains
- `item_bound`: the campaign reached `max_items`
- `structural_failure`: required evidence or a contract was invalid

The process exit code is:

- `0` when no item ran or every item was accepted
- `1` when an item parked or failed
- `2` when configuration, preflight, or the campaign command raised an error

The scratch root contains `runs/<run-id>/`. Each run directory contains:

- `manifest.json` with configuration and adapter identities
- `events.jsonl` with campaign and item events
- `evidence/` with workflow-authored evidence such as the accepted trajectory
- `report.txt` with the final summary

Command output, harness output, and diff patches live under their own scratch subdirectories. Events point to those files. The brief, validator result, diff, review verdict, and commit identities form the acceptance record.

## Changes made by accepted and parked outcomes

An accepted item creates separate commits for product files and tracker state. AutoBuild merges the item branch into the default branch with a merge commit, pushes the default branch, and checks that the remote points at that commit.

A parked item writes the reason to the selected tracker and pushes the tracker-only result. AutoBuild does not merge the unaccepted product changes. The isolated worktree is released after the park record is delivered.

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

Version 0.2.0 does not attach a new campaign to an abandoned worktree automatically. Preserve the worktree until you have inspected it.

Check the repository and tracker:

```text
git status --short
git worktree list
git branch --list "autobuild/*"
```

Use `pinax status` for Pinax. For a Markdown backlog, inspect the claimed row. Read the latest run record under the scratch root before deciding whether to continue the branch manually or park the item.

Do not start another AutoBuild campaign for the same claimed item until the tracker and worktree state are reconciled.

## Common startup failures

`tracker preflight failed` means Pinax and the configured backlog path were unavailable or invalid. Check `[tracker]`, `.ergon/`, the `pinax` command, and the backlog table.

`primary checkout must be clean` means the main repository has tracked or untracked changes. Commit, move, or intentionally resolve them before retrying.

`required git remote is missing: origin` means the repository has no `origin` remote. Add the correct writable remote before the run.

`authentication is unavailable` means the selected coding assistant command was found but its authentication probe failed. Run the harness-specific checks in this guide.

`missing required run configuration` names the profile facts that are absent.

`validator ... did not pass` parks the item and records the validator output under the run directory.

## Technical documentation

- [Architecture](architecture.md)
- [Harness adapter contract](harness-adapters.md)
