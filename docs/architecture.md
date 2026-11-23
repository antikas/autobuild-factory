# Architecture

You have found something to change in AutoBuild. Perhaps you want it to drive a coding assistant it does not support yet, or to deliver accepted work to git in another way. Before you change anything, find the module that owns the behaviour and the tests that prove it, because the import rules and the documentation tests below fail a change that ignores them.

To add a coding assistant, go to [Extension points](#extension-points). To change how accepted work reaches git, go to [Git delivery model](#git-delivery-model). The sections before them describe the layers and the state that both rely on.

The [README](../README.md) explains what AutoBuild does for a user. The exact command and environment of each lane are in the [harness adapter reference](reference/harness-adapters.md), and the files a run writes are in [events and records](reference/events-and-records.md).

The page uses these terms throughout:

- A campaign is one run of `autobuild run` over the items of one tracker.
- An item is one piece of work in the tracker, with one brief.
- A brief is the written request for one item, kept as a file in the repository.
- A seat is one assistant session with one job: builder, reviewer or specialist.
- A harness adapter is the class that starts one coding assistant for a seat and reads its result.
- A lane is one coding assistant set up for AutoBuild in the profile, driven by one harness adapter.
- The validator is the command named in the profile whose exit code decides whether a change passes.
- A worktree is the separate git working copy in which one item is built.
- A port is a Python protocol in `src/autobuild/ports/contracts.py` through which the application reaches one external dependency.
- An adapter is a class behind a port that talks to one real tool, service or file.
- The composition root is `src/autobuild/bootstrap/composition.py`, which reads the profile, builds one adapter for each port and hands them to the campaign.
- The run folder is the folder of files that a campaign writes about itself, `<scratch root>/runs/<run-id>/`.
- The setup check is the set of launch checks in `enforcement/preflight.py` that run before any claim, and that `autobuild preflight` runs on its own.
- A boundary is a part of the repository named under `[process.boundaries]` in the profile, which gets its own specialist reviewer in strict mode.

## Dependency direction

The code lives under `src/autobuild/`, in these layers:

| Layer | Folder | What it owns |
|---|---|---|
| Domain | `domain/` | Value types, enums and errors that need no outside world, such as effort levels, item states, stop reasons, hold conditions, the review verdict rule, usage levels and delivery park reasons. |
| Ports | `ports/` | The port protocols, in `contracts.py`. |
| Enforcement | `enforcement/` | The rules that check evidence. Among them are the setup checks, the brief checks before a claim and the reader that finds git staging orders in a brief. Others are the policy wrappers, the close gate that refuses to close an item on bad evidence, review depth and review scope, the revalidation decision, and capacity and host pressure. |
| Application | `application/` | The campaign (`campaign.py`), the item workflow (`item.py`), the item state machine (`state_machine.py`), seat instructions (`prompts.py`), progress lines (`progress.py`) and subscription routing (`subscriptions.py`). |
| Adapters | `adapters/` | One module per real tool, such as the lanes, the trackers, git, commands, run folders, leases, usage meters and host readings. |
| Bootstrap | `bootstrap/`, `cli.py`, `__main__.py` | The command line, the profile, the adapter registry and the composition root. |

`tests/architecture/test_dependency_direction.py` enforces these import rules, and a change that breaks one fails the test:

| Layer | May not import |
|---|---|
| `domain` | `autobuild.adapters`, `autobuild.application`, `autobuild.bootstrap`, `autobuild.enforcement`, `autobuild.ports`, `ctypes`, `platform`, `subprocess` |
| `ports` | `autobuild.adapters`, `autobuild.application`, `autobuild.bootstrap`, `autobuild.enforcement`, `ctypes`, `platform`, `subprocess` |
| `enforcement` | `autobuild.adapters`, `autobuild.application`, `autobuild.bootstrap`, `ctypes`, `platform`, `subprocess` |
| `application` | `autobuild.adapters`, `autobuild.bootstrap`, `ctypes`, `platform`, `subprocess` |

Two more rules bind the logic layers. Workflow logic must not depend on a provider or an operating system, so a string literal in `application` may not contain the words claude, codex, copilot, windows, darwin or linux. Code in `application` and `domain` may not read the wall clock, for example through `datetime.now` or `time.time`; it receives a clock instead, and `time.monotonic` and `time.sleep` stay allowed.

`adapters` and `bootstrap` carry no import rule. `application` may import `enforcement`. Some adapters import from the inner layers, for example `adapters/progress.py` imports `application`, and `adapters/git_workspace.py` imports `enforcement`.

## Runtime composition

`cli.py` parses the command line and calls the composition root. Before it builds a single adapter, the composition root refuses a `run` without `--allow-delivery`, and refuses the current-branch options with the default delivery mode.

The composition root builds the lanes in `_build_lanes`. It creates an `AdapterRegistry`, registers the first-party harness adapters with `register_first_party_harnesses` from `bootstrap/builtins.py`, and loads third-party adapters with `load_entry_points`. For each lane in `run.lanes` it calls `registry.create(PortKind.HARNESS, lane.name, config)`. It checks every lane's efforts against the adapter's `effort_levels` before any probe runs. It then probes each lane and checks its `capabilities`. A lane is capable when its probe reports it available and its adapter declares every required capability, and the first capable lane is the active lane.

The registry is used for harness adapters only. The composition root constructs every other adapter directly:

- `_repository_adapters` builds `PinaxTrackerAdapter`, `BacklogTrackerAdapter` and `GitWorkspaceAdapter`, gives them one shared lock on the primary checkout, and picks the tracker from `[tracker] kind` or the probes.
- `_command_adapter` builds `PosixCommandAdapter` or `WindowsCommandAdapter`, chosen by the operating system.
- `_runtime` builds `FilesystemLeaseAdapter`, `FilesystemLaneStateAdapter` and `LocalRunRecordAdapter`, and the knowledge adapter. The knowledge adapter is `KoineKnowledgeAdapter`, which runs the command named by `[knowledge] command`, when the refill plan holds fog records, and `NoRefillKnowledgeAdapter` otherwise. A refill plan is a JSON file, named by `[refill] plan`, whose proposals AutoBuild registers as items that never run when the queue runs dry. A fog record is an open question in that plan, which the knowledge adapter writes to the file named by `[knowledge] fog_ledger`.
- `_usage_meter` builds the usage meter, and `_shared_meter` wraps it in `SharedUsageMeterCache` so that readers on one scratch root share one reading. A meter id is the name a harness adapter gives the meter that measures its seats. When no first-party meter knows the meter id of any lane, the meter is `NullUsageMeterAdapter`. Its readings always fail, so `[usage] on_probe_failure` decides what happens.

`_runtime` probes the adapters in turn, and a failed probe stops the launch with `<name> preflight failed: <detail>`. The names are `tracker`, `workspace`, `command`, `knowledge`, `lease`, `lane_state` and `run_record`.

The composition root also sets the number of items that run at once. `item_concurrency` uses `run.concurrency` when it is an integer. Otherwise it runs a `BACKLOG.md` tracker one item at a time, and for Pinax it calls `derive_capacity` from `enforcement/capacity.py` on a host reading. It reads each brief once with `read_brief` from `enforcement/brief_file.py`. `declared_item_class` from `enforcement/triage.py` reads the brief's optional `Item class:` line, which picks a seat timeout from `[run.item_classes]`.

`_preflight` then runs the setup checks against the active lane. The composition root wraps the ports in the policy layer of `enforcement/policy.py` (`PolicyGateway`, `EnforcedHarnessPort`, `EnforcedCommandPort` and the others), and hands them to `CampaignRunner` in `application/campaign.py`. Under subscription routing, which spreads seats across Claude and Codex subscriptions, it also passes a `SubscriptionAdmissionStore` from `adapters/subscription_admission.py`. That store has no port and is used directly.

## Port contracts

Every port is a `typing.Protocol` in `src/autobuild/ports/contracts.py`. Most extend `Probeable`, whose one method `probe` returns a `ProbeResult`. The fakes come from `autobuild.testing`, described under [Architecture tests](#architecture-tests).

| Port | Methods | Adapters | Fake |
|---|---|---|---|
| `TrackerPort` | `probe`, `next_item`, `ready_items`, `blocked_item_ids`, `known_item_ids`, `resumable_claims`, `claim`, `close`, `park`, `propose`, `recover`, `skipped_items` | `PinaxTrackerAdapter`, `BacklogTrackerAdapter` | `FakeTrackerAdapter` |
| `TrackerProjectionPort` | `is_projection`, `regenerate_projection` (optional) | `PinaxTrackerAdapter` | none |
| `WorkspacePort` | `identify`, `create_isolated`, `list_worktrees`, `worktree_belongs_to`, `adopt_worktree`, `resume_delivery_commits`, `diff`, `progress_digest`, `commit_item`, `commit_tracker`, `snapshot`, `deliver`, `confirm_delivery`, `deliver_report`, `release`, `open_validation_checkout`, `close_validation_checkout` | `GitWorkspaceAdapter` | `FakeWorkspaceAdapter` |
| `HarnessPort` | `probe`, `invoke`, `cancel`, `collect_usage`, `classify_failure` | `ClaudeCodeHarnessAdapter`, `CodexHarnessAdapter` and `CopilotCliHarnessAdapter`, which subclass `CliHarnessAdapter`, and `ClaudeSdkHarnessAdapter` | `FakeHarnessAdapter`, and `FakeAgentSdkClient` for the SDK lane |
| `SeatTransportPort` | `seat_transport` (optional, required of an in-process harness) | `ClaudeSdkHarnessAdapter` | none |
| `CommandPort` | `probe`, `run`, `cancel` | `PosixCommandAdapter`, `WindowsCommandAdapter` | `FakeCommandAdapter` |
| `RunRecordPort` | `probe`, `create`, `append`, `timestamp`, `write_evidence`, `write_evidence_file`, `latest_phase_marker`, `pause_requested`, `clear_prior_pause`, `complete` | `LocalRunRecordAdapter` | `FakeRunRecordAdapter` |
| `KnowledgePort` | `probe`, `retrieve`, `record_fog` | `KoineKnowledgeAdapter`, `NoRefillKnowledgeAdapter` | `FakeKnowledgeAdapter` |
| `ProgressPort` | `begin`, `emit` | `FileProgressAdapter`, `StderrProgressAdapter`, `CommandHookProgressAdapter`, combined by `CompositeProgressAdapter` | `FakeProgressPort` |
| `LeasePort` | `probe`, `acquire`, `renew`, `release`, `live_holder` | `FilesystemLeaseAdapter` | `FakeLeaseAdapter` |
| `LaneStatePort` | `probe`, `active`, `cool`, `clear`, `snapshot` | `FilesystemLaneStateAdapter` | `FakeLaneStateAdapter` |
| `UsageMeterPort` | `probe`, `read` | `ClaudeUsageMeterAdapter`, `CodexSubscriptionMeterAdapter`, `CompositeUsageMeterAdapter`, `SharedUsageMeterCache`, `NullUsageMeterAdapter` | `FakeUsageMeter` |
| `SignInStatePort` | `sign_in_failure` (optional) | `ClaudeUsageMeterAdapter`, `CompositeUsageMeterAdapter`, `SharedUsageMeterCache` | none |
| `LastGoodReadingPort` | `last_good` (optional) | `SharedUsageMeterCache` | none |
| `NetworkProbePort`, `EnvironmentProbePort`, `FilesystemProbePort` | the probes behind the setup checks | `adapters/preflight_probes.py` | `FakeNetworkProbe`, `FakeEnvironmentProbe`, `FakeFilesystemProbe` |
| `HostResourcePort` | `probe`, `read` | `ProcHostResourceAdapter`, `Kernel32HostResourceAdapter`, `PortableHostResourceAdapter` | `FakeHostResources`, `NullHostResources` |

A projection is a set of files a tracker generates from its own event log. Pinax generates `.ergon/board.md` and `.ergon/items/*.md` this way, so delivery settles a merge conflict in those files by generating them again from the merged log.

`UsageMeterPort.read` never raises. Every failure, an unknown meter id included, comes back as a reading with `ok=False`, and no credential appears in a reading. The Claude meter reads an endpoint that is undocumented and may change or stop working without notice. It uses Claude Code's own sign-in, which AutoBuild never prints or stores.

`PortKind` in `domain/models.py` names `tracker`, `workspace`, `harness`, `command`, `run_record` and `knowledge`, and the composition root reads only `harness` from the registry.

## Campaign state

`CampaignRunner` in `application/campaign.py` owns the state of one campaign. When it starts, it creates the run folder, takes the tracker lease, and fixes the launch set: the id of every item the tracker holds at that moment, read once through `known_item_ids`. A campaign selects only launch-set items. The selection fence, made of the profile's allow and exclude lists in `enforcement/selection.py`, also refuses any item the campaign proposed itself.

A running campaign is admitting, holding or draining. It admits items up to its concurrency while the lease renews and the host pressure allows, and at the usage warning level it admits a new item only when nothing else runs. A hold stops admissions until its condition clears. The conditions are the `HoldCondition` values `lanes_exhausted`, `tracker_pending`, `tracker_unreachable`, `tracker_read_failed`, `lease_renew_failed` and `batch_validation_failed`, one at a time. A hold that lasts `run.hold_limit_seconds` ends the campaign.

A stop lets running items finish and admits nothing more. A campaign ends with one of `queue_dry`, `item_bound`, `hold_limit`, `paused`, `usage_stop` and `launch_set_unreadable`. `CampaignStopReason` also holds `structural_failure`, `scope_fence_violation`, `lanes_exhausted`, `tracker_environment` and `tracker_refused`. No campaign ends with these five; they remain in the enum, and the item workflow uses the last two to classify a tracker failure.

## Item state

`ItemStateMachine` in `application/state_machine.py` holds an item's state, and `ALLOWED_TRANSITIONS` lists every legal move. An item starts `ready`. The checks before the claim move it to `verified`, or park it in two cases. The first is a brief whose `Item nature:` line names `machine`, `cross-repository` or `owner-gated`, or which declares a path outside the repository; a brief without the line counts as `repository`. The second is a brief in which `enforcement/brief_git.py` finds an order to stage or commit with git. From `verified` the item is `claimed`, then `isolated` in its worktree, then `built`.

A failed validator moves a `built` item back to `isolated`, when a fresh builder retries at a higher effort. A passing validator moves it to `validated`, and the review to `reviewed`. From `reviewed` the item goes to `correcting`, `escalated` or `finalised`, and `correcting` returns to `built`. In strict mode with a boundary map, the last correction round allowed is judged by the validator alone, with no new review, so that item goes from `validated` straight to `finalised`. In flow mode, and in strict mode without a boundary map, every correction is reviewed again. Any state before `finalised` can move to `parked`, and both `finalised` and `parked` end in `released`.

The item workflow in `application/item.py` writes a phase marker as the item moves. A resume restarts from `claimed`, `isolated`, `built`, `validated`, `reviewed`, `correcting`, `escalated` or `finalised`. Each item ends with one `ItemDisposition`: `accepted`, `parked` or `failed`. Each item also has its own lane router, the object that picks the lane for each of the item's seats, so a limit on one item never moves another item's lane.

## Evidence chain

AutoBuild accepts an item on evidence that the code checks at each step, and every step is recorded through `RunRecordPort`.

The builder's report is saved as evidence and handed to the reviewer. The validator runs through `EnforcedCommandPort`, which refuses any run whose argument list differs from the one in the profile. The reviewer runs through `EnforcedHarnessPort`, which refuses a review seat that asks for the `write`, `shell`, `python` or `git` tool. The reviewer receives the brief, the change, the validator output and the builder's report, and is not given the builder's transcript.

The reviewer must return the `review-verdict-v1` result, described under [Extension points](#extension-points), and the verdict rule in `domain/models.py` refuses a `pass` that marks a clause `unmet`. `enforcement/review_depth.py` builds the acceptance checklist from the brief and records a shallow review. In strict mode, `enforcement/review_scope.py` adds one specialist seat for each boundary that the change touches.

Before delivery, the close gate in `enforcement/policy.py` refuses an item whose validation is stale, whose validator failed or was not approved, whose evidence reference is missing, or whose changed paths leave the worktree. A parked item keeps a snapshot of its worktree in the run's evidence folder. The strict mode's checks catch a builder's mistakes, and they are not a security barrier.

## Git delivery model

The delivery code is in `adapters/git_workspace.py`, in the class `GitWorkspaceAdapter`.

The delivery mode is chosen with `--delivery-mode`, which `cli.py` parses with the values of the `DeliveryMode` enum in `domain/models.py`, and `protected-default` is the default. In `protected-default`, AutoBuild merges every accepted change into the repository's default branch and pushes it to `origin` without asking you, whatever branch you launched from. It checks out the default branch in your own checkout to do this, and your local branch moves only onto a commit that `origin` accepted. In `current-branch-pr`, AutoBuild delivers to the branch you launched from, and it refuses the default branch unless you also pass `--allow-current-branch-default`.

`GitWorkspaceAdapter.deliver` delivers one item, and `application/item.py` runs deliveries one at a time. `deliver` picks the target branch from the mode. `_integrate` merges the latest target into the item's branch, never rebasing, with a merge commit whose subject starts `MERGED: integrate`, set by `INTEGRATION_SUBJECT`; that commit reaches `origin` with the item. `enforcement/integration.py` decides whether the incoming changes overlap the item's paths, and the validator runs again only when they do. `_push` pushes the result to the target on `origin` and checks that `origin` holds it, and `_settle_target` then moves the target branch in the primary checkout. When the mode pushes nothing, `_fast_forward_target` moves the local target directly.

A delivery that cannot finish raises `DeliveryParked`, defined in `domain/models.py`, with a reason that starts `delivery_conflict`, `delivery_revalidation_failed` or `delivery_contended`, and it pushes nothing. The number of attempts against a moving target or a rejected push is fixed in the code at three, by `DELIVERY_ATTEMPTS`. Every local change to the primary checkout runs under one lock, created by `primary_checkout_lock` in `bootstrap/composition.py` and shared by the tracker and workspace adapters. The campaign report is delivered by `deliver_report` in the same class.

The tests that cover delivery are `tests/integration/test_git_workspace.py`, `tests/integration/test_serial_delivery.py`, `tests/integration/test_recovery_gaps_delivery.py`, `tests/integration/test_park_delivery_fallback.py` and `tests/unit/test_serial_delivery_workflow.py`.

## Tracker adapters

AutoBuild ships two tracker adapters, in `adapters/`. `PinaxTrackerAdapter` drives the `pinax` command from [pinax-tracker](https://github.com/antikas/pinax-tracker), which keeps its items in the repository's `.ergon/` folder. `BacklogTrackerAdapter` reads and writes a Markdown table in `BACKLOG.md`.

Each adapter states whether the tracker commits its own changes, in the class attribute `commits_itself`. Pinax commits every change itself, and `adapters/tracker_commits.py` stops the campaign at the first change it left uncommitted. The backlog adapter does not, so the workspace adapter commits its changes. `PinaxTrackerAdapter` also implements `TrackerProjectionPort`.

A tracker adapter that cannot complete a write raises `TrackerStop` from `domain/errors.py`, with the tracker's exit code in `code` and `environment` set when the environment must be fixed first. The item workflow records `item.tracker_failed`, the campaign calls the tracker's `recover`, and admissions hold with `tracker_unreachable` or `tracker_pending`. The campaign does not stop.

A tracker cannot be added from outside the package. The registry has a `tracker` kind, but the composition root never reads it, and `[tracker] kind` and `--tracker` accept only `auto`, `pinax` and `backlog`. A new tracker touches all of these places inside the package:

- a new adapter module in `adapters/` that implements `TrackerPort`, plus the attribute `commits_itself` and the method `validate_proposal`, which the composition root uses and the port does not declare;
- `TrackerProjectionPort`, if the tracker generates files from a log;
- the kind check in `bootstrap/profile.py`, and the `--tracker` choices in `cli.py`;
- `_repository_adapters` in `bootstrap/composition.py`, which constructs the two adapters directly and chooses between them, checks `isinstance(tracker, BacklogTrackerAdapter)` for the tracker paths, and passes `tracker.recover` to the workspace for Pinax only;
- `_runtime` in the same file, which checks `isinstance(tracker, BacklogTrackerAdapter)` for the lease surface and calls `tracker.validate_proposal` for each refill proposal;
- `item_concurrency` in the same file, which checks `isinstance(tracker, BacklogTrackerAdapter)` for the number of items that run at once.

## Extension points

A harness adapter is the one kind of adapter you can add from outside the package. To add one inside the package, register it in `register_first_party_harnesses` in `bootstrap/builtins.py` in place of steps 4 and 5 below.

### The port

Implement `HarnessPort` from `ports/contracts.py`. Its five methods are:

```python
def probe(self) -> ProbeResult: ...
def invoke(self, request: SeatRequest) -> SeatResult: ...
def cancel(self, run_ref: str) -> None: ...
def collect_usage(self, run_ref: str) -> SeatUsage: ...
def classify_failure(self, result: SeatResult) -> LaneSignal | None: ...
```

The types are in `domain/models.py`:

- `ProbeResult` holds `available`, `identity` and `diagnostics`. Build it with `ProbeResult.ready(identity, *diagnostics)` or `ProbeResult.unavailable(*diagnostics)`. `identity` is an `AdapterIdentity` with `name`, `version` and `capabilities`, and it must be present exactly when the adapter is available.
- `SeatRequest` carries, among other fields, `seat`, `model_class`, `workspace`, `tool_policy`, `instructions`, `result_contract`, `timeout_seconds` and `effort`, and the callback `on_launch`, which the adapter calls with the seat's launch just before the seat runs.
- `SeatResult` holds `run_ref`, `outcome` (a `SeatOutcome`: `succeeded`, `failed`, `cancelled`, `timed_out`, `stalled` or `unavailable`), `payload`, `raw_output_ref`, `usage`, `started_at`, `ended_at`, `diagnostics`, `exit_code`, `model` and `stall_sample_times`.
- `SeatUsage` holds `input_tokens`, `output_tokens`, `cost` and `source`.
- `LaneSignal` holds `kind` (`rate_limit`, `quota`, `auth`, `spawn`, `probe` or `usage`), `reset_at` and `detail`. Return one only from structured error fields, never from prose, and return `None` for a seat that succeeded or failed without a limit.

The `payload` of a succeeded seat is what the campaign reads, and `request.result_contract` names its shape. For a builder it is `builder-report-v1`, a `BuilderReport` with `summary` and `report_ref`. For a reviewer or specialist it is `review-verdict-v1`, a `ReviewVerdict` with `decision` (`pass`, `correct`, `escalate` or `park`), `findings`, `evidence_ref`, `inspected` and `acceptance`. Each finding has `code`, `consequence`, `evidence_ref`, `blocking` and `specialist_boundary`. Each `inspected` entry has `path` and `lines`, and each `acceptance` entry has `clause`, `outcome` (`met`, `unmet` or `not_checked`) and `evidence_ref`. Both contracts are defined as JSON schemas, `BUILDER_SCHEMA` and `REVIEW_SCHEMA`, in `adapters/harness_cli.py`, and `result_schema` returns the schema for a contract name.

### Subclass or implement

`CliHarnessAdapter` in `adapters/harness_cli.py` is the base of every first-party lane that starts a command-line program. A subclass gets `probe`, `invoke`, `cancel`, `collect_usage` and `classify_failure`, and it gets the four required capabilities. Its `invoke` writes the brief and evidence into the instructions, runs the seat through the command port, reads the typed result and the token usage from the output, and checks the result against the contract. A subclass writes three methods: `_version_argv`, the version command; `_probe_authentication`, the sign-in check; and `_invocation`, the argument vector for one seat. It also sets `adapter_name`, `effort_levels`, `telemetry_environment` and, for a metered lane, `meter_id`.

Subclass `CliHarnessAdapter` when the assistant is a program that AutoBuild starts as a child process. Implement `HarnessPort` directly when the seat runs some other way, as `ClaudeSdkHarnessAdapter` does through a Python library.

A seat transport is the way a harness hands one seat to its child: the largest single argument in bytes, whether the instructions travel by file or standard input, and the environment the harness adds. An in-process harness is one that runs the seat from inside the campaign process, with no child program.

### Steps

1. Implement the port, by subclassing `CliHarnessAdapter` or directly.
2. Declare the class attributes the composition root reads. `capabilities` must contain all of `fresh-seat`, `cancel`, `typed-result` and `usage`, or the lane is not capable. `effort_levels` is a set of `EffortLevel` values, and a profile that sets another effort for your lane is refused before any lane is probed. `telemetry_environment` is a tuple of name and value pairs that turn telemetry off, or the preflight's `telemetry` probe fails.
3. Declare the seat transport. A `CliHarnessAdapter` subclass has it already. Otherwise implement `SeatTransportPort.seat_transport`, which returns a `SeatTransport`; an in-process harness must. A harness with neither fails every launch with `harness <name> declares no seat transport for the preflight`. The instructions must travel by file or standard input, and no argument may exceed 8192 bytes, a limit set by `max_argv_bytes` in `enforcement/preflight.py`.
4. Write the factory, the callable that builds your adapter. It receives a config map and returns the adapter. The map holds `command_port` (a `CommandPort`), `output_root` (a `Path`, `<scratch>/harness/<lane>`), `model_map` (seat model classes to model names) and `subscription_only` (a `bool`). It also holds `prepare_argv` and `key_file` when the lane table sets them, and `command` only when the profile has exactly one lane and overrides the harness command.
5. Register the factory under `PortKind.HARNESS`. Declare an entry point in the group `autobuild.adapters`, whose target is either an object with a `register(registry)` method or a callable that takes the registry, and call `registry.register(PortKind.HARNESS, "<lane name>", factory)` there.
6. Name the lane in the profile. The name in `run.lanes` is the registered name, and the lane needs a `[lanes.<lane name>]` table with `builder` and `reviewer`.

These steps meet every check the composition root makes, and three limits remain. A plugin name that is already registered makes `register` raise `adapter already registered: harness:<name>` inside `load_entry_points`. The registry loads entry points after the first-party adapters on every launch and preflight, and in the `subscription-status` and `subscription-review` commands, so the clash stops all of them. The setup checks test the transport of the active lane only, so a lane further down `run.lanes` is not checked at launch. Subscription routing accepts only the `claude-code` and `codex` lanes, so a new lane cannot take part in it.

### Testing without the assistant

The first-party adapters are tested with no real assistant. `tests/contract/test_harness_adapters.py` builds each adapter with `FakeCommandAdapter` from `autobuild.testing` as its command port. The test scripts the fake with recorded output through `scripted_results`, a list of `CommandResult` values whose standard output files hold the assistant's JSON. It then calls `invoke`, `probe` or `classify_failure`, and checks the `SeatResult` and the argument vector the fake received in `requests`. Write the same tests for your adapter: a builder result, a review result, a failed seat, a limit code on each output stream, prose that must not classify, and a probe with no model call.

For registration, copy the `FakeEntryPoint` class in `tests/contract/test_adapter_registry.py`, a stand-in entry point whose `load` returns your registrar, and pass it to `load_entry_points`. To test a whole campaign against fakes, follow `tests/unit/test_workflows.py`, which drives `CampaignRunner` with `FakeHarnessAdapter`, `FakeTrackerAdapter` and the other fakes.

### The first real launch

Run `autobuild preflight --harness <lane name>` before any launch. It moves your lane to the front, builds the adapters, runs the setup checks against your lane, and claims nothing.

A real `autobuild run --allow-delivery` claims an item in the tracker and pushes accepted work to `origin`. Make the first one in a scratch repository whose `origin` is a bare repository on your own machine, so the claim and the push stay there:

```
git init --bare scratch-remote.git
git init scratch-repo
git -C scratch-repo remote add origin ../scratch-remote.git
```

Then add a test command, a `BACKLOG.md` with one item, a brief and a profile, commit and push them, and launch, as the [README](../README.md) shows.

## Architecture tests

Run the tests with `uv run pytest`. The development dependencies in `pyproject.toml` hold `pytest` and `pytest-xdist`, and the pytest settings put `src` on the path and collect from `tests`. To run the tests that cover your change, pass their files, for example:

```
uv run pytest tests/unit/test_cli.py tests/unit/test_enforcement.py
```

The tests are split by folder. `tests/unit/` holds the unit tests, `tests/contract/` the adapter contract tests, `tests/integration/` the integration tests, and `tests/architecture/` the architecture tests. `tests/test_smoke.py`, `tests/test_subscription_auth.py` and `tests/test_subscription_review.py` sit at the top level. The integration tests run real `git`, and many of them run real `pinax`, so both must be on `PATH`. The integration tests fail when the installed `pinax` leaves its changes uncommitted, so `pinax` must be pinax-tracker 0.2.1 or later.

The fakes are importable from `autobuild.testing`: `FakeAdapter` (the base), `FakeClock`, `FakeHarnessAdapter`, `FakeAgentSdkClient`, `FakeTrackerAdapter`, `FakeWorkspaceAdapter`, `FakeCommandAdapter`, `FakeRunRecordAdapter`, `FakeKnowledgeAdapter`, `FakeLeaseAdapter`, `FakeLaneStateAdapter`, `FakeUsageMeter`, `FakeProgressPort`, `FakeNetworkProbe`, `FakeEnvironmentProbe` and `FakeFilesystemProbe`. `FakeHostResources` and `NullHostResources` are importable only from `autobuild.testing.fakes`. No fake exists for `TrackerProjectionPort`, `SeatTransportPort`, `SignInStatePort`, `LastGoodReadingPort` or the admission store.

The architecture tests in `tests/architecture/` guard the rules on this page:

| Test file | What it guards |
|---|---|
| `test_dependency_direction.py` | The import rules, the provider and host words, and the wall clock rule under [Dependency direction](#dependency-direction). |
| `test_public_documentation.py` | Every published Markdown file: `README.md`, the pages under `docs/` at any depth, and `skills/**/*.md`. Every local link and its anchor must exist, no long dash may appear, no heading may start with What, How or For, and a file that mentions the usage meter must say that its endpoint is undocumented. It also compares the pages with the code: the commands and options in `docs/reference/commands.md` with the parser, the keys in `docs/reference/profile.md` with the profile loader, and the stop reasons and unbuilt reasons the pages list with the ones the code can print. It requires the section headings and port names of this page. Read it before you rename or remove a heading. |
| `test_entry_shim.py` | The `autobuild` script in `pyproject.toml`, and its route through `cli.py`, `bootstrap/composition.py` and `application/campaign.py`. |
| `test_coordinated_method.py`, `test_coordinated_builder_tests.py` | The coordinated skill. |

## Entry skills and executable

The executable is the console script `autobuild`, declared in `pyproject.toml` and served by `cli.py`. `python -m autobuild` runs the same program through `__main__.py`. The Python wheel holds only `src/autobuild`, which includes `autobuild.testing`, so the fakes ship with the package.

A skill is a set of written instructions in `skills/` that a coding assistant follows. Three skills are the entry points for an assistant:

- `skills/autobuild/` launches the `autobuild` application for a user, or runs the coordinated mode when the user chooses it.
- `autobuild-plan`, in `skills/autobuild-plan/`, turns one build request into a short plan and a registered queue, and stops before any launch.
- `skills/autobuild-coordinated/`, the coordinated skill, runs an approved queue inside the assistant's own session without the `autobuild` application, with fresh builder and reviewer seats.
