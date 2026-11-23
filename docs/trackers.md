# Trackers

Before AutoBuild can build an item from your queue, it has to find the items that are ready, mark one as taken so that no one else takes it, and write down what happened to it when the work ends. This guide shows you how to set up a list that AutoBuild can do those three things with, what to leave alone while a campaign runs, and how to choose between the two kinds of list.

A tracker is the list of work items that AutoBuild reads and writes. AutoBuild supports two: a Markdown table in a `BACKLOG.md` file, and [Pinax](https://github.com/antikas/pinax-tracker), a command line work tracker that keeps its items in the repository's `.ergon/` folder. The two tracker sections follow one item, `ITEM-7`, titled "Add a --json option to the export command", from ready to done, and then to parked. The [README](../README.md) explains the campaign, the brief, the seats and the delivery that this guide refers to.

## Choose the tracker

Set the tracker with `kind` in the `[tracker]` table of your profile, or with `--tracker` for one launch. The value is `auto`, `pinax` or `backlog`, and `auto` is the default. In the profile the value may be written in any case, and any other value is refused with `tracker.kind must be auto, pinax or backlog`. On the command line the option is case-sensitive, so write the value in lower case; any other value is refused with the command line parser's own "invalid choice" message.

With `auto`, AutoBuild uses Pinax when its check passes, and otherwise the `BACKLOG.md` table when that check passes. When neither passes, the launch stops with `tracker preflight failed: <diagnostics>`. When you name a tracker with `pinax` or `backlog`, AutoBuild checks only that one, so the same message means that the tracker you named failed its check.

AutoBuild makes this choice again at every launch, and the campaign keeps it until it ends. The Pinax check passes as soon as `pinax` is on your `PATH`, the repository has an `.ergon/` folder and `pinax --help` exits 0. Creating an `.ergon/` folder therefore moves every later campaign under `auto` to Pinax, even while your `BACKLOG.md` table is still there. To keep using the table in a repository that also has an `.ergon/` folder, set `kind = "backlog"` in the profile.

The run's `manifest.json` records which tracker the campaign used. While the campaign runs, it also holds a lease on the tracker's folder, which is the `.ergon` folder for Pinax or the folder that holds the backlog file. The lease is a file in the `leases` folder of the scratch root, named from the tracker folder's path, so AutoBuild writes nothing into your repository for it. A second campaign that uses the same scratch root sees that lease and stops with exit code 2 before it claims anything.

AutoBuild keeps the tracker's files out of the product commit. A change to the tracker always travels in a commit of its own.

Both trackers publish to a git remote named `origin` in the default delivery mode, and every launch needs that remote. Without it, the launch stops with `required git remote is missing: origin`.

## A `BACKLOG.md` table

### Where AutoBuild looks

AutoBuild reads `BACKLOG.md` at the root of the repository, and when that file does not exist, `docs/BACKLOG.md`. To use another file, set `path` in the `[tracker]` table, or pass `--backlog PATH` for one launch. A relative path resolves against the repository, and the file must be inside the repository.

### The table

The backlog is the first Markdown table in the file whose header holds the four columns below, followed by a separator row. AutoBuild matches the column names without regard to case, and accepts these names for each column:

| Column | Accepted names |
|---|---|
| Item | `item`, `id`, `item id` |
| Title | `title`, `work item` |
| Status | `status`, `state` |
| Brief | `brief`, `brief ref`, `brief_ref`, `plan` |

Here is a backlog with `ITEM-7` ready to build:

```
| Item | Title | Status | Brief |
|---|---|---|---|
| ITEM-6 | Add a --csv option to the export command | Done (3f9a1c27b04e) | docs/briefs/item-6.md |
| ITEM-7 | Add a --json option to the export command | ready | docs/briefs/item-7.md |
| ITEM-8 | Document the export formats | todo | docs/briefs/item-8.md |
```

The rows end at the first line that is not part of the table. Every row needs all four cells filled, and every item id must be unique. The order of the rows is the order of the queue, so AutoBuild offers `ITEM-7` before `ITEM-8`.

The header may hold other columns as well, and their cells may be empty. When AutoBuild rewrites a row, it keeps every cell, your extra columns included, and changes only the Status cell. It writes the line back with one space on each side of every `|`, so any alignment you padded by hand is lost on that line.

### Statuses

A row is ready when its status is one of `ready`, `approved`, `queued`, `todo`, `to do` or `not started`. AutoBuild compares the status without regard to case and removes any `*` first, so `**Ready**` counts as ready.

AutoBuild also understands the statuses `claimed`, `in progress`, `done`, `accepted`, `shipped`, `parked`, `blocked` and `proposed`. Each of these words must be the whole status, or be followed by a space, a colon, or a space and an opening parenthesis. So `Done (3f9a1c27b04e)` and `Parked: <reason>` are understood, while `Done.`, `Blocked-by ITEM-6` and `Parked-later` are not. AutoBuild reports only rows whose status is `blocked` as blocked, and a `BACKLOG.md` table has no dependencies between items.

Check every status before you launch, because one status that AutoBuild does not recognise makes the whole table unreadable. AutoBuild checks the whole table before the campaign starts, so a bad status stops the launch with `tracker preflight failed: BACKLOG status is not recognised: <status>`, and `run` exits 2 with `AutoBuild failed before completion: ...`. No campaign starts and no report is written. A campaign ends with `launch_set_unreadable` only when the table becomes unreadable between that check and the moment the campaign reads its items.

### The brief column

The Brief cell holds the path to the item's brief, which is the written request that the builder and the reviewer receive. A relative path resolves against the repository. When a brief has no `## Acceptance` heading, the review checks the tracker's acceptance line, and for a `BACKLOG.md` item that line is always `Measurable acceptance is defined by <brief>`. The guide to [writing briefs](writing-briefs.md) says what the brief itself must contain.

### From ready to done

Commit the backlog and every brief on the branch you launch from before you launch. Nothing has to be on `origin` yet, because the setup check and each item's worktree read your local commits, and the first claim pushes its claim commit together with every earlier local commit on that branch. That push is refused when `origin` holds commits that your branch does not, so bring the branch up to date with `origin` first. Then leave the checkout as the section [While a campaign runs](#while-a-campaign-runs) describes.

1. **The claim.** AutoBuild first checks your checkout and refuses to claim when it holds any change, including an untracked file. It then writes `Claimed by builder@claude-code` into the Status cell of `ITEM-7`, where `claude-code` is the lane the builder starts on. It commits the file as `MODIFIED: claim ITEM-7 in BACKLOG`, pushes that exact commit to `origin` on the branch you have checked out, and checks with `git ls-remote` that `origin` holds it. Only then does the work start.
2. **The work.** The builder, the validator and the review run in the item's worktree, as the [README](../README.md) describes.
3. **The close.** When the item is accepted, AutoBuild writes `Done (<first 12 characters of the item commit>)` into the Status cell, in the worktree. That change is committed as `MODIFIED: close ITEM-7 in tracker`, separately from the product commit `ADDED: Add a --json option to the export command`, and both are delivered together.

After the delivery, the row in the file on `origin` reads:

```
| ITEM-7 | Add a --json option to the export command | Done (5d21e8a0c4b7) | docs/briefs/item-7.md |
```

With `--delivery-mode current-branch-pr`, AutoBuild commits the claim and skips its push, unless you also pass `--push-current-branch`.

When another writer has pushed to the same branch first, `origin` rejects the claim's push. The claim then fails with `git push ... failed: ...`, and the claim commit stays on your checkout without reaching `origin`. The section [After a campaign](#after-a-campaign) says how to put this right.

### Parked

To park an item is to set it aside with a reason. When `ITEM-7` parks, AutoBuild writes `Parked: <reason>` into the Status cell, with each `|` in the reason turned into `/` and runs of whitespace collapsed, so that the reason cannot break the table. The reason ends with the place of the saved snapshot, for example:

```
| ITEM-7 | Add a --json option to the export command | Parked: validator tests did not pass [snapshot=evidence/ITEM-7-park] | docs/briefs/item-7.md |
```

A park that happens before the product commit is committed in the worktree and delivered. A park that happens after it is written on your checkout, committed as `MODIFIED: park ITEM-7 in BACKLOG` and pushed. In both cases none of the item's commits reach `origin`. To try the item again, change its status back to a ready status yourself and commit the file. The guide to [running campaigns](running-campaigns.md) lists the park reasons and what each one means.

### Follow-ups

A follow-up is a new item that AutoBuild registers from a reviewer finding on an accepted item. In a `BACKLOG.md` table it is a new row at the end of the table, with a title `Follow-up: ITEM-7 <finding code>` and the campaign report `docs/campaigns/<campaign>.md` as its brief. Its status starts `Proposed:` and carries the finding's question and rationale, and `Evidence: <ref>` when the finding has one. AutoBuild commits the row as `ADDED: propose <title> in BACKLOG` and pushes it.

The follow-up's id is `ABP-` and a number of three digits or more, one above the highest `ABP-` number already in the table, compared without regard to case. The `ABP` prefix is fixed. Because AutoBuild counts above every `ABP-` id it finds, a row of yours with such an id never clashes at the moment AutoBuild adds one. A row you add later with an id that AutoBuild has already used makes the table unreadable with `BACKLOG contains duplicate item id <id>`, so give your own items another prefix.

A `Proposed:` status is not a ready status, so AutoBuild never builds a follow-up on its own. To build one, write a brief for it, put the brief's path in its Brief cell, and change its status to a ready status. AutoBuild refuses to add a row whose title or brief path holds a `|` or a line break.

### One item at a time

A campaign on a `BACKLOG.md` table builds one item at a time. To run several items at once, set `run.concurrency` to a number in the profile; the [profile reference](reference/profile.md) describes the key.

## Pinax

[Pinax](https://github.com/antikas/pinax-tracker) keeps each item as a file in the repository's `.ergon/` folder and records every change as an event in a log. Pinax decides which items are ready, and AutoBuild asks it through its command line.

### Install and prepare Pinax

Install Pinax so that the `pinax` command is on your `PATH`, and create the `.ergon/` folder in the repository with Pinax. AutoBuild's check passes when `pinax` is found, the `.ergon/` folder exists, and `pinax --help` exits 0. The Pinax documentation describes how to install it and how to create items.

Before you launch, read [While a campaign runs](#while-a-campaign-runs): one edited product file in your checkout is enough for a Pinax campaign to build nothing.

### The commands AutoBuild calls

AutoBuild runs each Pinax command as `pinax --root <folder> ...` in the folder that the command acts on. The queue reads, the claim, a follow-up and a park after the product commit run in your checkout. The close and a park before the product commit run in the item's worktree. The repair after a partial write runs in the folder where the write happened, and the setup check runs a bare `pinax --help`. AutoBuild calls these commands:

| Purpose | Command |
|---|---|
| Read the queue | `pinax ready --actor coordinator@autobuild --json` and `pinax next --actor coordinator@autobuild --json` |
| Claim an item | `pinax claim <id> --actor builder@<lane> --json` |
| Close an item | `pinax done <id> --briefing <file> --actor ... --json` |
| Park an item | `pinax park <id> --reason ... --actor ... --json` |
| Register a follow-up | `pinax add --prefix abp --allow-new-prefix ...`, then `pinax block <id> --gate proposal`, then `pinax note add <id> --ref <brief> --caption <pointer>` |
| Repair after a partial write | `pinax verify`, and `pinax verify --fix` when it fails |

AutoBuild also reads Pinax's files directly. The known items are the `item.created` events in `.ergon/log/*.jsonl`, and a missing log folder is an error. The blocked items and their dependencies come from the front matter of `.ergon/items/*.md`.

### Attach the brief

AutoBuild finds an item's brief in the item's notes. The brief is the `ref` of the newest note on the item, and that note's caption is the acceptance line that the review checks when the brief has no `## Acceptance` heading. Attach the brief to `ITEM-7` with `pinax note add`, with `--ref` set to the brief's path and `--caption` set to one line of acceptance.

Keep your briefs under `docs/` or `projects/` in the repository. Pinax accepts a `ref` only when it starts with one of a fixed set of prefixes, and these two are the ones for a path inside your repository. A brief at `docs/briefs/item-7.md` can be attached, while one at `briefs/item-7.md` cannot, even though AutoBuild itself would accept that path.

Any later note replaces the brief, because AutoBuild reads only the newest one, so add a new note only when you mean to change the brief. A ready item with no note is never claimed: AutoBuild records it once as skipped with the reason `no_brief`, and the report lists it under Skipped. The guide to [writing briefs](writing-briefs.md) says what the brief itself must contain.

### Readiness, dependencies and gates

An item is ready when `pinax ready --json` lists it. AutoBuild reads that list and `pinax next --json`, and puts the item that `next` names first. When the output is empty, or says that no item is ready or that the queue is empty, AutoBuild has nothing to build.

Pinax lists an item as ready when its status is `queued` or `ready`, no one holds a live claim on it, every item that blocks it is done, and its dependencies form no cycle. AutoBuild treats an item as blocked when its status is `blocked`, or when any item it depends on has a status other than `done`. A parked dependency therefore still blocks, because parked is not done, and the items that wait on it stay blocked until it is done. A gate is a named reason that blocks an item, set with `pinax block`, and AutoBuild uses the gate `proposal` for its own follow-ups.

A dependency can open during a campaign. When `ITEM-8` depends on `ITEM-7`, and both existed at launch, AutoBuild builds `ITEM-8` once `ITEM-7` is delivered in the same campaign. An item that someone creates after the launch waits for the next campaign.

### From ready to done

1. **The claim.** AutoBuild runs the claim on your checkout. It first commits any changes that touch only the `.ergon` folder, and it then refuses to claim when any file is staged or modified, product files included. Only an untracked product file is let through. It then runs `pinax claim <id> --actor builder@claude-code --json`, where `claude-code` is the lane the builder starts on. Pinax records the claim and commits it, and AutoBuild checks that commit.
2. **The work.** The builder, the validator and the review run in the item's worktree.
3. **The close.** When the item is accepted, AutoBuild writes a briefing file `.autobuild-ITEM-7-briefing.md` in the worktree. It holds the outcome, the item commit, the worktree's revision, the validator and its output, the review verdict, the item's trajectory, the builder's lane, every finding and the changed paths. AutoBuild runs `pinax done` with that file under the actor `coordinator@autobuild`, deletes the file, and delivers Pinax's commit as the tracker commit with the product commit.

### Parked

When `ITEM-7` parks, AutoBuild runs `pinax park` with the reason, under the actor `coordinator@autobuild`. The reason ends with the place of the saved snapshot, as in a `BACKLOG.md` table. The park runs in the worktree, or on your checkout after AutoBuild has checked that no product file there is staged or modified. In both cases none of the item's commits reach `origin`.

### Follow-ups

For a reviewer finding on an accepted item, AutoBuild adds a new Pinax item with the prefix `abp` and blocks it with the gate `proposal`. It then attaches a note whose `ref` is the brief path and whose caption is a pointer of the form `Full text: <ref>, section <id>`. The brief path must be a path inside the repository or a URI, and a pointer longer than 200 characters is refused before the item is added. A blocked item is never ready, so AutoBuild never builds a follow-up on its own. To build one, write a brief for it, attach it, and make the item ready in Pinax yourself.

### When Pinax refuses or fails

AutoBuild reads Pinax's exit code and acts on it:

| Exit code | AutoBuild's reading |
|---|---|
| 3 on a claim | Another writer took the item, and the campaign selects the next one. |
| 4 | The tracker environment is wrong. One case is accepted: a claim that Pinax committed only locally (status `committed_local`) on a branch other than the remote default, when the delivery mode is `current-branch-pr`. |
| 5 with `push_rejected` | When Pinax committed the change and only the push lost a race, AutoBuild keeps the write and publishes it with the next delivery. On a claim it stops. |
| 7 with `commit_refused` or `stage_failed` | Pinax wrote part of the change. AutoBuild commits only the leftover `.ergon` files with Pinax's own subject, up to 4 attempts with waits of 2, 5 and 10 seconds, and pushes when the remote default branch is checked out. |
| 1, 2, 5, 6, 7 otherwise | Pinax refused the change. |
| Any other non-zero code | An error in AutoBuild's Pinax adapter. |

A tracker failure on one item holds the campaign until the tracker answers again; the guide to [running campaigns](running-campaigns.md) describes the holds and their retry times. When `git add`, `git commit` or `git push` fails with "permission denied" or "unpacker error", AutoBuild tries the same command again, up to 4 times, which makes 5 attempts in all. This applies to both trackers.

The board file `.ergon/board.md` and the item files `.ergon/items/*.md` are views that Pinax builds from its log. When a delivery meets a merge conflict only in these view files, AutoBuild settles it with `pinax verify --fix`. A conflict in any other file parks the item with a conflict reason.

### The Pinax version

AutoBuild reads no Pinax version. It expects Pinax to commit each change itself and checks the repository for that commit. When Pinax leaves a change uncommitted, AutoBuild stops at that first change, and the message names pinax-tracker 0.2.1 or later. The check proves that the Pinax you run commits its own changes. It does not prove that any particular Pinax version works with AutoBuild, because AutoBuild never asks which version it is talking to.

## While a campaign runs

AutoBuild writes the tracker on your checkout and commits only the tracker's files there, so it refuses to write when other changes could be swept into its commits. An item whose claim fails is marked failed and is not picked again in that campaign. One stray change can therefore leave a campaign that builds nothing. Leave the checkout as you committed it until the campaign ends, and keep clear of these changes:

| Tracker | Change in your checkout | What it causes |
|---|---|---|
| `BACKLOG.md` | Any uncommitted change, an untracked file included | The next claim refuses with `primary checkout must be clean before a tracker claim`, and that item ends failed. |
| `BACKLOG.md` | Any product file changed, untracked included, when a park or follow-up is written on the checkout | The commit refuses with `primary checkout has non-tracker changes; refusing tracker commit`, and the row stays written but uncommitted, so every later claim refuses. |
| `BACKLOG.md` | An uncommitted edit to the backlog, such as a new row, when a park or follow-up is written on the checkout | AutoBuild commits your edit together with its own row, in its park or follow-up commit. |
| `BACKLOG.md` | A committed edit to the backlog, such as a new row | The next claim pushes your commit with its own. When your edit touches the same lines as an item's close, or lines next to them, the delivery meets a merge conflict and the item parks with a conflict reason. |
| Pinax | A staged or modified product file | Every claim refuses with `primary checkout must be clean before a tracker claim`, and each item ends failed. A park or follow-up refuses with `checkout has staged or modified non-tracker changes; refusing a tracker write: <paths>`. |
| Pinax | An untracked product file | Nothing. The claim lets it through. |
| Pinax | A Pinax command that changes `.ergon`, such as adding an item | AutoBuild commits the leftover `.ergon` changes before the next claim, and the claim goes ahead. |

A row or item that you add during a campaign is never built by it, because a campaign builds only the items that existed at launch. It waits for the next campaign, so add new work between campaigns.

## After a campaign

A claim commit that stayed local after a rejected push leaves your branch behind `origin`, and every later claim push in that state is rejected the same way. The item behind it was never built. Bring in the other writer's commits with `git pull --rebase origin <branch>`. Then set the row's Status cell from `Claimed by builder@<lane>` back to `ready`, commit it with `git commit -m "MODIFIED: reopen ITEM-7 in BACKLOG" -- BACKLOG.md`, and push with `git push origin <branch>`.

A row left written but uncommitted by a refused commit needs the commit that AutoBuild meant to make, which is also what AutoBuild does itself when Pinax leaves a partial write. For a park row, run `git commit -m "MODIFIED: park ITEM-7 in BACKLOG" -- BACKLOG.md`, and for a follow-up row, `git commit -m "ADDED: propose <title> in BACKLOG" -- BACKLOG.md`. Naming the file commits the backlog alone and leaves your own changes where they are. Then push with `git push origin <branch>`. A claim row left uncommitted is the one case to undo, because its item never started: run `git restore BACKLOG.md`, which also discards any other uncommitted edit you made to that file.

A row or item still marked as claimed by a builder after a run ended, for example after a crash, is picked up by the next campaign that uses the same scratch root. That campaign resumes the item from where it stopped when the item's recorded state still matches. Otherwise it parks the item with a `resume:` reason such as `resume:missing-marker` or `resume:head-moved`. To start the item over instead, set its status back to a ready status before you launch, and commit and push that change.

## Compare the two

| | `BACKLOG.md` table | Pinax |
|---|---|---|
| Setup | One Markdown file in the repository | The `pinax` command and an `.ergon/` folder |
| Queue order | The order of the rows | The order Pinax gives, with the `next` item first |
| Dependencies | None | Items wait for the items they depend on |
| Blocking | A row whose status is `blocked` | A `blocked` status, an open dependency, or a gate |
| Brief | The Brief cell | The `ref` of the newest note |
| Claim refuses | Any uncommitted or untracked change in your checkout | Any staged or modified file; only untracked product files are allowed |
| Follow-ups | A row with a `Proposed:` status | An item blocked with the gate `proposal` |
| Items at once | One, unless you set `run.concurrency` | `run.concurrency`, or the number AutoBuild works out from the machine |
| Merge conflicts | The item parks with a conflict reason | Settled with `pinax verify --fix` in the view files; elsewhere the item parks |

A `BACKLOG.md` table suits a short queue whose order you set yourself, and which you edit by hand between campaigns. Pinax suits a queue whose items depend on each other or wait behind gates. It also suits a queue that other writers claim from, because its claim tolerates untracked files and its own `.ergon` changes in your checkout.

### Moving from a table to Pinax

AutoBuild has no importer from a `BACKLOG.md` table to Pinax. To move, create each open item in Pinax yourself and attach its brief with a note, as [Attach the brief](#attach-the-brief) describes. Do this between campaigns. Once the `.ergon/` folder exists, a profile with `kind = "auto"` uses Pinax from the next launch, and the table is no longer read.

## Limits

A `BACKLOG.md` table has no dependencies, so you must order the rows yourself and mark a row `blocked` until the work it needs is delivered. Because one unknown status makes the whole table unreadable, a single typing error in the Status column stops the next launch before a campaign starts. The claim refuses any change in your checkout, including an untracked file, so a campaign on a `BACKLOG.md` table needs a checkout that nothing else writes to. Without `run.concurrency`, it builds one item at a time.

Pinax must be on your `PATH` with an `.ergon/` folder in the repository, and AutoBuild reads no version from it. AutoBuild relies on Pinax to commit every change, and it detects a Pinax that does not only at the first change. Because a staged or modified product file in your checkout refuses every claim, park and follow-up, a Pinax campaign also needs a checkout that nobody edits while it runs. A brief inside the repository must sit under `docs/` or `projects/`, because Pinax refuses other references. A later note replaces the brief without any warning, and an item with no note is skipped until you attach one, while a parked dependency keeps blocking the items that wait on it.

Both trackers see only the items that existed at launch, and neither builds a follow-up until you make it ready yourself. Two campaigns see each other's tracker lease only when they use the same scratch root.
