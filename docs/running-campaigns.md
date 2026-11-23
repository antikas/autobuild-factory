# Running campaigns

A campaign is running, or it has just ended, and you need to know what it did and what to do next. Some items were delivered, one sits in the tracker with a reason you have not seen before, and `autobuild run` exited with code 1. This guide follows one campaign from launch to report, then lists every park reason, stop reason and exit code for lookup, and ends with the commands that watch, pause, resume, clean up and relaunch a campaign.

If the campaign has already ended and you were not watching, start at [Find out what a campaign did](#find-out-what-a-campaign-did) in Part three. It takes you from the run folder to each item's fate, the stop reason, the clean-up and the relaunch.

The [README](../README.md) explains the terms used here: campaign, brief, seat, lane, validator, worktree, park and batch validation. The [guide to writing briefs](writing-briefs.md) says how to write a brief that is built and not parked.

## Part one: one campaign from launch to report

Follow one campaign on a repository whose `BACKLOG.md` holds four ready items, `ITEM-7` to `ITEM-10`, with Claude Code as the only lane. The profile sets `concurrency = 2` under `[run]`, so two items can run at once, and `builder_effort = "medium"` under `[models]`. Read the three sections below before you launch it.

### Where accepted work goes

Know the delivery target before you launch. In the default delivery mode, `protected-default`, AutoBuild merges every accepted change into the repository's default branch and pushes it to `origin`. To do this it checks out the default branch in your own checkout. The campaign report goes to the default branch the same way. A `BACKLOG.md` claim is pushed to the branch that is checked out when the claim is made, which in this mode is the default branch as long as you leave the checkout alone. In `current-branch-pr` mode it pushes the claim only with `--push-current-branch`.

Launch the default mode from the default branch, at the head you want delivered, because each item's worktree starts from whatever your checkout has at `HEAD`. A launch from another branch or from a detached head is refused before any lane is probed and before any claim, so that branch's own commits cannot reach the default branch unreviewed. The message is `protected-default delivery must be launched from the default branch <default>; the checkout is on <branch>; switch to <default>, or pass --delivery-mode current-branch-pr to deliver to <branch>`. On a detached head, `<branch>` in the middle clause reads `a detached head at <short revision>`, and the message ends at `current-branch-pr`. When the clone records no remote head and `origin` does not answer with its head, the launch is refused at the same point with `the default branch of origin could not be established: this clone records no remote head and origin did not answer with its head; make origin reachable, or set the remote head in this clone, then launch again`. The default branch established at launch is the delivery target for the whole campaign, even when `origin` later names another one. The check is made at launch only: do not switch branches while the campaign runs. To collect accepted work on a branch of your own, use the other mode.

With `--delivery-mode current-branch-pr`, the target is the branch you launched from. That mode refuses a detached head with `current-branch-pr delivery requires a named invoking branch`. It refuses the default branch with `current-branch-pr delivery refuses the detected default branch; pass --allow-current-branch-default after human approval`, unless you pass `--allow-current-branch-default`. Without `--push-current-branch` this mode pushes nothing: the deliveries and the report are committed on that branch in your checkout only, and you push it yourself. AutoBuild opens no pull request in either case, so you open one yourself if you want one. The options `--push-current-branch` and `--allow-current-branch-default` are refused with `protected-default`.

### Leave your checkout alone while it runs

AutoBuild claims items, delivers them and writes some parks to the tracker from the checkout you launched from, so leave it alone until the campaign ends. Make no edits to tracked files, do not switch its branch, and with a `BACKLOG.md` tracker do not create untracked files in it either. A changed or staged tracked file stops every delivery with `primary checkout must be clean before delivery`. In `current-branch-pr` mode a branch switch or a rewritten history stops it with `primary checkout no longer has the captured target branch` or `captured target revision is no longer in the current branch history`. With a `BACKLOG.md` tracker any change, untracked files included, refuses the next claim with `primary checkout must be clean before a tracker claim`. Do any other work on the repository in a separate clone.

### Keep the scratch root

The scratch root is `autobuild` in the system temporary folder unless you set `[run] scratch_root` or `--scratch-root`, and anything that clears the temporary folder clears it too. It holds the worktrees, the park snapshots, the leases and the phase markers. A phase marker is a small file in a run folder that records how far one item got, and a later campaign reads it to resume that item. Once these files are gone, the next campaign parks the interrupted items at resume, and the snapshots this guide points you to no longer exist. Set `[run] scratch_root` in the profile to a fixed folder that nothing cleans.

### Launch

You launch from a clean checkout of the default branch:

```
autobuild run --allow-delivery
```

### Before the first claim

Without `--allow-delivery`, `autobuild run` stops before it builds or probes anything, prints `this workflow claims items and delivers accepted changes; pass --allow-delivery after human approval`, and exits 2. A probe is AutoBuild's check that one of its parts can work, such as a lane, the tracker or the scratch root. With `--allow-delivery`, AutoBuild loads the profile, checks for an `origin` remote, checks that the checked-out branch fits the delivery mode, probes each lane and runs the setup checks in a fixed order. The first setup check that fails stops the launch with `<probe> preflight failed: <cause>` and exit code 2, and nothing is claimed.

The `validator-offline` check runs your full validator once in your checkout, with the proxy variables pointed at a closed local port and the package tools set to offline. A direct connection still works, so this check does not block the network. The validator must already pass on the committed head: a test command that finds no tests, for example pytest exiting 5, fails this check. The [command reference](reference/commands.md) lists every check and what makes it fail.

### The set of items, and their order

Once the setup checks pass, the campaign creates its run folder and takes a lease on the tracker. The run folder, `<scratch>/runs/<run-id>/`, holds the events, the progress log and the evidence of this one campaign. The lease is a record under the scratch root that tells other campaigns this tracker is in use. Then AutoBuild reads the id of every item in the tracker, in any status, and fixes that set (the launch set). It reads the set once and never again. Only items in the launch set are ever selected, so an item that someone adds during the run, and every follow-up the campaign registers, waits for a later campaign. An item in the launch set that becomes ready during the run, for example because its blocker was delivered, is still built.

Admission is the moment the campaign lets an item start, or lets an interrupted one go on. Selection follows the tracker's order. With an allow-list (`[selection] allow` or `--allow-item`), the allow-list order is the dispatch order and nothing outside the list is claimed. Without one, a campaign that runs one item at a time asks the tracker for its next ready item. A campaign that runs several reads the tracker's whole list of ready items and takes them in its order. The exclude-list is checked at every selection. Claims left over from an interrupted campaign, and builders that continue after a timeout, are admitted before new work.

A ready item waits when it declares an exact path under `## Declared paths` that an item in flight also declares. A glob never makes an item wait. `run.max_items` (20 by default) limits the items one campaign handles. Each item counts against it from the moment it starts, and it keeps counting once the item ends, whether it was accepted, parked or failed.

### Items at once, and host pressure

The number of items that run at once is `run.concurrency`. Set to `"auto"`, the default, it comes from one host sample of 0.5 seconds taken at launch. AutoBuild takes the logical processor count divided by 4, and the available memory less `reserve_gb` divided by `per_item_gb`, both keys of the `[capacity]` table. The figure is the smaller of the two, held between 1 and `[capacity] max_concurrency`. A `BACKLOG.md` campaign runs one item at a time unless you set `run.concurrency` to a number, which is why this example sets it. Run `autobuild host` to see the reading and the figure it gives.

When more than one item can run, every validator run takes a heavy slot, a turn to run one validator, and no more than `run.heavy_concurrency` validators run at once. A pressure monitor also reads the host every `sample_seconds` (10 by default, in the `[pressure]` table) and gives one of four levels: `normal`, `recovering`, `high` or `critical`. Only `normal` admits new items. Each further `decrease_seconds` at `high` or `critical` lowers the number of items allowed by one, down to 1, and each `increase_seconds` at `normal` raises it by one, up to the concurrency. With nothing of its own running, a campaign always admits one item.

Pressure never stops a running seat or command. A seat's time limit grows by every second it spends at `high` or `critical`, up to twice its normal limit. The patient form of the validator is `[validator] patient_argv`, a second command you can set for runs under load. A validator that starts at `high` or `critical`, or within 60 seconds after, runs that patient form, or runs `argv` with `AUTOBUILD_HOST_PRESSURE=1` when you set none. A validator that timed out while pressure held back heavy commands is run once more alone, and the result of that second run stands.

Usage limits also slow admission. While the first lane's shared usage reading is at its warning level, a campaign that runs several items admits a new one only when nothing runs. The [guide to lanes and usage](lanes-and-usage.md) covers the levels and the meter.

### One item through its stages

`ITEM-7`, "Add a --json option to the export command", goes through every stage. The other items leave the path at the points marked below.

**Triage.** Before any claim, AutoBuild reads the brief. An item whose `Item nature:` line names `machine`, `cross-repository` or `owner-gated` is parked with the reason `nature:<value>`, with no claim, worktree or seat. `ITEM-8` has `Item nature: owner-gated`, so it parks here as `nature:owner-gated` and the next item starts. A brief that orders git staging, such as `git add`, parks the same way with a reason that starts `brief:git-staging`, and so does an item whose brief file is missing, empty, over 1 MiB or not readable as UTF-8 text, with a `brief:` reason that names the cause.

**Claim.** AutoBuild claims `ITEM-7` under the actor `builder@claude-code`. When another writer has already taken the item, AutoBuild records `item.claim_taken` and selects the next one. If a seat later moves to another lane, the item keeps its claim and is never claimed again.

**Worktree.** AutoBuild creates a worktree for the item under `<scratch>/workspace`, on a branch `autobuild/<campaign>/ITEM-7-<8 characters>`. It also writes the item's phase marker, `ITEM-7-phase.json`, in the run's `evidence/` folder, and rewrites it at every change of state.

**Builder.** The builder seat starts fresh with the brief. It is told to leave its changes uncommitted, to run only the tests for the files it changed, and to return a report, which AutoBuild saves as evidence and hands to the reviewer. In `flow` mode nothing enforces that test scope. If the seat runs out of time before the item commit, AutoBuild gives the item to a new builder in the same worktree, up to 2 times. A third timeout parks it `timeout:builder` with a snapshot. A seat that stalls parks the item as `stall:<seat>:<signals>`, and a seat that ends in any other failure parks it as `<seat> ended <outcome>`.

**Validation.** The validator runs in the worktree on the builder's change, and only exit code 0, without a timeout or a cancel, counts as a pass. A red validator gives a fresh builder in the same worktree one effort level more, with the validator output as context, but only when a builder effort is set and is below `high`. A raised level holds for the later builder seats of that item. `ITEM-9` fails the validator at `medium`, fails again at `high`, and parks with `validator tests did not pass`, where `tests` is the validator id from the profile.

**Review.** Each review round starts one blind reviewer with read-only tools. It receives the brief, the change, the validator output and the latest builder report, and never the builder's transcript. It returns one decision, `pass`, `correct`, `escalate` or `park`, with its findings and an outcome for each acceptance clause it checked. A `correct` or `escalate` must carry a blocking finding, and a `pass` may carry neither a blocking finding nor an `unmet` clause. A verdict that breaks these rules parks the item. A review that inspected fewer changed paths than the change touches, or checked no clause as met or unmet, is recorded as shallow in the report, and AutoBuild still accepts its verdict.

The reviewer of `ITEM-7` returns `correct`. The builder makes the correction in the same worktree, the validator runs again, and a second review returns `pass`.

**Corrections and specialists.** In the default process mode, `flow`, an item gets 2 correction rounds, fixed in the code. On `escalate`, one specialist seat judges the evidence that triggered it, and its `correct` gives one more correction round when the limit allows. In `strict` mode with a `[process.boundaries]` map, each boundary that the item's paths touch adds one specialist seat to every review round, in map order. There an item that touches no boundary gets 1 correction, and one that touches a boundary gets 2. In `flow` mode every correction is reviewed again, and a `correct` verdict after the last allowed correction is accepted when the validator passes. In `strict` mode with a boundary map, no review follows the correction that reaches the limit, and the validator alone decides that round. A final `correct` on a green validator is accepted and delivered, and each finding becomes a follow-up. A `park` verdict, or an `escalate` that the specialist does not settle, parks the item with `review disposition was <decision>`.

**Delivery.** An accepted item gets a product commit `ADDED: Add a --json option to the export command` and a separate tracker commit `MODIFIED: close ITEM-7 in tracker`. AutoBuild then writes a `finalised` phase marker, so a delivery that is interrupted resumes by delivering. Deliveries run one at a time, in the order they were requested. AutoBuild merges the delivery target into the item's branch with commits whose subject starts `MERGED: integrate`, and never rebases. It runs the validator again only when the incoming changes touch a path that overlaps the item's changed or declared paths.

AutoBuild then pushes the result to the target on `origin`, checks that `origin` holds it, and only then moves your local target branch forward. A moved target or a push that `origin` rejects because another writer pushed first is retried, up to 3 attempts in all. A merge conflict, a failed revalidation or a target that stays contended parks the item with a reason that starts `delivery_conflict`, `delivery_revalidation_failed` or `delivery_contended`. Any other delivery error parks the item with its own text. Among those are `primary checkout must be clean before delivery`, the two `current-branch-pr` checks above, any other push error, and `remote verification did not observe the delivered commit`. After that last one the push may already have reached `origin`. An untracked file in your checkout at a path the delivery brings in refuses the delivery with `untracked files in the primary checkout are at paths the delivery brings in: <paths>`, and the item parks with that text.

**Close.** With the delivery, the tracker shows `ITEM-7` as done. Each finding on an accepted item becomes a proposal titled `Follow-up: <item> <code>`, which no campaign runs. In Pinax the proposal is blocked, and in a `BACKLOG.md` table its status starts `Proposed:`. To build one, change it to a runnable status yourself.

`ITEM-10` passes its first review and is delivered the same way.

### Batch validation

Each item's validator runs on that item's branch. Batch validation checks the delivered result as a whole. After every `run.batch_validate_every` deliveries (5 by default), and once at the end of the campaign when anything was delivered since the last batch, AutoBuild runs the validator on the head of the delivery target. It runs there in a separate checkout under the scratch root.

A failed batch reverts nothing. It holds new admissions with `batch_validation_failed`, and AutoBuild fetches the target every 600 seconds to look for a new head. The hold is released only when a batch passes on a head that has moved since the failure. If the hold lasts `run.hold_limit_seconds` (21600 by default), the campaign ends with `hold_limit`. The result of the last batch is the campaign's batch verdict, and a `failed` verdict makes `autobuild run` exit 1 even when every item was accepted.

In this campaign two items were delivered since the last batch, so the batch at the end of the campaign runs on the new head of the default branch and passes.

### The end of the campaign

The campaign ends when a stop applies, here `queue_dry` because no ready item is left. A stop admits nothing new and lets the running items finish. AutoBuild then commits the report `docs/campaigns/<campaign-id>.md` with the subject `ADDED: campaign <id> report`, delivered through the same mode as the items, and prints the campaign result as JSON on standard output. The last progress line starts with its UTC time and reads:

```
<UTC time> campaign completed: shipped 2, parked 2, failed 0; report docs/campaigns/<campaign-id>.md; stop queue_dry progress hook failures: 0
```

`autobuild run` exits 1, because two items parked. The report lists `ITEM-8` and `ITEM-9` under Parked with their bare reasons, which means without the snapshot suffix the tracker adds. In the tracker, `ITEM-9`'s reason ends with ` [snapshot=evidence/ITEM-9-park]`, the snapshot of its worktree in the run folder. `ITEM-8` parked at triage before it had a worktree, so it has no snapshot.

## Part two: reasons and exit codes

### Park reasons

A parked item has its reason written in the tracker and is listed under Parked in the report. When the park took a snapshot of the item's worktree, the tracker reason ends with ` [snapshot=evidence/<item>-park]`, and the report shows the reason without it. A park at triage has no worktree and so no snapshot, and a `resume:` park writes the bare reason to the tracker.

To try a parked item again once you have fixed its cause, [make it ready again](#make-an-item-ready-again).

| Reason | Meaning | What to do |
|---|---|---|
| `nature:<value>` | The brief's `Item nature:` names `machine`, `cross-repository` or `owner-gated`, or a `repository` brief declares a path outside the repository and allowed roots. Parked before any claim. | Do the work outside AutoBuild, or correct the nature line or the declared paths. See [writing briefs](writing-briefs.md). |
| `brief:missing <brief>`, `brief:empty <brief>` | The brief file does not exist, or holds no text. Parked before any claim. | Write the brief and commit it, or correct its path in the tracker, then make the item ready again. |
| `brief:too-large <bytes> bytes, over 1048576 <brief>` | The brief is over 1 MiB. Parked before any claim. | Shorten the brief, then make the item ready again. |
| `brief:unreadable <brief>` | The brief could not be read as UTF-8 text. Parked before any claim. | Save the brief as plain UTF-8 text, then make the item ready again. |
| `brief:git-staging line <n> ...` | The brief orders git staging or committing at the line named. Parked before any claim. | Describe git only in a clause that forbids it, a report or a quotation, then make the item ready again. |
| `validator <id> did not pass` | The validator failed and no retry at a higher effort was left. | Read the validator output in the run's evidence, then fix the brief or the code it depends on. |
| `review disposition was park` | The reviewer returned `park`. | Read the findings in the report and the round evidence. |
| `review disposition was escalate` | The reviewer escalated and the specialist did not settle it. | Read the specialist's findings, then sharpen the brief. |
| `timeout:<seat>` | The seat ran past its time limit. For the builder this is the third timeout. | Raise `run.seat_timeout_seconds`, or split the item. You can also give the brief an `Item class:` line, which names a class whose seat time limit, in seconds, you set in the `[run.item_classes]` table. A brief without the line uses `run.seat_timeout_seconds`. |
| `stall:<seat>:<signals>` | The seat showed no progress in its output, worktree or processor use for `run.seat_stall_seconds`. | Check the seat's log under the scratch root. Raise `run.seat_stall_seconds` if the work is slow and silent. |
| `<seat> ended <outcome>` | The seat ended in a failure other than a timeout or stall. | Check the seat's log under `<scratch>/harness/<lane>/`. |
| `<lane>:<signature>` | No capable lane was left for a seat. The signature names the cause, for example `usage` or `usage_meter_unavailable`. | See [lanes and usage](lanes-and-usage.md). |
| `subscription-admission` | Subscription routing admitted no lane for a seat after the claim. The reason carries no lane. At builder admission, before the claim, the same reason ends the item `failed`. | See [lanes and usage](lanes-and-usage.md). |
| `delivery_conflict: ...` | The merge of the delivery target into the item's branch conflicted. | Resolve the overlap with the other change, then make the item ready again. |
| `delivery_revalidation_failed: ...` | The validator failed after the incoming changes were merged in. | Check how the item and the incoming changes interact. |
| `delivery_contended: ...` | The target moved or the push was rejected on all 3 attempts. | Launch again when the other writers on the branch are quiet. |
| `untracked files in the primary checkout are at paths the delivery brings in: <paths>` | An untracked file in your checkout is in the way of the delivery. | Move or commit those files, then make the item ready again. |
| `primary checkout must be clean before delivery` | A tracked file in your checkout was changed or staged at delivery, or a tracker file was left untracked there. The item parks after its product commit, and its worktree is kept. With a `BACKLOG.md` tracker the park is refused as well, and the item ends `failed` with its worktree kept. | Clean your checkout and leave it alone during the next campaign. The built change is on the item's branch in the kept worktree. |
| `primary checkout no longer has the captured target branch` | In `current-branch-pr` mode, your checkout was switched off the branch you launched from. | Switch back to that branch and leave it checked out while a campaign runs. |
| `captured target revision is no longer in the current branch history` | In `current-branch-pr` mode, the history of the launch branch was rewritten during the run. | Do not rewrite the launch branch while a campaign runs. |
| `seat brief is outside the allowed roots: <brief>` | The brief lies outside the repository, the scratch root and `[policy] allowed_roots`. No setup check catches this, so it parks after the claim. | Move the brief into the repository or add its folder to `allowed_roots`. |
| `seat brief is missing: <brief>` | The brief was in your checkout before the claim but is not in the item's worktree, for example because it was never committed. `<brief>` is the path as the tracker gives it, or only the file name when that path is absolute, a file URL or starts with `~`. | Commit the brief, then make the item ready again. |
| `seat brief is empty: <brief>` | The brief in the item's worktree holds no text, or only spaces and blank lines, for example because the committed copy differs from the one in your checkout. | Commit the brief with its text, then make the item ready again. |
| `seat brief exceeds 1 MiB: <brief>`, `seat brief cannot be read as text: <brief>` | The copy of the brief in the item's worktree is too large or is not readable as UTF-8 text. | Correct and commit the brief, then make the item ready again. |
| `resume:missing-marker` | A claimed item from an earlier campaign has no phase marker. | [Recover the work from the snapshot](#recover-a-parked-items-work), then make the item ready again. |
| `resume:tracker-mismatch` | The tracker state does not match the phase marker. | As above. |
| `resume:missing-worktree` | The marker's worktree is no longer registered. | As above. |
| `resume:lease-held` | Another live campaign holds a lease on the worktree or the tracker. No snapshot is taken. | Wait for that campaign to end. |
| `resume:head-moved` | The worktree's head no longer matches the marker. | As for `resume:missing-marker`. |
| `resume:revision-changed` | The product changes differ from the ones the marker recorded. | As for `resume:missing-marker`. |
| `resume:orphan-worktree` | A registered worktree under the scratch root belongs to no claimed item, for example one kept after a park. Listed under Parked and left in place, and listed again in every later campaign until you remove it. | Inspect the worktree, then remove it as [Clean up and relaunch](#clean-up-and-relaunch) shows. |
| `<reason>; park delivery failed: <error>` | The park itself could not be delivered from the worktree, so it was recorded from your checkout. | Read the error, then check the tracker shows the item parked. |
| `<reason>; workspace release refused: <error>` | The item ended with the first reason, and its worktree could not be removed afterwards. The second part is added to the reported reason. | Read the error, then remove the worktree yourself. |
| Any other text | An evidence error, for example a review verdict that breaks the verdict rules, or an adapter error. | Read the text and the run's `events.jsonl`. |

An item can also end `failed`, which happens in four cases. The first is a failure before its claim: the item stays ready in the tracker and is not picked again in that campaign. Two such failures are `primary checkout must be clean before a tracker claim`, when a `BACKLOG.md` claim finds any change in your checkout, untracked files included, and `subscription-admission` at builder admission.

The second case is a park that failed entirely, which leaves the item unfinished with its claim and worktree kept. The third is a `resume:` park whose tracker write fails; the item then ends `failed` with the tracker's error as its reason. The fourth is a builder continuation that the campaign stopped before admitting, reported as failed with `timeout:builder; kept with its claim and worktree for the next campaign` and listed under Unfinished. No item error stops the campaign.

### Stop reasons

The first stop that the campaign observes wins, and a requested pause is read before any other stop.

| Stop | Meaning |
|---|---|
| `queue_dry` | No selectable ready item is left in the launch set, including when every ready item is excluded. |
| `item_bound` | The campaign reached `run.max_items`. |
| `hold_limit` | One hold on admissions lasted `run.hold_limit_seconds`. |
| `paused` | Someone ran `autobuild pause` for this run. |
| `usage_stop` | Every lane is cooled for usage. The report names the earliest reset. |
| `launch_set_unreadable` | The tracker could not list its items at launch, so nothing was admitted. |

A hold pauses admissions without ending the campaign. Only one hold is in force at a time, and the report lists them under Holds.

| Hold | Cause | Retry |
|---|---|---|
| `lanes_exhausted` | Every lane is cooling, and at least one cooling is not for usage. | At the earliest lane reset, between 1 and 600 seconds. |
| `tracker_pending` | An item's tracker failure whose cause is unclear. | Every 60 seconds. |
| `tracker_unreachable` | An item's tracker failure caused by the environment. | After 30, 60, 120, 240 and 480 seconds, then every 600. |
| `tracker_read_failed` | Reading the ready queue raised an error. | As for `tracker_unreachable`. |
| `lease_renew_failed` | The tracker lease could not be renewed while nothing ran. | Every 30 seconds. |
| `batch_validation_failed` | The last batch validation failed. | Every 600 seconds, by fetching the target. |

### Exit codes of `autobuild run`

| Code | Meaning |
|---|---|
| 0 | No item ran, or every item was accepted, and the batch verdict is not `failed`. |
| 1 | An item ended parked or failed, or the batch verdict is `failed`. |
| 2 | The launch or the campaign stopped before completion: `AutoBuild failed before completion: <error>` on standard error and no JSON. This covers the profile, the adapters, the setup checks, the usage check, a tracker lease held by another campaign and an internal error in AutoBuild. It also covers an unknown or missing option, which the option parser rejects with its usage text. |

An internal error in AutoBuild ends the campaign only after every running item has ended, and then no campaign report is written.

## Part three: operating a campaign

### Find out what a campaign did

Work through these steps in order the morning after a campaign you did not watch.

**Find the run.** The report is committed to the delivery target as `docs/campaigns/<campaign-id>.md`, with the subject `ADDED: campaign <id> report`. In the default mode that is the default branch on `origin`, so pull it and list the reports with `git log --oneline -- docs/campaigns`. A campaign id has the form `autobuild-<YYYYMMDDTHHMMSSZ>` unless you named one with `--campaign-id`. The report's last section, Run record, gives the absolute path of the run folder and of its `progress.log`.

Without a report, look in `<scratch>/runs`. Each run folder is named `<campaign-id>-<YYYYMMDDTHHMMSSZ>-<8 characters>`, and that name is the run id. Its `manifest.json` names the repository the run belongs to, which matters when several repositories share one scratch root.

**Tell an ended run from a killed one.** A campaign that ended writes `report.txt` into its run folder, a single line of the form `campaign=<id>; items=<n>; stop=<reason>; report=<path>`. The last line of its `progress.log` is the completion line, and the last event in `events.jsonl` is `campaign.completed`. A run folder without `report.txt` belongs to a campaign that was killed, whose machine stopped, or that exited with code 2. In the first two cases [resume it](#resume-after-an-interruption). In the third, the terminal shows `AutoBuild failed before completion: <error>`, so fix that error before you launch again.

**Read each item's fate.** The completion line counts only shipped, parked and failed items, so look further for the rest. An item can end in any of these ways:

- Shipped: listed under Shipped in the report. A `BACKLOG.md` row reads `Done (<commit>)`, and a Pinax item is done.
- Parked: listed under Parked with its reason. A `BACKLOG.md` row reads `Parked: <reason>`, and a Pinax item is parked. The [park table](#park-reasons) says what each reason means.
- Failed: listed under Failed with its reason. An item that failed before its claim is still ready in the tracker.
- Unfinished: listed under Unfinished, with the line `Ended without a recorded park; each worktree is kept as evidence.`, and also under Failed. The tracker still shows it claimed, so the next campaign treats it as interrupted and either resumes it or parks it with a `resume:` reason.
- Skipped: a Pinax item with no brief note, the note whose reference names the brief, listed under Skipped as `no_brief`. Nothing claimed it.
- Never started: the campaign stopped before it reached the item, so the tracker still shows it ready. With an allow-list the report names it under Allowed items left unbuilt, with its reason. Without one the report names it only as the next ready item, in the Next section, after an `item_bound` stop.
- Not in the launch set: an item added after the launch, which no part of the report mentions.

**Read why the run stopped.** The completion line and `report.txt` end with the stop reason, and the [stop table](#stop-reasons) explains each one. The report's Next section says what to do next, and its Holds section lists every hold on admissions during the run.

### Recover a parked item's work

A park of an item that has a worktree keeps a snapshot of that worktree in the run folder, `<scratch>/runs/<run-id>/evidence/<item>-park/`. It holds `changes.patch`, a `files/` folder and `snapshot.json`. For a park inside a running campaign, the tracker reason ends with ` [snapshot=evidence/<item>-park]`, so you can find the folder from the tracker. A `resume:` park never carries that suffix, so look for its snapshot in the evidence folder of the run that parked it.

Some parks have no snapshot at all: a park at triage, `resume:lease-held`, and a resume park whose worktree could not be read. A resume park takes no snapshot either when the item has no usable marker and owns more than one worktree, because nothing says which one holds the work.

`changes.patch` is a Git diff of the item's changes to tracked files against the commit the worktree started from, and `snapshot.json` records that commit as `start_commit`. The `files/` folder holds the files the item created and Git did not yet track, at their paths in the repository. To bring the work back by hand, start a branch at that commit in your checkout, apply the patch, and then copy the contents of `files/` into the repository at the same paths:

```
git switch -c recover-ITEM-9 <start_commit>
git apply <scratch>/runs/<run-id>/evidence/ITEM-9-park/changes.patch
```

A park before the product commit commits only the park's tracker change in the worktree and delivers that. A park after the product commit is recorded from your checkout, and the worktree is kept under `<scratch>/workspace`, so the item's branch is still there to inspect. In both cases none of the item's commits reach `origin`, except after `remote verification did not observe the delivered commit`, when the push may already have landed.

### Make an item ready again

A second attempt at a parked item, or at one that failed before its claim, starts clean. Once the item is ready again, the next campaign claims it as new work and builds it in a new worktree from your checkout's `HEAD`. It does not reuse the snapshot, the kept worktree, the old item branch or the earlier phase markers, so recover anything you want to keep first.

With a `BACKLOG.md` tracker, change the row's Status cell to `ready`. The words `approved`, `queued`, `todo`, `to do` and `not started` also mean ready. Then commit the change, because the claim refuses a checkout with uncommitted changes. Pushing it yourself is optional, because the next claim pushes it to `origin` with its own commit:

```
git commit -m "MODIFIED: make ITEM-9 ready in BACKLOG" -- BACKLOG.md
git push origin HEAD
```

With Pinax, a park leaves the builder's claim in place, and an item with a live claim is never ready. Release the claim first, and then set the status:

```
pinax release ITEM-9 --reason "retry after the fix"
pinax status ITEM-9 ready
```

`pinax release` commits its own change and pushes it when the default branch is checked out. If it prints that the item carries no live claim, the claim has already ended, for example because it expired, and there is nothing to release. `pinax status <id> ready` changes the local log only and neither commits nor pushes, so commit the changed files under `.ergon/` and push them yourself. If you set the status without releasing the claim, the next campaign sees an item still claimed by the builder and not parked. It treats that item as interrupted and tries to resume it, and the resume checks park it again.

### Clear a claim left behind

A claimed item that a later campaign leaves out of its selection stays claimed, because a campaign neither resumes nor reports a claim outside its selection. With a `BACKLOG.md` tracker such a row reads `Claimed by builder@<lane>`: change its Status cell to `ready`, or to the status you want, and commit and push it as above. With Pinax, run `pinax release <id> --reason "<why>"`.

Clear only a claim that no campaign will resume. A claim inside the next campaign's selection is how that campaign finds an interrupted item.

### Clean up and relaunch

AutoBuild removes less than it creates. Every item branch, `autobuild/<campaign>/<item>-<8 characters>`, stays in your repository after its worktree is removed, since no code deletes a branch. A worktree kept after a park stays registered, and every later campaign reports it as `resume:orphan-worktree` until you remove it. Work in this order:

1. Leave in place everything that belongs to an item you want resumed: its claim in the tracker, its worktree and the run folders under the same scratch root. These are what resume reads.
2. Take what you need from each parked item's snapshot and kept worktree.
3. Remove those kept worktrees and their branches from your checkout, before you relaunch, so the next campaign does not report them as orphans. Remove the worktree first, because Git does not delete a branch that a worktree has checked out.
4. Make the items you want to retry ready again, clear any claim left behind, and commit and push those changes.
5. Check that your checkout is clean and on the default branch, then launch again with the same scratch root.

These commands do step 3:

```
git worktree list
git worktree remove <worktree path>
git branch -D autobuild/<campaign>/<item>-<8 characters>
```

### Watch a campaign

Run this in a second terminal:

```
autobuild watch --latest
```

`watch` reads the run's `progress.log` every 0.1 seconds, prints each complete line to standard output as it lands, and never writes anything. Use `--run ID` in place of `--latest` to follow one run by its run id. `--latest` picks the run folder with the newest modification time under `<scratch>/runs`, whatever repository it belongs to, so on a shared scratch root it can follow another project's run.

`watch` looks for runs in `<scratch>/runs`, and it finds the scratch root from `--scratch-root`, else `[run] scratch_root` in the profile, else `autobuild` in the system temporary folder. A relative `--scratch-root` resolves against the profile's folder, or the repository when there is no profile, for `run`, `watch` and `pause` alike. Pass the same `--repository`, `--profile` and `--scratch-root` to all three.

`watch` exits 0 after it prints the completion line, 2 when the profile cannot be read or the run does not exist, and 3 when `--timeout-seconds` passes first. By default `autobuild run` also writes every progress line to standard error, and `[progress] command` can pass each line to a command of your own. AutoBuild writes `progress.log` only while `[progress] file` is true, which is the default.

Every progress line starts with its UTC time. These are the lines that mark the start of a stage:

```
item <id> seat <seat> started: model <m>, effort <e|default>, lane <l>
item <id> validation started: validator <id>[ (revalidation at delivery)][ (load rerun)]
item <id> delivery started: target branch <b>
batch validation <n> started: validator <id> on <rev>
```

A seat completion line ends with `, estimated cost <0.00>` when the lane reported a cost. Some events write no progress line, and `events.jsonl` holds them all. The completion line has this form:

```
campaign completed: shipped N, parked N, failed N; [batch validation failed on <rev>; output <ref>|error <err>|output none; ]report <path>; stop <reason> progress hook failures: <n>
```

The [events and records reference](reference/events-and-records.md) explains every event and every file in the run folder.

### Pause a campaign

`--latest` picks the newest run under `<scratch>/runs` whatever repository it belongs to, so on a shared scratch root it can pause another project's run. Name the run with `--run ID` when several repositories share the folder:

```
autobuild pause --latest
```

`pause` writes `pause-requested.json` into the run folder and prints `pause requested for run <id>: <marker path>`. `pause` exits 0 once the marker is written. It exits 2 when the run has already printed its completion line, and then it writes nothing.

The campaign checks for the marker before every admission and at every lease heartbeat. Once it sees the marker, it admits nothing new and lets the running items finish, which takes as long as those items need. While they run, the progress log shows `pause requested: no new admissions; waiting for <items> to finish, then the campaign stops`, or `pause requested: nothing is running, the campaign stops now` when nothing runs. The pause has taken effect when the completion line shows `stop paused`, which it prints after the report is committed.

To go on, launch `autobuild run --allow-delivery` again with the same scratch root. The new campaign removes the pause marker from the newest earlier run of the same repository under the same scratch root, prints `resuming paused run <run-id>: its pause request is lifted`, and builds the rest. If you never set a scratch root, the default is the same folder each time, as long as nothing has cleared the system temporary folder. Check that the earlier run folder is still under `<scratch>/runs` before you launch.

### Resume after an interruption

If a campaign was killed, or the machine stopped, launch it again with the same scratch root. At start, the new campaign looks for items that the tracker shows as claimed by a builder actor, such as `builder@claude-code`, that are neither done nor parked, and that are inside the selection. It reads each one's newest phase marker across all runs under `<scratch>/runs` and admits it before new work.

A resume goes ahead only when all of these hold:

- the marker is in a state that can resume;
- the marker's worktree is still registered;
- no other live campaign holds a lease on the worktree or the tracker;
- the worktree's head matches the marker, with an integration merge on top allowed;
- for `built`, `validated` and `reviewed`, the product changes are the same as when the marker was written.

The item then restarts from the state its marker records. Each state names the last step the item finished, and it decides where the item starts again:

- `claimed` (the tracker claim is taken), `isolated` (the worktree exists) and `correcting` (a correction round has started) run the builder again.
- `built` (the builder finished), `validated` (the validator passed), `reviewed` (a review returned) and `escalated` (a review escalated) run validation and review again.
- `finalised` (the item and tracker commits exist) runs the delivery again.

When a condition fails, resume refuses the item and parks it with one of the `resume:` reasons in the park table, with a snapshot taken in place except in the cases [Recover a parked item's work](#recover-a-parked-items-work) lists. A claimed item outside the selection is neither resumed nor reported, and its worktrees are left alone. It stays claimed until you [clear the claim](#clear-a-claim-left-behind). A worktree that no claimed item owns is reported as `resume:orphan-worktree` and left in place.

### Run a second campaign

The tracker lease keeps two campaigns on one repository apart. The lease covers the tracker's folder: `.ergon` for Pinax, or the folder that holds the backlog file. A campaign that finds a live lease held by another campaign writes the event `campaign.lease_refused` and exits 2 before any claim, and the `scratch` setup check names the holder's campaign, process and host.

A lease is live while its heartbeat is younger than `run.lease_stale_seconds` (1800 by default) and, on the same host, its process is alive. A holder on another host is judged by its heartbeat age only. A stale lease is reclaimed and listed in the report under Lease reclaims. AutoBuild never takes over a live lease.

The lease records live under `<scratch>/leases/`, so two campaigns see each other's leases only when they share a scratch root. Two campaigns with different scratch roots on one repository do not see each other. Resume and pause also work within one scratch root, so keep one scratch root per repository. Lane coolings are shared through `lanes.json` under `run.lane_state_root`, which is the scratch root unless you set it.

### Read the report

The report is `docs/campaigns/<campaign-id>.md` on the delivery target. When its delivery fails, the campaign result carries `report delivery failed: <error>` in place of the path. The run folder also holds `report.txt`, one line with the campaign, the item count, the stop reason and the report path.

The sections appear in this order, and those marked "when any" appear only when they have content.

| Section | What it holds |
|---|---|
| Shipped | The accepted and delivered items. |
| Parked | Each parked item with its reason, and any orphan worktree. |
| Failed | Each failed item with its reason. |
| Batch validation | Each batch run and the verdict. |
| Unfinished (when any) | Items that ended without a recorded park, and builder continuations the campaign stopped before admitting, each with its claim and worktree kept for the next campaign. |
| Skipped (when any) | Pinax items skipped with `no_brief` because they have no brief note. |
| Holds (when any) | The holds on admissions during the campaign. |
| Follow-ups | One subsection `### <id> <title>` per follow-up, with the parent item, the finding code, whether it was blocking (`yes`, `no` or `none`), the evidence reference, and the question and rationale in full. |
| Unpublished tracker writes (when any) | Tracker changes that were committed and not pushed. |
| Allowed items left unbuilt (with an allow-list) | Each allowed item that was not built, with its reason. |
| Lease reclaims (when any) | Each stale lease the campaign took over, with the previous holder. |
| Next | What to do next: `queue dry`, `hold limit`, `launch set unreadable`, `scope fence violation`, `tracker read failed: <error>`, a sentence for a pause, a sentence for a usage stop with the earliest reset, or the next ready item id. |
| Seat usage | The usage of each seat, with cost rounded to two places and `-` for a missing value. |
| Review depth | How deep each review went, and `none` rows when no review ran. |
| Schedule | The campaign's schedule. |
| Run record | The absolute paths of the run folder and of its `progress.log`. |

The reason for an allowed item left unbuilt is one of `excluded`, `outside the launch set`, `claim taken`, `blocked`, `not ready`, `item bound`, `hold limit`, `paused`, `launch set unreadable` or `ready after the last selection`.

## Limits

Because the launch set is read once, AutoBuild never selects an item created after the launch, and it never builds a follow-up it registered itself. It does not decide product scope, does not make a proposal runnable, does not open a pull request, and does not deploy your software or make a release. What it does push to `origin` is its tracker writes, the accepted changes and the report.

Because leases and run folders live under the scratch root, two campaigns on one repository protect each other only when they share it, and resume finds only the markers under the same scratch root. A live lease is never taken over, so a launch that finds one held by another campaign is refused with exit code 2 and waits for nothing. Launch again once that campaign has ended or its heartbeat has gone stale.

Because the number of correction rounds and delivery attempts is fixed in the code, no profile key changes them. Host pressure never stops a running seat: it slows admission, stretches seat time limits, switches the validator to its patient form and runs a validator that timed out once more. The strict process mode catches a builder's mistakes, and you should not treat it as a security barrier.

Because delivery of the report can fail, the report is committed only when that delivery succeeds, and an internal error in AutoBuild writes no report at all. The run folder under the scratch root keeps `events.jsonl` either way.
