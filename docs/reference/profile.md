# Profile reference

This page lists every key of the AutoBuild project profile, with its type, its default, the values it accepts, what it does, and the message AutoBuild gives when it refuses a value.

For what AutoBuild is and how a campaign runs, start with the [README](../../README.md). The command line options that replace a profile key for one launch are in the [command reference](commands.md).

## Using this page

The page opens with the two forms a profile can take, the money a lane can spend, and the smallest profile that launches. It then explains how AutoBuild finds and checks the profile. After that comes one section for each table, in the order of the index below, and each section holds a table of its keys followed by the messages it refuses with. To find a key, open the section of the table it belongs to: `max_items`, for example, is under [`[run]`](#run). The page ends with the values no key sets, the limits of the profile, and a complete example.

The tables, in the order this page describes them:

- [`[run]`](#run) and [`[run.item_classes]`](#runitem_classes): bounds, timeouts, folders and lanes.
- [`[models]`](#models): models and effort levels in the single-lane form.
- [`[lanes.<name>]`](#lanesname): models and settings of each lane in the lane form.
- [`[validator]`](#validator): the command that decides whether a change is accepted.
- [`[policy]`](#policy): the builder's tools and the extra folders seats may use.
- [`[selection]`](#selection): which items a campaign may take.
- [`[usage]`](#usage): the allowance levels at which a lane stops.
- [`[subscription_routing]`](#subscription_routing): seats spread across subscriptions.
- [`[process]`](#process): the review procedure.
- [`[harness]`](#harness): the program a single lane runs.
- [`[tracker]`](#tracker): the list of work items.
- [`[preflight]`](#preflight): the setup checks.
- [`[capacity]`](#capacity): how many items run at once.
- [`[pressure]`](#pressure): how a busy host slows the campaign.
- [`[progress]`](#progress): where progress lines go.
- [`[refill]`](#refill): new work to register when the queue runs dry.
- [`[knowledge]`](#knowledge): the command that records open questions.

## The two forms of a profile

A lane is one coding assistant that you have set up for AutoBuild. A harness is the kind of coding assistant a lane drives, such as Claude Code or Codex, and AutoBuild drives each one through a harness adapter registered under the harness name: `claude-code`, `codex`, `github-copilot` or `claude-sdk`.

A profile takes one of two forms. The single-lane form names one harness with `run.harness` and sets its models in `[models]`. The lane form lists one or more lanes in `run.lanes`, in order of preference, and sets the models of each in its own `[lanes.<name>]` table. Setting `run.lanes` switches the profile to the lane form.

No profile key sets the delivery mode or controls pushing. Both are options of `autobuild run`: `--allow-delivery`, `--delivery-mode`, `--push-current-branch` and `--allow-current-branch-default`, described in the [command reference](commands.md). In the default mode, `protected-default`, delivery merges accepted changes into the repository's default branch and pushes them to `origin`, whatever branch you launched from.

## Money spent on API keys

Because the `claude-sdk` lane bills an API key and has no allowance meter, `[usage]` never stops it. When a subscription lane listed before it reaches its stop level, seats move to the `claude-sdk` lane and spend API money with no cap from AutoBuild. To keep a campaign on subscriptions alone, leave `claude-sdk` out of `run.lanes`. Subscription routing refuses that lane.

The `claude-sdk` lane gets its key from a file. In the lane form, `key_file` in the `[lanes.claude-sdk]` table names that file, and a relative path resolves against the profile's folder. AutoBuild passes the path to the lane, and before the first claim the lane's probe, the check that a lane is ready to run seats, confirms that the file exists, can be read and is not empty. The probe reads no other credential and makes no API call, so a wrong key passes it. The single-lane form has no `key_file`, because AutoBuild ignores lane tables there, so a profile with `run.harness = "claude-sdk"` always fails that probe with `the claude-sdk lane table sets no key_file`. Set up this lane in the lane form.

The `claude-code` lane can also bill an API account. Outside subscription routing, AutoBuild removes only `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` from the environment of each Claude Code seat and probe, so a key in either of those two variables never reaches the seat. Its probe accepts any sign-in that Claude Code reports, so a Claude Code signed in with an API account passes. Under subscription routing AutoBuild also removes every variable whose name starts with `ANTHROPIC_`. The [checklist in the lanes and usage guide](../lanes-and-usage.md#spending-beyond-your-subscriptions) lists every way a profile can spend beyond your subscriptions and what to do about each.

## The smallest profile that launches

A profile in the single-lane form needs five keys:

```toml
[run]
harness = "claude-code"

[models]
builder = "claude-opus-5-5"
reviewer = "claude-opus-5-5"

[validator]
id = "tests"
argv = ["python", "-m", "pytest", "-q"]
```

- `run.harness` holds a harness name, as a non-empty string.
- `models.builder` and `models.reviewer` each hold a model name, as a non-empty string. Outside subscription routing AutoBuild passes the name to the coding assistant without checking it (see [`[models]`](#models)).
- `validator.id` holds a name for the validator, as a non-empty string.
- `validator.argv` holds the validator command, as a non-empty array of non-empty strings.

When `run.harness`, `models.builder`, `models.reviewer` or `validator.id` is missing, the launch is refused with `missing required run configuration: <names>`. A missing or empty `validator.argv` is refused with `validator.argv must be a non-empty string array`.

The same profile in the lane form needs `run.lanes`, a `[lanes.<name>]` table with `builder` and `reviewer` for each listed lane, and the two validator keys:

```toml
[run]
lanes = ["claude-code"]

[lanes.claude-code]
builder = "claude-opus-5-5"
reviewer = "claude-opus-5-5"

[validator]
id = "tests"
argv = ["python", "-m", "pytest", "-q"]
```

- `run.lanes` holds the lane names, as a non-empty array of harness names with no name twice.
- `lanes.<name>.builder` and `lanes.<name>.reviewer` each hold a model name, as a non-empty string, for every listed lane.
- `validator.id` and `validator.argv` hold the same values as in the single-lane form.

A listed lane with no table is refused with `[lanes.<name>] must be a TOML table`, and a listed lane without `builder` or `reviewer` with `missing lane configuration: <keys>`. A missing `validator.id` gives `missing required run configuration: validator.id` in this form too.

Neither profile sets an effort level, so each seat runs at its coding assistant's default effort, and a builder whose change fails the validator gets no retry. The validator must already exit 0 on the committed head: the setup check runs it once before the first claim, and a test command that finds no tests (pytest exits 5) fails that check.

## Finding the profile

The project profile is a TOML file that holds your settings for one repository. AutoBuild reads the file you name with `--profile PATH`, where a relative path resolves against your working directory. Without `--profile`, it reads `.autobuild.toml` in the repository when that file exists. With no profile file, the five required values of the single-lane form must come from the command line. The options are `--harness`, `--builder-model`, `--reviewer-model`, `--validator-id` and `--validator-argv-json`, and a launch that lacks one of them is refused with the messages given under [the smallest profile](#the-smallest-profile-that-launches).

AutoBuild checks the whole profile when it loads, before it builds any adapter. Every refusal on this page ends `autobuild run` with exit code 2 and the line `AutoBuild failed before completion: <error>` on standard error.

| Problem | Message |
|---|---|
| The named file does not exist | `run profile does not exist: <path>` |
| The named path is a folder | `run profile is not a file: <path>` |
| The file is not valid TOML | `run profile is invalid TOML: <error>` |
| A key that AutoBuild does not know, at the top level or in any table | `<table> contains unknown fields: <names>` |
| A table key holds a value that is not a table | `[<table>] must be a TOML table` |
| A string that must not be blank is blank | `<key> must be a non-empty string` |

AutoBuild refuses an unknown key when the profile loads, so a misspelt setting stops the launch at once. In the message, `<table>` is the word `profile` for a key at the top level, the table name for a key inside a table, and `lanes.<name>` for a key inside a lane table. A misspelt top-level table such as `[runn]` gives `profile contains unknown fields: runn`, and a misspelt `max_item` in `[run]` gives `run contains unknown fields: max_item`.

AutoBuild collects the unknown keys of the top level and of every table, and refuses them together in one message, the parts joined by `; `. Two cases stop outside that message. Unknown keys inside a `[process.boundaries.<name>]` table are refused later, in a message of their own (see [`[process]`](#process)). A table key whose value is not a table stops the check at once with `[<table>] must be a TOML table`.

The top-level keys are `run`, `models`, `lanes`, `validator`, `policy`, `selection`, `usage`, `subscription_routing`, `process`, `harness`, `tracker`, `preflight`, `capacity`, `pressure`, `progress`, `refill` and `knowledge`, in the order of the sections below.

Some names inside tables are yours to choose: class names in `[run.item_classes]`, path patterns in `[process.test_map]` and boundary names in `[process.boundaries]`. Lane names are fixed. A lane listed in `run.lanes` is named by its harness, and AutoBuild finds the harness adapter by that name. An inactive lane table, a `[lanes.<name>]` table that `run.lanes` does not list, can carry any name, so a misspelt lane table name stays inactive without a message (see [`[lanes.<name>]`](#lanesname)).

A relative path in `scratch_root`, `lane_state_root`, `allowed_roots`, `refill.plan`, `knowledge.fog_ledger` and `lanes.<name>.key_file` resolves against the profile's folder, or against the repository when there is no profile file. A relative `tracker.path` always resolves against the repository. A leading `~` is expanded to your home folder.

Four commands read less than a launch does. `autobuild watch`, `autobuild pause` and `autobuild usage` read only `[run] scratch_root`, so they work with a profile that would fail a launch, as long as the file exists and is valid TOML. They still refuse a `run` key whose value is not a table, with `[run] must be a TOML table`, and a blank `scratch_root`, with `run.scratch_root must be a non-empty string`. `autobuild host` reads only `[run] concurrency`, `[run] heavy_concurrency` and `[capacity]`, after it has refused unknown keys as a launch does.

## `[run]`

The `[run]` table holds the campaign's bounds, timeouts, folders and the list of lanes.

| Key | Type | Default | Allowed values | Effect |
|---|---|---|---|---|
| `harness` | string | none | a registered harness name: `claude-code`, `codex`, `github-copilot`, `claude-sdk`, or a name added through the `autobuild.adapters` entry point | Names the harness of the single lane. Required in the single-lane form, and ignored in the lane form. |
| `lanes` | array of strings | none | non-empty, no name twice | Switches the profile to the lane form: each name is a lane, in order of preference, with its own `[lanes.<name>]` table. Listing `claude-sdk` can spend API money with no cap: see [Money spent on API keys](#money-spent-on-api-keys). |
| `max_items` | integer | `20` | 1 or more | The most items one campaign takes. AutoBuild counts every item that has finished, whatever its outcome (accepted, parked or failed), plus every item still running, and starts no new item once the count reaches this value. Parked items include those parked at triage, the check of a brief before any claim that parks work a worktree cannot build, and those from an interrupted campaign that were parked on resume. A claim that another campaign took first does not count. |
| `seat_timeout_seconds` | number | `900` | above 0 | The wall-clock limit of every builder, reviewer and specialist seat of an item with no [item class](#runitem_classes). |
| `seat_stall_seconds` | number | `900` | above 0 | The progress deadline given to every seat. A seat that shows no progress for this long is stopped and its item parks with `stall:<seat>:<signals>`. |
| `lease_stale_seconds` | number | `1800` | above 0 | The heartbeat age after which a lease counts as stale and another campaign may reclaim it. The tracker lease is renewed every third of this time. |
| `command_timeout_seconds` | number | `600` | above 0 | The limit on every validator run, and the ceiling for any command AutoBuild starts. See [the validator's time limit](#the-validators-time-limit). |
| `hold_limit_seconds` | number | `21600` | above 0 | How long one hold on new items may last before the campaign ends with `hold_limit`. It also bounds one hold on usage meter failures, after which the item parks with a reason that ends `:usage_meter_unavailable`. |
| `scratch_root` | string (path) | `autobuild` in the system temporary folder | any path | The scratch root, where AutoBuild keeps worktrees, run folders, leases and seat files. |
| `lane_state_root` | string (path) | the scratch root | any path | The folder of `lanes.json`, the shared record of which lanes are set aside. Campaigns that share this folder see each other's lane coolings. |
| `lane_cool_seconds` | number | `3600` | above 0 | How long a lane is set aside after a rate limit, quota or sign-in failure that gives no reset time. |
| `concurrency` | `"auto"` or integer | `"auto"` | `"auto"`, or 1 to 64 | How many items run at once. `"auto"` works the number out from the host (see [`[capacity]`](#capacity)). |
| `heavy_concurrency` | `"auto"` or integer | `"auto"` | `"auto"`, or 1 or more | How many validator runs may run at once when more than one item runs. It never exceeds the item value. |
| `batch_validate_every` | integer | `5` | 1 or more | The number of deliveries between two batch validations, which run the validator on the head of the delivery target. |
| `item_classes` | table | empty | see [`[run.item_classes]`](#runitem_classes) | Longer seat limits for named kinds of item. |

An explicit `concurrency` is not bounded by `[capacity] max_concurrency`. With a `BACKLOG.md` tracker, a campaign runs one item at a time unless `concurrency` is an integer. On macOS the stall deadline never stops a seat of the `claude-code`, `codex` or `github-copilot` lane, so only the seat's time limit stops one of those; the [platforms guide](../platforms.md) gives the reason.

AutoBuild converts `max_items` and the five timeouts `seat_timeout_seconds`, `seat_stall_seconds`, `lease_stale_seconds`, `command_timeout_seconds` and `hold_limit_seconds` to numbers when it reads them, so a quoted number such as `"5"` is accepted as 5. A string that is not a number fails with Python's own `ValueError` instead of a message from the list below.

Refusals:

- A `max_items` below 1, or a `seat_timeout_seconds`, `seat_stall_seconds`, `lease_stale_seconds`, `command_timeout_seconds` or `hold_limit_seconds` at or below 0: `run bounds and timeouts must be positive`.
- A `lanes` value that is empty, not an array, or holds an empty or non-string entry: `run.lanes must be a non-empty string array`.
- A lane named twice in `lanes`: `run.lanes names a lane more than once: <names>`.
- A `concurrency` or `heavy_concurrency` that is neither `"auto"` nor a positive integer: `run.concurrency must be "auto" or a positive integer`, or the same message for `run.heavy_concurrency`.
- A `concurrency` above 64: `run.concurrency must be at most 64; <n> was named`.
- A `batch_validate_every` that is a boolean or not positive: `run.batch_validate_every must be a positive integer`.
- A `lane_cool_seconds` that is not positive: `run.lane_cool_seconds must be a positive number of seconds`.

## `[run.item_classes]`

An item class is a named kind of item with its own seat limit. Each key of `[run.item_classes]` is a class name that you choose, and its value is the limit in seconds for every seat of an item in that class.

| Key | Type | Default | Allowed values | Effect |
|---|---|---|---|---|
| `<class name>` | number | none | above 0 | The wall-clock limit of every builder, reviewer and specialist seat of an item whose brief has the line `Item class: <class name>`. |

An item whose brief has no `Item class:` line, or names a class that the table does not hold, uses `run.seat_timeout_seconds`. AutoBuild raises its ceiling on seat time limits to the largest class value, so the longest class can run in full. The ceiling on the commands AutoBuild starts stays `run.command_timeout_seconds`.

Refusal: a value that is not a positive number gives `run.item_classes.<name> must be a positive number of seconds`.

## `[models]`

The `[models]` table names the model and effort level of each seat in the single-lane form. An effort level is how hard you ask a model to reason. When `run.lanes` is set, AutoBuild ignores the model names in this table and refuses its effort keys.

| Key | Type | Default | Allowed values | Effect |
|---|---|---|---|---|
| `builder` | string | none | a model name, passed unchecked (see below) | The builder seat's model. Required. |
| `reviewer` | string | none | a model name, passed unchecked (see below) | The reviewer seat's model. Required. |
| `specialist` | string | the `reviewer` model | a model name, passed unchecked (see below) | The specialist seat's model. |
| `builder_effort` | string | none | `low`, `medium`, `high`, `xhigh`, `max` | The builder's effort level. Without it the builder gets no effort argument and runs at the assistant's default. |
| `reviewer_effort` | string | none | `low`, `medium`, `high`, `xhigh`, `max` | The reviewer's effort level. |
| `specialist_effort` | string | the `reviewer_effort` value | `low`, `medium`, `high`, `xhigh`, `max` | The specialist's effort level. |

Outside subscription routing, AutoBuild passes a model name to the coding assistant without checking it, on every lane. Under subscription routing, the `claude-code` lane accepts only `claude-opus-5-5`, and the `codex` lane only `gpt-5.6-terra` and `gpt-5.6-sol`. The same rules apply to the model names in a `[lanes.<name>]` table.

When the validator fails a builder's change and `builder_effort` is set below `high`, a fresh builder retries one level higher, up to `high`. A builder that starts at `low` gets two retries, one that starts at `medium` gets one, and one at `high` or above, or with no effort set, gets none.

Refusals:

- A missing `builder` or `reviewer`: `missing required run configuration: <names>`.
- An effort outside the five levels: `<key> must be one of: low, medium, high, xhigh, max`.
- An effort key here while `run.lanes` is set: `models.<key> is not used when run.lanes is set; set <key> in the [lanes.<harness>] tables`.

## `[lanes.<name>]`

In the lane form, `run.lanes` lists the lanes in order of preference, and each listed lane needs a `[lanes.<name>]` table, where `<name>` is the harness name, such as `claude-code` or `codex`. Before the first claim AutoBuild probes every listed lane, checking that its coding assistant is ready to run seats. The [lanes and usage guide](../lanes-and-usage.md) explains how AutoBuild moves seats between lanes.

| Key | Type | Default | Allowed values | Effect |
|---|---|---|---|---|
| `builder` | string | none | a model name, passed unchecked (see [`[models]`](#models)) | The builder seat's model on this lane. Required for a listed lane. |
| `reviewer` | string | none | a model name, passed unchecked (see [`[models]`](#models)) | The reviewer seat's model on this lane. Required for a listed lane. |
| `specialist` | string | the `reviewer` model | a model name, passed unchecked (see [`[models]`](#models)) | The specialist seat's model on this lane. |
| `builder_effort` | string | none | `low`, `medium`, `high`, `xhigh`, `max` | The builder's effort level on this lane. |
| `reviewer_effort` | string | none | `low`, `medium`, `high`, `xhigh`, `max` | The reviewer's effort level on this lane. |
| `specialist_effort` | string | the `reviewer_effort` value | `low`, `medium`, `high`, `xhigh`, `max` | The specialist's effort level on this lane. |
| `key_file` | string (path) | none | a path to a file | The file that holds the API key of the `claude-sdk` lane. AutoBuild stores only the resolved path; it never reads the key into the profile. Read only in the lane form. This lane spends API money with no cap: see [Money spent on API keys](#money-spent-on-api-keys). |
| `prepare_argv` | array of strings | none | non-empty; `codex` lane only | A command that runs in the worktree before every builder seat on the `codex` lane that may write. When it fails, times out or stalls, the builder does not start. |

An inactive lane table is a `[lanes.<name>]` table whose name `run.lanes` does not list. AutoBuild never probes it and refuses it as a `--harness` choice. It may leave out its models. When it holds `builder`, `reviewer`, `specialist`, `key_file` or an effort key, AutoBuild checks that value, and it never checks an inactive table's `prepare_argv`.

In the single-lane form AutoBuild ignores the model names, `key_file` and `prepare_argv` in every lane table and refuses their effort keys.

Refusals:

- A listed lane with no table: `[lanes.<name>] must be a TOML table`.
- A listed lane without `builder` or `reviewer`: `missing lane configuration: <keys>`.
- A `prepare_argv` that is empty or holds an empty or non-string entry: `lanes.<name>.prepare_argv must be a non-empty string array`.
- `prepare_argv` on a listed lane other than `codex`: `lanes.<name>.prepare_argv is only supported by the codex lane`.
- An effort key in a lane table while `run.lanes` is not set: `lanes.<name>.<key> is not used: lane tables are read only when run.lanes is set; set <key> in [models]`.
- An effort that the lane's harness adapter cannot pass, refused before any lane is probed: `lane <name> sets <seat> effort <level>, which its harness adapter cannot pass (accepted: <levels>)`. The four built-in adapters accept all five levels.
- A `--harness` value that is not in `run.lanes`: `--harness '<name>' is not one of run.lanes: <lanes>`.

## `[validator]`

The validator is the command you choose to decide whether a change is accepted. Only exit code 0 counts as a pass.

| Key | Type | Default | Allowed values | Effect |
|---|---|---|---|---|
| `id` | string | none | any non-empty name | The validator's name, used in command ids, progress lines and the park reason `validator <id> did not pass`. Required in both forms. |
| `argv` | array of strings | none | non-empty, no empty string | The validator command and its arguments. Required in both forms. |
| `budget_seconds` | number | none | above 0 | The time one validator run should stay within, checked only by the setup check at launch. That check fails when its offline run takes longer, or when `run.command_timeout_seconds` is below this value. |
| `patient_argv` | array of strings | none | non-empty | The patient form of the validator: the command to run while the host is under pressure, which is to say busy (see [`[pressure]`](#pressure)). |

AutoBuild runs `argv` as given, with no shell in between, except on Windows, where a `.cmd` or `.bat` validator starts through the command interpreter. AutoBuild checks every command it starts against a command policy. That policy allows the executable names of `argv` and `patient_argv`, and it accepts a validator run only when the run carries exactly one of the two lists, so no model can change them.

Before the first claim, the setup check runs `<argv[0]> --version` and then the full validator once in your checkout, with the proxy variables pointed at a closed local port and uv and pip set to work offline. A direct connection that ignores the proxy variables still works during that run.

A validator that starts while the host is at `high` or `critical` pressure, or within 60 seconds after, runs `patient_argv`. Without `patient_argv` it runs `argv` with `AUTOBUILD_HOST_PRESSURE=1` in its environment.

### The validator's time limit

The validator's time limit is `run.command_timeout_seconds`, 600 seconds by default. It applies to every validator run: the setup check's run, the run on each builder's change, and each batch validation. A run that reaches the limit is stopped and counts as a failure, the same as a non-zero exit. The effort retry under [`[models]`](#models) then applies as for any failure, and the park reason is `validator <id> did not pass`. One run is treated differently: a run that timed out while pressure held back other validator runs is run once more alone, and that verdict stands.

`budget_seconds` does not limit a run. Only the setup check reads it, and during a campaign no validator run is compared with it.

Refusals:

- A missing `id`, in either form: `missing required run configuration: validator.id`.
- A missing or malformed `argv`: `validator.argv must be a non-empty string array`.
- A malformed `patient_argv`: `validator.patient_argv must be a non-empty string array`.
- A `budget_seconds` that is not a positive number: `validator.budget_seconds must be a positive number`.
- At launch, an offline validator run that fails or outlasts `budget_seconds`: `validator-offline preflight failed: <cause>`.
- At launch, a `run.command_timeout_seconds` below `budget_seconds`: `validator-budget preflight failed: <cause>`.

## `[policy]`

The `[policy]` table sets which tools a builder may use and which folders outside the repository the seats may reach.

| Key | Type | Default | Allowed values | Effect |
|---|---|---|---|---|
| `allowed_tools` | array of strings | `["read", "write", "shell", "python", "git"]` | a non-empty list drawn from `read`, `write`, `shell`, `python`, `git` | The builder's tools. Reviewer and specialist seats always get `read` alone. |
| `allowed_roots` | array of strings (paths) | empty | any paths | Folders the seats may use besides the repository and the scratch root. |

A brief must lie inside the repository, the scratch root or an allowed root. A brief outside them is refused only when its first seat starts, after the claim, and the item parks with `seat brief is outside the allowed roots: <path>`; no setup check finds this earlier. A `repository` brief whose declared paths leave the repository and the allowed roots stops the launch when the setup check reads it, and otherwise parks before any claim as `nature:cross-repository` (see the [guide to writing briefs](../writing-briefs.md)).

On the command line, `--allowed-tool` replaces the whole `allowed_tools` list for one launch, while `--allowed-root` adds to `allowed_roots`.

Refusals:

- An empty `allowed_tools`, or one that holds an empty or non-string entry: `policy.allowed_tools must be a non-empty string array`.
- A tool name outside the five: `policy.allowed_tools contains unknown semantic tools: <names>`.
- An `allowed_roots` that is not an array, or holds an empty or non-string entry: `policy.allowed_roots must be a string array`.

## `[selection]`

The `[selection]` table fences which items a campaign may take.

| Key | Type | Default | Allowed values | Effect |
|---|---|---|---|---|
| `allow` | array of strings | empty | item ids, none blank | When set, the campaign takes only these items, in this order. |
| `exclude` | array of strings | empty | item ids, none blank | The campaign never takes these items. It checks the list at every selection. |

`--allow-item` and `--exclude-item` add to these lists for one launch. The profile's entries come first and duplicates are dropped. A campaign whose only ready items are excluded ends with `queue_dry`.

Refusal: a value that is not an array of non-empty strings gives `selection.<key> must be an array of non-empty strings`.

## `[usage]`

The `[usage]` table sets the levels at which AutoBuild stops using a coding assistant subscription before its allowance runs out. Without the table, AutoBuild reads no allowance meter and runs no usage check at launch.

| Key | Type | Default | Allowed values | Effect |
|---|---|---|---|---|
| `warn_percent` | number | `80` | above 0, at most 100, not above `stop_percent` | At this share of a limit, a campaign that runs several items at once starts a new item only when nothing else runs. |
| `stop_percent` | number | `90` | above 0, at most 100 | At this share of the weekly limit across every model, AutoBuild sets the lane aside until the limit resets and moves its seats to the next lane. A weekly limit can also cover only some models: at this share of such a limit, AutoBuild moves only the seats on those models and keeps the lane in use. |
| `five_hour_stop_percent` | number | `95` | above 0, at most 100 | The same stop for the short session window. AutoBuild does not compare it with `warn_percent`. |
| `on_probe_failure` | string | `"allow"` | `"allow"`, `"refuse"` | What happens when the meter cannot be read. |

A lane is metered when its coding assistant has an allowance meter that AutoBuild can read. Outside subscription routing only the `claude-code` lane is metered. AutoBuild reads its meter before each seat on that lane, and seats on the same scratch root share one reading for 60 seconds. The Claude meter reads an endpoint that is undocumented and may change or stop working without notice. When every lane is stopped for usage, the campaign lets running items finish and ends with `usage_stop`.

No level in this table stops the `claude-sdk` lane: see [Money spent on API keys](#money-spent-on-api-keys).

With `"allow"`, a seat starts when the meter cannot be read. With `"refuse"`, a launch whose meter cannot be read is refused, and a seat waits 300 seconds and reads again, up to three times, before its item parks with a reason that ends `:usage_meter_unavailable`. Once the meter has given a run a reading at or above `warn_percent`, a later failed reading holds the seat whatever `on_probe_failure` says, until a reading succeeds or the hold outlasts `run.hold_limit_seconds`.

Refusals:

- A value that is not a number, a boolean included: `usage.<key> must be a number`.
- A level at or below 0, or above 100: `usage.<key> must be above 0 and at most 100`.
- A `warn_percent` above `stop_percent`: `usage.warn_percent must not exceed usage.stop_percent`.
- Any other `on_probe_failure`: `usage.on_probe_failure must be one of: allow, refuse`.

The [lanes and usage guide](../lanes-and-usage.md) covers what the meter needs and what a usage stop looks like in a run.

## `[subscription_routing]`

Subscription routing is the mode in which AutoBuild spreads seats across Claude and Codex subscriptions by their remaining allowance. The `[subscription_routing]` table switches it on.

| Key | Type | Default | Allowed values | Effect |
|---|---|---|---|---|
| `enabled` | boolean | none; required when the table is present | `true`, `false` | `true` switches subscription routing on. `false`, or no table, leaves it off. |

With routing on, AutoBuild picks the lane for each seat from the allowance left on each subscription, and the order of `run.lanes` only breaks ties between equal scores. That choice of lane is called admission. Every seat on the `claude-code` lane must use the model `claude-opus-5-5`, and every seat on the `codex` lane `gpt-5.6-terra` or `gpt-5.6-sol`. A lane that names another model is reported unavailable when AutoBuild probes it, and a seat with another model is refused when it starts. `on_probe_failure` has no effect on the lane check at launch. The commands `autobuild subscription-status` and `autobuild subscription-review` need routing on and a `[usage]` table.

Routing strips every inherited setting that could send a seat to another provider. Each seat therefore loses every environment variable whose name contains, in any letter case, `API_KEY`, `AUTH_TOKEN`, `ACCESS_TOKEN`, `TOKEN`, `SECRET`, `PASSWORD`, `COOKIE`, `CREDENTIAL`, `BASE`, `PROVIDER`, `ENDPOINT` or `KEY_HELPER`. Variables such as `DATABASE_URL` and `GITHUB_TOKEN` are missing inside every seat, so a builder's tests that need one of them fail under routing.

Refusals, checked only when `enabled = true`:

- A missing or non-boolean `enabled`, checked whenever the table is present: `subscription_routing.enabled must be a boolean`.
- A lane other than `claude-code` or `codex`: `subscription routing permits only claude-code and codex run.lanes`.
- A lane with `key_file`: `subscription routing forbids lane API key files`.
- A `[harness] command` or `--harness-arg` on a profile with more than one lane, or one that is not exactly `claude` or `codex` for its single lane: `subscription routing forbids a non-official harness command`.
- No `[usage]` table: `subscription routing requires a [usage] policy`.

How admission scores the subscriptions, reserves allowance and records each decision is in the [lanes and usage guide](../lanes-and-usage.md).

## `[process]`

The process mode is the review procedure that every item follows, set by the `[process]` table.

| Key | Type | Default | Allowed values | Effect |
|---|---|---|---|---|
| `mode` | string | `"flow"` | `"flow"`, `"strict"` | The review procedure. |
| `test_command` | array of strings | empty | non-empty | The command that runs the test paths of a `test_map` entry. |
| `test_map` | table | empty | see below | Maps a source path pattern to the tests that cover it. Required in `strict` mode. |
| `boundaries` | table of tables | empty | see below | Names the parts of the repository that need extra review. |

Each `test_map` entry pairs a path pattern with either a non-empty array of test paths, run after `test_command`, or one command line as a string, split by shell quoting.

A boundary is a part of the repository that needs extra care. Each `[process.boundaries.<name>]` table holds `paths`, a non-empty array of path patterns, and an optional `invariants`, the path of a file of rules for that part. AutoBuild checks the boundary map in both modes, and only `strict` mode reads it.

In `flow` mode, and in `strict` mode without boundaries, an item gets two correction rounds. In `strict` mode with boundaries, AutoBuild sorts each item into one of two review sizes. A Small item is one whose paths touch no boundary, and it gets one correction round. A Material item is one that touches a boundary, and it stays Material for the rest of its run: it gets two correction rounds and one specialist seat for each boundary it touches, in map order. In this mode the correction that reaches the limit is judged by the validator alone.

In `strict` mode the builder seat alone carries a guard on which tests it runs. The `codex` and `github-copilot` lanes carry no such guard. The strict checks catch a builder's mistakes, and they are not a security barrier.

Refusals:

- A `mode` other than `flow` or `strict`: `process.mode must be "flow" or "strict", not <value>.`.
- `strict` mode without a `test_map` is refused, and so is a `test_map` with test paths and no `test_command`.
- A `test_map` value with an unclosed quote, or an array that is not a list of strings, is refused with one sentence that names the key.
- A malformed `test_command`: `process.test_command must be a non-empty string array`.
- A `boundaries` value that is not a table: `[process.boundaries] must be a table of named boundaries.`.
- A boundary key that AutoBuild does not know, inside `[process.boundaries.<name>]`: `process.boundaries.<name> contains unknown fields: <names>`.
- A boundary without a non-empty `paths` array: `process.boundaries.<name>.paths must be a non-empty list of path pattern strings.`.
- An `invariants` that is not a non-blank string: `process.boundaries.<name>.invariants must be the path of a file, as a string.`.
- A boundary table written twice under the same name is invalid TOML, so the file is refused before any boundary check: `run profile is invalid TOML: <error>`.

## `[harness]`

The `[harness]` table replaces the program that a single-lane profile runs for its coding assistant.

| Key | Type | Default | Allowed values | Effect |
|---|---|---|---|---|
| `command` | array of strings | the harness's own command | non-empty | Replaces the coding assistant's executable and its fixed leading arguments. |

`command` applies only when the profile has exactly one lane. With two or more lanes AutoBuild ignores it without a message, and the same holds for `--harness-arg`, which replaces it for one launch. The command and options each lane runs are in the [harness adapter reference](harness-adapters.md).

Refusal: a malformed `command` gives `harness.command must be a non-empty string array`.

## `[tracker]`

The tracker is the list of work items that AutoBuild reads and writes. The [trackers guide](../trackers.md) covers setting one up.

| Key | Type | Default | Allowed values | Effect |
|---|---|---|---|---|
| `kind` | string | `"auto"` | `auto`, `pinax`, `backlog`, in any letter case | Which tracker to use. `auto` uses Pinax when its check passes, otherwise the `BACKLOG.md` table when its check passes. |
| `path` | string (path) | `BACKLOG.md`, then `docs/BACKLOG.md` | a path inside the repository | The `BACKLOG.md` file. A relative path resolves against the repository. |

Refusals:

- Any other `kind`: `tracker.kind must be auto, pinax or backlog`.
- At launch, no tracker that passes its check: `tracker preflight failed: <diagnostics>`. With an explicit `kind`, only that tracker was checked.
- When the run starts, a `path` outside the repository: `BACKLOG.md must be inside the campaign repository`. AutoBuild always builds the `BACKLOG.md` adapter, so this refusal applies with every `kind`, Pinax included.

## `[preflight]`

The `[preflight]` table adjusts the setup checks that run before the first claim.

| Key | Type | Default | Allowed values | Effect |
|---|---|---|---|---|
| `tls_targets` | array of strings | empty | `host:port` entries | Hosts that the `dns-tls` check resolves and connects to. It also checks the `origin` host when `origin` uses `https`. |
| `accepted_environment` | array of strings | empty | variable names, none blank | Variables that may be set in this environment without failing the `interception` check. |

The `interception` check refuses a launch when any of `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `SSL_CERT_FILE`, `SSL_CERT_DIR`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `NODE_EXTRA_CA_CERTS`, `NODE_TLS_REJECT_UNAUTHORIZED`, `GIT_SSL_CAINFO`, `GIT_SSL_NO_VERIFY` or `SSLKEYLOGFILE` is set and not listed in `accepted_environment`.

Refusals:

- A `tls_targets` that is not an array of strings: `preflight.tls_targets must be a string array`.
- A `tls_targets` entry that is not `host:port`: `preflight.tls_targets entry must be host:port, got '<entry>'`.
- An `accepted_environment` that is not an array of non-blank strings: `preflight.accepted_environment must be a string array`.
- At launch, a failed check: `<probe> preflight failed: <cause>`.

## `[capacity]`

The `[capacity]` table sets how AutoBuild works out `run.concurrency` and `run.heavy_concurrency` when they are `"auto"`. Memory values are in GiB.

| Key | Type | Default | Allowed values | Effect |
|---|---|---|---|---|
| `reserve_gb` | number | `8` | 0 or more | Memory kept free for everything else on the host. |
| `per_item_gb` | number | `3` | above 0 | Memory counted for each running item. |
| `max_concurrency` | integer | `8` | 1 or more | The ceiling on the automatic item count. |

The automatic item count is `min(logical CPUs // 4, floor((available GiB - reserve_gb) / per_item_gb))`, held between 1 and `max_concurrency`. The automatic heavy count is `logical CPUs // 12`, held between 1 and the item count. AutoBuild takes one host reading of 0.5 seconds at launch. When memory cannot be measured, its term is dropped. When the processor count cannot be measured, both counts are 1.

Run `autobuild host` to see the reading and the counts this table gives on your machine.

Refusals, when the profile loads:

- A `reserve_gb` or `per_item_gb` that is not a number, a boolean included: `capacity.<key> must be a number of GiB`.
- A negative `reserve_gb`: `capacity.reserve_gb must not be negative`.
- A `per_item_gb` at or below 0: `capacity.per_item_gb must be a positive number`.
- A `max_concurrency` that is not an integer, or is below 1: `capacity.max_concurrency must be a positive integer`.

## `[pressure]`

Host pressure is how busy the machine is while a campaign runs. When more than one item runs, AutoBuild reads the host every `sample_seconds` and names a level: `normal`, `recovering`, `high` or `critical`. Only `normal` admits new items.

| Key | Type | Default | Allowed values | Effect |
|---|---|---|---|---|
| `sample_seconds` | number | `10` | above 0 | Time between two host readings. |
| `read_seconds` | number | `1` | above 0, at most `sample_seconds` | The time over which one reading measures the processors. |
| `window_seconds` | number | `60` | above 0 | The window over which the processor mean is taken. |
| `calm_seconds` | number | `120` | above 0 | The calm window that ends `recovering`. |
| `high_cpu_percent` | number | `90` | 0 to 100 | A processor mean over the full window at or above this is `high`. |
| `calm_cpu_percent` | number | `75` | 0 to 100, at most `high_cpu_percent` | Every processor sample of the calm window must be below this to leave `recovering`. |
| `high_memory_percent` | number | `10` | 0 to 100 | Available memory below the larger of this share and `high_memory_gb` is `high`. |
| `high_memory_gb` | number | `4` | 0 or more | See `high_memory_percent`. |
| `critical_memory_percent` | number | `5` | 0 to 100 | Available memory below the larger of this share and `critical_memory_gb` is `critical`. |
| `critical_memory_gb` | number | `2` | 0 or more | See `critical_memory_percent`. |
| `high_commit_percent` | number | `10` | 0 to 100 | Commit headroom below this share of the commit limit is `high`. |
| `calm_memory_gb` | number | `2` | 0 or more | To leave `recovering`, available memory must be at least this far above the `high` line. |
| `decrease_seconds` | number | `300` | above 0 | Each such period at `high` or `critical` lowers the running cap by one, down to 1. |
| `increase_seconds` | number | `60` | above 0 | Each such period at `normal` raises the running cap by one, up to the concurrency. |

Pressure never stops a running seat or command, and a campaign with nothing of its own running always admits one item. A seat's time limit grows by every second it spends at `high` or `critical`, up to twice its limit. A validator that timed out while pressure held back other validator runs is run once more alone, and that verdict stands.

Refusals, when the profile loads:

- A value that is not a number: `pressure.<key> must be a number`.
- A `sample_seconds`, `read_seconds`, `window_seconds`, `calm_seconds`, `decrease_seconds` or `increase_seconds` that is not above 0: `pressure.<key> must be a positive number`.
- A `read_seconds` above `sample_seconds`: `pressure.read_seconds must not exceed pressure.sample_seconds`.
- A share outside 0 to 100: `pressure.<key> must be between 0 and 100`.
- A `calm_cpu_percent` above `high_cpu_percent`: `pressure.calm_cpu_percent must not exceed pressure.high_cpu_percent`.
- A negative `high_memory_gb`, `critical_memory_gb` or `calm_memory_gb`: `pressure.<key> must not be negative`.

## `[progress]`

The `[progress]` table sets where AutoBuild sends its progress lines.

| Key | Type | Default | Allowed values | Effect |
|---|---|---|---|---|
| `file` | boolean | `true` | `true`, `false` | Whether progress lines are written to the run's file. |
| `stderr` | boolean | `true` | `true`, `false` | Whether `autobuild run` also prints each progress line to standard error. |
| `command` | array of strings | none | non-empty | A command that runs once for each progress line, with the line on standard input. |
| `command_timeout_seconds` | number | `5` | above 0 | The limit on one run of `command`. |

AutoBuild swallows every failure of `command` and counts it. The count is added once to the end of the completion line, as ` progress hook failures: <n>`. A non-boolean `file` or `stderr`, a malformed `command` and a non-positive `command_timeout_seconds` are refused when the profile loads. A malformed `command` gives `progress.command must be a non-empty string array`.

## `[refill]`

The `[refill]` table names a plan file of new work that AutoBuild registers when the queue runs dry. The plan holds proposals, which become items, and fog records. A fog record is an open question that blocks a direction of work: it names the direction, the question that blocks it and when to raise it again.

| Key | Type | Default | Allowed values | Effect |
|---|---|---|---|---|
| `plan` | string (path) | none | a JSON file | A file of proposals and open questions that AutoBuild registers when the queue runs dry and nothing is running. |

The plan file has the schema `autobuild.refill-plan.v1`, optional `proposals` and `fog` arrays, no other keys, and at least one record. Each proposal needs exactly `title`, `question`, `rationale` and `brief_ref`. Each fog record needs exactly `direction`, `blocking_question` and `surface_when`. An unknown, missing or blank field is refused with a message that names the record's index. The selected tracker checks each proposal before the campaign starts. AutoBuild registers every proposal as an item that it never runs, and the campaign still ends with `queue_dry`.

`--refill-plan` replaces this key for one launch.

Refusals of the plan file:

- No file at the path: `refill plan does not exist: <path>`.
- A file that is not JSON: `refill plan is invalid JSON: <error>`.
- JSON that is not an object: `refill plan must be a JSON object`.
- A missing or different schema: `refill plan schema must be autobuild.refill-plan.v1`.
- A plan with no proposal and no fog record: `refill plan must contain at least one proposal or fog record`.

## `[knowledge]`

The `[knowledge]` table connects a knowledge adapter, a command of your own that records the fog records of a refill plan in a file.

| Key | Type | Default | Allowed values | Effect |
|---|---|---|---|---|
| `command` | array of strings | none | non-empty | The knowledge adapter command that registers the fog records of a refill plan. |
| `fog_ledger` | string (path) | none | a path | The file the knowledge adapter writes fog records to. |

`command` and `fog_ledger` must be given together, and a refill plan that holds fog records needs both. Either case is refused when the profile loads. A malformed `command` gives `knowledge.command must be a non-empty string array`.

## Fixed values that no key sets

These values look like settings, and they are fixed in the code. No profile key sets them, and a key invented for one, such as `max_corrections`, is refused as unknown.

| Value | Where it applies |
|---|---|
| Two correction rounds | Every item in `flow` mode, and in `strict` mode without boundaries. |
| One correction round, or two | A Small or a Material item in `strict` mode with boundaries. |
| Two builder continuations | A builder that runs out of time before its commit is continued twice; a third timeout parks the item with `timeout:builder`. |
| `high` | The ceiling of the effort raise after a failed validator. |
| Three delivery attempts | A delivery that loses three times to a moved target or a rejected push parks with `delivery_contended`. |
| 60 seconds | How long one allowance meter reading is shared. |
| 300 seconds | The wait between meter retries under `on_probe_failure = "refuse"`, and the hold after the Claude meter answers a request with a rate limit and gives no retry time. |
| Three meter retries | Under `on_probe_failure = "refuse"`, before the item parks with a reason that ends `:usage_meter_unavailable`. |
| 600 seconds | How often a hold after a failed batch validation checks the target again. |
| 1 to 600 seconds | The wait before AutoBuild checks the lanes again when every lane is set aside: the earliest reset time, held inside this range. |
| 60 seconds after `high` pressure | The window in which a validator still runs its patient form, `patient_argv`. |
| Commit subjects | `ADDED: <title>`, `MODIFIED: close <item> in tracker`, `MODIFIED: park <item> in tracker` from the worktree, and a merge whose subject starts `MERGED: integrate`. A `BACKLOG.md` tracker adds `MODIFIED: claim <item> in BACKLOG`, `MODIFIED: park <item> in BACKLOG` from your checkout and `ADDED: propose <title> in BACKLOG`, and every campaign ends with `ADDED: campaign <id> report`. |

## Limits

A few profile keys are ignored without a message in one form, so check the form before you rely on them. In the lane form, AutoBuild ignores the model names in `[models]`, and the `--builder-model`, `--reviewer-model` and `--specialist-model` options have no effect. The lane form also ignores `run.harness`. With two or more lanes, `[harness] command` and `--harness-arg` have no effect. In the single-lane form, the model names, `key_file` and `prepare_argv` in lane tables are ignored, so the `claude-sdk` lane works only in the lane form (see [Money spent on API keys](#money-spent-on-api-keys)).

No profile key sets the delivery mode, turns delivery on or controls pushing: those are the `autobuild run` options in the [command reference](commands.md).

The profile cannot set the number of correction rounds, the delivery attempts or any other value in the table above. It holds no key for a network or file sandbox around the builder.

## A complete profile

This profile uses every table, in the lane form with subscription routing on, so each value obeys the rules of every table above. `[models]` and `[harness]` appear as comments, because the lane form ignores model names in `[models]` and routing refuses a harness command on two lanes. `[refill]` and `[knowledge]` are comments too, because each needs a file or a command of your own.

```toml
[run]
lanes = ["claude-code", "codex"]
max_items = 20
seat_timeout_seconds = 900
seat_stall_seconds = 900
lease_stale_seconds = 1800
command_timeout_seconds = 600
hold_limit_seconds = 21600
scratch_root = "../autobuild-work"
lane_state_root = "../autobuild-work"
lane_cool_seconds = 3600
concurrency = 2
heavy_concurrency = 1
batch_validate_every = 5

[run.item_classes]
long = 2700

# [models] is read only in the single-lane form, with run.harness:
# [models]
# builder = "claude-opus-5-5"
# reviewer = "claude-opus-5-5"
# builder_effort = "medium"

[lanes.claude-code]
builder = "claude-opus-5-5"
reviewer = "claude-opus-5-5"
specialist = "claude-opus-5-5"
builder_effort = "medium"
reviewer_effort = "high"

[lanes.codex]
builder = "gpt-5.6-terra"
reviewer = "gpt-5.6-sol"
specialist = "gpt-5.6-sol"
builder_effort = "medium"
reviewer_effort = "high"
prepare_argv = ["uv", "sync"]

[validator]
id = "tests"
argv = ["python", "-m", "pytest", "-q"]
budget_seconds = 300
patient_argv = ["python", "-m", "pytest", "-q", "-x"]

[policy]
allowed_tools = ["read", "write", "shell", "python", "git"]
allowed_roots = []

[selection]
allow = ["ITEM-7", "ITEM-8"]
exclude = []

[usage]
warn_percent = 80
stop_percent = 90
five_hour_stop_percent = 95
on_probe_failure = "allow"

[subscription_routing]
enabled = true

[process]
mode = "strict"
test_command = ["python", "-m", "pytest", "-q"]

[process.test_map]
"src/export/*.py" = ["tests/test_export.py"]
"src/cli.py" = "python -m pytest -q tests/test_cli.py"

[process.boundaries.payments]
paths = ["src/payments/*.py"]
invariants = "docs/invariants/payments.md"

# [harness] applies to a single-lane profile only:
# [harness]
# command = ["claude"]

[tracker]
kind = "backlog"
path = "BACKLOG.md"

[preflight]
tls_targets = ["github.com:443"]
accepted_environment = []

[capacity]
reserve_gb = 8
per_item_gb = 3
max_concurrency = 8

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

[progress]
file = true
stderr = true
command_timeout_seconds = 5

# [refill] needs a plan file with at least one record:
# [refill]
# plan = "docs/refill-plan.json"

# [knowledge] needs your own knowledge adapter command:
# [knowledge]
# command = ["your-knowledge-adapter"]
# fog_ledger = "docs/fog.md"
```

The `codex` lane's `prepare_argv` runs before each builder seat that may write on that lane. The item class `long` raises the seat limit to 2700 seconds for an item whose brief has the line `Item class: long`. With `tracker.kind = "backlog"`, the integer `concurrency` lets two items run at once.
