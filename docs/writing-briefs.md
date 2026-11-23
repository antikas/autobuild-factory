# Writing briefs

You know what you want built. The builder that AutoBuild starts for the item cannot ask you a question, and neither can the reviewer that checks the result afterwards. Neither has seen your conversations or the other's transcript, so what you want from the item reaches them only through what you write down.

A brief is the written request for one item, kept as a file and named by the item's row in the tracker. Write one brief for each item. AutoBuild reads a few marked lines and headings in it to decide whether the item may be built at all, how long its seats may run, which items may run beside it and what the reviewer must check. It then gives the full text to every seat. This page first teaches the format through one complete brief, then lists every reason a brief can make AutoBuild park an item or refuse a launch, with what to change. The terms used here (campaign, seat, worktree, validator, park) are the ones the [README](../README.md) introduces.

## One brief, from a blank file to a delivered item

Follow one item, `ITEM-7`, titled "Add a --json option to the export command", in a repository whose tracker is a `BACKLOG.md` table.

### The file and the tracker row

Create the brief as a Markdown file in the repository, for example `docs/briefs/item-7.md`, and name it in the item's row. In a `BACKLOG.md` table the brief is the value of the Brief column (the header may also say `brief ref`, `brief_ref` or `plan`):

```
| Item | Title | Status | Brief |
|---|---|---|---|
| ITEM-7 | Add a --json option to the export command | ready | docs/briefs/item-7.md |
```

Every row needs all four cells filled, so a row with an empty Brief cell makes the whole table unreadable. In Pinax, the brief is the reference (`ref`) of the newest note attached to the item, and the guide to [trackers](trackers.md) shows how to attach one and how to set up either tracker.

A relative brief path is read against the repository at the setup check and before the claim, and against the item's worktree for every seat. The worktree is a second working tree of the repository on its own branch, started from your checkout's local `HEAD`, so a brief you have not committed is not in it. A claim from a `BACKLOG.md` table also refuses a checkout that holds any uncommitted or untracked file. Commit the brief and its tracker row before you launch; you need not push them yourself. In the default delivery mode, the first claim from a `BACKLOG.md` table commits the row change and pushes that commit, with every commit beneath it, to your current branch on `origin`.

The setup check reads only the next ready item's brief, or every ready one when items run in parallel. AutoBuild checks the brief file of every other item just before that item's claim, and parks the item when the file is missing, empty, over 1 MiB or not readable as UTF-8 text. Before every launch, check that the brief file of every ready item exists and is committed.

### The nature and class lines

Two marked lines may sit anywhere in the brief, each on a line of its own. Put them just under the title, where a reader sees them first. AutoBuild ignores case and the spaces around a line, reads only the first line that starts with each label, and ignores backticks around the value and a full stop after it.

The item nature line says whether the work can be done inside this repository:

```
Item nature: repository
```

The values are `repository`, `machine` (work that needs installs, services, global configuration, the registry or scheduled tasks), `cross-repository` (work in a path outside the repository) and `owner-gated` (work that needs a person or production access). AutoBuild builds only `repository` items. An absent line means `repository`, and so does a value AutoBuild does not recognise, which means a misspelt `owner-gated` is built without any warning. `ITEM-7` changes code in this repository, so its brief needs no nature line.

The item class line chooses how long each of the item's seats may run, and that time limit is the only thing a class changes. You name the classes in the project profile, each with a number of seconds:

```toml
[run.item_classes]
large = 2400
```

and you pick one in the brief:

```
Item class: large
```

Every builder, reviewer and specialist seat of the item then has that time limit. Without the line, or with a class the profile does not name, the seats use `run.seat_timeout_seconds` (900 by default). Add a class only when a seat of the item needs longer than that. A seat that runs past its limit ends with the reason `timeout:<seat>`, and a builder that times out is first handed to a fresh builder in the same worktree, up to two times. `ITEM-7` is a small change, so its brief carries no class line. The [profile reference](reference/profile.md) lists the other time limits.

### The acceptance heading

An acceptance clause is one bullet under the `## Acceptance` heading, and it states one thing the reviewer must check:

```
## Acceptance

- `export --json` prints the export as one JSON document, and a test in `tests/test_export.py` parses that output.
- `export` without `--json` prints the text it prints today.
```

AutoBuild numbers the clauses `A1`, `A2` and onwards and hands that numbered checklist to the reviewer. The reviewer marks each clause `met`, `unmet` or `not_checked`, with a reference to its evidence. AutoBuild refuses a `pass` verdict that leaves any clause `unmet`, and the item parks.

The fallback acceptance line is the line the reviewer gets as its whole checklist when the brief has no `## Acceptance` heading. It comes from the tracker. For a `BACKLOG.md` table it is always the one sentence `Measurable acceptance is defined by <brief>`. In Pinax it is the newest note's caption, or that same sentence when the caption is empty. Write the heading in every brief, so that the reviewer gets real clauses to check.

Write both headings exactly as `## Acceptance` and `## Declared paths`. Case is ignored, but a variant such as `## Acceptance criteria` or `### Acceptance` is not recognised: the checklist then falls back to the tracker's line, and the item declares no paths. A `## Acceptance` heading with no bullets under it gives the reviewer an empty checklist with no fallback, so every review of the item is recorded as shallow. A bullet may start with `-`, `*`, `+`, `1.` or `1)`, and the acceptance list ends at the next `## ` or `# ` heading.

### The declared paths heading

The declared paths heading lists the files the item is expected to touch. AutoBuild reads every token in backticks under `## Declared paths`, up to the next `## ` heading. A token may be an exact path, a folder or a glob pattern such as `tests/**`:

```
## Declared paths

- `src/export/cli.py`
- `src/export/json_writer.py`
- `tests/test_export.py`
```

The heading is optional. Each declared path must lie inside the fence, which is the repository together with the allowed roots (`[policy] allowed_roots` in the profile, or `--allowed-root` for one launch). The reasons table below says what happens when a path lies outside it.

When several items run at once, a ready item waits while an item that is already running declares the same exact path, and a glob pattern never makes an item wait. At delivery, AutoBuild runs the validator again when the changes merged in from the target branch reach the item's scope. The scope is every file the item changed or declared, every file in the same top-level package as one of those, and every file at the repository root. A top-level package is the first folder of a path, or the first two folders under `apps/` and `packages/`. Merged changes confined to `.ergon/`, `docs/` or Markdown files never run the validator again, even on a declared path.

In the strict process mode the declared paths do more. They choose the item's covering tests, which are the test-map entries in `[process] test_map` whose patterns match a declared path. The builder is told which tests they are, and on the `claude-code` and `claude-sdk` lanes a builder test run that selects any other test, or the whole suite, is refused. Each boundary in `[process.boundaries]` that a declared or changed path touches adds one specialist reviewer. With a boundary map, a path that touches a boundary also raises the item's correction rounds from one to two.

A brief without declared paths still runs. The item then never waits for another item, and the validator rerun at delivery follows only the files it changed. In the strict mode every test-map entry counts as covering, and the boundaries follow the changed files alone.

A builder may change a file the brief did not declare, in either mode, and nothing refuses that change. The changed file still counts toward the validator rerun at delivery, the specialist reviewers and the correction rounds. In the strict mode on those two lanes the builder may run only the covering tests chosen from the declared paths, so declare every file the item should touch.

Write declared paths relative to the repository. A token that starts with `/`, has no file extension and names nothing that exists is read as a route, such as `/api/export`, and ignored. A token that starts with `/` and has an extension is read as a path, and it counts as outside unless it resolves under the repository or an allowed root.

### The rest of the text

Everything else in the brief is free text that every seat receives in full: the problem, the context, the approach you want, the files to read first. AutoBuild reads nothing else in it, the `#` title line included, apart from the git rule that the reasons below describe. Write it for a capable engineer who has never seen the repository.

### The builder's instructions

The builder starts as a fresh seat in the item's worktree. Its instructions are AutoBuild's fixed prompt followed by the full text of your brief. The prompt tells the builder to leave its changes uncommitted, to run only the tests for the files it changed, and never to run the full suite or the validator. The builder ends with a short report of what it changed, which AutoBuild saves and later gives to the reviewer. AutoBuild makes the commits itself once the item is accepted.

The builder knows about the item only what the brief, the repository's files and that prompt tell it. Two things are added to that prompt: the validator's output when a fresh builder retries after a failed validator, and the review verdict with its findings in a correction round. It has no memory of earlier seats. If the brief relies on a decision you made in a conversation, write the decision down.

### Acceptance clauses a reviewer can fail

The reviewer is a separate fresh seat with read-only tools. It receives the brief, the change, the validator's captured output and the builder's latest report, and it never sees the builder's transcript. A clause is useful only when this reviewer, reading those four things, could find it `unmet`.

Name something the reviewer can see. Because the reviewer cannot run the program, a clause about behaviour is checkable when a test that the validator runs shows that behaviour, so name the test file. Put one fact in each clause, because `met` or `unmet` applies to the whole bullet, and say what must stay the same as well as what must change. Leave out clauses that only repeat the validator ("the tests pass"), because the validator already decides that on its own.

A clause such as "The export is easier to use" cannot fail, because nothing in the change can contradict it. "`export --json` prints one JSON document, and `tests/test_export.py` parses it" can fail, because the reviewer can read in the change whether the option and the test are there.

The reviewer may leave a clause `not_checked`, and nothing rejects a review that leaves clauses out. A review that marks no clause `met` or `unmet` is recorded as shallow in the Review depth section of the campaign report. The guide to [running campaigns](running-campaigns.md) follows the item from the review through correction rounds and delivery.

### The complete brief

This is the brief for `ITEM-7`, saved as `docs/briefs/item-7.md` and committed with its `BACKLOG.md` row.

```
# Add a --json option to the export command

The export command prints a text table. Scripts that read it break whenever a column changes width. Add a --json option that prints the same records as one JSON document, so that scripts can parse the output.

Keep the text output exactly as it is today. Put the JSON writing in a new module beside the command, and reuse the record loading that the text output already uses. Do not run `git add` or `git commit`; AutoBuild makes the commits.

## Declared paths

- `src/export/cli.py`
- `src/export/json_writer.py`
- `tests/test_export.py`

## Acceptance

- `export --json` prints the export as one JSON document, and a test in `tests/test_export.py` parses that output.
- Each record in the JSON document has the same fields as a row of the text output, and a test checks the field names.
- `export` without `--json` prints the text it prints today, and an existing test in `tests/test_export.py` covers that output unchanged.
- The JSON writing lives in `src/export/json_writer.py`, and `src/export/cli.py` only adds the option and calls it.
```

The reviewer receives these four clauses as `A1` to `A4`. The brief has no nature line, so it is a `repository` item, and no class line, so its seats use the default time limit. Every declared path lies inside the repository, and the brief holds no code block. The builder's prompt already forbids commits, so the sentence about `git add` is optional, and it shows the form of a git mention that AutoBuild allows.

### Changing a brief

Edit a brief before its item is claimed, not during its run. The rules AutoBuild applies to the item come from one read of your checkout just before the claim: the nature, the class, the numbered checklist, the declared paths and the git rule. The seats read the copy in the worktree, which is fixed at the claim. An edit to your checkout after the claim reaches no seat of that item, and the checklist is not read again.

## Reasons a brief stops an item

A setup check reason stops the whole launch before anything is claimed. The setup check reports `briefs preflight failed:` with the cause, `autobuild preflight` exits 1, and `autobuild run` exits 2 with `AutoBuild failed before completion:`. A park reason is written to the tracker: in a `BACKLOG.md` table as `Parked: <reason>` in the Status cell, and in Pinax through `pinax park`. A park after the claim adds ` [snapshot=evidence/<item>-park]` to the reason, naming the saved copy of the worktree.

To try a parked item again, fix the brief and set the item back to ready. In a `BACKLOG.md` table, change the Status cell to a ready status such as `ready` and commit the file. In Pinax, make the item ready with Pinax's own commands, because AutoBuild has none for it. The guide to [running campaigns](running-campaigns.md#park-reasons) lists the park reasons that do not come from the brief.

| Reason as printed | Cause in the brief | What to change |
|---|---|---|
| `briefs preflight failed: brief for <item> is missing: <path>` | The setup check read a brief whose file does not exist. | Write and commit the file, or correct the path in the tracker. |
| `briefs preflight failed: brief for <item> is empty: <path>` | The setup check read a brief that holds no text, or only spaces and blank lines. | Write the brief and commit it. |
| `briefs preflight failed: brief for <item> exceeds 1048576 bytes: <path>` | The setup check read a brief over 1 MiB. | Shorten the brief. Put long material in repository files that the brief names. |
| `briefs preflight failed: brief for <item> cannot be read as text: <error>` | The setup check could not read the brief as text. | Save the brief as plain UTF-8 text. |
| `briefs preflight failed: brief for <item> declares <paths>, which resolves outside the repository and the allowed roots, ...` | The setup check read a `repository` brief with a declared path outside the fence. | Make the path relative to the repository, add its folder to `[policy] allowed_roots`, or move the work out of this queue. |
| `nature:machine`, `nature:cross-repository`, `nature:owner-gated` | The nature line names that value. A brief the setup check did not read also parks as `nature:cross-repository` when it declares a path outside the fence. | Do the work yourself, or split the part that lives in this repository into its own item. For a declared path, change it as in the row above. |
| `brief:missing <brief>` | The brief file does not exist in your checkout when the item's turn comes. Like the next three reasons, it parks the item before its claim. `<brief>` is the path as the tracker gives it, or only the file name when that path is absolute, a file URL or starts with `~`. A Windows drive-letter or network path counts as absolute on every system. | Write and commit the file, or correct the path in the tracker. |
| `brief:empty <brief>` | The brief holds no text, or only spaces and blank lines. | Write the brief and commit it. |
| `brief:too-large <bytes> bytes, over 1048576 <brief>` | The brief is over 1 MiB. | Shorten the brief. Put long material in repository files that the brief names. |
| `brief:unreadable <brief>` | The brief could not be read as UTF-8 text. | Save the brief as plain UTF-8 text. If it already is, check that you may read the file. |
| `brief:git-staging line <n> ...` | A line of the brief orders the builder to stage or commit through git. | Rewrite every refused line, not only the one the reason names. |
| `no_brief` (Pinax only, a skip and not a park) | The item has no note, or its newest note has an empty reference. | Attach a note whose reference is the brief. |
| `seat brief is outside the allowed roots: <brief>` | The brief file lies outside the repository, the scratch root and the allowed roots. | Move the brief into the repository and commit it, or add its folder to `[policy] allowed_roots`. |
| `seat brief is missing: <brief>` | The brief was in your checkout before the claim but is not in the item's worktree, for example because it was never committed. `<brief>` is written as in `brief:missing`. | Commit the brief. |
| `seat brief is empty: <brief>` | The brief in the item's worktree holds no text, or only spaces and blank lines, for example because the committed copy differs from the one in your checkout. | Commit the brief with its text. |
| `seat brief exceeds 1 MiB: <brief>`, `seat brief cannot be read as text: <brief>` | The copy of the brief in the item's worktree is too large or is not readable as UTF-8 text. | Correct the brief and commit it. |
| `a pass verdict must not report acceptance clause <An> unmet` | The reviewer passed the item while it marked clause `<An>` `unmet`. | Read the review. Correct the clause if it asks for something the item should not do, then set the item back to ready. |
| `timeout:<seat>` | A seat ran past the item's time limit. | Add a class line, or pick a class with more seconds. |

### Nature

AutoBuild reads the nature line before it claims an item. The values `machine`, `cross-repository` and `owner-gated` park the item with the reason `nature:<value>`. No claim is made, no worktree is created and no seat starts, and the campaign moves on to the next item.

Before the claim, AutoBuild also checks the declared paths of every brief against the fence. A `repository` brief with a declared path outside the fence then parks as `nature:cross-repository`, because no builder in the worktree could edit that path. An explicit nature value other than `repository` is used as written, and the path check does not apply to it. The setup check catches the same declared path in the briefs it reads and stops the launch instead.

### The git rule

A git order is a line of the brief that tells the builder to stage or commit through git. AutoBuild parks, before the claim, an item whose brief holds one, because the builder must leave its changes uncommitted for AutoBuild to stage. The tracker reason reads:

```
brief:git-staging line <n> `<invocations>`[ and <k> more line(s)]; describe git only in a clause that forbids it, a report or a quotation
```

In that printed reason, "clause" means what this page calls a phrase. The reason is cut to at most 499 characters, and it names only the first refused line and a count of the others, so fixing only that line parks the item again. Every refused line is in the `item.triaged` event of the run folder, the folder where a campaign keeps its events and evidence ([events and records](reference/events-and-records.md)). The setup check also lists every line in a warning that says the item will be parked at its claim, and the launch goes on.

The commands that count are `git add`, `git rm`, `git mv`, `git commit` and `git stash`. `git apply` counts with `--index` or `--cached`. It also counts with `-3` or `--3way`, unless the same line holds a `git reset` that only empties the index. `git reset` counts unless every argument is `-q`, `--quiet`, `--` or a path that follows `--`, so `git reset -- src/a.py` passes and `git reset src/a.py` is refused.

AutoBuild reads each line as phrases. A phrase is the text between the ends of the line and the marks `;`, `:`, `!`, `?` and a full stop followed by a space or the end of the line. In prose, a command counts only when its phrase gives an order. Such a phrase starts with a verb such as `run`, `stage`, `commit`, `use` or `make`, or it holds `must`, `should`, `need` or `have to` before the command, or it is the command alone. A report of what happened passes, as in "The deletions were staged by `git rm` in the first build."

A negation exempts a command when it comes earlier in the same phrase. The negations are the words `never`, `not`, `no`, `nor` and `without`, and any word ending in `n't`. A phrase that follows a one-word label and its colon is exempt too, when the label is a negation or `Forbidden`, `Prohibited` or `Banned`. These two lines pass:

```
Do not run `git add -A`.
Never: `git add -A`.
```

A negation does not reach into the next phrase, so these two lines are refused:

```
Do not edit the lock file; run `git add -A`.
Do not edit main: run `git add -A` instead.
```

A quotation is exempt too, as a line that starts with `>`. Inside a fenced code block in the brief every git command counts, whatever the words around it say, so never put a git command in a code block in a brief.

### Setup check reasons and a missing brief

The setup check reads only some briefs. In a campaign that runs one item at a time, it checks the next ready item. When more than one item runs at once, it checks every ready item that the selection allows. Run `autobuild preflight` after every brief you write, because it reads the next brief without claiming anything:

```
autobuild preflight
```

Before the claim, AutoBuild applies the nature line, the fence check and the git rule to every brief. Before those rules, it checks that the brief file exists, is within 1 MiB, reads as UTF-8 text and holds some text. An item whose brief fails that check parks with one of the four reasons `brief:missing`, `brief:empty`, `brief:too-large` and `brief:unreadable`, with no claim, worktree or seat, and the campaign goes on to the next item. A seat is also refused before it starts, with `seat brief is missing: <brief>`, when its copy of the brief is not in the worktree, and with `seat brief is empty: <brief>` when that copy holds no text.

A brief file must also lie inside the repository, the scratch root or an allowed root. A brief anywhere else passes the setup check, which does not check its location. Its first seat is refused after the claim. Relative values in `[policy] allowed_roots` resolve against the profile's folder.

## Limits

Because the brief is text that the seats read, it cannot change what AutoBuild's own code enforces. The validator is the only command AutoBuild runs to test a change, and its argument list comes from the profile, so a brief cannot add a test command or change one. The reviewer always has read-only tools, so a brief cannot ask it to run anything. The 1 MiB limit and the git rule are fixed in the code, and no profile key changes them.

A brief raises the number of correction rounds only through its declared paths: in the strict process mode with a boundary map, a declared path that touches a boundary gives the item two rounds in place of one. After the last allowed round, a `correct` verdict on a passing validator is accepted and delivered, and every finding becomes a follow-up item. An `unmet` clause still holds an item back after that, because a `pass` that reports any clause `unmet` is refused and the item parks.

A brief cannot make a non-repository item run, and it cannot set its own time limit: the class line only picks a class the profile already names. Models, effort levels, lanes and the delivery mode come from the profile and the command line, which the [profile reference](reference/profile.md) and the [command reference](reference/commands.md) describe. A brief cannot make AutoBuild build a follow-up it registered during the same campaign, and it cannot make AutoBuild deploy or publish anything.
