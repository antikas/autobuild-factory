# Runtime adapter: Claude Code

The protocol in `SKILL.md` and `rules.md` is unchanged here. This file maps its seats and signals onto Claude Code.

## Seats

- A builder or reviewer is a subagent started with the `Agent` tool: `subagent_type: general-purpose`, `model` pinned from the profile (`[models] builder` or `reviewer`), `run_in_background: true`. The prompt is the rendered brief from `templates/`; the agent's fresh context is the blindness guarantee.
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
