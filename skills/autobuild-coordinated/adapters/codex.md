# Runtime adapter: Codex

The protocol in `SKILL.md` and `rules.md` is unchanged here. This file maps its seats and signals onto the Codex CLI.

## Seats

Codex has no in-session subagent tool. A fresh seat is a separate `codex exec` process started from the coordinator's shell, the same way the AutoBuild application drives its Codex harness. This command passes no effort, so a seat runs at the effort Codex is configured with, whatever the profile names:

```text
codex -a never -s <workspace-write | read-only> -C <lane-path> -m <tier> exec --ephemeral --ignore-rules --json \
  --output-schema <schema-file> -o <last-message-file> - < <brief file>
```

- A builder runs in the `workspace-write` sandbox; a reviewer in `read-only`. The approval policy is `never`; the session is ephemeral and ignores repository rules, so the brief carries everything.
- The rendered brief is written under the scratch root and piped to standard input (the trailing `-`); the seat's last message is read from the output file and validated against the schema the brief names.
- A correction round is a new process whose prompt quotes the reviewer's finding and names the lane.

## Tool policy

The sandbox is the tool policy: `workspace-write` for builders, `read-only` for reviewers. A read-only seat cannot write anywhere, so a reviewer whose brief requires red proofs on scratch copies runs as `workspace-write` against a copy of the lane the coordinator makes under the scratch root (`-C <scratch copy>`), never against the lane itself; a reviewer whose brief needs no writes runs `read-only` against the lane. `--add-dir` widens a `workspace-write` seat to a further directory and grants nothing to a `read-only` seat.

## Signals

A seat's process exit is its completion; liveness for a long seat is the newest file in its lane or scratch directory. Validator runs are shell processes started from the coordinator's shell; wait for them, never kill them.

## Meter

Codex exposes plan usage in its own interface; record seat count and wall time per item and read the account's usage at the owner's cadence.

## Records

Tracker commands are as in `SKILL.md`; the coordinator runs them in the main tree.
