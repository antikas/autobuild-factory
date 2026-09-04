# AutoBuild

Coding assistants can work through a backlog, but the result becomes hard to trust when each assistant carries a different build process in its prompt. AutoBuild puts the campaign in one Python application so the same sequence, checks and delivery rules apply whichever supported assistant runs it.

A fresh builder changes one ready item from the project's tracked queue in an isolated Git worktree. The queue can be [Pinax](https://github.com/antikas/pinax-tracker) or a supported `BACKLOG.md` table. A fresh reviewer sees the approved request, changed files and test evidence without the builder transcript. The application accepts, parks or stops the item, then records enough evidence to judge the outcome without reconstructing a chat session.

The workflow is harness-neutral. Claude Code, Codex and GitHub Copilot sit behind adapters selected at startup; the campaign sequence does not change with the assistant, operating system or shell.

## Run sequence

1. Check the selected harness, repository and project test command before claiming work.
2. Build and review one ready item in an isolated worktree.
3. Deliver accepted product and tracker commits to the selected branch mode. Local PR delivery keeps the result on the invoking branch; protected delivery merges and pushes the default branch.
4. Continue until the queue is dry, the item limit is reached or required evidence fails.

## Repository contents

- `src/autobuild/`: the cycle and its adapter interfaces
- `src/autobuild/adapters/`: the adapters, one per mechanism
- `src/autobuild/enforcement/`: gates, validators and schemas
- `skills/autobuild-plan/`: plans, reviews and registers an end-to-end queue, then stops before launch
- `skills/autobuild/`: configures and launches the Python workflow against a ready queue
- `tests/`: the test lane

## Start here

The [setup and run guide](docs/running-autobuild.md) explains installation, harness authentication, item briefs, Pinax and `BACKLOG.md`, the project profile, campaign results, refill and failure handling from a standing start.

AutoBuild has two stages. Use `autobuild-plan` for research, planning, independent review and tracker registration. After the owner approves that result, use `autobuild` to run the queue. [The operating guide starts with the planning stage](docs/running-autobuild.md#stage-1-plan-and-register-the-work).

Release 0.4.0 is available from PyPI as [`autobuild-factory`](https://pypi.org/project/autobuild-factory/) and as a Python wheel and source archive on the [GitHub release page](https://github.com/antikas/autobuild-factory/releases/tag/autobuild-factory-0.4.0). The guide also has a [macOS setup path](docs/running-autobuild.md#set-up-autobuild-on-macos) and [GitHub Copilot setup](docs/running-autobuild.md#github-copilot-cli).

Platform and coding assistant are separate choices. You can use Codex on macOS, GitHub Copilot on Windows, or any other supported combination.

The [architecture guide](docs/architecture.md) explains the layers, state machines, ports, adapters, evidence chain, Git delivery model and extension points.

Run the test lane with `uv run --native-tls python -m pytest tests -q`.

The [harness adapter guide](docs/harness-adapters.md) explains how one builder and reviewer contract runs through Claude Code, Codex and GitHub Copilot.

## Delivery checks

AutoBuild probes its adapters before claiming work, confines changes to an isolated worktree, and runs only the declared validator. It verifies the remote revision for protected delivery or an explicitly pushed current branch. An accepted result must carry matching diff, validator, review, product-commit and tracker-commit evidence.

## Scope and limits

Product decisions, public releases and production deployments stay outside AutoBuild. An optional queue-refill plan records proposed work and unresolved questions. It cannot make a proposal runnable.
