---
name: autobuild
description: Run a repository's ready Pinax or BACKLOG.md queue through the released portable AutoBuild workflow. Use for “run the backlog”, “burn down the backlog”, or “autobuild this project”. The skill configures and launches the Python command; it does not contain a second build sequence.
user-invocable: true
---

# AutoBuild

Use the released Python application for the complete campaign. This file is only an invocation shim.

Use the repository passed by the user. A bare invocation means the current repository only. Read its instructions and identify the operational tracker once. Use `pinax status --json` when `.ergon/` exists and Pinax is available. Otherwise check the supported `BACKLOG.md` or `docs/BACKLOG.md` table described in `docs/running-autobuild.md`. The application selects and takes the next item.

Build the run configuration from approved project facts. Prefer a committed `.autobuild.toml`. When it is absent, create a temporary profile from the repository's declared validator and the models available in the current harness. Ask only if those facts are ambiguous. Keep the temporary profile in the caller's permitted temporary area. Do not add it to the project.

Select the harness that is running this skill: `claude-code`, `codex`, or `github-copilot`. Launch:

```text
autobuild run --repository <repo> --profile <profile> --harness <current-harness> --allow-delivery
```

If `autobuild` is not installed, run version 0.2.0 from the public release:

```text
uvx --from git+https://github.com/antikas/autobuild.git@v0.2.0 autobuild run ...
```

Supply `--scratch-root` only when the caller or machine has provided one. With no override, AutoBuild uses the operating system's standard temporary directory.

When the user supplies an approved `autobuild.refill-plan.v1` file, pass it with `--refill-plan`. Do not invent or approve new work as part of an execution request. Refill proposals remain non-runnable in either tracker.

Do not reproduce campaign states, item states, review loops, close logic, Git delivery or tracker write-back in this skill. Configuration and invocation stay here; sequencing and policy stay in the Python package.

Return the application's outcome in plain language: what shipped, what parked, what failed and the reported stop reason. Preserve its run record for diagnosis.
