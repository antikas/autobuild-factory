# Command reference

This page lists every `autobuild` command with its options, what it prints and writes, each exit code and its cause, and the messages it refuses with.

The page opens with what all commands share. A section for each command follows, in the order of the index below. Each command section gives the synopsis, a table of options, what the command prints and writes, and a table of exit codes at its end. To look something up, go to the command's section and find the option or the exit code in its table. Two shared sections then list everything that reaches `origin` and every command that spends allowance, and Limits closes the page.

Shared sections:

- [Common to every command](#common-to-every-command): the command line, terms, the repository, the profile, the scratch root and the environment variables.
- [Everything that reaches `origin`](#everything-that-reaches-origin): every push, by delivery mode.
- [Commands that spend allowance](#commands-that-spend-allowance): the commands that start a seat or read a meter.
- [Limits](#limits): what no command does.

Commands:

- [`autobuild run`](#autobuild-run) starts a campaign.
- [`autobuild preflight`](#autobuild-preflight) checks whether a launch would pass.
- [`autobuild watch`](#autobuild-watch) follows a campaign's progress lines.
- [`autobuild pause`](#autobuild-pause) asks a campaign to stop admitting items.
- [`autobuild host`](#autobuild-host) prints the concurrency the machine allows.
- [`autobuild usage`](#autobuild-usage) reads the Claude subscription meter.
- [`autobuild subscription-status`](#autobuild-subscription-status) shows which lane subscription routing would admit.
- [`autobuild subscription-review`](#autobuild-subscription-review) reviews one commit with one reviewer seat.

The [README](../../README.md) explains the terms used here: campaign, brief, seat, lane, validator, worktree, park and batch validation. The [profile reference](profile.md) describes every profile key, and the [guide to running campaigns](../running-campaigns.md) explains what happens to an item once a campaign has started.

## Common to every command

The program is `autobuild`, and it takes exactly one command: `run`, `preflight`, `watch`, `pause`, `host`, `usage`, `subscription-status` or `subscription-review`. There are no others. `python -m autobuild` runs the same program. The program and every command also take the built-in `-h` or `--help`, which prints the help text and exits 0.

### A bad command line

A missing command, an unknown option, a missing required option or a value outside an option's list of choices is refused before anything runs. The program prints its usage to standard error and exits 2. Exit code 2 means a bad command line for every command, in addition to the causes listed in its own section.

### Terms on this page

- A **harness** is the kind of coding assistant a lane drives. AutoBuild starts it and reads its answer through a harness adapter, and the lane names `claude-code`, `codex`, `github-copilot` and `claude-sdk` are harness names.
- A profile in the **single-lane form** names one coding assistant in `run.harness` and its models in `[models]`. A profile in the **lane form** lists several in `run.lanes`, each with its own `[lanes.<name>]` table. The [profile reference](profile.md) describes both.
- A **probe** is a check, made before any item is claimed, that an adapter or a lane can work. It does no work itself, and a failed probe names what is missing.
- **Admission** is the decision, under subscription routing, of which lane gets the next seat, made from each lane's sign-in and allowance reading. Each decision is recorded in the admission ledger under the scratch root. The [guide to lanes and usage](../lanes-and-usage.md#admission-and-reservation) explains it.

### The repository

`--repository PATH`, by default `.`, names the target repository. Every command takes it. For `run` and `preflight` the path must exist and be a folder.

### The profile

The project profile is a TOML file that holds your settings for one repository. `--profile PATH` names it, and a relative path resolves against the working directory. Without `--profile`, AutoBuild uses `.autobuild.toml` in the repository when that file exists, and otherwise reads no profile.

A named profile that does not exist fails with `run profile does not exist: <path>`, and a folder fails with `run profile is not a file: <path>`. A file that is not valid TOML fails with `run profile is invalid TOML: <error>`. The commands read different amounts of the profile:

| Command | What it reads from the profile |
|---|---|
| `run`, `preflight`, `subscription-status`, `subscription-review` | The whole profile. An unknown key in any table is refused, and without any profile `run` fails with `missing required run configuration: <names>`. |
| `host` | Only the concurrency settings, after the check for unknown keys, so a profile without models or a validator works. |
| `watch`, `pause`, `usage` | Only `[run] scratch_root`, so a profile that would fail a launch still works. Without `--scratch-root`, a file that is not valid TOML, a named profile that is missing and a profile that is a folder still fail. With `--scratch-root`, they read no profile at all. |

### The scratch root

The scratch root is the folder where AutoBuild keeps its working files: run folders, worktrees, leases, the shared lane state and the shared meter reading. A command finds it in this order: `--scratch-root`, then `[run] scratch_root` from the profile, then `autobuild` inside the system temporary folder. `host` has no scratch root.

A relative path, from `--scratch-root` or from `[run] scratch_root`, resolves against the folder of the selected profile, or against the repository when no profile is selected, in every command. An absolute path is used as given, and a leading `~` is expanded to your home folder. So the same `--repository`, `--profile` and `--scratch-root` name the same scratch root in every command, whatever your working directory, except that a relative `--repository` or `--profile` itself resolves against the working directory.

### Environment variables

The commands read these variables from the environment you start them in. The [harness adapter reference](harness-adapters.md#the-seat-environment) lists which variables reach each seat, with and without subscription routing.

| Variable | Read by | Effect |
|---|---|---|
| `TMPDIR`, `TEMP`, `TMP` | Every command with a scratch root | Python takes the system temporary folder from these, and the default scratch root is `autobuild` inside it. |
| `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `SSL_CERT_FILE`, `SSL_CERT_DIR`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `NODE_EXTRA_CA_CERTS`, `NODE_TLS_REJECT_UNAUTHORIZED`, `GIT_SSL_CAINFO`, `GIT_SSL_NO_VERIFY`, `SSLKEYLOGFILE` | `run`, `preflight` | The `interception` check fails when one is set and not listed in `[preflight] accepted_environment`. |
| `PYTEST_ADDOPTS` | `run` | Each item and each batch validation keeps your value, with any `--basetemp` replaced by a folder of its own. |
| `COPILOT_GITHUB_TOKEN`, `GH_TOKEN`, `GITHUB_TOKEN` | `run`, `preflight` with the `github-copilot` lane | Any one of them set makes that lane's sign-in check pass. |
| Names that start with `ANTHROPIC_` | Seats of the `claude-code` lane | Under subscription routing every such variable is removed from the seat. Outside it, only `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` are removed. |
| `COMSPEC` | `run`, `preflight` on Windows | A `.cmd` or `.bat` validator runs through this command interpreter, by default `C:\Windows\System32\cmd.exe`. |
| `CLAUDE_CONFIG_DIR` | None | The Claude meter does not read it. It looks for Claude Code's credentials file only in its default place in your home folder. |

## `autobuild run`

`run` starts a campaign: it claims ready items from the tracker, builds, validates, reviews and delivers each one, and commits a report of the run. Every builder, reviewer and specialist seat that `run` starts spends allowance on its lane's subscription or API account.

```
autobuild run --allow-delivery [--delivery-mode MODE] [--push-current-branch]
              [--allow-current-branch-default] [--campaign-id ID] [--output PATH]
              [profile options]
```

### Delivery and result options

| Option | Type | Default | Effect |
|---|---|---|---|
| `--allow-delivery` | flag | off | Required. Without it `run` stops before it builds or probes anything (see Refusals). |
| `--delivery-mode` | `protected-default` or `current-branch-pr` | `protected-default` | `protected-default` merges every accepted change into the repository's default branch and pushes it to `origin`, whatever branch you launched from, and checks out the default branch in your checkout to do so. It refuses a launch from another branch or a detached head, and one whose default branch cannot be established (see Refusals). `current-branch-pr` delivers to the branch you launched from. |
| `--push-current-branch` | flag | off | Pushes and verifies the branch you launched from, in `current-branch-pr` mode only. In that mode a `BACKLOG.md` claim is pushed only with this option. [Everything that reaches `origin`](#everything-that-reaches-origin) lists each push. |
| `--allow-current-branch-default` | flag | off | Lets `current-branch-pr` mode run when the branch you launched from is the default branch. The campaign's work and its report are then committed straight onto the default branch in your checkout, and with `--push-current-branch` they are pushed straight to `origin`'s default branch. The mode refuses the default branch without this option, so think about who reviews that work before you pass it. |
| `--campaign-id ID` | string | `autobuild-<UTC time as YYYYMMDDTHHMMSSZ>` | Names the campaign. The committed report is `docs/campaigns/<campaign-id>.md`, and the run id is `<campaign id made safe for a file name>-<UTC time as YYYYMMDDTHHMMSSZ>-<8 hex characters>`. |
| `--output PATH` | path | none | Also writes the printed JSON, with a final newline, to this file. Missing parent folders are created. A relative path resolves against the working directory. |

### Deliver to your own branch

To build at most two items onto a branch of your own and leave the default branch alone, switch to that branch and launch `current-branch-pr` with an item limit of 2:

```
git switch feature/export-json
autobuild run --allow-delivery --delivery-mode current-branch-pr --push-current-branch --max-items 2
```

AutoBuild refuses to start when `feature/export-json` is the default branch or the head is detached. Each accepted item is merged into `feature/export-json` in your checkout and pushed to `origin` as `refs/heads/feature/export-json`. That push creates the branch on `origin` when it is not there yet, and AutoBuild sets no upstream for your local branch. The report, a `BACKLOG.md` claim, park records and follow-up items go to the same branch, and nothing is pushed to the default branch. Your checkout stays on your branch. AutoBuild opens no pull request, so open one in your git host when the campaign ends. Leave out `--push-current-branch` to keep every commit local and push the branch yourself.

### Profile options

`run` and `preflight` share these options. Each one replaces or extends a profile value for this launch only and leaves the profile file unchanged. An empty value for a single-valued option counts as not given, except `--harness` in the lane form, which refuses it with `--harness must be a non-empty string`. A value of only spaces for `--harness`, a model option or `--validator-id` is refused. An empty `--allowed-tool` is refused with `policy.allowed_tools must be a non-empty string array`, and an empty `--allowed-root` is refused too. A blank `--allow-item` or `--exclude-item` is dropped.

| Option | Type | Default | Effect |
|---|---|---|---|
| `--repository PATH` | path | `.` | The target repository. |
| `--profile PATH` | path | `.autobuild.toml` when it exists | The profile file, relative to the working directory. |
| `--harness NAME` | string | from the profile | In the single-lane form, replaces `run.harness`. In the lane form (`run.lanes` set), it must name a listed lane and moves that lane to the front for this launch. Under subscription routing in the lane form, admission picks the starting lane, so the order this option sets does not decide it. |
| `--harness-arg ARG` | string, repeatable | none | Replaces `[harness] command` with the collected arguments. Without subscription routing, it is ignored without a message when the profile has two or more lanes. Under subscription routing it is refused with `subscription routing forbids a non-official harness command`, unless the profile has one lane and the arguments are exactly `claude` for the `claude-code` lane or `codex` for the `codex` lane. |
| `--builder-model NAME` | string | from `[models]` | Replaces `models.builder`. Ignored without a message when `run.lanes` is set. |
| `--reviewer-model NAME` | string | from `[models]` | Replaces `models.reviewer`. Ignored without a message when `run.lanes` is set. |
| `--specialist-model NAME` | string | from `[models]` | Replaces `models.specialist`. Ignored without a message when `run.lanes` is set. |
| `--validator-id ID` | string | from `[validator]` | Replaces `validator.id`. |
| `--validator-argv-json JSON` | JSON array | from `[validator]` | Replaces `validator.argv`. It must be a JSON array of non-empty strings with at least one element. |
| `--allowed-tool NAME` | string, repeatable | from `[policy]` | Replaces the whole `[policy] allowed_tools` list for this launch; it does not add to it. The names are `read`, `write`, `shell`, `python` and `git`. Reviewer and specialist seats keep only `read` whatever this list holds. |
| `--allowed-root PATH` | path, repeatable | none | Adds a folder to `[policy] allowed_roots`. A relative path resolves against the profile's folder, or the repository when there is no profile file. |
| `--max-items N` | integer | `run.max_items`, 20 | Replaces `run.max_items`. A value below 1 is refused. |
| `--scratch-root PATH` | path | see The scratch root | Replaces `[run] scratch_root`. |
| `--lane-state-root PATH` | path | the scratch root | Replaces `[run] lane_state_root`, the folder that holds the shared lane cooling file `lanes.json`. It resolves like `--scratch-root`. |
| `--tracker KIND` | `auto`, `pinax` or `backlog` | `[tracker] kind`, `auto` | Replaces `[tracker] kind`. With `auto`, Pinax is used when its probe passes, else the backlog when its probe passes. |
| `--backlog PATH` | path | `BACKLOG.md`, then `docs/BACKLOG.md` | Replaces `[tracker] path`. A relative path resolves against the repository, and the file must lie inside it. |
| `--allow-item ID` | string, repeatable | none | Adds an item id to `[selection] allow`. The allow-list order is the dispatch order, and nothing outside it is claimed. |
| `--exclude-item ID` | string, repeatable | none | Adds an item id to `[selection] exclude`. |
| `--refill-plan PATH` | path | `[refill] plan` | Replaces `[refill] plan`, a JSON file with schema `autobuild.refill-plan.v1`. A relative path resolves against the profile's folder. |

For `--allow-item` and `--exclude-item`, profile entries come first and duplicates are dropped. The result's `selection` block names the source of each list as the profile path, the words `command line`, or both.

### Output and files

On standard output, `run` prints the campaign result as indented JSON with sorted keys and schema `autobuild.campaign-result.v1`. The result holds `schema`, `version`, `campaign_id`, `repository`, `scratch_root`, `adapters` (each with `boundary`, `name` and `version`), `stop_reason`, `selection`, `refill` (`enabled`, `proposal_count`, `fog_count`), `report_ref`, `repository_report_ref`, `progress_ref`, `schedule`, `batch_validation` and `items`.

| Block | Fields |
|---|---|
| `schedule` | `concurrency`, `concurrency_source`, `heavy_concurrency`, `peak_concurrency`, `makespan_seconds`, `item_seconds`. The last two are rounded to three decimal places. |
| `batch_validation` | `verdict` (`none`, `passed` or `failed`) and `runs`. Each run has `batch`, `final`, `revision`, `passed`, `exit_code`, `stdout_ref` and `error`, which is null when empty. |
| each entry of `items` | `item_id`, `disposition` (`accepted`, `parked` or `failed`), `states`, `reason`, `item_commit`, `tracker_commit`, `merged_commit`, `pushed`. |

On standard error, `run` writes every progress line while it works, unless `[progress] stderr` is false. Unless `[progress] file` is false, the same lines go to `progress.log` in the run folder, which `autobuild watch` follows. When it is false, no `progress.log` is written and `watch` never sees the completion line.

The run folder is `<scratch>/runs/<run-id>/`. It holds `manifest.json`, `events.jsonl`, `evidence/`, `progress.log` when the file is enabled, and, at the end, `report.txt`; the [events and records reference](events-and-records.md) describes each one. At the end of every campaign, AutoBuild also commits `docs/campaigns/<campaign-id>.md` with the subject `ADDED: campaign <campaign-id> report` and delivers it through the selected delivery mode. When that delivery fails, the result carries `report delivery failed: <error>` in place of the report's path.

### Exit codes

| Code | Cause |
|---|---|
| 0 | No item ran, or every item was accepted, and the batch verdict is not `failed`. |
| 1 | An item ended `parked` or `failed`, or the batch verdict is `failed`, even when every item was accepted. |
| 2 | The command line was bad, or `run` stopped before completion. It prints `AutoBuild failed before completion: <error>` on standard error and prints no JSON. |

Exit code 2 covers a failure in the profile, an adapter, the setup checks, the usage check, the tracker lease or the campaign itself. A second campaign that finds a live tracker lease held by another campaign on the same scratch root exits 2 before it claims anything. An internal scheduler error is raised only after every running item has ended, and `run` then exits 2 without writing a campaign report.

### Refusals

`run` checks the launch in the order below and stops at the first refusal. Each message follows `AutoBuild failed before completion: ` on standard error, with exit code 2.

1. Profile errors, for example `missing required run configuration: <names>`, `<table> contains unknown fields: <names>` and `--harness '<name>' is not one of run.lanes: <lanes>`. Others are `--validator-argv-json is not valid JSON`, `validator.argv must be a non-empty string array`, `run bounds and timeouts must be positive` and `policy.allowed_tools contains unknown semantic tools: <names>`. The [profile reference](profile.md) lists the rest.
2. No `--allow-delivery`: `this workflow claims items and delivers accepted changes; pass --allow-delivery after human approval`. No adapter is built or probed.
3. `--push-current-branch` or `--allow-current-branch-default` with `protected-default`: `--push-current-branch and --allow-current-branch-default require --delivery-mode current-branch-pr`.
4. The tracker, before any lane is built. With `--tracker auto`, when neither tracker passes its probe: `tracker preflight failed: <diagnostics>`. A backlog path outside the repository: `BACKLOG.md must be inside the campaign repository`.
5. No `origin` remote: `required git remote is missing: origin`.
6. A branch that does not fit the delivery mode. No lane has been built or probed yet, so no lane is cooled.
   - In `protected-default` mode, when the clone records no remote head and `origin` does not answer with its head: `the default branch of origin could not be established: this clone records no remote head and origin did not answer with its head; make origin reachable, or set the remote head in this clone, then launch again`.
   - In `protected-default` mode, any branch but the default branch: `protected-default delivery must be launched from the default branch <default>; the checkout is on <branch>; switch to <default>, or pass --delivery-mode current-branch-pr to deliver to <branch>`. On a detached head: `protected-default delivery must be launched from the default branch <default>; the checkout is on a detached head at <short revision>; switch to <default>, or pass --delivery-mode current-branch-pr`.
   - In `current-branch-pr` mode, a detached head: `current-branch-pr delivery requires a named invoking branch`. On the default branch without `--allow-current-branch-default`: `current-branch-pr delivery refuses the detected default branch; pass --allow-current-branch-default after human approval`.
7. A proposal in the refill plan that the tracker does not accept.
8. A lane effort that the lane's adapter cannot pass: `lane <name> sets <seat> effort <level>, which its harness adapter cannot pass (accepted: <levels>)`. No lane has been probed yet.
9. No capable lane: `no capable harness lane: <diagnostics>`. Under subscription routing, a lane's sign-in check can fail because it could not read the lane's meter. The message is then `no usage meter could be read: <lane> (<meter id>): <error>; try again at <time>`. When other lanes failed for other reasons, ` | no other lane is capable: <diagnostics>` follows. In place of `try again at <time>` it can say `try again once the sign-in is refreshed` or `try again once the meter answers`.
10. A failed probe of the tracker, workspace, command, knowledge, lease, lane state or run record adapter: `<name> preflight failed: <detail>`.
11. A setup check: `<probe> preflight failed: <cause>`. The checks and their order are listed under `autobuild preflight`.
12. With a `[usage]` table, the usage check. Outside subscription routing it refuses only when every configured lane is metered and at its stop level, or when a meter fails under `on_probe_failure = "refuse"`, and that message ends with `; try again at <time>`. Under subscription routing it refuses with `no capable subscription has allowance for a builder`. When some meters could not be read, that message continues `; a usage meter could not be read: <lanes>`, and when no meter could be read it is `no usage meter could be read: <lanes>`.

## `autobuild preflight`

`preflight` checks whether a launch would pass: it checks the branch against the delivery mode, probes every lane and runs the setup checks and the usage check, and it claims nothing.

```
autobuild preflight [--delivery-mode MODE] [--json] [profile options]
```

### Options

`preflight` takes every profile option of `run` (see Profile options), with the same types, defaults and effects, and two more:

| Option | Type | Default | Effect |
|---|---|---|---|
| `--delivery-mode` | `protected-default` or `current-branch-pr` | `protected-default` | Checks the launch as that mode of `run` would. `protected-default` refuses any branch but the default branch, and a default branch that cannot be established. `current-branch-pr` refuses a detached head. |
| `--json` | flag | off | Prints the report as JSON in place of text lines. |

It takes no `--allow-delivery` and no other delivery option. With `--delivery-mode current-branch-pr` it accepts the default branch, because `--allow-current-branch-default` belongs to `run`.

### Lane state and the ledger

`preflight` claims nothing, and its lane probes cool no lane. It judges a usage stop against a lane state of its own, under `<scratch>/preflight/<id>`, where the id is `preflight-<UTC time as YYYYMMDDTHHMMSSZ>`. Under subscription routing it reads the shared lane state and the admission ledger, and it neither changes nor creates either one.

### Order of the steps

`preflight` runs these steps in order and stops at the first one that fails. The exit code tells you which group failed: step 4 and steps 8 to 10 exit 1, and the other steps exit 2.

1. It reads the profile.
2. It chooses the tracker and checks the backlog path, as in step 4 of the refusals for `run`.
3. It finds the `origin` remote.
4. It checks that the checked-out branch fits the delivery mode, as in step 6 of the refusals for `run`, apart from the default branch in `current-branch-pr` mode. When this fails, no lane is probed.
5. It checks the refill plan's proposals.
6. It checks each lane's effort levels, and then probes every lane: the coding assistant must be installed and signed in. A lane that fails its probe is reported as not capable, and the steps go on.
7. It probes the `tracker`, `workspace`, `command`, `knowledge`, `lease`, `lane_state` and `run_record` adapters.
8. It stops when no lane is capable.
9. It runs the setup checks in the table below, from `dns-tls` to `briefs`.
10. With a `[usage]` table, it runs the usage check.

The README's short list of checks names four of these. The signed-in coding assistant is the lane probe of step 6, and the writable scratch root, the offline validator run and the brief are the `scratch`, `validator-offline` and `briefs` checks of step 9.

### Setup checks

The setup checks run in this order, and the first failure stops the run with `<check> preflight failed: <cause>`. `run` runs the same checks before its first claim. An adapter probe of step 7 fails with a message of the same form, `<adapter> preflight failed: <detail>`, so tell them apart by the name: the adapter names are the ones listed in step 7.

| Check | It fails when |
|---|---|
| `dns-tls` | A target in `[preflight] tls_targets`, or the `origin` host when `origin` uses `https`, does not resolve or complete a TLS handshake. |
| `interception` | A proxy, certificate or key log variable is set and not listed in `[preflight] accepted_environment`. The names are `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `SSL_CERT_FILE`, `SSL_CERT_DIR`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `NODE_EXTRA_CA_CERTS`, `NODE_TLS_REJECT_UNAUTHORIZED`, `GIT_SSL_CAINFO`, `GIT_SSL_NO_VERIFY` and `SSLKEYLOGFILE`. |
| `scratch` | The scratch root cannot be created, is not writable, holds a lock from another process, or its tracker is held by a live lease. The message names the holding campaign, process and host. |
| `telemetry` | The lane declares no variables that turn its telemetry off, or the seat environment does not set them. |
| `removed-environment` | Never fails. It warns by name when your environment sets a variable that the lane removes from every seat. |
| `validator-runnable` | The validator's first argument does not resolve, `<argv[0]> --version` does not run in the repository within 20 seconds, or a relative `.py` file named as the second argument does not exist. |
| `validator-offline` | The full validator, run once in your checkout, does not exit 0 within `run.command_timeout_seconds`, or runs longer than `validator.budget_seconds`. The run points `HTTP_PROXY`, `HTTPS_PROXY` and `ALL_PROXY` at a closed local port, empties `NO_PROXY`, and sets `UV_OFFLINE=1`, `PIP_NO_INDEX=1` and `UV_NO_SYNC=1`. A direct connection still works. A test command that finds no tests, for example pytest exiting 5, fails here. |
| `validator-budget` | `run.command_timeout_seconds` is below `validator.budget_seconds`. |
| `transport` | The lane would pass a seat's instructions on the command line, or one argument would exceed 8192 bytes. |
| `briefs` | A brief to be checked is missing, empty, larger than 1048576 bytes or unreadable, or a `repository` brief declares a path outside the repository and the allowed roots. A brief that orders git staging passes with a warning that the item will park at claim. |
| `usage` | Runs last, and only with a `[usage]` table. Its refusals are item 12 under Refusals for `run`. |

The `briefs` check reads the next ready item's brief when one item runs at a time, and every ready item's brief inside the selection when more than one runs.

### Output

Without `--json`, `preflight` prints one line per lane, `lane <name>: capable|not capable (<detail or version>)`, with `; cooled until <time>` appended for a lane that is cooling. When every setup check passed, it then prints `preflight <probe>: <detail>` for each check, followed by `preflight failed: <error>` if the usage check failed. When a setup check failed, it prints no `preflight <probe>:` line at all, only `preflight failed: <error>`, and with `--json` the `preflight` array is empty. When the branch does not fit the delivery mode, it prints only `preflight failed: <error>`, and with `--json` the `lanes` array is empty too.

With `--json` it prints schema `autobuild.preflight.v1` with `version`, `repository`, `scratch_root`, `ok`, `error`, `active_lane`, `lanes` and `preflight`. Each entry of `lanes` has `name`, `capable`, `version`, `detail`, `cooled_until` and `cooled_signature`, and each entry of `preflight` has `name`, `passed` and `detail`. When no lane is capable, `error` is `no capable harness lane`, or the `no usage meter could be read: ...` text from item 9 under Refusals for `run` when a lane's meter could not be read.

### Exit codes

| Code | Cause |
|---|---|
| 0 | Every step passed, so a launch would pass. |
| 1 | Step 4, 8, 9 or 10 failed. The text output ends with `preflight failed: <error>`, and with `--json`, `ok` is false and `error` holds the same text. The error is the branch refusal of `run` for step 4, `no capable harness lane` or the `no usage meter could be read: ...` text for step 8, `<check> preflight failed: <cause>` for step 9, and the usage check's message for step 10. |
| 2 | The command line was bad, or step 1, 2, 3, 5, 6 or 7 failed. It prints `AutoBuild preflight failed before completion: <error>` on standard error and nothing on standard output. The error is a profile error, `tracker preflight failed: <diagnostics>`, `BACKLOG.md must be inside the campaign repository`, a refill plan refusal, the lane effort message, `<adapter> preflight failed: <detail>` or `required git remote is missing: origin`. |

## `autobuild watch`

`watch` prints a campaign's progress lines as they are written and exits when the campaign completes.

```
autobuild watch (--run ID | --latest) [--repository PATH] [--profile PATH]
                [--scratch-root PATH] [--timeout-seconds N]
```

| Option | Type | Default | Effect |
|---|---|---|---|
| `--run ID` | string | none | The run to follow. It must be a folder directly under `<scratch>/runs`. Give exactly one of `--run` and `--latest`. |
| `--latest` | flag | off | Follows the run folder with the newest modification time under `<scratch>/runs`, whatever repository it belongs to. On a scratch root that several repositories share it can follow another project's run, so give `--run` there. |
| `--repository PATH` | path | `.` | Where to look for `.autobuild.toml` when `--profile` is not given. |
| `--profile PATH` | path | `.autobuild.toml` when it exists | The profile to read `[run] scratch_root` from. |
| `--scratch-root PATH` | path | see The scratch root | The scratch root. |
| `--timeout-seconds N` | number | none | Stops with exit code 3 when the completion line has not arrived within this many seconds. |

`watch` reads `progress.log` in the run folder every 0.1 seconds and writes nothing. It prints each complete line to standard output as it lands, and holds a partial last line until its newline arrives. Diagnostics go to standard error. The completion line is a line whose body starts with `campaign completed: shipped `, either at the start of the line or after one ISO timestamp; the same words elsewhere in a line do not count.

### Find the run id

The run id is the name of the run folder, `<scratch>/runs/<run-id>/`, and it starts with the campaign id: `<campaign id made safe for a file name>-<UTC time as YYYYMMDDTHHMMSSZ>-<8 hex characters>`. `autobuild run` prints its result only when the campaign ends, so while it runs, list `<scratch>/runs/` and take the folder whose name starts with your campaign id. Pass `--campaign-id` at launch when you want to know that prefix in advance. The [guide to running campaigns](../running-campaigns.md) shows where a finished run names its run folder.

### A run that never completes

`watch` exits only when it has printed the completion line, or when `--timeout-seconds` passes. When the campaign ends without writing that line, for example because it was killed, `watch` keeps reading the log, and without `--timeout-seconds` it waits forever. It sees no line at all when the profile sets `[progress] file = false`, because then no `progress.log` is written. A run id with no folder under `<scratch>/runs` ends `watch` at once with exit code 2 and `run does not exist: <id>`. Give `--timeout-seconds` whenever a script waits on `watch`.

| Code | Cause |
|---|---|
| 0 | The completion line was printed. |
| 2 | The command line was bad; the profile could not be read (one line on standard error starting `autobuild watch:`); the run does not exist (`run does not exist: <id>`); or `--latest` found no run (`no run exists under <scratch>/runs`). |
| 3 | `--timeout-seconds` passed first: `timed out after <n>s waiting for the campaign to complete` on standard error. |

## `autobuild pause`

`pause` asks a running campaign to admit no new item, let its running items finish, and end with the stop reason `paused`.

```
autobuild pause (--run ID | --latest) [--repository PATH] [--profile PATH]
                [--scratch-root PATH]
```

The options `--run`, `--latest`, `--repository`, `--profile` and `--scratch-root` mean the same as for `autobuild watch`, and `pause` finds the run the same way. [Find the run id](#find-the-run-id) under `watch` says where the id comes from. With `--latest`, `pause` takes the newest run whatever its repository, so on a scratch root that several repositories share it can pause another project's campaign; give `--run` there.

`pause` writes the pause marker `pause-requested.json` into the run folder in one atomic step, with schema `autobuild.pause.v1`, `run_id` and `requested_at`. It then prints `pause requested for run <id>: <marker path>`. A second request replaces the first. The campaign checks for the marker before every admission decision and at every lease heartbeat. The next `autobuild run` on the same repository and scratch root lifts the pause; the [guide to running campaigns](../running-campaigns.md) describes the resume.

| Code | Cause |
|---|---|
| 0 | The marker was written. |
| 2 | The command line was bad, the profile could not be read, the run does not exist, `--latest` found no run, or the marker could not be written. It also exits 2 when the run's `progress.log` already holds the completion line. It then prints `run <id> has already completed; there is nothing to pause.` and writes nothing. |

### Stop a campaign at once

`pause` is the only orderly stop, and it waits for running items to finish. AutoBuild sets no handler of its own for an interrupt such as Ctrl+C or for a kill signal, so either one stops the process where it is, and `run` prints no result. The run folder, the worktrees, the claimed items and the tracker lease stay behind. The next `autobuild run` on the same host takes over the lease once the old process is gone. From another host it takes the lease over once the lease's last heartbeat is older than `run.lease_stale_seconds`, 1800 seconds by default. The [guide to running campaigns](../running-campaigns.md#resume-after-an-interruption) describes how that run resumes the claimed items.

## `autobuild host`

`host` reads the machine's processors and memory once and prints the concurrency that a launch would derive from them.

```
autobuild host [--repository PATH] [--profile PATH] [--json] [--sample-seconds N]
```

| Option | Type | Default | Effect |
|---|---|---|---|
| `--repository PATH` | path | `.` | Where to look for `.autobuild.toml`. |
| `--profile PATH` | path | `.autobuild.toml` when it exists | The profile to read the concurrency settings from. |
| `--json` | flag | off | Prints the reading and the capacity as JSON. |
| `--sample-seconds N` | number | 1 | The window over which processor use is sampled. It must be a positive finite number. |

The text output prints the logical processor count, processor use, available and total memory, available and total commit, each diagnostic as a `note:` line, and the concurrency line. A field the host cannot measure reads `unmeasured`. When `run.concurrency` is `auto`, a last line reminds you that it is the default.

With `--json`, `host` prints schema `autobuild.host.v1` with three blocks. `reading` holds `logical_cpus`, `cpu_busy_percent` to one decimal place, `sample_seconds`, the memory and commit totals and available amounts in GiB to three decimal places, `measured` and `diagnostics`. `profile` holds `concurrency`, `heavy_concurrency`, `reserve_gb`, `per_item_gb` and `max_concurrency`. `capacity` holds `items`, `heavy`, `items_source`, `heavy_source`, `cpu_term` and `memory_term`. An unmeasured field is null.

| Code | Cause |
|---|---|
| 0 | The reading was printed. |
| 2 | The command line was bad, the profile could not be read, or `--sample-seconds` is not a positive finite number. |

## `autobuild usage`

`usage` reads the Claude subscription meter once and prints the reading. The only files it writes are the shared reading file and the meter command's output files, both under the scratch root and described below.

```
autobuild usage [--json] [--repository PATH] [--profile PATH]
                [--scratch-root PATH]
```

| Option | Type | Default | Effect |
|---|---|---|---|
| `--json` | flag | off | Prints the reading as JSON. |
| `--repository PATH` | path | `.` | Where to look for `.autobuild.toml` when `--profile` is not given. |
| `--profile PATH` | path | `.autobuild.toml` when it exists | The profile to read `[run] scratch_root` from. |
| `--scratch-root PATH` | path | see The scratch root | The scratch root that holds the shared reading and the meter command's output files. |

`usage` reads only `[run] scratch_root` from the profile, so the levels in `[usage]` play no part, and it reads only the Claude meter. The Claude meter reads an endpoint that is undocumented and may change or stop working without notice. It needs `curl` on `PATH` and Claude Code's own sign-in in its default place, and a sign-in kept in a keychain or a custom `CLAUDE_CONFIG_DIR` is not found. The sign-in holds an access token, which AutoBuild never prints or stores.

The reading goes through the shared reading file `<scratch>/usage-meter/claude-subscription.json`, which every reader on the same scratch root uses. A reading younger than 60 seconds is served from that file without asking the meter, and a failed reading is shared for the same 60 seconds. A rate limited answer is shared until the retry time the meter gives, or for 300 seconds when it gives none. Before any request, AutoBuild compares the sign-in's stored expiry with the clock. An expired sign-in fails at once with `sign-in expired: run Claude once in this venue to refresh it (expired at <time>)`, and that failure is never written to the shared file. The meter command writes its output files under `<scratch>/usage/`.

When the meter answered, the text output is `usage meter claude-subscription at <fetched time>:`, then one line per limit, `<kind> <percent>%, resets <time>`. A scoped limit has ` [<scope>]` after the kind, and a limit with no reset reads `an unknown time`. A line `extra usage enabled` or `extra usage off` follows when the meter reports it. When the meter did not answer, the text output is `usage meter claude-subscription unavailable: <error>`. Both forms end with `reading <n> s old, from the shared subscription cache` or `reading <n> s old, read from the meter`, and say `of unknown age` in place of `<n> s old` when the reading has no fetch time.

With `--json`, `usage` prints schema `autobuild.usage.v1` with `meter_id`, `ok`, `error`, `fetched_at`, `extra_usage_enabled` and `limits`. Each limit has `kind` (`session`, `weekly_all` or `weekly_scoped`), `scope`, `percent` and `resets_at`. The JSON carries no age and no source.

| Code | Cause |
|---|---|
| 0 | The meter answered. |
| 1 | The meter did not answer, or the read raised an error, which prints `usage meter unavailable: <error type>` on standard error. |
| 2 | The command line was bad, or the profile could not be read. |

## `autobuild subscription-status`

`subscription-status` shows, for each configured lane, whether subscription routing would admit a seat now and which lane it recommends, and it claims nothing.

```
autobuild subscription-status [--repository PATH] [--profile PATH]
                              [--scratch-root PATH] [--json]
```

| Option | Type | Default | Effect |
|---|---|---|---|
| `--repository PATH` | path | `.` | The target repository. |
| `--profile PATH` | path | `.autobuild.toml` when it exists | The profile. It must set `[subscription_routing] enabled = true` and hold a `[usage]` table. |
| `--scratch-root PATH` | path | see The scratch root | The scratch root that holds the admission ledger and the shared meter readings. |
| `--json` | flag | off | Prints the full assessment as JSON. |

`subscription-status` checks each lane's sign-in, reads each lane's meter through the shared reading at the scratch root, and reads the admission ledger. It cools no lane and neither creates nor changes the ledger. The [guide to lanes and usage](../lanes-and-usage.md) explains subscription routing and admission.

The text output prints `lane <name>: available|unavailable (<reason>)` for each lane that admission judged. For each lane that failed its sign-in check it prints `lane <name>: unavailable (<meter error or probe detail>)`. The last line is `recommendation: <lane>` or `recommendation: none`. When a lane's meter read failed without saying anything about the account, admission may judge the lane on its last good reading. The reading must be younger than 90 seconds with every limit below `warn_percent`, and the reason then ends `; <error>; judged on the reading of <time>`.

With `--json`, `subscription-status` prints schema `autobuild.subscription-status.v1` with `manifest` (the lane probes), `lanes` and `recommendation` for the builder seat, `available` (the recommendation for each seat role) and `roles` (the full assessment for each role). Each lane row has `lane`, `eligible`, `reason`, `score` and `windows`, and at a stop also `usage_reset_at`. Each window has `key`, `kind`, `used_percent`, `resets_at`, `stop_percent`, `reserved_percent` and `estimated_seat_percent`. Any text in the report that contains `/` or `\` is replaced as a whole by `Local diagnostic withheld; see the repository-relative evidence reference.`

| Code | Cause |
|---|---|
| 0 | A lane is recommended for the builder seat. |
| 1 | No lane is recommended. |
| 2 | Any error, including a profile without subscription routing or without `[usage]`. It prints only `AutoBuild subscription status failed` on standard error. |

## `autobuild subscription-review`

`subscription-review` reviews one commit with one read-only reviewer seat, on a subscription lane that admission chooses, and prints the verdict. The seat runs on the lane's real coding assistant, so each review spends that lane's subscription allowance and is recorded in the shared admission ledger.

```
autobuild subscription-review --commit SHA --brief PATH [--timeout-seconds N]
                              [--json] [--repository PATH] [--profile PATH]
                              [--scratch-root PATH]
```

| Option | Type | Default | Effect |
|---|---|---|---|
| `--commit SHA` | string | required | The commit to review, as a full 40-character lowercase SHA that resolves to itself in the repository. |
| `--brief PATH` | path | required | The brief to review against. It must be a file. A relative path resolves against the working directory. |
| `--timeout-seconds N` | number | 900 | The reviewer seat's time limit. It must be positive. |
| `--json` | flag | off | Prints the result as JSON. |
| `--repository PATH` | path | `.` | The repository that holds the commit. |
| `--profile PATH` | path | `.autobuild.toml` when it exists | The profile. It must enable subscription routing and hold a `[usage]` table. |
| `--scratch-root PATH` | path | see The scratch root | The scratch root that holds the review worktree, the receipts and the admission ledger. |

`subscription-review` checks the brief, the timeout and the profile first. It then checks out the commit as a detached worktree under `<scratch>/subscription-reviews/worktrees/` and takes the commit's first-parent patch. It reserves one reviewer seat in the admission ledger, and admission picks the lane. The seat runs that lane's reviewer model at the lane's `reviewer_effort`, with no tools, the brief, and the patch in its instructions. It calls the lane's adapter directly, without the policy layer that wraps a campaign's seats. A lane found at its stop level is cooled until its reset. The worktree is removed afterwards.

The full receipt is kept at `<scratch>/subscription-reviews/receipts/<receipt id>.json`. It holds `when`, `lane`, `model`, `verdict`, `commit`, `effort`, `findings` and `receipt_id`. A finding keeps only the evidence references that resolve to files inside the reviewed tree, and otherwise reads `Local diagnostic withheld; no verified repository-relative evidence reference.` Any finding text that contains `/` or `\` is replaced by a fixed sentence.

The command prints only `commit`, `verdict`, `lane`, `model` and `receipt_id` with `--json`, and `<verdict> via <lane> (<model>)` without it. The verdict is one of `pass`, `correct`, `escalate` and `park`.

| Code | Cause |
|---|---|
| 0 | The verdict is `pass`. |
| 1 | The verdict is anything else. |
| 2 | The command line was bad, or the review failed (see below). |

A review failure prints `AutoBuild subscription review failed: <message>` on standard error, with one of these messages:

- `review brief must be a file`
- `review timeout must be positive`
- `review commit must be a full 40-character SHA`
- `review commit is not an available full commit SHA`
- `could not create isolated review checkout`
- `review patch exceeds the 1 MiB evidence limit`
- `no subscription reviewer lane is available`
- `independent subscription review did not produce a typed verdict`

Any other error, including a profile without subscription routing, prints the fixed message `AutoBuild subscription review failed while accessing the configuration, checkout or evidence.`, which names no local path.

## Everything that reaches `origin`

Only `autobuild run` pushes, and the delivery mode and `--push-current-branch` decide where. No profile key sets the delivery mode or turns pushing off, so you choose both on the command line at each launch. In the table, the launch branch is the branch checked out when you launch; `protected-default` launches only from the default branch. With `--allow-current-branch-default` the launch branch is the default branch, and the last column then pushes to `origin`'s default branch.

| Commit | `protected-default` (the default) | `current-branch-pr` | `current-branch-pr --push-current-branch` |
|---|---|---|---|
| An accepted change and its tracker close, `MODIFIED: close <id> in tracker` | Pushed from the worktree to the default branch on `origin`. | Committed onto the launch branch in your checkout. Not pushed. | Pushed to the launch branch on `origin`. |
| The park record of an item parked in its worktree, before its product commit | Pushed to the default branch on `origin`. The item's product change is not pushed. | Committed onto the launch branch. Not pushed. | Pushed to the launch branch on `origin`. |
| In a `BACKLOG.md` tracker: the claim `MODIFIED: claim <id> in BACKLOG`, a follow-up item `ADDED: propose <title> in BACKLOG`, and a park record written from your checkout | Pushed to the launch branch on `origin`, which in this mode is the default branch. | Committed onto the launch branch. Not pushed. | Pushed to the launch branch on `origin`. |
| In Pinax: the claim, a follow-up item and a park record written from your checkout | Pinax commits each one and pushes it itself, because Pinax pushes when `origin`'s default branch is checked out. A Pinax claim from another branch is refused in this mode. | Pinax commits each one. It pushes only when the launch branch is `origin`'s default branch, which needs `--allow-current-branch-default`; otherwise the commit stays local. | As in the column before. A commit that stayed local goes to `origin` with the next push of the launch branch. |
| The report, `ADDED: campaign <campaign-id> report` | Committed in a separate checkout under the scratch root and pushed to the default branch on `origin`. | Committed onto the launch branch. Not pushed. | Pushed to the launch branch on `origin`. |
| The item branches, `autobuild/<campaign>/<item>-<8 characters>` | Never pushed. | Never pushed. | Never pushed. |

A park record is written from your checkout when the item had no worktree yet, when it parks after its product commit, and when the park in the worktree could not be delivered. Each push of an accepted change, a park record from the worktree, a `BACKLOG.md` commit or the report names its exact commit, and AutoBuild checks afterwards that `origin` holds it. AutoBuild opens no pull request in either mode.

## Commands that spend allowance

A seat is one assistant session that does one job, and every seat spends allowance on the subscription or API account its lane is signed in with. A meter read asks the provider how much of an allowance is used. Read the [list of ways a profile can spend beyond your subscriptions](../lanes-and-usage.md#spending-beyond-your-subscriptions) before your first launch.

| Command | Seats it starts | Meters it reads |
|---|---|---|
| `run` | Every builder, reviewer and specialist seat of every item it admits. | With a `[usage]` table, the meters of the metered lanes, through the shared reading. |
| `subscription-review` | One reviewer seat. | The meters that admission reads to choose its lane. |
| `preflight` | None. | With a `[usage]` table, every lane's meter, for the usage check. |
| `subscription-status` | None. | Every lane's meter. |
| `usage` | None. | The Claude meter. |
| `watch`, `pause`, `host` | None. | None. |

Some options widen what `run` spends. `--max-items` raises the number of items, and every item starts at least a builder and a reviewer. `--harness`, `--harness-arg` and `run.lanes` choose which coding assistant runs, and so which account it bills; the `claude-sdk` lane bills an API key, and `--harness-arg` can name any program. The model options choose which model each seat uses. Without a `[usage]` table nothing limits what a campaign spends.

## Limits

No command undoes a delivered change, lists runs, or resumes a paused campaign on its own; the next `autobuild run` lifts the pause. `--latest` in `watch` and `pause` picks the newest run under `<scratch>/runs` whatever repository it belongs to, so on a shared scratch root it can pick another project's run. Leases are kept under the scratch root, so two campaigns see each other's leases only when they share one. The shared lane state is kept there too unless `--lane-state-root` or `[run] lane_state_root` moves it. `usage` reads only the Claude meter. The Codex meter exists only under subscription routing, and its state shows in `subscription-status`. A passing `preflight` does not check the `current-branch-pr` conditions. No setup check tests whether a brief lies inside the repository, the scratch root or an allowed root. A brief outside them is refused only when the first seat starts, after the claim, and the item parks with `seat brief is outside the allowed roots: <brief>`. No profile key sets the delivery mode or turns pushing off.
