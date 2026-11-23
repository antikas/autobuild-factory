# Events and records

Use this page to look up what a campaign leaves behind: the committed report, the run folder with its events, and the folders in the scratch root.

After a run, open the campaign report first. It is the Markdown file `docs/campaigns/<campaign-id>.md` in your repository, and its Shipped, Parked, Failed and Next sections tell you what happened and what needs you. Its Run record section gives the path of the run folder, where `events.jsonl` holds the full record.

The page runs in this order. [After a run](#after-a-run) walks from the report to the run folder and the scratch root. [Sharing a run folder](#sharing-a-run-folder) says what to check before a run folder leaves your machine. [Finding every parked item](#finding-every-parked-item) shows how to list them from the files. [Events](#events) has the fields every event shares, an [index of events by name](#index-of-events), and one table per stage. [Progress lines](#progress-lines), [the campaign report](#the-campaign-report) and [the printed result](#the-result-that-autobuild-run-prints) follow, and the page ends with [what the records do not hold or prove](#limits-of-the-records).

The [README](../../README.md) explains campaigns, items, seats and lanes. To watch, pause or recover a campaign, read [running campaigns](../running-campaigns.md). The meaning of each exit code is in the [command reference](commands.md).

## After a run

### The terms

- The scratch root is the folder the [README](../../README.md) defines: the one you name with `--scratch-root` or `[run] scratch_root`, and without either `autobuild` inside the system temporary folder.
- The folder `<scratch root>/runs` holds one run folder per campaign.
- A run folder is the record of one `autobuild run`, at `<scratch root>/runs/<run-id>/`. AutoBuild creates it when the campaign starts.
- An item folder is the folder AutoBuild makes for one item of one run. Its name is the cleaned item id and a 12-character hash, a length fixed in the code.

### From the campaign id to the run folder

The run id has the form `<campaign id>-<UTC time as YYYYMMDDTHHMMSSZ>-<8 hex characters>`, with the campaign id made safe for a file name. The campaign id is the value you pass with `--campaign-id`, or `autobuild-<UTC time as YYYYMMDDTHHMMSSZ>` when you pass none. The run folder of a campaign is therefore the folder under `<scratch root>/runs` whose name starts with its campaign id, for example:

```
ls -d <scratch root>/runs/autobuild-20261123T091500Z-*
```

`autobuild watch --latest` and `autobuild pause --latest` pick the run folder with the newest modification time under `<scratch root>/runs`, whatever repository it belongs to. Give every campaign on one repository the same scratch root, and use `--run <run-id>` when several repositories share it.

### The run folder

| Path | Written | What it holds |
|---|---|---|
| `manifest.json` | At the start | Schema `autobuild.run.v1`, with `run_id`, `campaign_id`, `repository` (an absolute path), `created_at` and a `runtime` block. `runtime` holds `selection`, `preflight`, `workflow_version`, `adapters`, `harness`, `lanes`, `tracker`, `models`, `validator`, `refill` and `concurrency`. `runtime.preflight` holds the result of the full preflight pass, and `lanes` holds `order`, `active`, `cool_seconds`, `state` and `cooled`. |
| `events.jsonl` | Through the run | One JSON object per line with sorted keys, one line per event, appended in order. See [Events](#events). |
| `progress.log` | Through the run, while `[progress] file` is true, which is the default | One plain line for each event that has a progress line. See [Progress lines](#progress-lines). |
| `report.txt` | Only when the campaign completes | One line: `campaign=<id>; items=<n>; stop=<reason>; report=<path>`, where the path is replaced by `report delivery failed: <error>` when the report could not be delivered. |
| `evidence/` | Created empty at the start | The evidence files of each item, below. |
| `pause-requested.json` | When you run `autobuild pause` | The pause marker: schema `autobuild.pause.v1`, with `run_id` and `requested_at`. A second request replaces the first, and a pause on a completed run is refused. The next campaign of the same repository under the same scratch root removes the marker from the newest earlier run. |

The evidence folder holds these files:

| Path | What it holds |
|---|---|
| `<item>-phase.json` | The phase marker, schema `autobuild.item-phase.v1`, with `item_id`, `state`, `worktree_root`, `branch`, `head_commit`, `workspace_revision`, `correction_count` and `timestamp`. AutoBuild rewrites it as the item moves on. An accepted item's marker ends `closed` and a parked item's `parked`. A failed item, an item whose park failed, and a builder continuation keep the last state written. |
| `<item>-builder-report-<n>.txt` | The JSON `summary` and `report_ref` of each successful builder seat, numbered in order. The latest one is handed to the reviewer and to the boundary specialists. |
| `<item>-review-round-<n>.txt` | The JSON `seats` and `verdicts` of one review round, where `verdicts` holds each seat's own verdict. It is written only for a round with boundary specialists, and `<n>` is the number of corrections so far plus one. |
| `<item>-trajectory.txt` | Lines of the form `disposition: <disposition>`, `states: <state> -> <state> ...`, one `seat <seat>: <outcome> (model_class=<class>, lane=<lane>)` line per seat, and `reason: <reason>`. It is written for accepted, parked and failed items. |
| `<item>-park/` | The snapshot of a parked item's worktree: `changes.patch`, the `files/` folder with a copy of every untracked file that git does not ignore, and `snapshot.json`. `snapshot.json` has schema `autobuild.park-snapshot.v1`, with `item_id`, `reason`, `start_commit`, `patch`, `tracked_paths`, `files` and `seats`. |

A park takes a snapshot only when the item has a worktree, so a park at triage or before the worktree has none. When an item park takes a snapshot, the park reason in the tracker ends with ` [snapshot=evidence/<item>-park]`. A resume park writes the bare `resume:` reason to the tracker before it takes its snapshot.

A resume reads the phase marker of an item from every run folder under `<scratch root>/runs`, and uses the newest one by modification time. It restarts the item from the marker's state: `claimed`, `isolated` and `correcting` re-run the builder; `built`, `validated`, `reviewed` and `escalated` re-run validation and review; `finalised` re-runs delivery.

### The scratch root

The scratch root holds these folders beside `runs`. Every run on the same scratch root shares them.

| Path in the scratch root | What it holds |
|---|---|
| `workspace/worktrees/<item>-<8 hex characters>` | The item worktrees, each on a branch named `autobuild/<campaign>/<item>-<8 hex characters>`. |
| `workspace/evidence/<lease id>/changes.patch` | The review patch of an item's current candidate. |
| `workspace/reports/`, `workspace/validation/` | The checkouts that deliver the campaign report, and the checkouts for batch validation. |
| `harness/<lane>/prompts/` | The instructions file of each seat, which holds the seat's prompt, the brief and the evidence text. |
| `harness/<lane>/contracts/` | The JSON schema of each seat's result. |
| `harness/<lane>/normalised/` | Each seat's result as AutoBuild read it. A builder's `report_ref` points here. |
| `harness/<lane>/last-messages/` | The last message of each Codex seat. |
| `harness/<lane>/logs/` | The session logs of each Copilot seat. |
| `harness/<lane>/scratch/` | The caches of the lane's seats, and each seat's temporary folder, `scratch/<item folder>/tmp`. |
| `harness/<lane>/recovered/` | Builder results that a Claude seat wrote into its worktree and AutoBuild recovered. |
| `harness/<lane>/test-guards/` | The test guard of each strict builder seat, which checks the test commands the builder runs. |
| `harness/<lane>/sdk/` | The full message stream of each `claude-sdk` seat, tool output included. |
| `harness/<lane>/config/` | A fresh, empty configuration folder for each `claude-sdk` seat. |
| `harness/<lane>/subscription-preflight/` | The working folder of the sign-in check under subscription routing. |
| `harness/preflight/` | The working folder of the setup checks. |
| `commands/<command-id>-<8 hex characters>/` | `stdout.txt` and `stderr.txt` of every command AutoBuild runs: command-line seats, validators, batch validation, the usage meter and probes. The command id of a seat is `<run-id>:<item>:<seat>:<lane>`, with every character other than letters, digits, `.`, `_` and `-` replaced by `-`. |
| `items/<item folder>/` | The temporary folder and the pytest base folder of each item's or batch's validator commands. |
| `tmp/`, `uv-cache/`, `pip-cache/`, `pycache/`, `xdg-cache/`, `npm-cache/` | The temporary folder of the AutoBuild process itself, and the package caches. |
| `leases/` | The lease records, schema `autobuild.lease.v1`. Two campaigns see each other's leases only when they share this scratch root. |
| `lanes.json` and `lanes.json.lock` | The lane coolings, schema `autobuild.lanes.v1`, here when `[run] lane_state_root` is not set and under that folder when it is. |
| `usage-meter/<meter id>.json` | The shared usage reading, schema `autobuild.usage-reading.v1`. It holds the normalised reading, and beside a rate-limited or transport failure it also keeps the last good reading. |
| `usage/` | The working folder of the usage meter commands. |
| `subscription-admission/factory.db` | The admission ledger of subscription routing, shared by every process on this scratch root. |
| `subscription-reviews/` | The detached checkouts of `autobuild subscription-review` in `worktrees/`, removed after each review, and its receipts in `receipts/<receipt-id>.json`. |
| `preflight/<id>/` | The private lane state of a preflight check. The id is the campaign id when there is one, and `preflight-<UTC time as YYYYMMDDTHHMMSSZ>` otherwise. |

AutoBuild never removes the item folders under `harness/<lane>/scratch/` and `items/`.

## Sharing a run folder

Because AutoBuild captures command and seat output unredacted, the scratch root can hold secrets and seat transcripts. Every seat and validator command starts from the environment of the AutoBuild process. Its standard output and standard error are written as they are to `commands/<command-id>-<8 hex characters>/stdout.txt` and `stderr.txt`, so a validator that prints its environment writes any credential in it. The command-line lanes keep each seat's output there, and the `claude-sdk` lane writes every message of each seat, tool output included, to `harness/<lane>/sdk/`. Treat the scratch root as private.

A run folder is smaller than the scratch root, and it can still hold private data. Check these files before you attach a run folder to a bug report:

- `evidence/<item>-park/files/` holds a copy of every untracked file of a parked worktree that git does not ignore. An untracked `.env` file that is not in `.gitignore` is copied here. Remove anything that must not leave your machine.
- `evidence/<item>-park/changes.patch` holds the item's whole change.
- `events.jsonl` holds absolute paths that contain your user name, for example in `evidence_refs`, `raw_output_ref` and `stdout_ref`. `seat.launched` holds each seat's full argument vector. Item titles, brief paths and error texts appear in many events.
- `manifest.json` holds the absolute path of the repository.
- `progress.log`, `report.txt` and the builder reports hold paths, titles and the builder's own summary.

The references in a run folder point at captured output in the scratch root. Never attach `commands/` or `harness/` without reading every file you attach.

AutoBuild's own records are narrower than the captured output. A seat's launch event lists the names of the environment variables it sets and removes, never their values. AutoBuild never writes the `claude-sdk` lane's API key to a record, log, brief or commit. The Claude usage meter uses Claude Code's own sign-in, which AutoBuild never prints or stores, and the shared reading in `usage-meter/` holds the normalised reading only. A `subscription-review` receipt keeps only the finding citations that resolve to files inside the reviewed tree. It replaces any sentence that holds a `/` or `\` with a sentence saying that a diagnostic was withheld.

## Finding every parked item

An item can be parked in four ways, and each one writes its own events:

| Park | Events |
|---|---|
| After a seat, a validator, a review or a delivery | `item.parked` |
| At triage, before any claim | `item.triaged`, then `item.parked` |
| On resume | `item.resume_parked` only, with no `item.parked` |
| When every lane is set aside | `item.parked` with the reason `<lane>:<signature>`, for a claimed item. An item that was not yet claimed ends `failed` and writes no park event. |

A park that cannot land in the tracker writes `item.park_failed`, and the item counts as `failed`, not parked. `resume.orphan_worktree` names a worktree, not an item, and the report lists it under Parked as `resume:orphan-worktree`.

To list every parked item of one run, search the events file for the two park events, for example with `grep`:

```
grep -E '"event_type": "item\.(parked|resume_parked)"' <run folder>/events.jsonl
```

The report's Parked section holds the same items, each with its reason.

## Events

An event is one line of `events.jsonl`. The launch set is the id of every item the tracker holds once the campaign has its tracker lease, read once; a campaign builds only items of its launch set. Every event is a JSON object with these fields:

| Field | Type | Holds |
|---|---|---|
| `event_type` | string | The name of the event, from the tables below. |
| `occurred_at` | string | The time of the event, in UTC ISO 8601 with offset. |
| `item_id` | string or null | The item the event belongs to. It is null on the campaign-level events: those whose name starts with `campaign.`, except `campaign.scope_fence_violation` when the selected item lies outside the launch set, and also `resume.orphan_worktree` and `workspace.lease_release_diagnostic`. |
| `evidence_refs` | list of strings | Absolute paths of evidence files. |
| `payload` | object | The fields particular to the event type, listed in the tables below. |

A field in square brackets in the tables appears only in the case the table names.

### Field meanings

Most payload fields say what they hold. These do not:

| Field | Type | Meaning |
|---|---|---|
| `accepted`, `parked`, `failed` in `campaign.completed` | integer | The number of items with each disposition. The completion line and the report call the accepted items shipped. |
| `report` in `campaign.completed` | string | The report's path inside the repository, `docs/campaigns/<campaign-id>.md`. |
| `report_path` in `campaign.completed` | string | The report's absolute path in your checkout, or `report delivery failed: <error>`. |
| `structural` in `item.parked` | boolean | True when the item failed on broken evidence or an internal type or value error, such as a seat result that does not fit its contract, and not on a seat, a validator or a review decision. |
| `kill_kind` in `item.parked` | string | How the last seat ended when AutoBuild stopped it or it failed: `stall`, `timeout`, or the seat's outcome (`failed`, `cancelled` or `unavailable`). |
| `last_sample_times` in `item.parked` | object | For each progress signal of the killed seat (`output`, `worktree`, `cpu`), the seconds after the seat started when it last advanced. |
| `last_state` in `item.parked` | string | The item state the park left, such as `built`. |
| `seat_evidence` in `item.parked` | list of objects | One entry per seat, with `seat`, `outcome`, `raw_output_ref` and `stderr_ref`. |
| `limits` in `usage.stop` and `usage.seat_rerouted` | list of objects | The binding limits: the limits at or above their stop level, each with `kind`, `scope`, `percent` and `resets_at`. |
| `reset_at` in `usage.stop` | string or null | The latest reset among the binding limits. |
| `detail` in `usage.stop` | string | Each binding limit as `<kind> <percent>%, resets <time> (stop <level>%)`, joined by `; `. |
| `reading_age_seconds` | integer or null | The age of the meter reading in whole seconds, on the clock that stamps the run's events, null when the reading has no fetch time. |
| `model_class` | string | The seat's model key: `builder`, `reviewer` or `specialist`. |
| `signature` in `lane.cooled` | string | The kind of signal that set the lane aside, the same word as `kind`. |
| `review_lane` in `item.finalised` | string or null | The item's review size: `small` for an item that touches no boundary in strict mode, `material` for one that does, and null in flow mode or without a boundary map. A boundary is a part of the repository named under `[process.boundaries]`. |
| `patient` | boolean | True when the validator ran in its patient form under host pressure, see [Seats and validation](#seats-and-validation). |
| `candidate` in `validation.started` | string | `reviewed` for the candidate the reviewer read, `integrated` for the candidate after delivery merged the target. |

### Example lines

Each line below is one event as AutoBuild writes it. The values are made up.

```
{"event_type": "item.parked", "evidence_refs": ["/home/dev/autobuild/runs/autobuild-20261123T091500Z-20261123T091500Z-3f9c2a1e/evidence/ITEM-7-trajectory.txt"], "item_id": "ITEM-7", "occurred_at": "2026-11-23T09:41:07.512334+00:00", "payload": {"corrections_completed": 0, "last_state": "built", "reason": "validator tests did not pass", "seat_evidence": [{"outcome": "succeeded", "raw_output_ref": "/home/dev/autobuild/commands/autobuild-20261123T091500Z-20261123T091500Z-3f9c2a1e-ITEM-7-builder-claude-code-5d6e7f80/stdout.txt", "seat": "builder", "stderr_ref": "/home/dev/autobuild/commands/autobuild-20261123T091500Z-20261123T091500Z-3f9c2a1e-ITEM-7-builder-claude-code-5d6e7f80/stderr.txt"}], "snapshot_path": "evidence/ITEM-7-park", "snapshotted_paths": ["changes.patch", "snapshot.json"], "structural": false}}
```

```
{"event_type": "usage.stop", "evidence_refs": [], "item_id": "ITEM-8", "occurred_at": "2026-11-23T10:02:11.204118+00:00", "payload": {"detail": "weekly_all 91%, resets 2026-11-27T08:00:00+00:00 (stop 90%)", "lane": "claude-code", "limits": [{"kind": "weekly_all", "percent": 91.0, "resets_at": "2026-11-27T08:00:00+00:00", "scope": null}], "meter_id": "claude-subscription", "reading_age_seconds": 14, "reset_at": "2026-11-27T08:00:00+00:00", "seat": "reviewer"}}
```

```
{"event_type": "campaign.completed", "evidence_refs": [], "item_id": null, "occurred_at": "2026-11-23T10:15:42.870021+00:00", "payload": {"accepted": 2, "batch_validation": "passed", "failed": 0, "parked": 1, "report": "docs/campaigns/autobuild-20261123T091500Z.md", "report_path": "/home/dev/projects/example/docs/campaigns/autobuild-20261123T091500Z.md", "stop_reason": "queue_dry"}}
```

### Index of events

| Event | Stage |
|---|---|
| `campaign.batch_started` | [Batch validation](#batch-validation) |
| `campaign.batch_validated` | [Batch validation](#batch-validation) |
| `campaign.batch_validation_failed` | [Batch validation](#batch-validation) |
| `campaign.cap_changed` | [Admission and host pressure](#admission-and-host-pressure) |
| `campaign.completed` | [Holds and stops](#holds-and-stops) |
| `campaign.draining` | [Admission and host pressure](#admission-and-host-pressure) |
| `campaign.hold_limit` | [Holds and stops](#holds-and-stops) |
| `campaign.hold_released` | [Holds and stops](#holds-and-stops) |
| `campaign.hold_retry` | [Holds and stops](#holds-and-stops) |
| `campaign.hold_started` | [Holds and stops](#holds-and-stops) |
| `campaign.lanes_exhausted` | [Holds and stops](#holds-and-stops) |
| `campaign.launch_set_failed` | [Campaign start, lease and launch set](#campaign-start-lease-and-launch-set) |
| `campaign.launch_set_fixed` | [Campaign start, lease and launch set](#campaign-start-lease-and-launch-set) |
| `campaign.lease_acquired` | [Campaign start, lease and launch set](#campaign-start-lease-and-launch-set) |
| `campaign.lease_reclaimed` | [Campaign start, lease and launch set](#campaign-start-lease-and-launch-set) |
| `campaign.lease_refused` | [Campaign start, lease and launch set](#campaign-start-lease-and-launch-set) |
| `campaign.lease_release_diagnostic` | [Campaign start, lease and launch set](#campaign-start-lease-and-launch-set) |
| `campaign.lease_renew_failed` | [Campaign start, lease and launch set](#campaign-start-lease-and-launch-set) |
| `campaign.lease_restored` | [Campaign start, lease and launch set](#campaign-start-lease-and-launch-set) |
| `campaign.lease_withheld` | [Campaign start, lease and launch set](#campaign-start-lease-and-launch-set) |
| `campaign.pause_check_failed` | [Pause and resume](#pause-and-resume) |
| `campaign.pause_requested` | [Pause and resume](#pause-and-resume) |
| `campaign.pressure_changed` | [Admission and host pressure](#admission-and-host-pressure) |
| `campaign.pressure_fallback` | [Admission and host pressure](#admission-and-host-pressure) |
| `campaign.pressure_read_failed` | [Admission and host pressure](#admission-and-host-pressure) |
| `campaign.pressure_unmeasured` | [Admission and host pressure](#admission-and-host-pressure) |
| `campaign.refill_failed` | [Park, failure and follow-ups](#park-failure-and-follow-ups) |
| `campaign.resume_scan_failed` | [Pause and resume](#pause-and-resume) |
| `campaign.resumed` | [Pause and resume](#pause-and-resume) |
| `campaign.scope_fence_violation` | [Campaign start, lease and launch set](#campaign-start-lease-and-launch-set) |
| `campaign.started` | [Campaign start, lease and launch set](#campaign-start-lease-and-launch-set) |
| `campaign.usage_stop` | [Holds and stops](#holds-and-stops) |
| `campaign.waiting` | [Admission and host pressure](#admission-and-host-pressure) |
| `delivery.revalidation` | [Delivery and close](#delivery-and-close) |
| `delivery.started` | [Delivery and close](#delivery-and-close) |
| `item.admitted` | [Admission and host pressure](#admission-and-host-pressure) |
| `item.builder_continued` | [Seats and validation](#seats-and-validation) |
| `item.claim_taken` | [Triage, claim and worktree](#triage-claim-and-worktree) |
| `item.claimed` | [Triage, claim and worktree](#triage-claim-and-worktree) |
| `item.correcting` | [Review and correction](#review-and-correction) |
| `item.delivery_waiting` | [Delivery and close](#delivery-and-close) |
| `item.effort_raised` | [Seats and validation](#seats-and-validation) |
| `item.finalised` | [Delivery and close](#delivery-and-close) |
| `item.follow_up_failed` | [Park, failure and follow-ups](#park-failure-and-follow-ups) |
| `item.follow_up_proposed` | [Park, failure and follow-ups](#park-failure-and-follow-ups) |
| `item.lane_changed` | [Triage, claim and worktree](#triage-claim-and-worktree) |
| `item.park_delivery_failed` | [Park, failure and follow-ups](#park-failure-and-follow-ups) |
| `item.park_failed` | [Park, failure and follow-ups](#park-failure-and-follow-ups) |
| `item.parked` | [Park, failure and follow-ups](#park-failure-and-follow-ups) |
| `item.resume_park_failed` | [Pause and resume](#pause-and-resume) |
| `item.resume_parked` | [Pause and resume](#pause-and-resume) |
| `item.resume_selected` | [Pause and resume](#pause-and-resume) |
| `item.resume_snapshot_failed` | [Pause and resume](#pause-and-resume) |
| `item.resumed` | [Pause and resume](#pause-and-resume) |
| `item.skipped` | [Triage, claim and worktree](#triage-claim-and-worktree) |
| `item.tracker_failed` | [Triage, claim and worktree](#triage-claim-and-worktree) |
| `item.triaged` | [Triage, claim and worktree](#triage-claim-and-worktree) |
| `lane.cooled` | [Lanes and usage](#lanes-and-usage) |
| `lane.usage_cooling_cleared` | [Lanes and usage](#lanes-and-usage) |
| `resume.orphan_worktree` | [Pause and resume](#pause-and-resume) |
| `review.combined` | [Review and correction](#review-and-correction) |
| `review.completed` | [Review and correction](#review-and-correction) |
| `seat.completed` | [Seats and validation](#seats-and-validation) |
| `seat.launched` | [Seats and validation](#seats-and-validation) |
| `seat.started` | [Seats and validation](#seats-and-validation) |
| `specialist.completed` | [Review and correction](#review-and-correction) |
| `subscription.admission` | [Triage, claim and worktree](#triage-claim-and-worktree) |
| `tracker.unpublished` | [Triage, claim and worktree](#triage-claim-and-worktree) |
| `usage.meter_failed` | [Lanes and usage](#lanes-and-usage) |
| `usage.seat_rerouted` | [Lanes and usage](#lanes-and-usage) |
| `usage.stop` | [Lanes and usage](#lanes-and-usage) |
| `usage.warning` | [Lanes and usage](#lanes-and-usage) |
| `validation.completed` | [Seats and validation](#seats-and-validation) |
| `validation.load_rerun` | [Seats and validation](#seats-and-validation) |
| `validation.started` | [Seats and validation](#seats-and-validation) |
| `workspace.created` | [Triage, claim and worktree](#triage-claim-and-worktree) |
| `workspace.lease_release_diagnostic` | [Delivery and close](#delivery-and-close) |
| `workspace.release_refused` | [Delivery and close](#delivery-and-close) |

### Campaign start, lease and launch set

The tracker lease is the lock record a campaign holds on its tracker and renews on a timer, the heartbeat, so that no second campaign on the same scratch root uses the tracker at the same time.

| Event | Written when | Payload |
|---|---|---|
| `campaign.started` | The run folder exists. It is the first event of a campaign. | `harness`, `models`, `efforts`, `item_bound`, `delivery_mode`, `validator_id`, `manifest_path` |
| `campaign.lease_acquired` | A fresh tracker lease is granted, with no stale lease to reclaim. | `campaign_id`, `process_id`, `host`, `started_at`, `heartbeat_at`, `surface_path`, `surface_kind` |
| `campaign.lease_refused` | Another campaign holds a live lease on the tracker. `autobuild run` then exits 2 before any claim, and no report is written. | The same fields as `campaign.lease_acquired`, describing the holder. |
| `campaign.lease_reclaimed` | A stale tracker lease is taken over. The report lists it under Lease reclaims. | `surface_kind`, `surface_path`, `previous_holder` |
| `campaign.lease_renew_failed` | A renewal of the tracker lease fails, at the heartbeat or between items. | `error`, `consecutive` |
| `campaign.lease_withheld` | The third renewal in a row fails, a count fixed in the code. No item is admitted until a renewal succeeds. | `failures`, `error` |
| `campaign.lease_restored` | A renewal succeeds after `campaign.lease_withheld`. Admissions resume. | No fields. |
| `campaign.lease_release_diagnostic` | At the end of the campaign, the release of the tracker lease reports that nothing was released. | `surface_kind`, `surface_path`, `diagnostics` |
| `campaign.launch_set_fixed` | The launch set is read, once the lease is held. | `items`, the number of items. |
| `campaign.launch_set_failed` | The tracker cannot list its items at launch. Nothing is admitted, and the campaign ends `launch_set_unreadable`. | `error` |
| `campaign.scope_fence_violation` | Selection offers an item outside the selection fence, the allow and exclude lists of `[selection]` and the command line, or the selected item lies outside the launch set. Nothing is claimed. | `error` |

### Admission and host pressure

The pressure monitor samples the machine's processor and memory load every `[pressure] sample_seconds` and holds back new items when the load is high. AutoBuild runs it, and writes `item.admitted`, `campaign.waiting` and `campaign.draining`, only in a campaign that may run more than one item at a time.

| Event | Written when | Payload |
|---|---|---|
| `item.admitted` | A new or resumed item starts on a worker. | `running`, `concurrency`, `resume` |
| `campaign.waiting` | Admission is held back, once for each distinct state. | `reason` (`cap`, `usage`, `lease`, `path_overlap` or `pressure`), `running`, `concurrency`, `items` |
| `campaign.draining` | A stop, or a scheduler error, is first seen while items still run. | `stop_reason` (`error` for a scheduler error), `running` |
| `campaign.pressure_changed` | The pressure monitor's level changes. | `level`, `previous`, `cpu_busy_percent`, `memory_available_gib`, `memory_high_line_gib`, `cap` |
| `campaign.cap_changed` | The pressure monitor changes the effective item cap. | `cap`, `previous`, `capacity`, `level` |
| `campaign.pressure_unmeasured` | The first host reading has no CPU busy share or no memory figure, or fails. The pressure monitor stops. | `unmeasured`, `diagnostics`, `concurrency` |
| `campaign.pressure_read_failed` | After a measured reading, a reading fails, the first of a run of failures. | `consecutive`, `unmeasured`, `diagnostics` |
| `campaign.pressure_fallback` | The third host reading in a row fails, a count fixed in the code. | `consecutive`, `level`, `cap`, `capacity` |

### Holds and stops

A hold stops new admissions until its condition clears. The condition is one of `lanes_exhausted`, `tracker_pending`, `tracker_unreachable`, `tracker_read_failed`, `lease_renew_failed` and `batch_validation_failed`, and only one hold is in force at a time.

| Event | Written when | Payload |
|---|---|---|
| `campaign.lanes_exhausted` | Admission finds every lane set aside and no usage cooling clears. A `campaign.usage_stop` or a `lanes_exhausted` hold follows. | `lanes`, the lane names. |
| `campaign.usage_stop` | Every lane is set aside with the `usage` signature. No new item is admitted, running items finish, and the campaign ends `usage_stop`. | `lanes`, each with `lane`, `cooled_until` and `signature`; `resets_at`, the earliest reset; `running` |
| `campaign.hold_started` | A hold begins. | `condition`, `detail`, `retry_seconds`, `limit_seconds` |
| `campaign.hold_retry` | A due check finds the condition still in force. | `condition`, `retry`, `retry_seconds`, `detail` |
| `campaign.hold_released` | A check finds the condition cleared, or a batch passes during a batch hold. Admissions resume. | `condition`, `seconds`, `retries` |
| `campaign.hold_limit` | The hold has lasted `run.hold_limit_seconds`. The campaign ends `hold_limit`. | `condition`, `seconds`, `limit_seconds`, `detail` |
| `campaign.completed` | The last event of a completed campaign, after the report is delivered and before `report.txt` is written. A campaign whose lease was refused, or that ended on a scheduler error, writes none. | `stop_reason`, `accepted`, `parked`, `failed`, `report`, `report_path`, `batch_validation`, and [when the batch failed] `batch_revision`, `batch_stdout_ref`, `batch_error` |

### Batch validation

A batch validation runs the validator on the head of the delivery target after every `run.batch_validate_every` deliveries. At the end of the campaign it runs once more only when something was delivered since the last batch, or when a batch is still due. During a hold for a failed batch, a new batch also starts when the head of the target has moved. A heavy slot is one of the places, at most `run.heavy_concurrency` at once, that every validator run takes when more than one item may run.

| Event | Written when | Payload |
|---|---|---|
| `campaign.batch_started` | A batch's validator run is given its heavy slot. A checkout that cannot open writes no start event, only a failed result. | `batch`, `revision`, `validator_id` |
| `campaign.batch_validated` | The batch passes. | `batch`, `revision`, `deliveries`, `commits`, `final`, `exit_code`, `stdout_ref`, [under host pressure] `patient`, [on an error] `error` |
| `campaign.batch_validation_failed` | The batch fails, or its checkout cannot open. AutoBuild reverts nothing and holds admissions with `batch_validation_failed`, except at the end of the campaign, when nothing is held. | The same fields as `campaign.batch_validated`. |

### Pause and resume

| Event | Written when | Payload |
|---|---|---|
| `campaign.pause_requested` | An admission decision finds the pause marker. AutoBuild admits nothing more, lets running items finish, and ends `paused`. | `running`, the list of running items. |
| `campaign.pause_check_failed` | The pause check at the lease heartbeat fails. The lease still renews. | `error`, `consecutive` |
| `campaign.resumed` | Before the first admission, AutoBuild removes the pause marker of the newest earlier run of the same repository. | `prior_run_id` |
| `campaign.resume_scan_failed` | The tracker's list of items a builder still holds cannot be read. Nothing is resumed. | `error` |
| `item.resume_selected` | A resume candidate's phase marker passes every check, and the item is admitted to resume. | `prior_run_id`, `marker_state`, `correction_count` |
| `item.resumed` | On the worker, the kept worktree is adopted and its lease taken, before the item re-enters its workflow. Builder continuations write it too. | `marker_state`, `prior_run_id`, `correction_count`, `worktree_root`, `branch` |
| `item.resume_parked` | A resume candidate fails a check and parks with a `resume:` reason: `resume:missing-marker`, `resume:tracker-mismatch`, `resume:missing-worktree`, `resume:lease-held`, `resume:head-moved` or `resume:revision-changed`. | `reason`, `marker_state`, `snapshot_path`, `worktree_roots` |
| `item.resume_park_failed` | The tracker park of a resume candidate fails. The item counts as failed. | `reason`, `error` |
| `item.resume_snapshot_failed` | Adopting or snapshotting the worktree of a failed resume fails. The park stands without a snapshot. | `reason`, `error` |
| `resume.orphan_worktree` | After the resume candidates are admitted, or at the end of the schedule, a registered worktree in the scratch root belongs to no claimed item. AutoBuild leaves it in place and lists it under Parked as `resume:orphan-worktree`. | `worktree_root` |

### Triage, claim and worktree

Triage is the check of a brief before any claim. It parks an item whose brief file cannot be used, whose brief cannot be built in a worktree, or whose brief orders git staging.

| Event | Written when | Payload |
|---|---|---|
| `item.skipped` | The tracker passed over a ready item of the launch set, such as a Pinax item without a brief note. It is written once per item and campaign. | `reason`, for example `no_brief` |
| `item.triaged` | Before any claim, the item parks because its brief file is missing, empty, over 1048576 bytes or not readable as UTF-8 text, because its nature is not `repository` or because its brief orders git staging. A `repository` brief that declares a path outside the repository and the allowed roots gets the nature `cross-repository`. `item.parked` follows. | `nature` and `reason`; or, for a brief file that cannot be used, `triage` (`brief:missing`, `brief:empty`, `brief:too-large` or `brief:unreadable`) and `reason`; or, for git staging, `triage` (`brief:git-staging`), `reason` and `lines`, each with `line_number`, `invocations` and `text` |
| `subscription.admission` | Under subscription routing, admission decides a lane: for the builder before the claim, and then for each later seat attempt. | `seat`, `lanes` and `recommendation`. Each row of `lanes` has `lane`, `eligible`, `reason`, `score`, `windows` and, at a stop, `usage_reset_at`. |
| `item.claimed` | The tracker claim succeeds. | `actor`, `lane`, `brief_ref`, `title` |
| `item.claim_taken` | The claim reports that another writer took the item first. The campaign selects the next item. | `error` |
| `tracker.unpublished` | A claim, park or proposal is committed to the tracker and not yet published. | `operation`, `note` |
| `item.tracker_failed` | The item ends with a tracker stop. AutoBuild asks the tracker to recover, and holds only when the recovery does not come back clear. | `kind`, `exit_code`, `error` |
| `workspace.created` | The worktree is created and its lease taken. The evidence reference is the start commit. | `worktree_root`, `branch`, `start_commit` |
| `item.lane_changed` | After the first build, the builder runs on a lane other than the one it claimed on. The item is not claimed again. | `lane`, `from_lane` |

### Seats and validation

| Event | Written when | Payload |
|---|---|---|
| `seat.started` | Each seat attempt, just before the lane runs it. | `seat`, `model_class`, `model`, `effort`, `lane` |
| `seat.launched` | The lane reports the launch, just before the seat runs. | `seat`, `lane`, `argv`, `set_environment` and `removed_environment` (variable names), `in_process` |
| `seat.completed` | Each seat attempt ends. `raw_output_ref` points at the seat's raw output in the scratch root. | `seat`, `model_class`, `effort`, `model`, `outcome`, `exit_code`, `started_at`, `ended_at`, `duration_seconds`, `input_tokens`, `output_tokens`, `cost`, `raw_output_ref`, `stderr_ref`, `lane`, and [when host pressure stretched the seat's time limit] `timeout_extension_seconds` |
| `item.builder_continued` | The builder timed out before the item commit, and a continuation is left. The claim and worktree are kept, up to two continuations, a number fixed in the code. | `reason`, `continuation`, `limit`, `marker_state`, `worktree_root` |
| `validation.started` | A validator run, on the reviewed candidate, on the integrated candidate at delivery, or as a rerun, is given its heavy slot. | `validator_id`, `candidate`, `rerun` |
| `validation.load_rerun` | A validator timed out while host pressure held back heavy commands. AutoBuild runs it once more alone, and that verdict stands. | `validator_id`, `exit_code`, `timed_out`, `duration_seconds`, `stdout_ref`, `stderr_ref` |
| `validation.completed` | Each validator run ends, after any rerun. | `validator_id`, `exit_code`, `timed_out`, `duration_seconds`, `stdout_ref`, `stderr_ref`, `changed_path_count` or `candidate`, and [under host pressure] `patient` |
| `item.effort_raised` | The validator failed and a higher builder effort is left. A fresh builder retries at that effort. | `seat`, `from_effort`, `effort`, `raises`, `triggering_evidence_ref` |

While the pressure monitor runs, a validator that starts at host pressure `high` or `critical`, or within 60 seconds after, a window fixed in the code, runs in its patient form. The patient form is `[validator] patient_argv`, or the usual argument list with `AUTOBUILD_HOST_PRESSURE=1` when `patient_argv` is not set. Its `validation.completed` event, or the batch event, then carries `patient: true`.

### Review and correction

| Event | Written when | Payload |
|---|---|---|
| `review.completed` | AutoBuild accepts the blind reviewer's verdict. | `decision`, `finding_codes`, `blocking_finding_codes`, `non_blocking_finding_codes`, `evidence_ref`, `duration_seconds`, `inspected`, `acceptance`, `depth` |
| `specialist.completed` | AutoBuild accepts a specialist's verdict: a boundary specialist in strict mode, or the escalation specialist. | The verdict fields, `boundary`, `duration_seconds`, `inspected`, `acceptance`, and [for a boundary specialist] `depth` |
| `review.combined` | A round with boundary specialists is combined. The round takes the most severe decision, in the order park, escalate, correct, pass, and keeps the findings, inspected paths and clause checks in seat order. | The verdict fields, `boundaries` |
| `item.correcting` | A correction round starts. | `round`, `triggering_evidence_ref` |

### Delivery and close

| Event | Written when | Payload |
|---|---|---|
| `item.delivery_waiting` | A delivery, of an item or of a park, waits behind others, because deliveries run one at a time. | `ahead` |
| `delivery.started` | A delivery's turn begins, for an item or a park from the worktree. | `target_branch` |
| `delivery.revalidation` | Integrating the target brought in product changes since the validated candidate, and AutoBuild decides whether to run the validator again. | `run`, `reason` (`overlap`, `no_overlap`, `docs_only`, `unknown_scope` or `no_product_change`), `incoming`, `overlapping` |
| `item.finalised` | The delivery is confirmed, before the worktree is released. A resumed `finalised` item writes it too. | `item_commit`, `tracker_commit`, `merged_commit`, `pushed`, `review_lane`, `seat_lanes`; or, for a resumed item, `resumed` |
| `workspace.release_refused` | Releasing the worktree at the end of the item fails. | `error` |
| `workspace.lease_release_diagnostic` | The release of the worktree lease releases nothing. | `surface_path`, `diagnostics` |

### Park, failure and follow-ups

Refill registers the proposals of the plan named by `[refill] plan` as items that never run, when the queue runs dry.

| Event | Written when | Payload |
|---|---|---|
| `item.parked` | The park lands in the tracker. | `reason`, `structural`, `corrections_completed`, `last_state`, [for a seat AutoBuild stopped or that failed] `kill_kind` and `last_sample_times`, [with a snapshot] `snapshot_path`, `snapshotted_paths` and `seat_evidence` |
| `item.park_delivery_failed` | A park from the worktree, its tracker commit or its delivery fails. AutoBuild then tries the park from the primary checkout. | `reason`, `error`, `snapshot_path`, `worktree_root` |
| `item.park_failed` | The park cannot land at all, also not from the primary checkout. The item is `failed` and unfinished, and its worktree is kept. | `reason`, `error`, `snapshot_path`, `worktree_root` |
| `item.follow_up_proposed` | Each finding of the accepting verdict is registered as a follow-up that never runs. The evidence reference is the proposal id. | `code`, `title`, `brief_ref`, `evidence_ref`, `consequence` |
| `item.follow_up_failed` | Registering follow-ups fails after the delivery. The delivery stands. | `error` |
| `campaign.refill_failed` | Refill of a dry queue fails. | `error` |

### Lanes and usage

The Claude usage meter reads an endpoint that is undocumented and may change or stop working without notice. A limit in these payloads has the fields `kind` (`session`, `weekly_all` or `weekly_scoped`), `scope`, `percent` and `resets_at`. A limit is binding when it is at or above its stop level: `[usage] five_hour_stop_percent` for `session`, and `[usage] stop_percent` for the weekly kinds.

| Event | Written when | Payload |
|---|---|---|
| `lane.cooled` | A structural signal sets a lane aside: a rate limit, a quota, a failed sign-in, a seat that ended with no structured output (`spawn`), or a usage stop. | `lane`, `kind`, `signature`, `reset_at` |
| `lane.usage_cooling_cleared` | A usage cooling, from this campaign or another, is judged again, and a good reading below every stop level of this run clears it. | `lane`, `meter_id`, `campaign_id` (of the campaign that set the cooling), `cooled_until`, `percent` |
| `usage.warning` | A limit newly reaches `[usage] warn_percent`. It is written once per crossing for each meter and limit, and again only after the limit falls below and crosses again. | `lane`, `meter_id`, `seat`, `warn_percent`, the limit fields, `reading_age_seconds` |
| `usage.stop` | Before a seat, a binding limit without a model scope is found on the seat's lane. The lane is set aside until the latest reset of the binding limits, and the seat moves to the next capable lane. When no lane is left, a claimed item parks with the reason `<lane>:usage`. When every lane is set aside for usage, the campaign lets running items finish and ends `usage_stop`. | `lane`, `meter_id`, `seat`, `limits`, `reset_at`, `detail`, `reading_age_seconds` |
| `usage.seat_rerouted` | A binding limit scoped to a model covers the seat's model. The seat skips that lane, and the lane is not set aside. | `lane`, `meter_id`, `seat`, `model_class`, `limits` |
| `usage.meter_failed` | A meter reading before a seat fails. | `lane`, `meter_id`, `seat`, `error`, `action` (`allow`, `retry`, `hold` or `stop`), `retry_seconds`, `reading_age_seconds`, and [after an earlier reading at the warning level] `last_good`, `warn_percent`, `stop_percent`, `hold_limit_seconds` |

## Progress lines

A progress line is the short text form of one event. Each line in `progress.log` is the event's UTC timestamp followed by a body. Only some events have a line, and the others write none. Among the events without a line are `item.triaged`, `review.combined`, `specialist.completed`, `seat.launched`, `item.resumed`, `item.resume_parked`, `item.lane_changed`, `lane.cooled`, `campaign.launch_set_fixed`, `workspace.created` and the lease events other than `campaign.lease_withheld` and `campaign.lease_restored`.

By default AutoBuild also writes every line to standard error. With `[progress] command` set, it runs that command once per line with the line on standard input.

| Event | Line |
|---|---|
| `seat.started` | `item <id> seat <seat> started: model <model>, effort <effort or default>`, followed by `, lane <lane>` when the lane is set. Without a model name it reads `model class <class>`. |
| `seat.completed` | The line ends with `, estimated cost <0.00>` when the lane reported a cost. |
| `validation.started` | `item <id> validation started: validator <id>`, followed by ` (revalidation at delivery)` or ` (load rerun)` when either applies. |
| `delivery.started` | `item <id> delivery started: target branch <branch>` |
| `campaign.batch_started` | `batch validation <n> started: validator <id> on <revision>` |
| `usage.warning` | `item <id> seat <seat> on lane <lane>: usage <kind> <percent>% is at or above the warning level <warn>%; admissions slow to one item at a time; reading <n> s old` |
| `usage.stop` | `item <id> seat <seat> on lane <lane>: lane stopped at its usage level (<detail>); the seat moves to the next lane or the item waits; reading <n> s old`. These are the line's own words. What follows is in the `usage.stop` row under [Lanes and usage](#lanes-and-usage): the seat moves to the next capable lane, and with no lane left a claimed item parks. |
| `usage.seat_rerouted` | `item <id> seat <seat> on lane <lane>: a scoped usage limit at its stop level (<limits>) covers the seat's model; the seat moves past the lane` |
| `usage.meter_failed` | `item <id> seat <seat> on lane <lane>: usage meter reading failed (<error>)`, followed by what happens next, for example `; the profile allows a failed reading, so the seat starts`. |
| `campaign.usage_stop` | `every lane is stopped for usage (<lanes>): no new item is admitted; the items in flight finish and the campaign ends with usage_stop; the earliest reset is <time>` |
| `lane.usage_cooling_cleared` | `lane <lane> usage cooling until <time> from campaign <id> cleared: the meter reads <percent>%, below this run's stop levels` |
| `subscription.admission` | `item <id> <seat> subscription <lane, or on hold> (<lane> <reason>, ...)` |
| `campaign.pause_check_failed` | One line for each run of failures in a row. |
| `campaign.completed` | The completion line, below. |

A limit with a model scope shows the scope in brackets after its kind, for example `weekly_scoped [<scope>] 92%`. A reading without a fetch time ends `; reading age unknown`.

The completion line is the last line of a campaign:

```
campaign completed: shipped N, parked N, failed N; [batch validation failed on <revision>; output <ref>|error <error>|output none; ]report <path>; stop <reason>
```

The part in square brackets appears only when the batch validation failed, and its evidence is written as one ASCII line. AutoBuild appends ` progress hook failures: <n>` to every completion line, with `0` when the progress command never failed. `autobuild watch` exits 0 after it prints a line whose body starts `campaign completed: shipped `.

## The campaign report

The campaign report is a Markdown file, `docs/campaigns/<campaign-id>.md`, committed with the subject `ADDED: campaign <id> report`. AutoBuild writes it at the end of a campaign, after the last batch validation and before `campaign.completed`. It writes no report when the tracker lease is refused, because the campaign stops before it starts. It writes none after an internal scheduler error either, when `autobuild run` exits 2 once every running item has ended.

The report has its own delivery, separate from an item's, and no integration or revalidation runs for it. In the default delivery mode, AutoBuild commits the report in a detached checkout under `workspace/reports/`, pushes it to the default branch on `origin`, and checks the push with `git ls-remote`. It then moves your local default branch forward. With `--delivery-mode current-branch-pr`, it commits the report on the branch you launched from and pushes it only with `--push-current-branch`. In that mode it refuses the default branch unless you pass `--allow-current-branch-default`.

Report delivery can fail. AutoBuild then writes `report delivery failed: <error>` in place of the report's path, in `repository_report_ref` of the printed result, in `report.txt` and in the `report_path` of `campaign.completed`.

The report opens with the title `# Campaign <id>`, and its sections appear in this order:

| Section | Present | What it holds |
|---|---|---|
| Shipped | Always | One line per accepted item, `- <id> <title> item=<commit> merged=<commit> verified`, or `none`. |
| Parked | Always | One line per parked item, `- <id>: <reason>`, and one per orphan worktree, `- <name>: resume:orphan-worktree`, or `none`. |
| Failed | Always | One line per failed item, `- <id>: <reason>`, or `none`. A builder continuation that the campaign stopped before admitting again is here, with the reason `timeout:builder; kept with its claim and worktree for the next campaign`. |
| Batch validation | Always | `none`, or a sentence on the failure and one line per batch with its number, whether it was final, its revision, `passed` or `failed`, its exit code, its output and its error. |
| Unfinished | When any | The sentence "Ended without a recorded park; each worktree is kept as evidence.", then one line per item whose park itself failed, `- <id>: <error> (worktree <path>)`. A builder continuation that the campaign stopped before admitting again is also listed here. |
| Skipped | When any | The sentence "Ready in the tracker and passed over; none of these was claimed.", then `- <id>: <reason>`. |
| Holds | When any | One line per hold, `- <condition>: <seconds> seconds, <how it ended>`, where it ended released, at the hold limit, or with the campaign. |
| Follow-ups | Always | One subsection per follow-up or refill proposal, see below, or `none`. |
| Unpublished tracker writes | When any | One line per tracker write committed and not yet pushed, `- <id> <operation>`. |
| Allowed items left unbuilt | With an allow-list | `- <id>: <reason>` for each allowed item that was not built, or `none`. The reason is one of `excluded`, `outside the launch set`, `claim taken`, `blocked`, `not ready`, `item bound`, `hold limit`, `paused`, `launch set unreadable` and `ready after the last selection`. |
| Lease reclaims | When any | One line per stale tracker lease taken over, `- <kind> <path> reclaimed from campaign <id> (process <pid> on <host>)`. |
| Next | Always | `queue dry`, `hold limit`, `launch set unreadable`, `scope fence violation`, `tracker read failed: <error>`, a sentence for a pause, a sentence for a usage stop with the earliest reset, or the id of the next ready item. The usage stop sentence starts `usage_stop: every lane is stopped for usage until <reset>`. |
| Seat usage | Always | A table with one row per seat: item, seat, lane, model, requested effort, duration, input tokens, output tokens and estimated cost. Cost is rounded to two decimal places, a rounding fixed in the code, a missing value shows as `-`, and one `none` row stands when no seat ran. |
| Review depth | Always | A table with one row per review: item, seat with its boundary, duration, changed paths inspected, clauses checked, depth and reasons. One `none` row stands when no review ran. |
| Schedule | Always in a run | The concurrency, the peak number of running items, the makespan in seconds, the sum of the item seconds and, when the pressure monitor ran, the seconds spent at each pressure level. |
| Run record | Always | The path of the run folder and of the progress log. |

### Follow-ups

A follow-up is a new tracker item that AutoBuild registers for each reviewer finding on an accepted item, and that it never runs. Its title is `Follow-up: <item> <code>`. The Follow-ups section also lists the refill proposals registered on a dry queue. In the report, each follow-up has a subsection `### <id> <title>`. The subsection names the parent item, the finding code, whether the finding was blocking (`yes`, `no` or `none`) and the evidence reference. The question and the rationale follow in fenced blocks, word for word. When registering a follow-up fails, the follow-ups registered before it keep their subsections.

### Review depth

A review is shallow when the reviewer inspected fewer changed paths than the change touches, deleted paths not counted, or when it marked no acceptance clause `met` or `unmet`. AutoBuild records a shallow review and never rejects a verdict for it. The reviewer is not given the builder's transcript, so the depth comes from the reviewer's own `inspected` and `acceptance` lists, checked against the change.

## The result that `autobuild run` prints

When a campaign completes, `autobuild run` prints its result on standard output as indented JSON with sorted keys and the schema `autobuild.campaign-result.v1`. With `--output <path>`, AutoBuild also writes the same JSON to that file, creating missing folders, with the path read from the working directory. When `autobuild run` stops before completion it prints no JSON.

| Field | Holds |
|---|---|
| `schema` | `autobuild.campaign-result.v1` |
| `version` | The AutoBuild version. |
| `campaign_id` | The campaign id. |
| `repository` | The repository. |
| `scratch_root` | The scratch root. |
| `adapters` | One entry per adapter, with `boundary` (the name of the port the adapter serves, such as `tracker`), `name` and `version`. |
| `stop_reason` | `queue_dry`, `item_bound`, `hold_limit`, `paused`, `usage_stop` or `launch_set_unreadable`. |
| `selection` | The item selection, with its sources recorded as the profile path, the words `command line`, or both. |
| `refill` | `enabled`, `proposal_count` (the refill proposals registered) and `fog_count` (the open questions of the refill plan recorded). |
| `report_ref`, `repository_report_ref`, `progress_ref` | References to the report and the progress log. |
| `schedule` | `concurrency`, `concurrency_source`, `heavy_concurrency`, `peak_concurrency`, `makespan_seconds` and `item_seconds`, the last two rounded to three decimal places. |
| `batch_validation` | `verdict` (`none`, `passed` or `failed`, from the last batch) and `runs`. Each run has `batch`, `final`, `revision`, `passed`, `exit_code`, `stdout_ref` and `error`, which is null when empty. |
| `items` | One entry per item, with `item_id`, `disposition` (`accepted`, `parked` or `failed`), `states`, `reason`, `item_commit`, `tracker_commit`, `merged_commit` and `pushed`. |

## Limits of the records

The run folder holds references to each seat's raw output, not the output itself, and the builder report evidence holds only the builder's `summary` and `report_ref`. The output stays in the scratch root.

A review's depth is the reviewer's own account. AutoBuild checks the reviewer's `inspected` list against the number of changed paths, and it does not check that the reviewer read those lines.

The records cover one campaign on one scratch root. An item created in the tracker after the launch set was read appears in no event of that campaign. Two campaigns on different scratch roots write to different run folders and see neither each other's records nor each other's leases.

No record is written for a campaign that never started, such as one whose launch failed a setup check, and a campaign whose lease was refused writes only its first events and no report.
