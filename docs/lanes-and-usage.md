# Lanes and usage

You pay for one or two coding assistant subscriptions, and each one comes with an allowance that runs out: a short window that resets every few hours and a weekly limit. Your queue is longer than one allowance, so the work has to move to the other subscription when the first one is nearly used, and it has to wait when both are.

AutoBuild does that moving for you. You tell it which coding assistants it may use, in which order, and at which percentage of an allowance it should warn and stop. With a `[usage]` table, it reads the Claude allowance before each seat and judges it against those levels. When an assistant is limited, AutoBuild moves the seat to the next one. The [README](../README.md) explains campaigns, seats and the validator.

Subscription routing is a mode that you turn on in the profile. In it AutoBuild reads the allowance of both subscriptions and chooses the assistant for each seat, and it refuses an assistant that is not signed in through a subscription plan. Without it, AutoBuild reads only the Claude allowance and follows your order. Each part of this guide says which of the two runs it describes. The last part covers `autobuild subscription-review`, which reviews one commit outside a campaign.

This guide follows one case. Your repository has a queue in which `ITEM-7`, "Add a --json option to the export command", is next. You hold a Claude Max subscription, which Claude Code uses, and a ChatGPT Plus subscription, which Codex uses.

## Spending beyond your subscriptions

Read this list before you set up any lane, because a profile can spend money beyond your subscriptions and AutoBuild does not always notice. Each entry says what AutoBuild checks and what you do about it.

- **The `claude-sdk` lane.** This lane always bills the API key in the file that its `key_file` names, and it has no meter, so no usage level ever stops it. Without subscription routing, seats move to it when a lane listed before it reaches its stop level, and they spend API money with no cap. Leave `claude-sdk` out of `run.lanes` unless you accept that billing. Subscription routing refuses it.
- **Claude Code signed in with an API account.** Without subscription routing, the Claude Code check accepts any signed-in method, an API account included. Run `claude auth status --json` yourself before a launch, or turn on subscription routing, which accepts only a sign-in through a Claude Pro or Max plan.
- **Claude extra usage.** Extra usage is the setting on your Claude account for use beyond the allowance. The Claude meter reads only whether it is on, never what it has billed, and `autobuild usage` prints `extra usage enabled` or `extra usage off`. Without subscription routing nothing acts on that value. Turn extra usage off at the provider, then confirm it with `autobuild usage`. Subscription routing refuses the Claude Code lane unless the reading reports extra usage as off.
- **`OPENAI_API_KEY` in a Codex seat.** Without subscription routing, a Codex seat inherits `OPENAI_API_KEY` from the shell that started AutoBuild, and the Codex check cannot tell a ChatGPT sign-in from an API-key one. Unset the variable in that shell and sign Codex in with ChatGPT. Subscription routing removes the variable and refuses a Codex account that is not a ChatGPT one.
- **A plan that bills by usage.** Without subscription routing, nothing reads which plan an account is on. Subscription routing refuses a plan that is not on its fixed list of included plans, such as the ChatGPT plan `prolite_usage_based`.
- **ChatGPT credits drawn after a seat starts.** AutoBuild checks an allowance only before a seat starts, so a long seat can run on past the included allowance. Whether credits are then drawn is decided by the provider, and AutoBuild neither controls it nor reports it. Settle this with the provider if you do not want credits drawn.
- **GitHub Copilot.** The Copilot check asks only whether a GitHub token variable is set or `gh auth status` succeeds. Nothing reads the account's plan, billing or usage, and the lane has no meter. List `github-copilot` only if you accept whatever your GitHub account bills. Subscription routing refuses it.

Without subscription routing, AutoBuild cannot guarantee that a run spends nothing beyond your subscriptions. With it, AutoBuild runs only the `claude-code` and `codex` lanes, with no key file and only the included models. It checks at launch that each lane is signed in through an included plan, and before every seat it judges a fresh reading, which for Claude must report extra usage as off. Every seat starts without the variables that could bill another account. What happens after a seat has started is outside these checks, and that includes credits and a setting changed during the seat.

## Lanes

This part holds with and without subscription routing, except where it says otherwise.

A lane is one coding assistant that you have set up for AutoBuild. Each seat runs entirely on one lane, with that lane's model and effort for the seat. Without subscription routing, AutoBuild counts a built-in lane as signed in under the condition in the last column.

| Lane | Install | Signed in when |
|---|---|---|
| `claude-code` | The `claude` command | `claude auth status --json` reports `loggedIn` or `authenticated` as true |
| `codex` | The `codex` command | `codex login status` exits 0 and its output does not contain "not logged" |
| `github-copilot` | The `copilot` command | `COPILOT_GITHUB_TOKEN`, `GH_TOKEN` or `GITHUB_TOKEN` is set, or else `gh auth status` exits 0, which needs the GitHub CLI |
| `claude-sdk` | `pip install "autobuild-factory[sdk]==0.12.0"` | The lane's `key_file` names a file that can be read and is not empty |

AutoBuild checks each command-line lane with a version command and a sign-in command, each with a 20-second time limit, and neither of them calls a model. The Copilot check accepts a token variable without testing the token, so a wrong token passes the check and fails at the first seat. The `claude-sdk` check also fails when the SDK is not installed, or when the `key_file` value is unset or contains `"`, `$` or a backtick. Every seat on every lane runs without `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN`, even when your own shell sets them.

### Choose one lane or several

For one lane, name it in `run.harness` and put the models in `[models]`, as the README's first run does. For several, list them in `run.lanes` in the order you prefer, and give each one a table of its own. In the case, Claude Code comes first and Codex second:

```toml
[run]
lanes = ["claude-code", "codex"]

[lanes.claude-code]
builder = "claude-opus-5-5"
reviewer = "claude-opus-5-5"
builder_effort = "medium"

[lanes.codex]
builder = "gpt-5.6-terra"
reviewer = "gpt-5.6-sol"

[validator]
id = "tests"
argv = ["python", "-m", "pytest", "-q"]
```

Each lane table needs `builder` and `reviewer`, and `specialist` takes the reviewer model when you leave it out. Efforts go in the same table, as `builder_effort`, `reviewer_effort` and `specialist_effort`, and a missing `specialist_effort` takes the reviewer's. With `run.lanes` set, AutoBuild takes its models from the lane tables alone and ignores the model names in `[models]`. An effort key left in `[models]` is refused, with a message that tells you to move it to the lane tables. To put a listed lane first for one launch, pass `--harness codex` to `autobuild run`. A name that `run.lanes` does not list is refused. The [profile reference](reference/profile.md) lists every lane key, and the [harness adapter reference](reference/harness-adapters.md) shows the exact command each lane runs.

### Builders and reviewers

A profile sets the models and efforts for each role on each lane, and the order of the lanes. It cannot send one role to one assistant and another role to the other. Without subscription routing, every seat of an item, builder and reviewer alike, starts on the first lane in the order that is not cooling, and moves down the order only when that lane cools. Under subscription routing, admission chooses the lane for each seat, so a role cannot be pinned to one subscription there either. To keep every seat on one assistant, list only that lane.

### When a lane fails, cools and returns

At launch AutoBuild checks every listed lane. A lane that fails its check is set aside, and the first lane that passes is where the first seat starts. When no lane passes, the launch stops with `no capable harness lane:` followed by each lane and its reason, for example `claude-code: Claude Code is not authenticated`.

During the campaign, AutoBuild reads a lane failure only from a seat whose process ended in error. It looks for a code in the assistant's structured output, never in its prose, and sorts the code into a kind: `rate_limit`, `quota` or `auth`. A seat that ends in error with nothing structured on its output or error stream gets the kind `spawn`, which also cools the lane and moves the seat. A seat that timed out, stalled or succeeded never counts against its lane. When a failure is found, AutoBuild sets the lane aside for a while (a cooling), records `lane.cooled`, and runs the same seat again on the next lane that can run it.

A cooling lasts until the reset time the assistant gave. When it gave none, the cooling lasts `run.lane_cool_seconds`, one hour by default. Coolings are kept in one file, `lanes.json`, under `run.lane_state_root`, which is the scratch root unless you set it. Two campaigns that share that folder therefore see each other's coolings. When a cooling ends, seats use the lane again. Without subscription routing, a lane that failed its lane check at launch also comes back into use once its cooling ends.

In the case, without subscription routing, the builder seat for `ITEM-7` gets a `rate_limit` from Claude Code with a reset time of 16:00. AutoBuild cools `claude-code` until 16:00 and starts the builder again on `codex`, with `gpt-5.6-terra`. The tracker still shows the claim as `builder@claude-code`, and the campaign writes the event `item.lane_changed`. Had Codex failed too, `ITEM-7` would park with a reason in the form `<lane>:<signature>`, where the signature names the failure. While every lane is cooling and at least one cooling is not for usage, the campaign starts nothing new (`lanes_exhausted`) and checks again at the earliest reset, waiting between 1 and 600 seconds each time. The guide to [running campaigns](running-campaigns.md) explains parks and holds in full.

## Usage levels

This part describes a run without subscription routing. Subscription routing uses the same `[usage]` levels for both of its lanes, and its own part says what changes.

A cooling reacts after an assistant has refused a seat. A usage level acts before that. With a `[usage]` table in your profile, AutoBuild reads your Claude subscription's allowance before each seat on the Claude Code lane and judges it against levels that you set. Without the table, no allowance is read at all. The Claude Code lane is the only lane with a meter: Codex, GitHub Copilot and the SDK lane are never read or cooled for usage.

Claude reports these kinds of limit. `session` is the short window that resets every few hours, `weekly_all` is the weekly limit across all models, and `weekly_scoped` is a weekly limit for the models its label names.

### The `[usage]` table

| Key | Default | Meaning |
|---|---|---|
| `warn_percent` | 80 | At or above this level, AutoBuild warns and slows admissions. |
| `stop_percent` | 90 | At or above this level, a `weekly_all` or `weekly_scoped` limit stops the lane. |
| `five_hour_stop_percent` | 95 | At or above this level, the `session` limit stops the lane. |
| `on_probe_failure` | `allow` | `allow` starts a seat when the meter cannot be read; `refuse` waits for a reading. |

Each level must be above 0 and at most 100, and `warn_percent` must not exceed `stop_percent`. An empty `[usage]` table takes the defaults. In the case you add one:

```toml
[usage]
warn_percent = 80
stop_percent = 90
five_hour_stop_percent = 95
on_probe_failure = "allow"
```

Before any claim, AutoBuild reads each meter once. It refuses the launch only when every configured lane has a meter and is at a stop level, with `usage preflight failed: every lane is at its usage stop level:` and the limits, or when a meter fails under `refuse`. Otherwise it cools each stopped lane until its reset and starts on the next one. In the case Codex has no meter, so the launch never refuses for usage.

### Leave room for one seat

A stop level is checked before a seat starts, never while it runs, so a running seat can carry a lane past its stop level. Set `stop_percent` and `five_hour_stop_percent` below the level you must not pass by more than one seat's share of the window. Under subscription routing, `autobuild subscription-status --json` shows that share for each window as `estimated_seat_percent`. Without routing AutoBuild keeps no estimate, so compare two `autobuild usage` readings, one before a seat and one after it.

### At the warning level

When a limit reaches `warn_percent`, AutoBuild records `usage.warning` once, and again only after the limit has dropped below the level and crossed it a second time. While the first lane's reading is at the warning level, a campaign that runs several items at once starts a new item only when no other item is running. AutoBuild also becomes stricter about failed readings from then on, as described under "The shared reading" below.

### At a stop level

A limit stops at or above its own level: `session` at `five_hour_stop_percent`, and both weekly kinds at `stop_percent`. When a `session` or `weekly_all` limit stops, AutoBuild records `usage.stop`, cools the lane for usage until the latest reset among the stopping limits, and moves the seat to the next lane. A `weekly_scoped` limit stops only the seats whose model its label covers. Such a seat skips the lane (`usage.seat_rerouted`), the lane itself stays open for other seats, and the item parks when no other lane is left. A stopping limit that gives no usable reset time cools nothing: AutoBuild handles that reading as a failed one, as described under "The shared reading".

AutoBuild checks a usage cooling again before it honours it, also when another campaign wrote it. A fresh reading below every stop level of this run clears the cooling and records `lane.usage_cooling_cleared`.

In the case, the weekly limit reaches 90% while `ITEM-7` waits for review. AutoBuild cools `claude-code` until the weekly reset, and the reviewer seat runs on `codex` with `gpt-5.6-sol`.

### When every lane is stopped

When every configured lane is cooled for usage, the campaign writes the event `campaign.usage_stop` with each cooling and the earliest reset. It starts nothing new, lets running items finish, and ends with the stop reason `usage_stop`. The report's line starts `usage_stop: every lane is stopped for usage until <reset>`. A lane without a meter is never cooled for usage, so in the case this stop cannot happen until subscription routing gives Codex a meter.

### The shared reading

This part holds with and without subscription routing.

Every process that reads a meter from one scratch root, a campaign or a command, shares one reading per meter. The reading is kept in `usage-meter/<meter id>.json` under that folder, and the file holds the percentages and reset times and no credential. A reading younger than 60 seconds is served from the file. An older one is refreshed by one process under a lock while the others wait, and a process that cannot take the lock within 15 seconds reads the meter itself. Inside one campaign, the seats on a lane reuse one good reading for 60 seconds. While one seat refreshes it, the other seats judge by the previous reading.

The campaign, its lane check at launch, `autobuild preflight`, `autobuild subscription-status` and `autobuild subscription-review` use this file. `autobuild usage` uses it too, and finds the scratch root from `--scratch-root`, else `[run] scratch_root` in the profile, else the default, so it shares the campaign's file when you give it the campaign's `--repository`, `--profile` and `--scratch-root`. It prints the Claude reading once and ends with its age and its source, either `from the shared subscription cache` or `read from the meter`:

```
autobuild usage
```

A rate-limited answer is a reading in which the meter itself refused to answer, with HTTP 429 or the error type `rate_limit_error`. Its error reads `claude-subscription meter rate-limited, next read at <time>`, and no process on the scratch root asks the meter again before that time. The time is the one the meter gave in its `retry-after` answer. When it gave none, the time is 300 seconds after the refusal, and it is never less than 60 seconds after the refusal.

A failed reading is handled in one of two ways, depending on whether a good reading in this run has already reached `warn_percent`. Until one has, `allow` records `usage.meter_failed` and starts the seat without a reading. Under `refuse`, AutoBuild tries again every 300 seconds, up to three times, and then parks the item with a reason that ends `:usage_meter_unavailable`, without cooling a lane. Once a good reading has reached the warning level, a failed reading holds the seat whatever `on_probe_failure` says. AutoBuild then reads again every 300 seconds, and when the hold outlasts `run.hold_limit_seconds`, six hours by default, the item parks with a reason that ends `:usage_meter_unavailable`.

An expired sign-in fails the reading before any request is sent. The reading's error is then `sign-in expired: run Claude once in this venue to refresh it`, followed by the time it expired. The venue in that message is the place where AutoBuild runs, so run Claude Code once there. AutoBuild never writes an expired sign-in into the shared file, so the next reading after you refresh it is fresh. A refusal for this reason ends with `try again once the sign-in is refreshed`.

The Claude meter reads the usage endpoint that the Claude Code client uses for its own usage view. That endpoint is undocumented and may change or stop working without notice. The meter needs `curl` and Claude Code's own sign-in, which AutoBuild never prints or stores. When the meter cannot find that sign-in, every reading fails.

## Subscription routing

This part describes a run with subscription routing.

Lanes in order and usage levels protect one Claude allowance, but they leave the second subscription unwatched. Subscription routing (the `[subscription_routing]` table) reads the allowance of both subscriptions and lets AutoBuild pick the lane for each seat. It also checks that each lane is signed in through an included plan, and it removes the variables that could send a seat to another account.

An included plan is a subscription plan on AutoBuild's fixed list for its lane: `pro` or `max` for Claude, and for ChatGPT `plus`, `prolite`, `pro`, `promax`, `team`, `business` or `enterprise`. The plan name must match exactly, so `prolite_usage_based`, a ChatGPT plan that bills by usage, is not included. Included usage is the use that such a plan's allowance covers.

### Credit on a ChatGPT account

Before you turn routing on with a ChatGPT account, know how it treats credit. A ChatGPT account on an included plan can show a credit balance while included usage is still allowed. AutoBuild treats the account's own flag for included usage as the answer, so a credit balance counts as neither paid use nor a stop. AutoBuild checks the allowance only before a seat starts, so a long seat can run on past the included allowance. Whether credits are then drawn is decided by the provider, and AutoBuild neither controls it nor reports it.

### Turn it on

```toml
[run]
lanes = ["claude-code", "codex"]

[lanes.claude-code]
builder = "claude-opus-5-5"
reviewer = "claude-opus-5-5"

[lanes.codex]
builder = "gpt-5.6-terra"
reviewer = "gpt-5.6-sol"

[validator]
id = "tests"
argv = ["python", "-m", "pytest", "-q"]

[usage]

[subscription_routing]
enabled = true
```

Subscription routing needs a `[usage]` table. The lanes may only be `claude-code` and `codex`, and no lane may set `key_file`. The `[harness]` table's `command` key replaces the command that starts the assistant on a profile with one lane. Under routing it is allowed only with one lane and only as `["claude"]` or `["codex"]`, so a profile with both lanes leaves it out. Under routing, admission picks the lane for every seat, so `--harness` does not pick the first lane. The order in `run.lanes` only breaks ties: when two lanes score the same, admission takes the one listed first.

### Models and sign-in on each lane

Under routing the Claude Code lane accepts only `claude-opus-5-5`, and the Codex lane accepts only `gpt-5.6-terra` and `gpt-5.6-sol`, for every seat. Another model makes the lane's check fail with `Claude subscription lane names a non-included model tier` or `Codex subscription lane names a non-included model tier`, and a seat that names one is refused.

The sign-in checks are stricter. On the Claude Code lane AutoBuild first needs a good meter reading that reports extra usage as explicitly off, else the check fails with `Claude extra usage is not explicitly disabled`. It then runs `claude auth status --json` with no Claude Code settings loaded, and needs `loggedIn` true, `authMethod` `claude.ai`, `apiProvider` `firstParty` and the plan `pro` or `max`. An answer that does not meet all four fails with `Claude is not signed in through the first-party subscription`. When the command exits with an error, the check fails with `Claude subscription authentication probe failed`, and when its output is not JSON, with `Claude subscription authentication probe returned invalid JSON`.

On the Codex lane the check passes only when the ChatGPT allowance reads successfully. AutoBuild reads it through the Codex app server, a local process that it starts with `codex app-server --stdio`, and that surface is also undocumented and may change or stop working without notice. The reading must show a ChatGPT account on an included plan with included usage allowed. It must also report no limit reached and no spending limit reached, and it must hold a weekly window. The failures read `Codex is not signed in through ChatGPT`, `Codex ChatGPT plan is not an included subscription`, `ordinary included Codex usage is unavailable` and `Codex usage is paid, stopped, or malformed`.

Under routing, a lane that fails its lane check at launch is left out for the whole run, even once its cooling has ended. The exception is a lane that failed only because its meter could not be read. Such a lane is not cooled, and the next launch checks it again once the meter answers.

### The seat's environment

Under routing, every Claude Code and Codex seat starts without any inherited variable whose name contains `API_KEY`, `AUTH_TOKEN`, `ACCESS_TOKEN`, `TOKEN`, `SECRET`, `PASSWORD`, `COOKIE`, `CREDENTIAL`, `BASE`, `PROVIDER`, `ENDPOINT` or `KEY_HELPER`. A fixed list goes too, among them `CLAUDE_CONFIG_DIR`, `CLAUDE_CODE_USE_BEDROCK`, `SSLKEYLOGFILE`, `SSH_AUTH_SOCK` and `GIT_ASKPASS`, and a Claude Code seat also loses every variable whose name starts with `ANTHROPIC_`. These variables can send a seat to another provider or bill it to an account, so AutoBuild removes them without reading their values.

The match is on any part of the name, so it also removes variables that your project needs. `DATABASE_URL` contains `BASE`, and `GITHUB_TOKEN` and `NPM_TOKEN` contain `TOKEN`. A builder's own test runs then fail where they need one of these. Check your test command's variables before you turn routing on. A Codex seat also ignores your Codex user configuration, and a Codex reviewer runs with network access off, as the [harness adapter reference](reference/harness-adapters.md) shows.

### Admission and reservation

Admission is the step that decides, before each seat, which lane has room for it. AutoBuild keeps the decisions in one ledger file, `subscription-admission/factory.db` under the scratch root, which every campaign and command on that folder shares.

A lane is eligible only when its reading is good and complete. A reading fails eligibility when it has no `weekly_all` window, is more than 90 seconds old, lists the same window twice, has a percentage outside 0 to 100, or has a reset time outside its window. A Claude reading must also report extra usage as off. When a rate-limited or broken connection stops a reading, admission may judge the lane on the last good reading. That reading must be less than 90 seconds old with every limit below `warn_percent`, and the lane's reason then ends `judged on the reading of <time>`.

Each seat reserves an estimate in each window of its lane, in percentage points of that window. The estimate is the largest of the last eight increases observed for that lane, seat role and window, and it is 1 point when none has been observed. A window has room for a seat when the estimate fits into what is left below the stop level, after the used percentage and other seats' reservations are taken off. When more than one lane has room, admission takes the lane that would have to run the most seats per hour to use up its allowance before its own reset.

When the seat finishes, the reservation stays counted for 90 more seconds and the next reading records what the seat used, which improves later estimates. A reservation that was never released, for example because its run crashed, is dropped only once it has expired and its process is no longer running. A live seat's reservation therefore keeps counting for as long as the seat runs. On Windows AutoBuild cannot tell that a process has died and treats every process as running, so a crashed run's reservation is never dropped. It clears only when its window resets, and for a weekly window that can be days away.

The builder seat is reserved before the item is claimed. When no lane has room, AutoBuild claims nothing. Every later seat reserves on its own, and a seat that finds no lane parks the item with the signature `subscription-admission`. A lane whose reading is at a stop level is cooled for usage until its reset, while room taken by reservations alone never cools a lane. When every lane reaches its stop level, the campaign ends with `usage_stop`. Each decision is recorded as a `subscription.admission` event with the full assessment of every lane.

In a second run of the case, now with routing on, both lanes have the same room left in every window. The Claude weekly reset is two days away and the Codex one is six days away. Claude must use its allowance sooner, so the builder for `ITEM-7` is admitted on `claude-code`.

### Read the state with `subscription-status`

Run this command to see what admission would decide now:

```
autobuild subscription-status
```

It reads the state for every seat role, and it claims nothing and neither creates nor changes the ledger. It prints one line per lane, then the lane it recommends for the builder. Later in the week, with the Codex weekly window nearly used, the case prints:

```
lane claude-code: available (within all applicable windows)
lane codex: unavailable (weekly_all:codex has insufficient unreserved allowance)
recommendation: claude-code
```

The text in brackets after `unavailable` is the reason admission refused the lane. `weekly_all:codex has insufficient unreserved allowance` means that the Codex weekly window, after the reservations of running seats, has no room left for one more seat's estimate. A lane that failed its sign-in check gets a line of its own with the check's reason. Any text that contains `/` or `\` is replaced with `Local diagnostic withheld; see the repository-relative evidence reference.`

Pass `--json` for the full state, with the schema `autobuild.subscription-status.v1`. Under `lanes`, each lane has `lane`, `eligible`, `reason`, `score` and `windows`, and `usage_reset_at` when it is at a stop. Each window has `key`, `kind`, `used_percent`, `resets_at`, `stop_percent`, `reserved_percent` and `estimated_seat_percent`. `available` gives the recommended lane for each seat role, and `roles` gives the full assessment for each. The command exits 0 when it recommends a builder lane and 1 when it recommends none. On any error it exits 2 and prints only `AutoBuild subscription status failed`, and a profile without routing is one such error.

### A refusal, and when to try again

Under routing the launch ignores `on_probe_failure` and refuses when admission finds no lane for a builder. When every lane was read and none has room, the refusal is `no capable subscription has allowance for a builder`. When a meter could not be read, the refusal names it and says when to try again, in this form:

```
no usage meter could be read: <lane> (<meter id>): <error>; try again at <time>
```

When one meter could not be read and the others left no room, the refusal starts `no capable subscription has allowance for a builder; a usage meter could not be read:` and goes on in the same form. For a rate-limited meter the time is the meter's own next read, so a launch before it would only be refused again. For an expired sign-in the end reads `try again once the sign-in is refreshed`: run Claude Code once to refresh it, then launch. `autobuild preflight` makes the same judgement read-only, without changing the lane state or the ledger, so you can run it first.

## Review one commit with `subscription-review`

This part describes a command that needs a profile with subscription routing.

Sometimes you want one independent review of one commit outside a campaign, for example of a change you made by hand. `autobuild subscription-review` reviews one commit against one brief, on one reviewer lane that admission reserves.

Find the full commit SHA with `git rev-parse HEAD`, then pass it with the brief. With the SHA that command printed in place of the one shown here:

```
autobuild subscription-review --commit 3f9a1c07d2b84e6a5c1f0e9d8b7a6c5d4e3f2a1b --brief docs/briefs/item-7.md
```

The commit must be a full, lowercase, 40-character SHA that names a commit in the repository. AutoBuild checks it out in a separate, detached worktree under the scratch root and removes that worktree afterwards. The reviewer gets the commit's patch against its first parent, up to 1 MiB, inside its instructions. It has no tools, so it runs no tests. It runs with the chosen lane's reviewer model and `reviewer_effort`, and `--timeout-seconds` sets its time limit, 900 seconds by default.

The command prints `<verdict> via <lane> (<model>)`, for example `pass via codex (gpt-5.6-sol)`. With `--json` it prints `commit`, `verdict`, `lane`, `model` and `receipt_id`. It exits 0 only for `pass`, 1 for any other verdict and 2 on a failure. A known failure prints `AutoBuild subscription review failed:` and its reason, for example `review commit must be a full 40-character SHA`, `review patch exceeds the 1 MiB evidence limit` or `no subscription reviewer lane is available`. Any other failure prints one fixed sentence with no local path.

The receipt is kept at `subscription-reviews/receipts/<receipt_id>.json` under the scratch root. It holds the time, the lane, the model, the effort, the verdict, the commit, and each finding with its code, consequence, evidence reference, whether it blocks, and its specialist boundary. A finding keeps an evidence reference only when it names a file inside the reviewed tree, and finding text that contains `/` or `\` is replaced with a fixed sentence. The receipt does not hold the patch, the brief or the reviewer's transcript. The review writes nothing to the tracker and delivers nothing, so a `pass` tells you what one reviewer concluded from the patch, and acting on it is your decision.

## Limits

The meter reads an allowance. It does not count tokens or money, so it cannot promise a bound on what a campaign spends. The reasons follow, and each says which run it applies to.

- In both runs, a reading can be up to 60 seconds old, and up to 90 seconds old under routing. AutoBuild checks the allowance before a seat starts, never during it, so a long seat can carry a lane past its stop level.
- Under routing, admission's estimates are increases observed in earlier readings, kept as percentages of an allowance. Your own work in the same subscription during a seat changes the readings and the estimates.
- Without routing, AutoBuild does not check which account or billing a lane uses, and only the Claude Code lane is read. The list under "Spending beyond your subscriptions" gives each route.
- Without routing, under `on_probe_failure = "allow"`, a seat starts without a reading when the meter fails before the warning level.
- In both runs, the two allowance surfaces are undocumented and may change or stop working without notice. When they change, readings fail, and the rules above for failed readings decide what happens.
- Under routing, a ChatGPT credit balance counts as neither paid use nor a stop. Whether credits are drawn is decided by the provider, and AutoBuild neither controls it nor reports it.
- Under routing on Windows, a crashed run's reservation keeps counting until its window resets.
