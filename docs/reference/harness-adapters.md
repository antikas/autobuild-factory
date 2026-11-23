# Harness adapter reference

This page lists, for each lane, what must be installed, how AutoBuild checks it, the exact command lines of a builder seat and a reviewer seat, the environment those commands receive, and how the lane reports a limit.

A lane is one coding assistant that you have set up for AutoBuild, and a harness adapter is the part of AutoBuild that runs it. The lanes built into AutoBuild are `claude-code`, `codex`, `github-copilot` and `claude-sdk`. The first three are command lanes: each starts its assistant's own command for every seat. The `claude-sdk` lane runs its seats through the Claude Agent SDK. Subscription routing is the mode, turned on with `[subscription_routing] enabled = true` in the profile together with a `[usage]` table, in which AutoBuild spreads seats across Claude and Codex subscriptions by reading each one's allowance before every seat. The [guide to lanes and usage](../lanes-and-usage.md) explains that mode and helps you choose a lane, and the [profile reference](profile.md) gives the rules and defaults of every key named here.

The page opens with two warnings, then the parts shared by every lane, then one section per lane. Every lane section has the same parts in the same order. It starts with install and sign in, the sign-in check, the builder command, the reviewer command and what differs between them. Then come permissions and sandbox, models and efforts, profile keys, limits and cooling, the usage meter, what is not supported, and the changes under subscription routing.

- [Two warnings](#two-warnings)
- [Shared by every lane](#shared-by-every-lane): the lane check, a seat from launch to result, time limits and stalls, a usage stop, review seats, the seat environment, limit signals and cooling, the lane keys of a profile
- Lanes: [`claude-code`](#claude-code), [`codex`](#codex), [`github-copilot`](#github-copilot), [`claude-sdk`](#claude-sdk)
- [Limits shared by the adapters](#limits-shared-by-the-adapters) and [lanes from other packages](#lanes-from-other-packages)

## Two warnings

### A builder runs commands as you

`[policy] allowed_tools` gives a builder all of `read`, `write`, `shell`, `python` and `git` by default, and AutoBuild itself confines no builder to the worktree. A builder can therefore run any command that your user account can run. On `claude-code` and `claude-sdk` the builder gets `Bash`, `Edit`, `Write` and `NotebookEdit` besides the read tools, under `--permission-mode dontAsk` with no sandbox option, so it runs commands without asking. On `github-copilot` it gets `--allow-tool=read,shell,shell(git:*),shell(python:*),write` with `--no-ask-user`, and no sandbox option either.

On `codex` the builder runs with `-a never`, so Codex never asks for approval, and with `-s workspace-write`, Codex's own sandbox mode. Codex defines what that sandbox allows, network access included, and AutoBuild sets nothing inside it, so the only confinement any builder has is this Codex sandbox. AutoBuild checks `[policy] allowed_roots` against its own requests and passes it to no command. The builder's tools come from `[policy] allowed_tools`, and each lane section shows what each name becomes. The restrictions under "Review seats" apply to review seats only.

### Credentials reach the seat

A seat on a command lane starts from a copy of the environment that you started AutoBuild from. Outside subscription routing, AutoBuild removes only `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` from it. Every other credential in that environment therefore reaches the coding assistant and the builder's shell, for example `GITHUB_TOKEN`, `GH_TOKEN`, `SSH_AUTH_SOCK`, cloud provider keys, and `OPENAI_API_KEY`, which reaches the `codex` lane too. Under subscription routing the `claude-code` and `codex` lanes remove many more names, as the environment table below shows. A `claude-sdk` seat with `shell` or `python` can also read the lane's own key file, as that section explains.

## Shared by every lane

### The lane check at launch

A launch, `autobuild preflight`, `autobuild subscription-status` and `autobuild subscription-review` each probe the lanes of the profile before anything else runs. Before any probe, AutoBuild refuses a lane that sets an effort its adapter cannot pass, with `lane <name> sets <seat> effort <level>, which its harness adapter cannot pass (accepted: <levels>)`.

A probe of a command lane first checks that the lane's command is found on `PATH` or as a file, and otherwise reports `<command> executable was not found`. It then runs a version command and a sign-in command, each with a limit of 20 seconds (fixed in the code) and without any model call. A lane is capable when its probe passes and its adapter declares each of the capabilities `fresh-seat`, `cancel`, `typed-result` and `usage`. Every lane built into AutoBuild declares them all. When the probe passes and a capability is missing, the lane's detail reads `missing capabilities: <names>`.

At a launch, an incapable lane cools with the signature `probe`; a signature is the name of the cause that a cooling records. The first capable lane in `run.lanes` is the active lane. When no lane is capable the launch stops with `no capable harness lane: ` followed by each lane and its reason. Outside subscription routing, a lane that failed its lane check at launch stays in the list. Later seats of the same campaign try it again once its cooling ends. Under subscription routing such a lane is left out for the whole run. `autobuild preflight` probes every lane and cools none.

### A seat, from launch to result

A seat is one assistant session that does one job: builder, reviewer or specialist. Its instructions are AutoBuild's prompt followed by the full text of the brief and of every other file the seat is given as context. AutoBuild refuses the seat when any one of those files is larger than 1 MiB, a limit fixed in the code. Every command lane receives the instructions on standard input from a prompt file, never on the command line. The `transport` check of the setup refuses a lane that would put the instructions in an argument, or more than 8192 bytes (fixed in the code) in one argument.

Seats ask for tools by their semantic names `read`, `write`, `shell`, `python` and `git`, and AutoBuild refuses any other name before the command starts. A builder gets the tools in `[policy] allowed_tools`. A seat with no effort level adds no effort option, and a seat with an effort the adapter does not declare is refused before the command starts.

Before the command runs, the seat writes its launch to the run folder: the argument vector, the names of the variables it sets and the names it removes. The [events and records reference](events-and-records.md) covers the run folder. A seat ends in one of these outcomes, checked in this order: `stalled`, `timed_out`, `cancelled`, `failed` (a non-zero exit) and `succeeded`. The first that applies is the seat's outcome. A Claude Code or SDK builder that left its result file is the one exception, described in the `claude-code` section.

The result contract is the JSON object a seat must return. A builder returns `builder-report-v1` with a `summary`, and a reviewer returns `review-verdict-v1` with a `decision`. A seat that succeeds without that object counts as failed for lack of evidence. AutoBuild also finds the JSON object when the assistant wraps it in prose or a code fence. Token counts are read from any of `input_tokens`, `inputTokens` or `prompt_tokens`, and `output_tokens`, `outputTokens` or `completion_tokens`. Cost is read from `total_cost_usd`, `cost_usd` or `cost`. A missing value is reported as `<adapter>:unavailable`.

### Time limits and stalls

Two separate settings stop a seat. The time limit is `run.seat_timeout_seconds`, or the limit of the seat's item class: a named time limit that a brief picks with its `Item class:` line from `run.item_classes` (see the [profile reference](profile.md)). The stall deadline is `run.seat_stall_seconds`, the time a seat may go without visible progress. Both default to 900 seconds. AutoBuild reads the time limit again on every check, and while the machine is under high load each second there adds a second to it, up to twice the limit.

On a command lane, a seat is stalled when its output size, the digest of its worktree and its processor time have all stayed unchanged for the whole stall deadline. AutoBuild samples them at most 60 seconds apart (fixed in the code). It reads processor time on Windows and on Linux, so the stall deadline stops a seat on both. Where processor time cannot be read, a seat is never stopped as stalled. That is the case on hosts without `/proc`, such as macOS, where only the time limit or cancellation stops a seat of a command lane. The `claude-sdk` section describes that lane's own stall check.

To stop a seat on Linux or macOS, AutoBuild sends `SIGTERM` to its process group, waits 2 seconds (fixed in the code), then sends `SIGKILL`. On Windows it runs `taskkill /PID <pid> /T /F`, which ends the whole process tree.

After a seat is stopped, its item parks with the reason `stall:<seat>:<signals>` for a stall and `timeout:<seat>` for a timeout. The one exception is a builder that times out before the item's commit. AutoBuild hands that item to a fresh builder in the same worktree, keeping its claim, up to 2 times (fixed in the code), and a third timeout parks it with `timeout:builder`.

### A usage stop

A usage stop is a lane's allowance reading at its stop level. These rules decide what happens to the seat and to its item:

- Without subscription routing, AutoBuild reads the meter before each seat on a metered lane. When a limit without a model scope is at its stop level, AutoBuild records `usage.stop`, cools the lane until the latest reset of the limits at their stop level, and starts the same seat on the next capable lane.
- When no capable lane is left, the item parks with the reason `<lane>:usage`, where `<lane>` is the last lane in `run.lanes`. With one lane, that happens to the first seat that meets the stop.
- A limit scoped to a model sends only the seat whose model it covers past the lane, and cools nothing. When no other lane is left, the item parks with `<lane>:usage`.
- Under subscription routing, AutoBuild reserves the builder's lane before it claims the item. When no lane has room, it claims nothing: the item ends failed with the reason `subscription-admission` and stays ready in the tracker. A later seat of a claimed item that finds no lane with room parks the item with the reason `subscription-admission`. A lane whose reading is at its stop level cools for usage until its reset.
- When every lane is cooling for usage, the campaign admits no new item, lets running items finish and ends with `usage_stop`. When a cooling has another cause, the campaign holds new admissions with `lanes_exhausted` until the earliest reset.
- At launch, a metered lane at its stop level cools before any claim, and the launch is refused only when every lane is metered and at its stop level.

### Review seats

In a campaign, reviewer and specialist seats get the `read` tool alone, and AutoBuild refuses a review seat that asks for more with `review seats must remain read-only`. The review seat of `autobuild subscription-review` gets no tools at all. Each lane turns a campaign review seat's `read` tool into its own options:

| Lane | Options particular to a review seat |
|---|---|
| `claude-code` | `--tools Glob,Grep,Read --allowedTools Glob,Grep,Read` |
| `codex` | `-s read-only`; under subscription routing, the `factory-review` permission profile described in the Codex section |
| `github-copilot` | `--available-tools=glob,grep,view --allow-tool=read` |
| `claude-sdk` | the same tool list as a Claude Code review seat |
| every lane | the options every seat of the lane gets, shown in the lane's own section |

### The seat environment

The environment of a command lane's seat starts as a copy of the environment that you started AutoBuild from, which AutoBuild has already changed at its start, as the [platforms guide](../platforms.md) describes. AutoBuild then removes the names on the lane's removal list and sets the values below over what is left. Everything else in the environment reaches the seat unchanged. On Windows the names are matched in upper case. The `removed-environment` check of the setup never fails: it warns, by name only, when you have set one of the variables that the lane removes.

| Lane | Removed outside subscription routing | Removed under subscription routing |
|---|---|---|
| `claude-code` | `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` | those two; every inherited name that starts with `ANTHROPIC_`; every name that contains a marker below; the fixed names below |
| `codex` | `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` | those two; `OPENAI_API_KEY`, `OPENAI_API_BASE`, `OPENAI_ORGANIZATION`, `OPENAI_PROJECT`; every name that contains a marker below; the fixed names below |
| `github-copilot` | `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` | the lane cannot be used |
| `claude-sdk` | nothing; four names are set to empty values, as its section shows | the lane cannot be used |

The markers are `API_KEY`, `AUTH_TOKEN`, `ACCESS_TOKEN`, `TOKEN`, `SECRET`, `PASSWORD`, `COOKIE`, `CREDENTIAL`, `BASE`, `PROVIDER`, `ENDPOINT` and `KEY_HELPER`. The fixed names are `OPENAI_API_KEY`, `OPENAI_API_BASE`, `OPENAI_BASE_URL`, `OPENAI_ORGANIZATION`, `OPENAI_PROJECT`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT`, `CLAUDE_CODE_API_KEY`, `CLAUDE_CONFIG_DIR`, `CODEX_MODEL_PROVIDER`, `CODEX_PROVIDER`, `CODEX_API_KEY`, `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX`, `CLAUDE_CODE_USE_FOUNDRY`, `SSLKEYLOGFILE`, `SSH_AUTH_SOCK` and `GIT_ASKPASS`. Because the match is on part of the name, a builder's own test runs also lose variables such as `DATABASE_URL` and `GITHUB_TOKEN`. A name that holds no marker still reaches the seat: `AWS_ACCESS_KEY_ID` holds none, while `AWS_SECRET_ACCESS_KEY` holds `SECRET` and is removed. These removals apply to seats and probes only; the validator that AutoBuild runs itself loses no variable in either mode.

Every seat of a command lane receives these values, in both modes:

| Variable | Value |
|---|---|
| `TMPDIR`, `TEMP`, `TMP` | `<scratch>/harness/<lane>/scratch/<item folder>/tmp`, one item folder per run and item |
| `PYTEST_ADDOPTS` | the inherited options, with a pytest `--basetemp` added in the same item folder |
| `UV_CACHE_DIR`, `PIP_CACHE_DIR`, `PYTHONPYCACHEPREFIX`, `XDG_CACHE_HOME`, `NPM_CONFIG_CACHE` | folders under the scratch root, shared by the items of a campaign |
| `NO_COLOR` | `1` |
| `CLAUDE_UNATTENDED` | `1` |
| telemetry variables | the lane's own, listed in its section |

The item folder is the cleaned item id followed by a hash of 12 characters (fixed in the code) made from the run id and the name. The `telemetry` check of the setup fails a lane that declares no variable to switch telemetry off, or whose seat would not carry it.

### Limit signals and cooling

A lane signal is a rate limit, a quota or a failed sign-in that a seat reports. AutoBuild looks for one only in a seat that ended `failed`, so a seat that succeeded, stalled, timed out or was cancelled never cools a lane. A failed seat with no whole JSON document on either output stream gives the signal `spawn`. Otherwise AutoBuild reads the code from the keys `code`, `type`, `error_type`, `error_code` and `reason`, at the top level or in a nested `error` object. It reads them in a JSON document that fills the stream or a whole line, with standard output first. Prose that mentions a limit never counts, and neither does JSON quoted inside prose. A failed seat whose JSON carries no limit code gives no signal and cools nothing.

| Code values | Signal |
|---|---|
| `rate_limit`, `rate_limit_error`, `rate_limit_exceeded`, `rate_limited`, `ratelimited`, `overloaded`, `overloaded_error`, `usage_limit_reached`, `429` | `rate_limit` |
| `insufficient_quota`, `quota_exceeded`, `quota`, `billing_hard_limit_reached` | `quota` |
| `authentication_error`, `invalid_api_key`, `unauthorized`, `401` | `auth` |

The reset time comes from `reset_at`, `resets_at` or `reset_time` in the same object or its nested `error`. A signal cools the lane, records `lane.cooled`, and runs the same seat again on the next capable lane. The cooling lasts until the reset time when that is in the future, and otherwise for `run.lane_cool_seconds`, 3600 by default. Coolings are kept in `lanes.json` in the lane state root, the folder set by `run.lane_state_root` and the scratch root by default. Campaigns that share that folder see each other's coolings.

When no capable lane is left, the item parks with the reason `<lane>:<signature>` and its outcome is flagged `lanes_exhausted`. The claim is made as `builder@<starting lane>`, and a builder that moves to another lane records `item.lane_changed` without a second claim. A seat runs entirely on one lane, with that lane's model and effort for its role.

### The lane keys of a profile

A profile names its lanes in one of two forms. The single-lane form sets `run.harness`, the models and efforts in `[models]`, and optionally `[harness] command`, which replaces the lane's command and its fixed leading arguments. The lane form lists the lanes in `run.lanes`, in the order you prefer, with one `[lanes.<harness>]` table for each:

| Key | Lanes | Meaning |
|---|---|---|
| `builder`, `reviewer` | all | the model name for that seat (required) |
| `specialist` | all | the specialist model, the reviewer model when absent |
| `builder_effort`, `reviewer_effort`, `specialist_effort` | all | one of `low`, `medium`, `high`, `xhigh`, `max`; an absent `specialist_effort` takes `reviewer_effort` |
| `key_file` | `claude-sdk` | the file that holds the lane's API key, relative to the profile's folder; the other lanes accept the key and ignore it without a warning |
| `prepare_argv` | `codex` only | a command run in the worktree before every writable builder seat |

A lane table that `run.lanes` does not list stays inactive: AutoBuild checks the values it holds and never probes it. `--harness <name>` moves a listed lane to the front for one launch, and refuses a name that `run.lanes` does not list. A harness command, from `[harness] command` or `--harness-arg`, reaches the lane only when the profile has exactly one lane. The single-lane form has no `key_file`, so `claude-sdk` works only in the lane form. Every lane passes its model names to its assistant without checking them, except under subscription routing. This fragment sets up two lanes with the model names that subscription routing also accepts:

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

## `claude-code`

**Install and sign in.** Install Claude Code so that the `claude` command is on `PATH`, and sign in with it. On Windows, when `claude` resolves to a `.cmd` or `.bat` npm shim, AutoBuild runs the package's `bin/claude.exe` in its place, or `node cli.js` when only that is present.

**The sign-in check.** AutoBuild runs `claude --version` and then `claude auth status --json`. The lane is capable when both commands exit 0 and the JSON of the second has `loggedIn` or `authenticated` set to `true`. The failure details are `claude-code version probe failed`, `Claude Code authentication probe failed`, `Claude Code authentication probe returned invalid JSON` and `Claude Code is not authenticated`. Subscription routing replaces this check, as its paragraph below describes.

**The builder command.** With the default `[policy] allowed_tools`, a builder seat runs this argument vector, shown with placeholders in angle brackets:

```
claude --print --safe-mode --no-session-persistence --permission-mode dontAsk
  --append-system-prompt <builder contract> --session-id <new id> --model <model>
  [--effort <level>]
  --tools "Bash,Bash(git *),Bash(python *),Edit,Glob,Grep,NotebookEdit,Read,Write"
  --allowedTools "Bash,Bash(git *),Bash(python *),Edit,Glob,Grep,NotebookEdit,Read,Write"
  --output-format json --json-schema <builder-report-v1 schema>
```

**The reviewer command.** A reviewer or specialist seat runs:

```
claude --print --safe-mode --no-session-persistence --permission-mode dontAsk
  --append-system-prompt <review contract> --session-id <new id> --model <model>
  [--effort <level>] --tools Glob,Grep,Read --allowedTools Glob,Grep,Read
  --output-format json --json-schema <review-verdict-v1 schema>
```

**What differs.** The two differ in the tool list, the result schema, and the contract text in `--append-system-prompt`. The builder's contract also tells it to write its result to `.autobuild-seat-result.json` at the root of the worktree. When the command ends in error, stalls, times out or is cancelled and that file exists, the seat counts as succeeded with that result, and AutoBuild removes the file in every case, so it never reaches the change. In strict mode, the review procedure that `[process] mode = "strict"` selects, the builder carries AutoBuild's test hook, which checks the tests the builder runs. That builder drops `--safe-mode`, runs with `--setting-sources ""`, `--strict-mcp-config` and `--disable-slash-commands`, and ends its vector with `--settings` holding the hook. AutoBuild refuses a vector that carries a hook and `--safe-mode` together, because safe mode switches every hook off.

**Permissions and sandbox.** `--permission-mode dontAsk` lets the seat use its listed tools without asking. `--tools` and `--allowedTools` limit the seat to the tools listed. `--safe-mode` switches off the user's and the project's own Claude Code customisations, hooks included. The lane passes no sandbox option. The semantic tools map to Claude Code tools as follows, and the list is sorted and joined with commas:

| Semantic tool | Claude Code tools |
|---|---|
| `read` | `Read`, `Glob`, `Grep` |
| `write` | `Edit`, `Write`, `NotebookEdit` |
| `shell` | `Bash` |
| `python` | `Bash(python *)` |
| `git` | `Bash(git *)` |

Every seat also receives `DO_NOT_TRACK=1`, `DISABLE_TELEMETRY=1`, `DISABLE_ERROR_REPORTING=1` and `MAX_STRUCTURED_OUTPUT_RETRIES=5`.

**Models and efforts.** The model name goes to `--model` as you wrote it, and outside subscription routing the lane keeps no list of models. The examples in this documentation use `claude-opus-5-5`, the one model that subscription routing accepts on this lane. The lane accepts all five effort levels and passes them as `--effort <level>`.

**Profile keys.** `[lanes.claude-code]` takes `builder`, `reviewer`, `specialist` and the three effort keys. In the single-lane form, `[harness] command` can replace `claude`.

**Limits and cooling.** The lane reads limit codes from `subtype` as well as the shared keys. After the code keys, it reads an integer `api_error_status` on a result that has `is_error` set to `true`, so an API status of 429 or 401 gives a signal with the detail `api <status>`.

**The usage meter.** The lane always declares the meter `claude-subscription`, and AutoBuild reads it only when the profile has a `[usage]` table. The meter reads the Claude subscription allowance from an endpoint that is undocumented and may change or stop working without notice. It needs `curl` on `PATH` and Claude Code's own sign-in. AutoBuild never prints the sign-in or stores it.

Readers that use the same scratch root share one reading per meter for 60 seconds (fixed in the code), a failed reading included. When the endpoint answers with a rate limit, the shared reading holds that answer until the time its `retry-after` gives. When it gives none, the hold is 300 seconds, and it always lasts at least 60 seconds; both figures are fixed in the code. A reading can also fail with `sign-in expired: run Claude once in this venue to refresh it`; run Claude Code once on that machine, as the message says. A limit can be scoped to a model by a label, such as a model family name. It covers a seat when every word of the label appears in the seat's model name, and the label's digits are ignored.

**Not supported.** Under subscription routing the lane accepts one model only. A limit that Claude Code reports only in its prose output is never read.

**Under subscription routing.** Every seat must use `claude-opus-5-5`. Another model makes the probe report `Claude subscription lane names a non-included model tier`, and a seat with another model is refused before it starts. A `[harness] command` must be exactly `claude`. The sign-in check changes to these steps, in order:

1. AutoBuild reads the Claude meter through the shared reading at the scratch root, and a failed reading fails the check.
2. The reading must report extra usage as explicitly off, or the check fails with `Claude extra usage is not explicitly disabled`.
3. AutoBuild runs, through a small helper of its own, `claude --setting-sources "" --strict-mcp-config --disable-slash-commands auth status --json`. Its JSON must have `loggedIn` set to `true`, `authMethod` set to `claude.ai`, `apiProvider` set to `firstParty`, and `subscriptionType` set to `pro` or `max`. Otherwise the detail is `Claude is not signed in through the first-party subscription`. A non-zero exit gives `Claude subscription authentication probe failed`, and output that is not JSON gives `Claude subscription authentication probe returned invalid JSON`.

A reading can fail because the meter answered with a rate limit or did not answer. In the sign-in check and in the admission of each seat, AutoBuild then uses the last good reading in its place. That reading must be at most 90 seconds old (fixed in the code), and every limit in it must be below `warn_percent`. The lane's detail then ends with `judged on the reading of <time>`. When no reading can be used, AutoBuild does not cool the lane, and a launch with no other capable lane stops with `no usage meter could be read: claude-code (claude-subscription): <error>; try again at <time>`. The environment table above lists the variables the lane's seats and probes lose.

## `codex`

**Install and sign in.** Install the Codex CLI so that the `codex` command is on `PATH`, and sign in with it.

**The sign-in check.** AutoBuild runs `codex --version` and then `codex login status`. The lane is capable when the second command exits 0 and its standard output does not contain `not logged`, in any letter case. The failure detail is `Codex is not authenticated`.

**The builder command.** A builder seat that has any of `write`, `shell`, `python` or `git`, as it does with the default `[policy] allowed_tools`, runs:

```
codex -a never -s workspace-write -C <worktree> -m <model>
  [-c model_reasoning_effort=<level>]
  exec --ephemeral --ignore-rules --json --output-schema <builder-report-v1 schema file>
  -o <last message file> -
```

**The reviewer command.** A reviewer or specialist seat runs:

```
codex -a never -s read-only -C <worktree> -m <model>
  [-c model_reasoning_effort=<level>]
  exec --ephemeral --ignore-rules --json --output-schema <review-verdict-v1 schema file>
  -o <last message file> -
```

**What differs.** The two differ in the sandbox mode and the result schema. A builder whose tools are `read` alone also gets `-s read-only`.

**Permissions and sandbox.** `-a never` means that Codex never asks for approval. `-s workspace-write` and `-s read-only` are Codex's own sandbox modes, and `-C` points Codex at the worktree. Codex defines what each sandbox mode allows, network access included. The lane has no per-tool mapping: `[policy] allowed_tools` decides only which sandbox mode the builder gets. Every seat also receives `DO_NOT_TRACK=1` and `OTEL_SDK_DISABLED=true`.

**Models and efforts.** The model name goes to `-m` as you wrote it, and outside subscription routing the lane keeps no list of models. The examples in this documentation use `gpt-5.6-terra` and `gpt-5.6-sol`, the two models that subscription routing accepts on this lane. The lane accepts all five effort levels and passes them as `-c model_reasoning_effort=<level>`.

**Profile keys.** `[lanes.codex]` takes `builder`, `reviewer`, `specialist`, the three effort keys and `prepare_argv`. The `prepare_argv` command runs in the worktree before every writable builder seat, with the seat's environment and time limit and no stall deadline. A non-zero exit or a timeout stops the seat with `Codex prepare command failed before builder admission`. In the single-lane form, `[harness] command` can replace `codex`.

**Limits and cooling.** The lane reads the shared code keys and reset keys from Codex's JSON events.

**The usage meter.** Outside subscription routing the lane declares no meter, so usage levels never cool it. Under subscription routing it declares the meter `chatgpt-subscription`. AutoBuild reads that meter by starting `codex app-server --stdio` from `PATH`, whatever `[harness] command` says, and asking it for the account and its rate limits within 20 seconds (fixed in the code). This usage surface of the Codex app server is undocumented and may change without notice. A reading succeeds only when all of these hold:

| Condition | Failure detail when it does not hold |
|---|---|
| The account is a ChatGPT account | `Codex is not signed in through ChatGPT` |
| The plan is `plus`, `prolite`, `pro`, `promax`, `team`, `business` or `enterprise` | `Codex ChatGPT plan is not an included subscription` |
| Ordinary included usage is allowed | `ordinary included Codex usage is unavailable` |
| No rate limit and no spend control has been reached | `Codex usage is paid, stopped, or malformed` |
| A weekly window is present, each window lasts 300 or 10080 minutes with a percent from 0 to 100, no window repeats, and each reset is in the future | `no included rate-limit windows`, `missing weekly rate-limit window`, `duplicate rate-limit windows`, `invalid rate-limit window` or `rate-limit reset is not in the future` |

A window of 300 minutes is a `session` limit and a window of 10080 minutes is a `weekly_all` limit, and both carry the scope `codex`. `autobuild usage` does not read this meter; `autobuild subscription-status` shows it.

**Not supported.** The lane carries no strict test hook. Its meter exists only under subscription routing, and always runs the `codex` found on `PATH`.

**Under subscription routing.** Every seat must use `gpt-5.6-terra` or `gpt-5.6-sol`. Another model makes the probe report `Codex subscription lane names a non-included model tier`, and a seat with another model is refused before it starts. A `[harness] command` must be exactly `codex`. The sign-in check becomes a reading of the `chatgpt-subscription` meter through the shared reading at the scratch root, and the lane is capable only when that reading succeeds. When it fails, AutoBuild does not cool the lane, and the refusal names the meter and when to try again. In the admission of each later seat, AutoBuild can use the last good reading in place of one that was rate limited or got no answer, on the same terms as the Claude Code lane.

Every seat adds `--ignore-user-config`. A reviewer or specialist seat drops `-s read-only` and runs under the permission profile `factory-review`. That profile can read the minimal system paths, the workspace and the Codex runtime, and it switches off the network, web search, apps, multiple agents and hooks. A review seat with no tools also gets `--disable shell_tool --disable unified_exec`. The environment table above lists the variables the lane's seats and probes lose.

## `github-copilot`

**Install and sign in.** Install the GitHub Copilot CLI so that the `copilot` command is on `PATH`. Then either set one of `COPILOT_GITHUB_TOKEN`, `GH_TOKEN` or `GITHUB_TOKEN`, or install the GitHub CLI and sign in with it.

**The sign-in check.** AutoBuild runs `copilot version`. The lane then passes at once when any of `COPILOT_GITHUB_TOKEN`, `GH_TOKEN` or `GITHUB_TOKEN` is set, and AutoBuild does not check the token. Without one of them, `gh auth status` must exit 0, so the lane then also needs the `gh` command. A missing `gh` gives `gh executable was not found`, and a failing one gives `GitHub Copilot is not authenticated`.

**The builder command.** With the default `[policy] allowed_tools`, a builder seat runs:

```
copilot -C <worktree> --prompt - --model=<model> [--reasoning-effort=<level>]
  --session-id <new id>
  --available-tools=apply_patch,bash,create,edit,glob,grep,list_bash,list_powershell,powershell,read_bash,read_powershell,stop_bash,stop_powershell,view,write_bash,write_powershell
  --allow-tool=read,shell,shell(git:*),shell(python:*),write
  --no-ask-user --no-auto-update --no-custom-instructions --no-experimental
  --no-remote --no-remote-export --disable-builtin-mcps --disallow-temp-dir
  --no-bash-env --no-color --stream=off --output-format=json --log-dir=<folder>
```

**The reviewer command.** A reviewer or specialist seat runs the same vector with `--available-tools=glob,grep,view --allow-tool=read`.

**What differs.** The two differ only in their tool lists. The lane passes no result schema to the command, and AutoBuild reads the typed result from its JSON output.

**Permissions and sandbox.** `--available-tools=` and `--allow-tool=` carry the seat's tools in Copilot's own names, and Copilot defines what each of those options permits. Every seat gets `--no-ask-user`. The lane passes no sandbox option. The semantic tools map to Copilot tools as follows; each list is sorted and joined with commas, and a name that several semantic tools give appears once.

| Semantic tool | In `--available-tools=` | In `--allow-tool=` |
|---|---|---|
| `read` | `view`, `glob`, `grep` | `read` |
| `write` | `apply_patch`, `create`, `edit` | `write` |
| `shell` | `bash`, `powershell`, `list_bash`, `list_powershell`, `read_bash`, `read_powershell`, `stop_bash`, `stop_powershell`, `write_bash`, `write_powershell` | `shell` |
| `python` | `bash`, `powershell` | `shell(python:*)` |
| `git` | `bash`, `powershell` | `shell(git:*)` |

For telemetry every seat receives `DO_NOT_TRACK=1` only.

**Models and efforts.** The model name goes to `--model=` as you wrote it, and the lane keeps no list of models. The lane accepts all five effort levels and passes them as `--reasoning-effort=<level>`.

**Profile keys.** `[lanes.github-copilot]` takes `builder`, `reviewer`, `specialist` and the three effort keys. In the single-lane form, `[harness] command` can replace `copilot`.

**Limits and cooling.** The lane reads the shared code keys only.

**The usage meter.** The lane declares no meter, so usage levels never cool it.

**Not supported.** The lane gives the command no result schema and no sandbox option, and it carries no strict test hook. Its sign-in check accepts any value in a token variable, and it cannot be used under subscription routing.

**Under subscription routing.** A profile with subscription routing refuses this lane.

## `claude-sdk`

**Install and sign in.** Install AutoBuild with its `sdk` part, which adds `claude-agent-sdk>=0.2.168,<0.2.169`:

```
pip install "autobuild-factory[sdk]==0.12.0"
```

This lane runs on an API key, which it reads from the file that `key_file` names in `[lanes.claude-sdk]`. AutoBuild never prints the key or stores it. The adapter runs inside the campaign process, and the SDK starts its bundled Claude Code command as a child process, which runs the tools. AutoBuild imports the SDK only when the lane is probed or runs a seat, so a profile without this lane needs no extra part.

**The sign-in check.** The probe makes no API call, so a wrong key passes it and fails only at the first seat. It fails with one of these details:

| Detail | Cause |
|---|---|
| `the claude-sdk lane table sets no key_file` | `key_file` is not set |
| `the key file path contains a shell quoting character: <path>` | the path contains `"`, `$` or a backtick |
| `the API key file is missing: <path>` | the file does not exist |
| `the API key file is unreadable: <path>` | the file cannot be read, or is not UTF-8 text |
| `the API key file is empty: <path>` | the file is empty after its whitespace is removed |
| `the claude_agent_sdk package is not installed; install autobuild-factory[sdk]` | the `sdk` part is not installed |
| `the Agent SDK probe failed: <exception type>` | any other error |

On success the detail reads `API key file present; no OAuth credential is read`.

**The builder command.** AutoBuild builds each seat from the Claude Code builder vector above and passes it to the SDK as options. It refuses a seat with an option the SDK cannot carry. The seat runs with `--no-session-persistence`, `--safe-mode` when it carries no hook, `permission_mode` set to `dontAsk`, no setting sources and all hooks switched off, and with the Claude Code builder's tool list.

**The reviewer command.** A reviewer or specialist seat is built the same way from the Claude Code reviewer vector, with the tools `Glob,Grep,Read`.

**What differs.** As on the Claude Code lane, the two differ in the tool list, the result schema and the contract text, and a strict builder carries AutoBuild's test hook with hooks allowed again for that hook and without safe mode. A builder's result file makes a seat that stalled, timed out or was cancelled count as succeeded, as on the Claude Code lane.

**Permissions and sandbox.** The seat's settings deny the rule `Read(<key file>)`, which covers the Read tool only, so a seat with `shell` or `python` can still read the key file through Bash. The lane passes no sandbox option. AutoBuild builds the seat's environment in these steps, each over the one before, and hands it to the SDK:

1. `TMPDIR`, `TEMP`, `TMP`, `UV_CACHE_DIR`, `PIP_CACHE_DIR`, `PYTHONPYCACHEPREFIX`, `XDG_CACHE_HOME`, `NPM_CONFIG_CACHE` and `NO_COLOR=1`.
2. `DO_NOT_TRACK=1`, `DISABLE_TELEMETRY=1`, `DISABLE_ERROR_REPORTING=1` and `MAX_STRUCTURED_OUTPUT_RETRIES=5`.
3. The item's own `TMPDIR`, `TEMP` and `TMP`, and `PYTEST_ADDOPTS`, over the values of step 1.
4. `CLAUDE_UNATTENDED=1`.
5. A fresh `CLAUDE_CONFIG_DIR` at `<scratch>/harness/claude-sdk/config/<run ref>-<12 hex characters>`.
6. `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL` and `CLAUDE_CODE_OAUTH_TOKEN` set to empty values.

What the SDK's bundled Claude Code command does with that environment, and with the rest of AutoBuild's own, is up to the SDK. The run folder lists the names set without the four blanked ones, and lists those four as removed.

The lane enforces the seat's time limit, its stall deadline and cancellation itself, and checks the time limit every 0.2 seconds (fixed in the code). Progress is an SDK message or a change in the worktree digest, and the lane reads no processor time, so its stall check works the same way on every system. When it stops a seat, it waits 30 seconds (fixed in the code) and then returns the outcome whether or not the seat has stopped.

**Models and efforts.** The model goes to the SDK as `model`, unchecked, and the effort as it would reach the Claude Code command. The lane accepts all five effort levels.

**Profile keys.** `[lanes.claude-sdk]` takes `builder`, `reviewer`, `specialist`, the three effort keys and `key_file`. A relative `key_file` path resolves against the profile's folder. The lane ignores `[harness] command`. The single-lane form cannot set `key_file`, so `run.harness = "claude-sdk"` always fails its probe with `the claude-sdk lane table sets no key_file`.

**Limits and cooling.** The lane reads the top-level fields of the SDK's messages:

| Field | Signal |
|---|---|
| an assistant `error` of `billing_error` | `quota` |
| an assistant `error` of `rate_limit` | `rate_limit` |
| an assistant `error` of `authentication_failed` | `auth` |
| a result with `is_error` and `api_error_status` | from the status; relayed text `credit balance is too low` or `reached your specified (workspace) API usage limits` gives `quota` |

A quota message that says `regain access on YYYY-MM-DD at HH:MM UTC` sets the reset time. A seat stops at the first retry that announces `authentication_failed`, or a status of 401 or 403, and returns `auth`. A seat whose key file has gone fails with `auth` before the SDK starts.

**The usage meter.** The lane declares no meter, and no meter scope covers its seats.

**Not supported.** The lane cannot be used under subscription routing, and it ignores that mode's setting. It has no command, so `[harness] command` and `--harness-arg` do not apply to it.

**Under subscription routing.** A profile with subscription routing refuses this lane when `run.lanes` lists it, and refuses any listed lane that sets `key_file`. An inactive lane table that sets `key_file` is not refused.

## Limits shared by the adapters

AutoBuild states no supported version of any coding assistant's command. It reads the version only to report it, and the seat commands above depend on the options they show. The strict test hook exists only on the `claude-code` and `claude-sdk` lanes. It catches a builder's mistakes, and you should not treat it as a security barrier. A harness command override reaches a lane only when the profile has exactly one lane. Each attempt of a seat runs on one lane from start to end, and after a lane signal AutoBuild starts the seat again on the next lane.

## Lanes from other packages

A package can add a lane through the Python entry point group `autobuild.adapters`. The entry point is either an object with `register(registry)` or a callable that takes the registry. AutoBuild loads entry points after its own lanes on every launch, preflight, status and review. It refuses a second registration of a name with `adapter already registered`, so another package cannot take the name of a lane built into AutoBuild. Such a lane is capable only when its adapter declares the capabilities listed under the lane check above. The [architecture guide](../architecture.md) covers writing one.
