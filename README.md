# AutoBuild

A coding assistant can build one change well while you watch it. A queue of forty changes is different: someone has to hand each one over, check the result, review it, merge it, and notice when the assistant's allowance runs out. AutoBuild does that work for a repository's queue.

You keep your list of work inside the repository, as a table in a file named `BACKLOG.md` or in Pinax, a command line task list that keeps its items in the repository's `.ergon/` folder ([pinax-tracker](https://github.com/antikas/pinax-tracker)). AutoBuild calls this list the tracker, and it both reads and writes it. Each entry in the list, with its title and status, is an item. For each item you write a short request in a Markdown file in the repository, called a brief, and put the brief's path in the item's row. An item is ready when the tracker lists it as runnable: in a `BACKLOG.md` table that means a status such as `ready` or `todo`, and in Pinax an item that `pinax ready` lists.

When you start a run, AutoBuild hands each ready item to a coding assistant you already use. Before it starts an item, it records in the tracker that the item is taken, so that nobody else takes it. AutoBuild calls that record a claim. The assistant builds the change in a separate folder. Your own test command, which AutoBuild calls the validator, decides whether the change works, and a second, separate assistant reads it against the brief. A change that passes both is merged and pushed to `origin`. A change that fails is set aside with its reason written in the tracker, and the next item starts; AutoBuild calls that parking the item.

One run of `autobuild run` over the ready items is called a campaign. It ends when no ready item is left, when it reaches its item limit, when you pause it, when every coding assistant has reached its usage stop level, or when one wait lasts past its time limit. It also ends at once when it cannot read the tracker at launch. AutoBuild then commits a report of the whole run. It suits a git repository with an `origin` remote and a test command that already passes, when you accept that changes which pass are pushed without your approval of each one.

By default AutoBuild merges every accepted change into the repository's default branch and pushes it to `origin` without asking you, whatever branch you launched from. A launch in this mode from another branch, or from a detached head, is refused before anything is claimed. The message names the default branch, the other delivery mode, and the branch you are on or, on a detached head, a short revision. A launch is also refused when the default branch of `origin` cannot be established, because the clone records no remote head and `origin` does not answer. After `origin` accepts a delivery, AutoBuild also checks out the default branch in your own checkout and moves it forward.

The other delivery mode, `--delivery-mode current-branch-pr`, merges accepted items into the branch checked out at launch and moves that branch forward, and it never touches the default branch. It refuses a detached head, and it refuses the default branch unless you also pass `--allow-current-branch-default`. It pushes only when you add `--push-current-branch`. AutoBuild opens no pull request. When the campaign ends, push the branch yourself if you did not pass `--push-current-branch`, and open the pull request in your git host.

AutoBuild suits your repository when all of these hold:

- It is a git repository with an `origin` remote.
- It has a test command that already passes on the committed head, without fetching packages from the network.
- You can write each piece of work as an item with a short written request, in a `BACKLOG.md` table or in Pinax.
- You accept that changes which pass the tests and the review are merged without you approving each one, or you use the delivery mode that collects them on a branch of your own.
- Because the number of correction rounds is limited, you accept that a change the reviewer still wants corrected after the last round is merged when the tests pass, with each finding kept as a new item.
- Your repository accepts the commit subjects AutoBuild writes, such as `ADDED: <title>`, which no setting changes.

## The coding assistant on your machine

Read this before your first `autobuild run`. AutoBuild works on each item through sessions of your coding assistant, each with one job, and calls each such session a seat. The builder is the seat that writes the change, the reviewer is the seat that reads it, and a specialist is a seat that gives a second judgement. Each coding assistant you set up for AutoBuild is called a lane. The built-in lanes are `claude-code`, `codex`, `github-copilot` and `claude-sdk`.

With the default tools in `[policy] allowed_tools` (`read`, `write`, `shell`, `python` and `git`), a builder runs any command as your user, and nobody is asked first. Each lane runs its builder as follows, and AutoBuild adds no sandbox of its own on any lane:

| Lane | How its builder runs |
|---|---|
| `claude-code` | Bash and file editing under Claude Code's `dontAsk` permission mode, with the network your machine has |
| `claude-sdk` | Bash and file editing under Claude Code's `dontAsk` permission mode, with the network your machine has |
| `github-copilot` | Shell and write tools with `--no-ask-user`, with the network your machine has |
| `codex` | Inside Codex's own workspace-write sandbox (`-s workspace-write`), with `-a never`, so it never asks either; Codex is the only lane that runs its builder in a sandbox |

Outside subscription routing, AutoBuild removes only `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` from a seat's environment; the `claude-sdk` lane also empties `ANTHROPIC_BASE_URL` and `CLAUDE_CODE_OAUTH_TOKEN`. Every other credential in your shell, such as `GITHUB_TOKEN`, `OPENAI_API_KEY` or an SSH agent, reaches the assistant and the builder's commands. On `claude-sdk`, a builder with `shell` can read the API key file.

Nothing in AutoBuild stops a builder from committing or pushing with git itself: its instructions only tell it to leave its changes uncommitted. The folders you list in `[policy] allowed_roots` are checked against what AutoBuild asks for, and are passed to no assistant. Run a campaign only on a machine, and with credentials, that you would trust to an unattended shell session.

## Everything AutoBuild pushes to origin

Besides accepted changes, these commits reach `origin` in the default mode. Any local commit on the default branch that `origin` does not yet hold goes out with the first of them.

- **The claim.** In a `BACKLOG.md` table, a commit `MODIFIED: claim <id> in BACKLOG` on the default branch, pushed before any work starts. Pinax commits and pushes its own claims.
- **The park record.** A tracker commit with the reason, pushed. The parked item's product change stays local.
- **New items from review findings.** In a `BACKLOG.md` table, a commit `ADDED: propose <title> in BACKLOG`, pushed.
- **The tracker close.** In a `BACKLOG.md` table, `MODIFIED: close <id> in tracker`; Pinax writes its own close commit. Either is pushed to the default branch with the item.
- **Merge commits.** When the default branch moved after an item started, AutoBuild merges it into the item's branch with a commit `MERGED: integrate <branch> into <item branch>`, and that commit is pushed with the item.
- **The report.** `ADDED: campaign <campaign-id> report`, pushed to the default branch at the end. Its Run record section holds the local path of the run folder, the folder where AutoBuild keeps the records of one run. On Windows that path contains your user name.

The item branches `autobuild/<campaign>/<item>-<8 characters>` stay in your local repository and are never pushed. In `current-branch-pr` without `--push-current-branch`, the deliveries, the `BACKLOG.md` claim and the report are committed on the launch branch only, and nothing of theirs is pushed. With `--push-current-branch`, AutoBuild pushes the launch branch to `origin`. The default branch on `origin` stays as it was unless you launched from it with `--allow-current-branch-default`.

## Seats and spending

Every seat is a session of your coding assistant and spends whatever that assistant is signed in to bill: your subscription allowance, an API account, or the API key on the `claude-sdk` lane. An item that reaches review starts at least two seats, one builder and one reviewer. An item that fails the validator with no retry left starts only builders, and a brief that parks the item starts none.

More seats start in these cases. Each validator retry and each timeout continuation starts another builder. A seat that a lane ends with a limit or a failed sign-in starts again on the next lane. Each correction round starts another builder and, in most cases, another review. When the reviewer decides `escalate`, which asks for a second judgement, a specialist starts. One also starts for each sensitive part of the repository that an item touches in the `strict` process mode, described under "Who sets the rules".

Without a `[usage]` table nothing limits this spending. Outside subscription routing AutoBuild does not check which account a lane bills: a Claude Code lane signed in with an API account passes its check, and a Codex seat keeps `OPENAI_API_KEY`. Because the `claude-sdk` lane bills an API key and has no allowance meter, `[usage]` never stops it ([docs/lanes-and-usage.md](docs/lanes-and-usage.md)).

With a single lane, a limit stops the work in one of two ways. The lane may report a rate limit, a quota or a failed sign-in, or end in error with no output AutoBuild can read. AutoBuild then cools the lane, which means it gives it no seats until its limit resets, and parks the running item with a reason such as `claude-code:rate_limit`. The campaign starts nothing new and checks again at the reset time, waiting at most ten minutes between checks. It ends with `hold_limit` if the wait passes `run.hold_limit_seconds`, six hours by default. With a `[usage]` table, a lane at its stop level parks the item that needed it, and the campaign ends with `usage_stop` once running items finish.

## Watch one campaign

Here is one campaign on a `BACKLOG.md` table of three ready items, with Claude Code as the only lane, launched from a clean checkout of the default branch.

1. You launch `autobuild run --allow-delivery`. Without that option AutoBuild refuses to start, because it is about to claim items and push changes.
2. Before it claims anything, AutoBuild runs its setup check. The coding assistant must be installed and signed in, and the scratch root, the folder where AutoBuild keeps its working files, must be writable. AutoBuild runs the validator once with the proxy settings pointed at a closed port and uv and pip told to work offline. A test command which needs the network therefore fails now, before any claim. The check also reads the first item's brief. The first failed check stops the launch and names its cause.
3. AutoBuild records the items that exist at this moment. An item that someone adds during the run waits for the next campaign.
4. It claims the first item and makes a worktree for it on a new branch. A worktree is a feature of Git: a second folder holding the files of the same repository, checked out on its own branch. A builder starts there, and the validator passes on its result. The reviewer, which never sees the builder's transcript, reads the change and passes it too. AutoBuild commits the change, merges it into the default branch and pushes it to `origin`.
5. The second item fails the validator. The builder's effort level, which is how hard its model is asked to reason, is `medium`, so a fresh builder tries again at `high` with the failing output in hand. The validator fails again. AutoBuild parks the item: it writes `validator tests did not pass` into `BACKLOG.md`, keeps a snapshot of the work, and moves on.
6. The third item passes the validator, and the reviewer asks for a correction. The builder makes it, the validator and a second review pass, and the item is delivered like the first one.
7. No ready item is left. AutoBuild runs the validator once more on the head of the default branch, commits a report to `docs/campaigns/<campaign-id>.md`, and prints the result as JSON. The campaign id has the form `autobuild-20261123T091500Z` unless you name one with `--campaign-id`. The program exits with code 1, because one item was parked.

While the campaign runs, `autobuild watch --latest` in a second terminal prints a progress line for each main step, each starting with its UTC time. Its last line contains `campaign completed: shipped 2, parked 1, failed 0`.

## One item, from the list to the default branch

Follow one item, `ITEM-7`, titled "Add a --json option to the export command", through a campaign.

**Selection.** A campaign handles at most 20 items by default, and every item it finishes counts, whether it was accepted, parked or failed. For a queue of forty, set `run.max_items = 40` in the profile, pass `--max-items 40` for one launch, or run a second campaign. The profile is the TOML file that holds your settings, by default `.autobuild.toml` in the repository.

**The brief.** At launch the setup check reads the brief of the next ready item. When the concurrency, the number of items a campaign builds at once, is above one, it reads the brief of every ready item. AutoBuild reads the brief again before it claims the item, and gives its full text to every seat that works on the item.

Some briefs park the item before any seat starts:

- An `Item nature:` line with the value `machine`, `cross-repository` or `owner-gated` parks it with the reason `nature:<value>`. Leave the line out for an ordinary change to this repository.
- A brief that orders git commands that stage or commit, such as `git add` or `git commit`, parks it too.
- A brief may list the files it expects to touch, as paths in backticks under a `## Declared paths` heading. A declared path outside the repository and the folders in `[policy] allowed_roots` parks the item with the reason `nature:cross-repository`. When the setup check reads that brief, the launch is refused instead.

The bullets under the brief's `## Acceptance` heading become the checklist for the review, numbered `A1`, `A2` and onwards.

**The claim.** AutoBuild claims `ITEM-7` under the name `builder@claude-code`, so that no other writer takes it. While a campaign runs, it also holds a lease on the tracker, which is a lock record that it renews on a timer. A second campaign that uses the same scratch root sees that lease and refuses to start.

**The worktree.** AutoBuild creates a worktree for `ITEM-7` in its scratch root, on a branch named `autobuild/<campaign>/ITEM-7-<8 characters>`. The scratch root holds worktrees, leases and run folders. It is `autobuild` inside the system temporary folder unless you set `[run] scratch_root`. The system may clear that folder, so set a fixed one for real work.

**The builder.** The builder for `ITEM-7` starts fresh, with the brief as its instructions. It is told to leave its changes uncommitted, to run only the tests for the files it changed, and to return a short report. A builder that commits in its worktree has its item parked, with a reason that starts `worktree head moved`. If the seat runs out of time, a new builder continues in the same worktree, up to two times, and a third timeout parks the item.

**The validator.** The validator is a command you choose, and it decides whether a change is accepted. AutoBuild runs it in the worktree on the builder's change, and only exit code 0 counts as a pass. Each time it fails, a fresh builder tries again one effort level higher, with the validator's output, until the level reaches `high`. The effort levels are `low`, `medium`, `high`, `xhigh` and `max`. A builder set to `low` therefore gets two retries, one set to `medium` gets one, and one set to `high` or above, or with no effort level, gets none. When no retry is left, AutoBuild parks the item with the reason `validator <id> did not pass`.

**The review.** The reviewer reads the change with read-only tools. It receives the brief, the change, the validator's output and the builder's report, and it never sees the builder's transcript. It reports each acceptance clause it checked as `met`, `unmet` or `not_checked`, and returns one decision: `pass`, `correct`, `escalate` or `park`. AutoBuild refuses a `pass` that marks a clause `unmet`. A `park` decision parks the item with its reason.

On `correct`, the builder gets another round, the validator runs again, and a new review follows. The process mode, which is the review procedure, sets the one exception. In `flow`, the default, every build and every correction gets a review, and an item gets two correction rounds. In `strict` mode with named sensitive parts, the correction that uses the last round is judged by the validator alone. When the last correction round still ends in `correct` and the validator passes, AutoBuild accepts the change and records each finding as a new item.

On `escalate`, a specialist judges the evidence the reviewer flagged. If the specialist passes the change, it is accepted. If it asks for a correction, the builder gets one more round while one is left; otherwise the change is accepted and each finding is kept as a new item. Any other specialist decision parks the item.

**The delivery.** AutoBuild commits the change as `ADDED: Add a --json option to the export command`, and the tracker update as a separate commit: `MODIFIED: close ITEM-7 in tracker` in a `BACKLOG.md` table, or Pinax's own close commit. Deliveries run one at a time. AutoBuild merges the latest default branch into the item's branch, never rebasing. It runs the validator again when the incoming changes touch paths that overlap the item's own. It pushes the result from the worktree to the default branch on `origin` and checks that `origin` holds it. Only then does it check out the default branch in your checkout and move it forward to the same commit. In `current-branch-pr` the target is the branch checked out at launch.

**The close.** With the delivery, the tracker shows `ITEM-7` as done. Each finding becomes a new item that AutoBuild never runs: in Pinax it is blocked, and in a `BACKLOG.md` table its status starts `Proposed:`. To build one, change it to a runnable status yourself.

AutoBuild also runs the validator on the head of the delivery target as a whole, which it calls batch validation. A batch runs after every fifth delivered item by default, and once more at the end when an item was delivered after the last batch. When a batch fails, AutoBuild reverts nothing and starts no new item. It checks the target branch again every ten minutes, and new work starts once someone pushes a commit there on which a batch passes. If that takes longer than `run.hold_limit_seconds`, the campaign ends.

**When a stage fails.** A parked item keeps its reason in the tracker. Each run has a run folder, `runs/<run-id>/` inside the scratch root, which holds `progress.log`, `events.jsonl` and an `evidence/` folder. When the item has a worktree, AutoBuild also saves a snapshot of it in the run folder's `evidence/<item>-park/` and adds that location to the tracker's reason.

A delivery that fails parks the item too. The usual causes give a reason that starts `delivery_conflict`, `delivery_revalidation_failed` or `delivery_contended`, and any other cause gives its own error text. An item failed when AutoBuild could not take it, or took it and could not record a park; the report names the error.

An item that parks or fails for its own reasons does not stop the campaign, and the next ready item starts. When it parks because no lane is left, or the tracker fails, the campaign holds new work, and it ends with `usage_stop` when every lane is stopped for usage. The report lists every item that needs you under Parked or Failed, each with its reason.

## Who sets the rules

You set the rules AutoBuild applies to your repository, in files and options that you can read and change. A few limits, such as the number of correction rounds, are fixed in the code. The process mode, and in `strict` mode whether an item touches a part named under `[process.boundaries]`, decide how many correction rounds an item gets; no setting names the number itself. Other fixed limits include the two builder continuations after a timeout, the three delivery attempts, the `high` ceiling on effort retries, and the commit subjects.

AutoBuild refuses to start when the profile holds a key it does not know, so a misspelt setting is reported at once. The profile names the validator command, and AutoBuild runs exactly that argument list, which no model can change. The profile also sets these:

| Setting | What it decides |
|---|---|
| `[models]` and the effort keys | The model and effort level for each seat |
| `run.max_items` | How many items a campaign may build |
| `run.seat_timeout_seconds` | How long a seat may run, 900 seconds by default |
| `run.concurrency` | How many items run at once; by default AutoBuild works it out from the machine's processors and memory, and you can set a number up to 64 |
| `[selection] allow` and `[selection] exclude` | Which items run; `--allow-item` and `--exclude-item` do the same for one launch |

You also write every brief, and with it the acceptance clauses that the reviewer checks.

You choose the process mode with `[process] mode`. The `strict` mode needs `[process] test_map`, which maps path patterns to the tests or the test command that cover them. In it you can also name the sensitive parts of the repository under `[process.boundaries]`, each with its paths and an optional file of rules. An item that touches one of these parts gets one specialist per part touched and two correction rounds. When at least one part is named, an item that touches none gets one correction round. With no part named, every item gets two, as in `flow`.

You can list several lanes in `run.lanes`, in the order you prefer. When a lane reports a rate limit, a quota or a failed sign-in, AutoBuild cools it until the limit resets, or for `run.lane_cool_seconds` (one hour by default) when no reset time is given. It then runs the same seat on the next lane.

With a `[usage]` table, AutoBuild also reads your Claude subscription allowance before each seat on the Claude Code lane:

- At `warn_percent` (80 by default), a campaign that runs several items at once starts a new item only when nothing else runs.
- At `stop_percent` (90) for the weekly limits, or `five_hour_stop_percent` (95) for the short session window, AutoBuild moves seats off that lane.
- When every lane is stopped for usage, the campaign lets running items finish and ends with `usage_stop`.

The Claude meter reads an endpoint that is undocumented and may change or stop working without notice. Spreading seats across Claude and Codex subscriptions (subscription routing) is described in the guide to lanes and usage.

## The checks that decide acceptance

- Every `autobuild run` needs `--allow-delivery`, and the setup checks run before the first claim.
- No model can change the validator: each run must carry the exact argument list from your profile.
- The reviewer is a separate seat with read-only tools, and its decision must fit fixed rules. The report has a Review depth section, which marks a review as shallow when it looked at fewer changed files than the change touches or checked no acceptance clause.
- AutoBuild refuses to close an item when its validation ran on an older state of the change, a validator failed, a reference to saved evidence is missing, or a changed path lies outside the worktree.
- The set of items is fixed at launch, and a campaign never builds a new item that it registered itself.
- AutoBuild never rebases. In the default mode, your local default branch moves only onto a commit that `origin` accepted.
- Batch validation checks the head of the delivery target branch, and a failed batch makes `autobuild run` exit 1 even when every item was accepted.
- Every event of a run is appended to `events.jsonl` in the run folder. When the report cannot be delivered, the printed result says `report delivery failed` with the cause.

Each of these checks is a rule the code applies to evidence, so an assistant's own claim that its work is done counts for nothing.

## Limits

AutoBuild builds the items that you made ready. It does not decide product scope, and it never makes its own proposed items runnable. AutoBuild itself runs no deploy or release step, although a builder with `shell` can run any command that a brief or a test leads it to. It has no command that undoes a delivered change.

Leave the checkout you launched from alone while a campaign runs, and do other work in another clone. In the default mode AutoBuild switches its branch to deliver. A modified tracked file there makes a delivery refuse with `primary checkout must be clean before delivery`. A `BACKLOG.md` claim refuses any uncommitted or untracked change there, and a Pinax claim refuses any staged or modified file.

Items created after a campaign starts wait for the next campaign. The reviewer never reads the builder's transcript. It judges the change itself, and AutoBuild measures each review's depth by comparing the reviewer's report with the change. Two campaigns see each other's leases only when they share a scratch root. A `BACKLOG.md` tracker runs one item at a time unless you set `run.concurrency` to a number.

Outside subscription routing, only the Claude Code lane has an allowance meter. That meter needs `curl` and Claude Code's own sign-in in its default place, and a sign-in kept in a keychain or a custom `CLAUDE_CONFIG_DIR` is not found. On macOS AutoBuild measures only the number of processors, and a stalled seat of the `claude-code`, `codex` or `github-copilot` lane is stopped only when its time limit runs out. The strict process mode adds checks that catch a builder's mistakes, and you should not treat them as a security barrier. An internal scheduler error ends the run with exit code 2 and no report.

## Install and first run

### Requirements

- Python 3.11 or later. The package has no runtime dependencies.
- A git repository with an `origin` remote, and a test command that exits 0 on the committed head with the proxy settings pointed at a closed port and uv and pip told to work offline.
- At least one coding assistant, installed and signed in: Claude Code (`claude`), Codex (`codex`) or GitHub Copilot CLI (`copilot`). The `claude-sdk` lane uses an API key read from a file, and needs the optional `sdk` part of the package.
- A tracker: Pinax (the `pinax` command from [pinax-tracker](https://github.com/antikas/pinax-tracker) 0.2.1 or later, with a `.ergon/` folder in the repository), or a `BACKLOG.md` table.

Platform and coding assistant are separate choices. AutoBuild picks its process handling from the operating system, and the coding assistant from your profile. The guide to platforms covers what each operating system does not do.

### Install

```
pip install autobuild-factory==0.12.0
```

For the `claude-sdk` lane, install the `sdk` part as well:

```
pip install "autobuild-factory[sdk]==0.12.0"
```

This installs the `autobuild` command, and `python -m autobuild` runs the same program. With uv you can run it without installing, for example:

```
uvx --from autobuild-factory==0.12.0 autobuild preflight
```

### A first run

Start from a clean checkout of the default branch of a repository whose test command already passes. The example below assumes that `python -m pytest -q` is that command; put your own in its place.

Add a `BACKLOG.md` file with one ready item:

```
| Item | Title | Status | Brief |
|---|---|---|---|
| ITEM-1 | Add a --json option to the export command | ready | docs/briefs/item-1.md |
```

Write the brief at `docs/briefs/item-1.md`:

```
# Add a --json option to the export command

## Acceptance

- `export --json` prints the export as one JSON document.
- The text output of `export` without the option is unchanged.
```

Add `.autobuild.toml`. In it, `harness` names the coding assistant of a profile with a single lane, so this profile needs no `[lanes]` tables. Put your test command in the `argv` list:

```toml
[run]
harness = "claude-code"

[models]
builder = "claude-opus-5-5"
reviewer = "claude-opus-5-5"
builder_effort = "medium"

[validator]
id = "tests"
argv = ["python", "-m", "pytest", "-q"]
```

Commit these three files, because a `BACKLOG.md` claim refuses a checkout with uncommitted or untracked changes. You need not push them: the first claim pushes them to `origin` together with its own commit. Then run the setup check without claiming any item:

```
autobuild preflight
```

When the setup is ready, the first line reads `lane claude-code: capable (Claude Code authentication is available)`, one `preflight <check>: <detail>` line follows for each check, and the command exits 0. When a check fails, it prints `preflight failed:` with the cause. Behind a proxy that needs certificate or proxy variables, such as `HTTPS_PROXY` or `SSL_CERT_FILE`, the check refuses and names the variable; [docs/getting-started.md](docs/getting-started.md) explains which setting allows it.

The profile above has no `[usage]` table, so nothing limits what the seats spend from your subscription. Start the campaign from the default branch, because the default mode refuses any other:

```
autobuild run --allow-delivery
```

In a second terminal, follow it:

```
autobuild watch --latest
```

For this one item, the last line reads `<UTC time> campaign completed: shipped 1, parked 0, failed 0; report docs/campaigns/<campaign-id>.md; stop queue_dry progress hook failures: 0`. The default branch on `origin` then holds your setup commit, followed by four new commits: the claim, `ADDED: Add a --json option to the export command`, the tracker close, and the report. Your checkout ends on the default branch at the report commit.

`autobuild run` exits 0 when every item was accepted or none ran, and 1 when an item parked or failed or the batch validation failed. It exits 2 when it stops before completion, and prints `AutoBuild failed before completion:` with the cause on standard error.

A skill is a set of written instructions that a coding assistant loads for one kind of task. AutoBuild's source repository holds one in the `skills/autobuild-plan/` folder, outside the installed package. It turns one build request into a short plan and a registered list of items, and it stops before any launch.

### The other documents

| Document | The question it answers |
|---|---|
| [docs/getting-started.md](docs/getting-started.md) | How do I go from a repository to my first delivered item? |
| [docs/writing-briefs.md](docs/writing-briefs.md) | What must a brief contain so the item is built and not parked? |
| [docs/running-campaigns.md](docs/running-campaigns.md) | What happens to an item, why did it park or the campaign stop, and how do I watch, pause, resume and recover? |
| [docs/lanes-and-usage.md](docs/lanes-and-usage.md) | Which coding assistant do I use, what happens at a usage limit, and how do I spread seats across subscriptions? |
| [docs/trackers.md](docs/trackers.md) | How do I set up Pinax or a `BACKLOG.md` table so AutoBuild can read and write it? |
| [docs/platforms.md](docs/platforms.md) | How do I install it on my operating system, and what does each one not do? |
| [docs/reference/commands.md](docs/reference/commands.md) | What does each command and option do, what does it print, and what does its exit code mean? |
| [docs/reference/profile.md](docs/reference/profile.md) | What does each profile key do, what is its default, and what makes it refuse? |
| [docs/reference/harness-adapters.md](docs/reference/harness-adapters.md) | What command, options and environment does each lane run? |
| [docs/reference/events-and-records.md](docs/reference/events-and-records.md) | What does a run folder hold, and what does each event and report section mean? |
| [docs/architecture.md](docs/architecture.md) | Where do I change or extend the code? |
