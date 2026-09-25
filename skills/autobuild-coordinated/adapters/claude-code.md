# Runtime adapter: Claude Code

The protocol in `SKILL.md` and `rules.md` is unchanged here. This file maps its seats and signals onto Claude Code.

## Seats

- A builder, reviewer or specialist is a subagent started with the `Agent` tool: `subagent_type: autobuild-seat-<effort>`, `model` pinned from the profile table that supplies the seat's model (`[models]` in the single-lane form, the lane's `[lanes.<harness>]` table in the lane form), `run_in_background: true`. The prompt is the rendered brief from `templates/`; the agent's fresh context is the blindness guarantee.
- Effort resolution: `<effort>` for a builder or reviewer seat is the profile's `builder_effort` or `reviewer_effort`, else `high`; for a specialist seat it is `specialist_effort`, else `reviewer_effort`, else `high`. The seat's effort comes from the same profile table that supplies its model: `[models]` in the single-lane form, or the lane's `[lanes.<harness>]` table in the lane form. Whether the model supports the level is decided by the harness at dispatch. The dispatch uses this profile effort for the seat unless the campaign record names a different effort for that item, matching `rules.md`.
- Install: the five definitions under `agents/claude-code/` install to `.claude/agents/` in your home directory (user level) or the project's `.claude/agents/` (project level); when both hold a copy, the project-level one is used. The coordinator installs the definitions before the first dispatch, replacing an installed copy that differs from the shipped file; when the install creates the agents directory, the owner restarts the session before the first dispatch.
- A correction round or a re-check continues the same seat with `SendMessage` so it keeps its context; a fresh review pass starts a new agent.
- The application's programmatic equivalent reads the brief from standard input: `claude --print --safe-mode --no-session-persistence --permission-mode dontAsk --append-system-prompt <return contract> --session-id <uuid> --model <tier> --tools <list> --allowedTools <list> --output-format json --json-schema <schema> < <brief file>`; use it only when a seat must run outside the session, for example under a scheduler.

## Tool policy

Map the profile's semantic tools to Claude Code tools when a seat must be restricted: `read` to Read, Glob, Grep; `write` to Edit, Write, NotebookEdit; `shell` to Bash; `python` to `Bash(python *)`; `git` to `Bash(git *)`. A reviewer seat is read-only in the lane; its brief forbids every mutating git command and names the scratch copy path for red proofs.

## Signals

- The completion notification for a background agent carries its final report; a seat that stops with an idle wait and no report is finished (stop it with `TaskStop` so its waiters stop firing).
- A background validator run is a `Bash` call with `run_in_background: true`; its output file is read with `grep` for pass lines and the evidence line. Never stop such a run; wait for it (`Wait-Process` on Windows, `until` on POSIX in a script file), see `rules.md`.
- Liveness for a long seat is checked from the lane, not the transcript: the newest file under the lane's build directory or scratch directory, or the process list filtered by the lane path.

## Meter

Read the subscription meter before every seat and after every item, through the `/usage` view or a probe of the same figures. Record session, weekly-all and weekly-model percentages in the campaign record with each row.

## Scratch

The harness names a scratchpad directory on the system drive in every agent's system prompt. Every brief forbids it by name and gives the scratch root on the build drive instead; `scripts/audit_scratch.sh` runs after every item because the prohibition lowers the rate of violations without zeroing it.

## Records

Tracker commands are as in `SKILL.md`; the coordinator runs them in the main tree.
