# Harness adapters

Coding assistants expose different commands, permission controls, output formats and authentication checks. An automated build becomes hard to trust when those differences change the build process itself.

AutoBuild sends the same typed seat request to every coding assistant. A small adapter translates that request into one command and converts the result back into the same builder or reviewer record. The build sequence does not know which command handled the work.

## Adapter sequence

1. AutoBuild creates a fresh builder request for one tracked item and its isolated workspace.
2. The selected adapter starts its command with the approved model, effort, tools, paths and timeout.
3. The builder leaves its product changes uncommitted so validation can inspect the final workspace state.
4. AutoBuild creates a fresh reviewer request containing the brief, diff and validator evidence. It does not include the builder transcript.
5. The adapter returns the same typed verdict and usage record whichever command ran the seat.

## Request and result flow

The operator approves the item, model classes and tool policy before the campaign starts. The policy is readable in the runtime profile and can be changed before a run. AutoBuild freezes the selected adapters after their executable, version and authentication probes pass.

The workflow renders the builder instructions once. They name the approved brief, acceptance criteria, workspace and result contract. The adapter receives those finished instructions through a typed request (`SeatRequest`).

The adapter maps the abstract model class to the command's model name, and the seat's effort level, when the request carries one, to the command's effort option. It also maps semantic tools such as `read`, `write`, `python` and `git` to the command's permission flags. An unknown tool fails before the command starts.

The host command adapter starts the process and captures both output streams under the active temporary work root. It owns quoting, environment inheritance, timeout, cancellation and process tree termination. The harness adapter never creates a second process runner.

The command must return one of two small JSON contracts. A builder returns a summary. A reviewer returns a disposition and concrete findings. AutoBuild writes a normalised JSON record and keeps the raw command output as diagnostic evidence.

## Seat safeguards

- Every seat receives a new session identifier and cannot resume a previous conversation.
- Review seats receive read-only tools and a read-only sandbox where the command supports one.
- The reviewer receives the brief, diff and validation evidence. Builder transcripts stay outside the review pack.
- The policy gateway rejects undeclared tools, paths outside the workspace and timeouts above the approved ceiling.
- Every child process receives `TMPDIR`, `TEMP`, `TMP` and package caches under the active temporary work root. The standard system temporary directory is the default. An operator can supply another root for a machine that needs it.
- Every seat child and every version and authentication probe child loses `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN`, so a subscription seat never bills an API key that happens to be set in the parent environment. The CLI adapter declares the names in `removed_environment` and places them on the command request; the host command adapter removes them from the inherited environment before it adds the scratch variables. Every other inherited variable reaches the child unchanged. The preflight records a warning that names each removed variable set in the parent, never its value.
- A successful process with missing or malformed result JSON is an evidence failure.
- Usage is reported when the command supplies it. Missing usage remains explicitly unavailable.

AutoBuild accepts a seat only when these checks pass. A command name or a successful exit code is not enough.

## Responsibility boundaries

The adapters do not choose work, decide acceptance, run validators, merge branches or update the tracker. Those responsibilities remain in the shared workflow and the other ports.

An adapter reports unavailable when its executable or authentication probe fails.

## Implementation details

### Shared result contracts

Builder result:

```json
{
  "summary": "What changed and why",
  "report_ref": "Reference supplied by the command, or an empty string"
}
```

Reviewer result:

```json
{
  "decision": "pass",
  "findings": [],
  "evidence_ref": "Reference supplied by the command, or an empty string",
  "inspected": [{"path": "src/app.py", "lines": "1-120,300-340"}],
  "acceptance": [{"clause": "A1", "outcome": "met", "evidence_ref": "src/app.py:42"}]
}
```

Blocking decisions use `correct`, `escalate` or `park` and require at least one finding. Each finding carries a code, concrete consequence and evidence reference. A specialist boundary is optional.

`inspected` and `acceptance` are required arrays; an adjudicating specialist usually returns both empty. Each `inspected` entry names a path the seat read and the line ranges it read, or `"all"`. Each `acceptance` entry names a clause number the seat was given (`A1`), its outcome (`met`, `unmet` or `not_checked`) and an evidence reference. Every key of both entries is required and no other key is allowed. A `pass` that reports a clause `unmet` is refused.

### Command mappings

| Adapter | Programmatic command | Fresh context and permissions | Result handling |
|---|---|---|---|
| Claude Code | `claude --print` | New session id, no persistence, safe mode (a strict builder seat: no setting sources, strict MCP configuration and no slash commands instead), explicit tools, `dontAsk` permission mode | `--output-format json` with `--json-schema` |
| Codex | `codex ... exec` | Ephemeral session, ignored project rules, `never` approval policy, workspace-write builder or read-only reviewer sandbox | JSONL events, `--output-schema`, and `--output-last-message` |
| GitHub Copilot | `copilot --prompt` | New session id, explicit available and allowed tools, no user questions, no remote export, no custom instructions | `--output-format json` JSONL normalised to the shared contract |
| Claude Agent SDK | `query()` in process, driving the SDK's bundled Claude Code CLI | The Claude Code seat vector translated to SDK options, no setting sources, a fresh empty configuration directory, hooks disabled (a strict builder seat: AutoBuild's test hook only) | `output_format` JSON schema; the message stream is kept as JSONL |

A builder seat in the strict build process carries a pre-command test check. The Claude Code command adds `--settings` with a `PreToolUse` hook on the Bash tool, and the Claude Agent SDK lane passes the same hook in its flag settings. The hook passes each shell command line to the strict refusal rule and denies a whole-suite test run with the rule's one sentence. The Codex and GitHub Copilot commands carry no such check, so on those lanes the builder brief alone states the rule. See "Build process" in the run guide.

`--safe-mode` switches off every customization, hooks included, so a seat started with it never calls the test hook. A seat that carries the hook is therefore started without `--safe-mode` and isolated by these options instead:

| Option | What it keeps out |
|---|---|
| `--setting-sources ""` | User, project and local settings, and with them the user's and the project's CLAUDE.md, hooks, enabled plugins, custom agents and commands, and output style |
| `--strict-mcp-config` | Every MCP server not passed on the command line; the seat is passed none |
| `--disable-slash-commands` | Skills and slash commands |
| `--settings <json>` | Nothing; it carries the test hook, the only hook the seat runs |

Stated differences from `--safe-mode`: settings from a managed policy still load, as they load for any Claude Code session that an administrator manages. No flag stops a plugin, agent or output style that such a policy enables, and a managed policy that sets `disableAllHooks` or `allowManagedHooksOnly` switches the test hook off without AutoBuild being told. Whether `--strict-mcp-config` also keeps account connectors from claude.ai off a subscription seat is not confirmed. These flags are not known to switch off Claude Code's per-project auto memory, so a strict builder seat may read the memory Claude Code keeps for its working directory. On Claude Code 2.1.316 a settings hook was observed not to fire with `--safe-mode` and to fire with `--setting-sources ""` plus `--strict-mcp-config` and `--disable-slash-commands` (probe of 2026-11-16). That no user or project customization loads under these flags is not yet confirmed by a real strict builder seat. Every seat without a hook (flow builders, reviewers, specialists) keeps `--safe-mode`. The adapter refuses a seat vector that carries a hook in `--settings` together with `--safe-mode`.

The GitHub Copilot command also uses `--disallow-temp-dir`. The child environment points every temporary and cache path at the active temporary work root.

### Seat effort

A seat request can carry an effort level: `low`, `medium`, `high`, `xhigh` or `max`. The level comes from the `builder_effort`, `reviewer_effort` or `specialist_effort` key of the lane the seat runs on. Each adapter passes it through its own command option:

| Adapter | Effort argument |
|---|---|
| Claude Code | `--effort <level>`, placed after `--model` |
| Codex | `-c model_reasoning_effort=<level>`, placed after `-m` and before `exec` |
| GitHub Copilot | `--reasoning-effort=<level>`, placed after `--model` |

A seat request without an effort adds no argument, so the command line is exactly the one the adapter builds without the setting and the harness applies its own configured default.

The seat request and the `seat.completed` run event carry the level AutoBuild requested. Whether the model applies it is decided by the harness when the seat starts: GitHub Copilot CLI 1.0.88 or later reports a level the model does not offer and leaves it unapplied, and Codex passes any value to its service.

Each adapter declares the levels it can pass in `effort_levels`. The three built-in adapters declare all five levels, because each command documents all five. Before any lane is probed, AutoBuild compares every lane's configured efforts with its adapter's declared levels and stops with a configuration error that names the lane and the seat when a level is not declared. An adapter that declares no levels therefore refuses any lane that sets an effort. An adapter that receives an undeclared level refuses the seat before it starts the command. No adapter drops a configured effort.

### Lane failure signals

Each adapter owns the classification of its own failures through `classify_failure`. It reads a lane signal from structural evidence only: the process exit state and the CLI's structured error fields, read from whole JSON documents on both standard output and standard error, with a code on standard output read first. A whole JSON document is a JSON object or array that forms the whole stream or one whole line; JSON after text on a line, and a bare value such as a number, are not documents. A document behind a byte order mark, or a pretty-printed document after a prose line, is not a whole document either, so such a failure is a spawn failure. Inside a document, a string field counts as structured only when its whole text is one JSON object or array, so JSON quoted inside prose in a result field never cools a lane. It never scans the event stream for words. A successful seat, or a seat the harness killed for stalling, timing out or cancellation, never cools a lane, so a report that mentions "rate limit" in prose with a clean exit and a valid result is not a false kill. A failed process that produced no structured document on either stream is a spawn failure and cools the lane.

The structured fields each adapter reads:

| Adapter | Structured limit fields |
|---|---|
| Claude Code | the result object `is_error` with a nested `error.type` (or `subtype`) following the Anthropic error taxonomy, such as `rate_limit_error`, `overloaded_error` and `authentication_error`, and the `api_error_status` on a result with `is_error`, where 429 is a rate limit and 401 is an auth failure |
| Codex | an `error` event or a `turn.failed` event with a nested `error` carrying the OpenAI error `type` or `code`, such as `rate_limit_exceeded`, `insufficient_quota` and `invalid_api_key` |
| GitHub Copilot | an `error` object with a `code` or `type` (or a nested `error`) carrying the GitHub Models limit codes, such as `rate_limited`, `quota_exceeded` and `unauthorized` |
| Claude Agent SDK | top-level message fields only: the assistant message `error` (`billing_error`, `rate_limit`, `authentication_failed`) and a result message with `is_error` and an `api_error_status`, whose relayed API message names an exhausted credit balance or a reached spend limit; message content is never read |

A signal carries a kind (`rate_limit`, `quota`, `auth`, `spawn`) and a reset time when the vendor supplies one. Where a CLI surfaces a limit only in prose and carries no structured field, `classify_failure` returns nothing for that lane and never falls back to prose matching. The one exception is the `claude-sdk` adapter, which matches the API's own error text relayed in a result already marked as an error, as its section below describes. A `usage` signal comes from the usage meter, not from a seat, and is described in "Usage meter".

### Usage meter

An adapter may declare `meter_id`, the opaque id of the usage meter that measures its seats. The Claude Code adapter declares `claude-subscription`; Codex, GitHub Copilot and the Claude Agent SDK declare none, so their lanes are never read or cooled by a meter. The workflow treats the id as an opaque string and reads it through the `UsageMeterPort`, whose Claude adapter reads the subscription meter with curl.

A meter can report a weekly limit scoped to a model, labelled with the meter's own model name. `meter_scope_covers(scope, model_class)` lets the adapter decide whether such a label covers the model a seat runs. The Claude Code adapter maps the seat's model class through its model map and answers true when every word of the label appears in the model name, ignoring version digits, so a `Fable` label covers a `fable` reviewer and not a `claude-opus-5-5` builder. An adapter without a meter covers nothing.

### Claude Agent SDK lane

The `claude-sdk` adapter runs Claude Code seats on an API key instead of a subscription. It runs in the campaign process through the Claude Agent SDK, which starts the SDK's own bundled Claude Code CLI. The SDK is the optional extra `autobuild-factory[sdk]`; AutoBuild imports it only when a `claude-sdk` lane is probed or runs a seat, so a profile without the lane imports nothing from it and the core keeps no runtime dependency.

The seat contract is the Claude Code lane's own. The adapter builds the argument vector the `claude-code` adapter would run for the seat and translates each option into an SDK option: tools and allowed tools, model, effort, permission mode, session id, the contract prompt, the result schema, safe mode and no session persistence. For a strict builder seat it carries the isolating options in place of safe mode: `--setting-sources ""` becomes the SDK's own empty `setting_sources`, and `--strict-mcp-config` and `--disable-slash-commands` reach the CLI through `extra_args`, as `--safe-mode` does for every other seat. The SDK passes `extra_args` to its bundled CLI unchanged, so `--safe-mode` there stops the hook exactly as on the Claude Code lane; the lane refuses a seat whose settings carry a hook while its flags hold safe mode. An option it cannot carry refuses the seat before the SDK starts. The builder result file fallback works as on the Claude Code lane.

The API key never enters an environment, a command line or a file AutoBuild writes. The lane table names the key file with `key_file`, and the adapter passes the SDK's CLI an `apiKeyHelper` command in flag settings that prints the file. The CLI child also gets:

- a fresh, empty `CLAUDE_CONFIG_DIR` under the lane's output directory for every seat, so no user settings, credentials, skills or plugins load;
- no setting sources, `disableAllHooks`, and a `permissions.deny` rule `Read(<key_file>)` so the seat's Read tool cannot open the key file. A builder seat in the strict build process instead carries AutoBuild's own test hook in the same flag settings, with `disableAllHooks` lifted and without `--safe-mode`; with no setting sources it is the only hook the seat runs;
- `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL` and `CLAUDE_CODE_OAUTH_TOKEN` set to empty values. The SDK merges the parent environment into its child and cannot remove a variable, and the bundled CLI treats an empty value as unset.

The helper reads the file as UTF-8 with an optional byte-order mark, strips surrounding whitespace and writes bytes, so the console code page cannot break it. The probe checks that the key file exists and is readable and not empty after stripping, and that the SDK imports. When the API refuses the key, the CLI announces a retry with `authentication_failed`; the adapter stops the seat at the first such retry and returns an `auth` signal, which cools the lane instead of waiting through the CLI's retries. It never reads an OAuth credential and never runs a model. A missing key file fails the probe, which cools the lane; the lane never falls back to subscription credentials. A seat whose key file has gone fails with an `auth` signal before the SDK starts.

The adapter enforces the seat timeout, the stall deadline (no message and no workspace change within `seat_stall_seconds`) and cancellation itself, because no host command runs the seat. Usage comes from the result message's token counts and `total_cost_usd`.

A credit balance that is exhausted and a workspace or organisation spend limit that is reached both cool the lane with the `quota` signal, so the seat moves to the next lane or the item parks with `lanes_exhausted`; neither is a builder failure. When the API names the time access returns, the lane cools until then.

The adapter declares how its seat reaches the child through `seat_transport`, so the preflight checks its transport and telemetry without reading private adapter methods.

### Runtime registration

The built-in adapter names are `claude-code`, `claude-sdk`, `codex` and `github-copilot`. Each factory receives the bound host command port, an output directory, an optional command override and a model map; the `claude-sdk` factory also receives the lane's `key_file`. Another adapter can register through the same Python entry-point surface without editing the workflow. It declares the effort levels it can pass in `effort_levels`, as described in "Seat effort". An adapter built on the shared CLI adapter inherits `removed_environment` and removes the same API-billing variables from its children.

### Version compatibility

The executable help text and official command documentation define the supported flags. AutoBuild records the observed CLI version in each run manifest. Install each coding assistant through its vendor's official tooling so its command and supporting runtime stay together.

Official references:

- [Claude Code command-line reference](https://code.claude.com/docs/en/cli-reference)
- [Codex CLI repository](https://github.com/openai/codex)
- [Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
- [GitHub Copilot CLI programmatic reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-programmatic-reference)
- [GitHub Copilot CLI command reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference)
