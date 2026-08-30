# AutoBuild

Coding assistants can work through a backlog, but the result becomes hard to trust when each assistant carries a different build process in its prompt. AutoBuild puts the campaign in one Python application so the same sequence, checks and delivery rules apply whichever supported assistant runs it.

A fresh builder changes one ready item from the project's tracked queue in an isolated Git worktree. The queue can be Pinax or a supported `BACKLOG.md` table. A fresh reviewer sees the approved request, changed files and test evidence without the builder transcript. The application accepts, parks or stops the item, then records enough evidence to judge the outcome without reconstructing a chat session.

The workflow is harness-neutral. Claude Code, Codex and GitHub Copilot sit behind adapters selected at startup; the campaign sequence does not change with the assistant, operating system or shell.

## What you can watch it do

1. Check the selected harness, repository and project test command before claiming work.
2. Build and review one ready item in an isolated worktree.
3. Deliver accepted product and tracker commits to the remote default branch.
4. Continue until the queue is dry, the item limit is reached or required evidence fails.

## What is in the box

- `src/autobuild/`: the cycle and its adapter interfaces
- `src/autobuild/adapters/`: the adapters, one per mechanism
- `src/autobuild/enforcement/`: gates, validators and schemas
- `skills/`: harness entry points
- `tests/`: the test lane

## How to use it

The [setup and run guide](docs/running-autobuild.md) explains installation, harness authentication, item briefs, Pinax and `BACKLOG.md`, the project profile, campaign results, refill and failure handling from a standing start.

Release 0.2.0 is available as a Python wheel and source archive on the [GitHub release page](https://github.com/antikas/autobuild/releases/tag/v0.2.0). The guide also has a [macOS setup path](docs/running-autobuild.md#set-up-autobuild-on-macos) and [GitHub Copilot setup](docs/running-autobuild.md#github-copilot-cli).

Platform and coding assistant are separate choices. You can use Codex on macOS, GitHub Copilot on Windows, or any other supported combination.

The [architecture guide](docs/architecture.md) explains the layers, state machines, ports, adapters, evidence chain, Git delivery model and extension points.

Run the test lane with `uv run --native-tls python -m pytest tests -q`.

The [harness adapter guide](docs/harness-adapters.md) explains how one builder and reviewer contract runs through Claude Code, Codex and GitHub Copilot.

## What keeps it honest

AutoBuild probes its adapters before claiming work, confines changes to an isolated worktree, runs only the declared validator and verifies the remote revision after delivery. An accepted result must carry matching diff, validator, review, product-commit and tracker-commit evidence.

## What this is not

Product decisions, public releases and production deployments stay outside AutoBuild. An optional queue-refill plan records proposed work and unresolved questions. It cannot make a proposal runnable.
