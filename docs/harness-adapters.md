# Harness adapters

Coding assistants expose different commands, permission controls, output formats and authentication checks. An automated build becomes hard to trust when those differences change the build process itself.

AutoBuild sends the same typed seat request to every coding assistant. A small adapter translates that request into one command and converts the result back into the same builder or reviewer record. The build sequence does not know which command handled the work.

## Adapter sequence

1. AutoBuild creates a fresh builder request for one tracked item and its isolated workspace.
2. The selected adapter starts its command with the approved model, tools, paths and timeout.
3. The builder leaves its product changes uncommitted so validation can inspect the final workspace state.
4. AutoBuild creates a fresh reviewer request containing the brief, diff and validator evidence. It does not include the builder transcript.
5. The adapter returns the same typed verdict and usage record whichever command ran the seat.

## Request and result flow

The operator approves the item, model classes and tool policy before the campaign starts. The policy is readable in the runtime profile and can be changed before a run. AutoBuild freezes the selected adapters after their executable, version and authentication probes pass.

The workflow renders the builder instructions once. They name the approved brief, acceptance criteria, workspace and result contract. The adapter receives those finished instructions through a typed request (`SeatRequest`).

The adapter maps the abstract model class to the command's model name. It also maps semantic tools such as `read`, `write`, `python` and `git` to the command's permission flags. An unknown tool fails before the command starts.

The host command adapter starts the process and captures both output streams under the active temporary work root. It owns quoting, environment inheritance, timeout, cancellation and process tree termination. The harness adapter never creates a second process runner.

The command must return one of two small JSON contracts. A builder returns a summary. A reviewer returns a disposition and concrete findings. AutoBuild writes a normalised JSON record and keeps the raw command output as diagnostic evidence.

## Seat safeguards

- Every seat receives a new session identifier and cannot resume a previous conversation.
- Review seats receive read-only tools and a read-only sandbox where the command supports one.
- The reviewer receives the brief, diff and validation evidence. Builder transcripts stay outside the review pack.
- The policy gateway rejects undeclared tools, paths outside the workspace and timeouts above the approved ceiling.
- Every child process receives `TMPDIR`, `TEMP`, `TMP` and package caches under the active temporary work root. The standard system temporary directory is the default. An operator can supply another root for a machine that needs it.
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
  "evidence_ref": "Reference supplied by the command, or an empty string"
}
```

Blocking decisions use `correct`, `escalate` or `park` and require at least one finding. Each finding carries a code, concrete consequence and evidence reference. A specialist boundary is optional.

### Command mappings

| Adapter | Programmatic command | Fresh context and permissions | Result handling |
|---|---|---|---|
| Claude Code | `claude --print` | New session id, no persistence, safe mode, explicit tools, `dontAsk` permission mode | `--output-format json` with `--json-schema` |
| Codex | `codex ... exec` | Ephemeral session, ignored project rules, `never` approval policy, workspace-write builder or read-only reviewer sandbox | JSONL events, `--output-schema`, and `--output-last-message` |
| GitHub Copilot | `copilot --prompt` | New session id, explicit available and allowed tools, no user questions, no remote export, no custom instructions | `--output-format json` JSONL normalised to the shared contract |

The GitHub Copilot command also uses `--disallow-temp-dir`. The child environment points every temporary and cache path at the active temporary work root.

### Runtime registration

The built-in adapter names are `claude-code`, `codex` and `github-copilot`. Each factory receives the bound host command port, an output directory, an optional command override and a model map. A fourth adapter can register through the same Python entry-point surface without editing the workflow.

### Version compatibility

The executable help text and official command documentation define the supported flags. AutoBuild records the observed CLI version in each run manifest. Install each coding assistant through its vendor's official tooling so its command and supporting runtime stay together.

Official references:

- [Claude Code command-line reference](https://code.claude.com/docs/en/cli-reference)
- [Codex CLI repository](https://github.com/openai/codex)
- [GitHub Copilot CLI programmatic reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-programmatic-reference)
