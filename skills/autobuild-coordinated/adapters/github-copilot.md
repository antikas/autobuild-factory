# Runtime adapter: GitHub Copilot

The protocol in `SKILL.md` and `rules.md` is unchanged here. This file maps its seats and signals onto GitHub Copilot CLI and agent mode.

## Seats

Copilot has no in-session subagent tool. A fresh seat is a separate Copilot CLI process started from the coordinator's shell with a custom agent and an explicit prompt, the same way the AutoBuild application drives its Copilot harness. This command passes no effort, so a seat runs at the effort Copilot is configured with, whatever the profile names:

```text
copilot -C <lane-path> --agent autobuild-builder --prompt - --model=<tier> \
  --session-id <uuid> --available-tools=<list> --allow-tool=<list> \
  --no-ask-user --no-auto-update --no-custom-instructions --no-experimental --no-remote \
  --no-remote-export --disable-builtin-mcps --disallow-temp-dir --no-bash-env --no-color \
  --stream=off --output-format=json --log-dir=<scratch>/logs/<seat>
```

- `agents/autobuild-builder.agent.md` and `agents/autobuild-reviewer.agent.md` are the custom agents. Install them to `~/.copilot/agents/` (personal) or a repository's `.github/agents/` (project). The file name without `.agent.md` is the `--agent` value.
- The rendered brief is written to a file under the scratch root and piped to standard input (`--prompt -`); the seat's JSON output is read from its log directory. A correction round is a new process whose prompt quotes the reviewer's finding and names the lane; the seat has no memory between rounds, so the brief carries everything.
- `--disallow-temp-dir` and the child environment (TMPDIR, TEMP, TMP, cache paths) point at the scratch root, as in the application.

## Tool policy

Semantic tools map to Copilot tools on the command line: `read` to view, glob, grep; `write` to apply_patch, create, edit; `shell` to bash, powershell and their list, read, stop and write forms; `python` to `shell(python:*)`; `git` to `shell(git:*)`. In the custom agent files the same policy is declared with Copilot's tool aliases: `read` (view), `search` (glob, grep), `edit` (apply_patch, create, edit) and `execute` (shell). A reviewer seat gets `read`, `search` and `execute`, restricted to non-mutating commands by its brief.

## Signals

- A seat's process exit is its completion; the coordinator reads the JSON output for the report. Liveness for a long seat is the newest file in its lane or scratch directory.
- Validator runs are shell processes started from the coordinator's shell; wait for them, never kill them.

## Meter

Copilot exposes no session meter. Record seat count, wall time and the plan's request budget per item instead, and read the account's usage page at the owner's cadence.

## Scratch

Copilot's temporary directory policy is enforced by `--disallow-temp-dir`; the audit script still runs after every item because tools inside a seat may write elsewhere.

## Records

Tracker commands are as in `SKILL.md`; the coordinator runs them in the main tree.
