# Getting started

You have a git repository and a first small change that you would like a coding assistant to build for you. This guide takes you from that repository to one delivered item: the change built, tested, reviewed, merged and pushed by AutoBuild, with a report of the run. An item is one piece of work in your list, with a written request of its own.

Before you start, you need:

- a remote named `origin` that you can push to;
- the repository's default branch checked out, with nothing uncommitted;
- Python 3.11 or later;
- a test command that already passes;
- Claude Code installed and signed in.

The guide follows one case. The repository has one ready item, `ITEM-1`, titled "Add a --json option to the export command", and Claude Code is the coding assistant. The item, its title, its brief, the test command, the model names and the branch name `main` are stand-ins: put your own values in their place. With your values in place, every command runs as shown. The [README](../README.md) explains what AutoBuild does with an item at each stage, and this guide uses its terms.

## The starting state

Three words come up from here on. The launch is the `autobuild run` command that starts the work. The setup check is the set of checks that the launch runs first, and that `autobuild preflight` runs on its own. A claim is a commit that marks an item as taken in the tracker, which AutoBuild pushes to `origin` before any work on the item starts.

Check four things before you install anything. The launch refuses a repository without the remote, and the setup check refuses one without a passing test command. The claim refuses a checkout that is not clean. The setup check and the launch both refuse any branch but the default one.

The repository has a remote named `origin`, and you can push to it. AutoBuild pushes the claim, the accepted change and the report to `origin`, and a launch without that remote is refused with `required git remote is missing: origin`.

Your test command passes on the committed head, without network access. AutoBuild calls this command the validator. The setup check runs it once in your checkout before it claims anything, and only exit code 0 counts as a pass. A command that finds no tests fails here too: pytest exits 5 when it collects nothing, and the launch is then refused. In this guide the test command is `python -m pytest -q`. Run yours first, to see that it passes:

```
python -m pytest -q
```

The setup check later runs it again with the package tools offline, so this run by hand shows only that the tests pass.

The checkout is clean. A `BACKLOG.md` claim refuses a checkout with any uncommitted change, untracked files included. Add anything your tools leave behind to `.gitignore`, creating the file if the repository has none. Typical entries are a virtual environment folder and the `__pycache__/` folders that running the tests writes. You commit `.gitignore` with the other setup files below.

You are on the repository's default branch, and it is also the default branch of `origin`. To see which branch that is, run `git remote show origin` and read its `HEAD branch:` line. In this guide that branch is `main`; use yours wherever the guide writes `main`. The warnings before the launch command explain why this matters.

## Install AutoBuild

Install the package into the environment you will run it from. A virtual environment inside the repository needs its `.gitignore` entry:

```
pip install autobuild-factory==0.12.0
```

This installs the `autobuild` command, and `python -m autobuild` runs the same program. Confirm that the command is on your `PATH`:

```
autobuild --help
```

It lists the commands, such as `run`, `watch` and `preflight`. The program has no `--version` option, and `autobuild preflight --json` later reports the installed version in its `version` field. The [platforms guide](platforms.md) covers installation on each operating system and what each one does not support.

## Sign in to Claude Code

AutoBuild runs each seat through the `claude` command, so that command must be on your `PATH` and signed in. Sign in the way Claude Code's own documentation describes, then run the two commands that AutoBuild runs to check it:

```
claude --version
claude auth status --json
```

A lane is one coding assistant that you have set up for AutoBuild, and this guide uses the `claude-code` lane. AutoBuild treats that lane as capable when both commands exit 0 and the JSON from the second has `loggedIn` or `authenticated` set to `true`. Each check has 20 seconds and makes no model call. The [guide to lanes and usage](lanes-and-usage.md) covers the other coding assistants and what happens at a usage limit.

## Add the tracker

A tracker is the list of work items that AutoBuild reads and writes. The ready items in it form the queue that a campaign works through. The smallest tracker is a Markdown table in a file named `BACKLOG.md` at the root of the repository. Create it with one ready item, using your own id, title and brief path:

```
| Item | Title | Status | Brief |
|---|---|---|---|
| ITEM-1 | Add a --json option to the export command | ready | docs/briefs/item-1.md |
```

AutoBuild reads the first table whose header has these four columns, and every row needs all four cells filled. A status it does not recognise makes the whole tracker unreadable, so use `ready` for an item you want built. The [trackers guide](trackers.md) lists the other column names and statuses it accepts, and covers Pinax.

## Write the brief

A brief is the written request for one item, kept as a file and named in the item's Brief cell. Write it at `docs/briefs/item-1.md`, describing a small change that fits your own repository in place of the example:

```
# Add a --json option to the export command

## Acceptance

- `export --json` prints the export as one JSON document.
- The text output of `export` without the option is unchanged.
```

The bullets under `## Acceptance` become the checklist that the reviewer answers, numbered `A1` and `A2`. Keep git out of the brief's instructions. To park an item is to set it aside with a reason written into the tracker. A brief that orders the builder to run `git add`, `git commit` or another staging command is parked when its turn comes, before any claim. The [guide to writing briefs](writing-briefs.md) covers everything else a brief can hold.

## Write the profile

The project profile is a TOML file, `.autobuild.toml` at the root of the repository, that holds your settings for this repository. AutoBuild finds it there when you run it from the repository root. Create the smallest profile that runs, with one setting added for the scratch root:

```toml
[run]
harness = "claude-code"
scratch_root = "D:/autobuild-work"

[models]
builder = "claude-opus-5-5"
reviewer = "claude-opus-5-5"
builder_effort = "medium"

[validator]
id = "tests"
argv = ["python", "-m", "pytest", "-q"]
```

`run.harness`, `models.builder`, `models.reviewer`, `validator.id` and `validator.argv` are required. `run.harness` names the lane that runs the seats, here `claude-code`. Put a model that your Claude Code plan offers in place of `claude-opus-5-5`. AutoBuild runs the `argv` list exactly as you wrote it, so replace it with your own test command, split into its words. With `builder_effort = "medium"`, a builder whose change fails the validator gets one more attempt at `high` before the item parks. AutoBuild refuses a profile that holds a key it does not know. The [profile reference](reference/profile.md) lists every key and its default.

Set `run.scratch_root` before your first launch, because the campaign report writes the absolute paths of your local run folder into the repository and pushes them to `origin`. The scratch root holds AutoBuild's worktrees and run folders. Without the setting it is `autobuild` inside your system's temporary folder, which on Windows sits under your user folder, so the pushed paths would carry your user name. Our advice is a neutral absolute path outside the repository, such as the one above. A relative path is resolved against the profile's folder, so a plain name such as `autobuild-work` would put the scratch root inside your checkout. The profile is itself committed and pushed, so the path you choose there reaches `origin` too.

## Commit the setup files

AutoBuild reads the committed head, and the claim needs a clean checkout, so commit the four files. Push them as well, so that `origin` holds your setup before the launch; otherwise the first claim pushes them with its own commit:

```
git add .gitignore BACKLOG.md docs/briefs/item-1.md .autobuild.toml
git commit -m "ADDED: AutoBuild tracker, brief and profile"
git push origin main
git status --short
```

The last command must print nothing.

## Check the setup

Run the setup check. It does everything a launch does before the first claim, and it claims nothing:

```
autobuild preflight
```

It prints one line for each lane, `lane claude-code: capable (Claude Code authentication is available)` or `lane claude-code: not capable (<reason>)`. When every check passes, one `preflight <check>: <detail>` line follows for each check. When a check fails, no check lines print, only `preflight failed:` followed by the error of the check that failed, which reads `<check> preflight failed: <cause>`. When the checkout is not on the default branch, no lane is probed and no lane line prints, only `preflight failed:` followed by a message that names the default branch and the branch you are on or, on a detached head, a short revision. The same happens when the default branch of `origin` cannot be established, and the message says so.

It exits 0 when a launch would pass, and 1 when the checkout is not on the default branch, the default branch of `origin` cannot be established, no lane is capable or a check failed. It exits 2 when the profile, the tracker or an adapter could not be loaded, and then prints `AutoBuild preflight failed before completion:` with the cause. An adapter is the part of AutoBuild that drives one coding assistant or one tracker.

AutoBuild loads the tracker and checks the branch before any of the checks below. The checks then run in a fixed order, and the first one that fails stops the rest.

- `dns-tls` resolves and opens a TLS connection to each host you listed in `[preflight] tls_targets`, and to the host of `origin` when `origin` is an `https` address. A failure means this machine cannot reach that host.
- `interception` fails when one of `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `SSL_CERT_FILE`, `SSL_CERT_DIR`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `NODE_EXTRA_CA_CERTS`, `NODE_TLS_REJECT_UNAUTHORIZED`, `GIT_SSL_CAINFO`, `GIT_SSL_NO_VERIFY` or `SSLKEYLOGFILE` is set in your environment. Unset it, or accept it in the profile when this machine needs it, as the troubleshooting section below shows.
- `scratch` fails when AutoBuild cannot create or write its scratch root, or when another live campaign holds the tracker. The message names the campaign, process and host that hold it.
- `telemetry` checks that the adapter for your coding assistant turns that assistant's telemetry off in every seat.
- `removed-environment` never fails. It warns when you have set `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN`, because AutoBuild removes both from every seat.
- `validator-runnable` finds the validator's first word on your `PATH`, runs it with `--version`, and checks that a relative `.py` file named as the second word exists. A failure means the command itself cannot start.
- `validator-offline` runs the whole validator once in your checkout, with the proxy variables pointed at a closed local port and the offline settings of pip and uv turned on. A direct network connection still works, so this check does not prove that your tests never use the network. It fails when the validator exits with any code other than 0, and the message then gives the exit code and the last line of output. It also fails when the validator runs past `run.command_timeout_seconds`, which is 600 seconds unless you raise it in the `[run]` table, or past a `validator.budget_seconds` that you set.
- `validator-budget` fails only when you set `validator.budget_seconds` higher than `run.command_timeout_seconds`. The profile above sets neither, so it passes.
- `transport` checks that the adapter for your coding assistant hands its instructions over in a file.
- `briefs` reads the brief of the next ready item. It fails when the brief is missing, empty, larger than 1048576 bytes or unreadable, or when the brief declares a path outside the repository. A brief that orders git staging passes with a warning, and the item is parked when its turn comes.

A `[usage]` table adds a `usage` check after these, and the profile above has none.

## Start the campaign

A campaign is one run of `autobuild run` over the queue. Read these four warnings before you start one.

**Delivery goes to the default branch.** By default AutoBuild merges every accepted change into the repository's default branch and pushes it to `origin` without asking you, whatever branch you launched from. The launch itself must come from the default branch, as in this guide: from another branch, or from a detached head, AutoBuild refuses to start and claims nothing. The message names the default branch and the branch you are on or, on a detached head, a short revision. AutoBuild also refuses to start when the clone records no remote head and `origin` does not answer, because the default branch is then not established. To collect accepted changes on a branch of your own instead, read about `--delivery-mode current-branch-pr` in the [command reference](reference/commands.md).

**Leave this checkout alone while the campaign runs.** The primary checkout is the checkout you launched from, and AutoBuild delivers through it: it checks out the default branch there and moves that branch forward. Delivery refuses a primary checkout in which a tracked file is changed or staged, with `primary checkout must be clean before delivery`. AutoBuild then tries to record the park from the primary checkout, and that refuses too. The item ends failed with its claim still on `origin`, and `autobuild run` exits 1. Do any other work on this repository in a separate clone.

**The builder runs commands on your machine.** The builder is the seat that writes the change, and it runs commands with your permissions without stopping to ask you. On the Claude Code lane AutoBuild adds no sandbox around it. Give a campaign only a machine and credentials that you would trust to an unattended shell session.

**Every seat spends your subscription.** A seat is one session of your coding assistant, so every builder and reviewer seat spends your Claude Code subscription allowance. The profile above has no `[usage]` table, so nothing limits that spending. The [guide to lanes and usage](lanes-and-usage.md) explains how to set a limit.

Start the campaign from the repository root, on `main`:

```
autobuild run --allow-delivery
```

`--allow-delivery` is required, because the launch claims items and pushes changes without asking you again. Without it AutoBuild reads and checks the profile and then stops, before it claims or builds anything.

While the campaign runs, AutoBuild writes a progress line for each main step to standard error, and each line starts with the UTC time of its event. To follow it from a second terminal, run:

```
autobuild watch --latest
```

`--latest` follows the newest run in the scratch root, whichever repository it belongs to. When several projects share one scratch root, name the run with `--run <run-id>` instead; the run id is the name of the run's folder under `runs` in the scratch root. `watch` exits 0 once it has printed the completion line, described below.

## Read the result

When `ITEM-1` passes the validator and the review, AutoBuild commits the change as `ADDED: Add a --json option to the export command` and the tracker update as `MODIFIED: close ITEM-1 in tracker`, and pushes both to `main` on `origin`. Before the build started it had already pushed a claim commit, `MODIFIED: claim ITEM-1 in BACKLOG`. In `BACKLOG.md` the row's status now reads `Done (` followed by the first 12 characters of the item commit and `)`.

The queue is then empty. AutoBuild runs the validator once more on the delivered head of `main`, and then commits a report of the campaign to `docs/campaigns/<campaign-id>.md` with the subject `ADDED: campaign <campaign-id> report`, delivered to the same branch. The campaign id is `autobuild-` followed by the UTC launch time, unless you set `--campaign-id`. The report lists the items under Shipped, Parked and Failed, then the batch validation, the follow-ups, the next ready item, seat usage, review depth, the schedule and the run record. The run record section holds the absolute paths of your run folder and of its `progress.log`, which is why you set `run.scratch_root` before the launch.

The default branch of `origin` now holds four new commits in this order: `MODIFIED: claim ITEM-1 in BACKLOG`, `ADDED: Add a --json option to the export command`, `MODIFIED: close ITEM-1 in tracker` and `ADDED: campaign <campaign-id> report`. Look at them together with your setup commit:

```
git fetch origin
git log --oneline -5 origin/main
```

In this run no merge commit appears, because the item was built on top of the claim and nothing else reached `main` meanwhile. When the default branch moves on during a campaign, AutoBuild merges it into the item's branch before delivery. That merge is a commit with a subject such as `MERGED: integrate origin/main into autobuild/<campaign-id>/ITEM-1-<8 hex>`, and it reaches `origin` with the item. AutoBuild never rebases.

Every commit carries the git identity configured for your repository, because AutoBuild runs plain `git commit` and sets no author of its own.

Your checkout is on `main`, fast-forwarded to the report commit. AutoBuild removes the item's worktree, the separate working copy in which the builder worked. It leaves a local branch named `autobuild/<campaign-id>/ITEM-1-` followed by 8 hexadecimal characters for each item it built. That branch is never pushed, and you may delete it.

The progress output ends with the completion line, which for this case reads `<UTC timestamp> campaign completed: shipped 1, parked 0, failed 0; report docs/campaigns/<campaign-id>.md; stop queue_dry progress hook failures: 0`. The stop reason `queue_dry` means that no ready item was left. On standard output, `autobuild run` prints the campaign result as JSON with the schema `autobuild.campaign-result.v1`. Each entry under `items` gives the item's `disposition` as `accepted`, `parked` or `failed`, with its `reason` and commits.

The exit code tells you whether anything needs you:

- 0 means every item was accepted, or no item ran.
- 1 means an item parked or failed, or the final validator run on the delivered head failed, even when every item was accepted.
- 2 means AutoBuild stopped before completion. It prints `AutoBuild failed before completion:` and the cause on standard error, and no JSON.

Everything AutoBuild recorded about the run is in a run folder under `runs` in the scratch root: `events.jsonl` holds every event in order, `progress.log` holds the progress lines, and `evidence/` holds the saved outputs. The [events and records reference](reference/events-and-records.md) explains each file and report section.

To undo a delivered change, use ordinary Git, because AutoBuild has no command for it. Once the campaign has ended, run `git revert` on the item commit, then push the revert to `origin`. The tracker row still reads `Done (...)`, so set its status yourself if you want the item built again.

## When the first run goes wrong

### `pass --allow-delivery after human approval`

The full message is `this workflow claims items and delivers accepted changes; pass --allow-delivery after human approval`, and the exit code is 2. You ran `autobuild run` without `--allow-delivery`. Nothing was claimed or built. Run it again with the option.

### `no capable harness lane`

The launch found no lane it could use, and the message names each lane with the reason it cannot be used. `claude executable was not found` means the `claude` command is not on the `PATH` of the shell that runs AutoBuild. `Claude Code is not authenticated` means `claude auth status --json` did not report you as signed in. Sign in again and run `autobuild preflight` until the lane line reads `capable`.

### `tracker preflight failed`

AutoBuild could not load a tracker, so it stopped before the checks. `autobuild preflight` prints `AutoBuild preflight failed before completion: tracker preflight failed:` with what it found, and exits 2. For a `BACKLOG.md` table, check that the header has the Item, Title, Status and Brief columns, that a `|---|` separator row follows it, that every cell is filled, and that every status is one AutoBuild recognises.

### `interception preflight failed`

The message reads, for example, `interception preflight failed: HTTPS_PROXY is set but is not listed under accepted_environment`. When this machine needs that variable, for a company proxy or certificate, accept it by name in the profile and commit the change:

```toml
[preflight]
accepted_environment = ["HTTPS_PROXY", "SSL_CERT_FILE"]
```

An accepted proxy variable does not break `validator-offline`, because that check sets `HTTP_PROXY`, `HTTPS_PROXY` and `ALL_PROXY` to a closed local port for its own run of the validator.

### `validator-offline preflight failed`

Your test command did not exit 0 when the setup check ran it, or ran past its time limit. `autobuild preflight` prints a failed run as, for example, `preflight failed: validator-offline preflight failed: the validator did not pass offline (exit 5): <last line of output>`. `autobuild run` prints the same message after `AutoBuild failed before completion:`.

The text after the exit code is the last line the command printed, or `no output`. Exit 5 from pytest means it collected no tests. A message that reads `the validator did not finish offline within 600.0s` means your suite is slower than `run.command_timeout_seconds`, so raise that setting in the `[run]` table. Run the command from your profile yourself on the committed head. If it passes there, look for a test that downloads something or installs a package, because the setup check runs it with the package tools offline.

### `validator-runnable preflight failed`

The first word of `validator.argv` could not be found or did not answer `--version`. On some Linux systems there is no `python` command, only `python3`. Write the command exactly as your shell finds it.

### `telemetry preflight failed` or `transport preflight failed`

Both checks test what the adapter for your coding assistant sets for its own seats. They read nothing from your repository, your brief or your validator, so no change of yours there fixes them. On a built-in lane such as `claude-code`, a failure points at AutoBuild itself: keep the full message and report it to the project.

### `required git remote is missing: origin`

The repository has no remote named `origin`. Add one with `git remote add origin <url>` and push `main` to it before you launch.

### `primary checkout must be clean before a tracker claim`

The `BACKLOG.md` claim found an uncommitted change or an untracked file in your checkout. The validator runs in your checkout during the setup check, so a test command that leaves new files behind causes this too. Commit or ignore those files, and check that `git status --short` prints nothing.

### A push that `origin` rejects

The claim is the first push AutoBuild makes, so missing push rights or a protected default branch stop the item there. The item ends failed with a reason that starts `git push origin <commit>:refs/heads/main failed:` and carries git's own message. The claim commit stays in your local checkout and never reaches `origin`.

Remove that local commit with `git reset --keep origin/main`, which restores the row to `ready`. Then get push rights to the default branch, or lift its protection for this account, and launch again.

### An item that ended failed with its claim on `origin`

The row on `origin` reads `Claimed by builder@claude-code`, and no campaign takes a claimed row. AutoBuild keeps the item's worktree and evidence in the scratch root. To run the item again, make the primary checkout clean, set the row's status back to `ready`, commit and push it, then launch again. The [guide to running campaigns](running-campaigns.md) covers why an item failed, how to resume a campaign and how to recover a run.

### `missing required run configuration`, `contains unknown fields` or `must be a non-empty string array`

The profile lacks a required key, or holds a key AutoBuild does not know, often a misspelt one. The message names each key. A missing or empty `validator.argv` reads `validator.argv must be a non-empty string array`. Compare the profile with the one in this guide.

### The item parked with `validator tests did not pass`

The builder's change failed your test command, and the retry at a higher effort failed too. The campaign itself completed, and `autobuild run` exits 1. In `BACKLOG.md` the row reads `Parked: validator tests did not pass [snapshot=evidence/ITEM-1-park]`. That folder, inside the run folder, holds the builder's changes to tracked files as `changes.patch`, and a copy of each new file the builder added under `files/`.

Read the patch, then sharpen the brief or split the item. To run it again, edit the row's status from `Parked: ...` to `ready` yourself, then commit and push the change.

### The item parked with `review disposition was park`

The reviewer judged that the change should not be delivered and returned `park`. After an `escalate`, the specialist can end the item the same way, and the reason then names its decision, `park` or `escalate`. In the run folder, `events.jsonl` records each review with its finding codes. Read them, sharpen the brief, and set the row back to `ready` as above.

### The item parked with `brief:git-staging`

The brief told the builder to run a git command that stages or commits. The reason names the first such line. Rewrite each such line so that it forbids the command, reports one, or quotes one. The [guide to writing briefs](writing-briefs.md) shows the forms that pass.

### `report delivery failed:`

The items were delivered, and the report commit was not. The JSON result then carries `report delivery failed:` and the cause in its `repository_report_ref` field, where the report's path would otherwise be. The completion line still names the report's intended path. Read the cause, fix it, and find the run's events in `events.jsonl` in the run folder.

## Limits of this first run

AutoBuild does not ask before it pushes: once you pass `--allow-delivery`, the claim, every accepted change and the report go to `origin` on their own. A `BACKLOG.md` tracker runs one item at a time unless you set `run.concurrency` to a number. An item that you add after the launch waits for the next campaign. The setup check runs your tests with the package tools offline and the proxies closed, and a direct network connection still works there. AutoBuild has no command that undoes a delivered change. AutoBuild itself runs no deploy or publish step. The builder seat can run shell commands, so a brief or a test that leads it to a deploy or publish command can still run one.

## Next questions

- [Writing briefs](writing-briefs.md): what a brief must contain so the item is built and not parked.
- [Running campaigns](running-campaigns.md): what happens to an item, why it parked or the campaign stopped, and how to watch, pause, resume and recover.
- [Lanes and usage](lanes-and-usage.md): which coding assistant to use, what happens at a usage limit, and how to spread seats across subscriptions.
- [Trackers](trackers.md): how to set up Pinax or a `BACKLOG.md` table.
- [Platforms](platforms.md): how to install on your operating system, and what each one does not do.
- [Command reference](reference/commands.md): every command, option, output and exit code.
- [Profile reference](reference/profile.md): every profile key, its default and what makes it refuse.
- [Events and records](reference/events-and-records.md): the run folder, each event and each report section.
