# Platforms

You want to run AutoBuild on your own machine, and you have already chosen at least one coding assistant for it to drive. The operating system and the coding assistant are separate choices. AutoBuild picks its process handling from the operating system it runs on. It picks the coding assistant from the lanes in your profile, so you set up a lane the same way on every system once its command is installed and signed in.

The systems differ in how AutoBuild starts, watches and stops the commands it runs, in how much of the machine it can measure, and in how it locks its own files. This guide covers installing AutoBuild on Windows, macOS and Linux, the folder where it keeps its working files and when you may clear it, and the environment variables it reads. The [README](../README.md) explains what AutoBuild does with your repository, and the [harness adapter reference](reference/harness-adapters.md) lists what each coding assistant needs.

## Requirements

Check that the machine has each of these before you install:

- Python 3.11 or later. The package, `autobuild-factory`, has no runtime dependencies.
- Git, and a repository with a remote named `origin`. A launch without that remote is refused with `required git remote is missing: origin`. AutoBuild checks no Git version.
- pip to install the package, or uv to run it without installing it. AutoBuild checks no uv version.
- At least one coding assistant, installed and signed in: Claude Code (`claude`), Codex (`codex`) or GitHub Copilot CLI (`copilot`). The `claude-sdk` lane needs no command; it needs the optional `sdk` part of the package and a file that holds an API key.
- `curl` on `PATH`, when you want AutoBuild to read your Claude usage allowance.
- The GitHub CLI (`gh`), signed in, when you use the Copilot lane without a token variable.
- A tracker, either Pinax or a `BACKLOG.md` table. The [trackers guide](trackers.md) covers both.

## Install AutoBuild

The package installs the `autobuild` command only, and `python -m autobuild` runs the same program, so you can always fall back to the second form when your shell does not find the first. The skills for coding assistants live in the `skills/` folder of the source repository, and pip does not install them. The `sdk` part, which only the `claude-sdk` lane needs, adds the Claude Agent SDK at the one release series AutoBuild accepts, `claude-agent-sdk>=0.2.168,<0.2.169`. To upgrade, run the same install command with the new version number.

### Windows

In PowerShell or the command prompt, install the package into the Python you will run AutoBuild from, and add the `sdk` part when you use the `claude-sdk` lane:

```
python -m pip install autobuild-factory==0.12.0
python -m pip install "autobuild-factory[sdk]==0.12.0"
```

With uv, run it without installing it:

```
uvx --from autobuild-factory==0.12.0 autobuild preflight
```

Windows limits the length of a file path, and every path AutoBuild writes starts with the scratch root described below. AutoBuild keeps its own folder names short for this reason, but a deep scratch root still lengthens every nested test folder. Set `[run] scratch_root` to a short folder, such as `C:\ab`, when your tests create deep folder trees.

### macOS

In the Terminal, install the package and, when you use the `claude-sdk` lane, its `sdk` part. Keep the quotation marks, because the shell would otherwise read the square brackets:

```
python3 -m pip install autobuild-factory==0.12.0
python3 -m pip install "autobuild-factory[sdk]==0.12.0"
```

These commands use `python3`. When your system has a `python3` command and no `python` command, use `python3` in `validator.argv` as well. The `validator-runnable` setup check looks the first word of that list up on `PATH`, and a shell alias or function does not count. With uv, the `uvx` command from the Windows part works unchanged.

### Linux

In your shell, install the package the same way as on macOS:

```
python3 -m pip install autobuild-factory==0.12.0
python3 -m pip install "autobuild-factory[sdk]==0.12.0"
```

The `python3` point from the macOS part applies here too: some Linux systems have a `python3` command and no `python` command, and then a validator that starts with `python` fails the `validator-runnable` setup check. The `uvx` command works unchanged.

## Check what AutoBuild can measure

AutoBuild decides how many items to run at once from the machine's processors and memory, and it watches the load while a campaign runs. Each operating system gives it different readings, so check yours from the root of your repository:

```
autobuild host
```

The command prints the number of logical processors, how busy they are over the sample window (`--sample-seconds`, 1 second by default), the memory figures and the commit figures, and the number of items AutoBuild would run at once. The commit figures are the system's limit on the memory it has promised to programs, and how much of that limit is left. A figure that this system cannot give is printed as `unmeasured`. When AutoBuild has a reason for it, the reason is printed on its own line starting `note:`, and some unmeasured figures come with no note. Add `--json` for the same reading as JSON with the schema `autobuild.host.v1`. The command exits 0, or 2 when the profile cannot be read or `--sample-seconds` is not a positive number.

The automatic number of items comes from two terms, set in the `[capacity]` table of the profile. The processor term is the processor count divided by 4. The memory term is the available memory, less `reserve_gb`, divided by `per_item_gb`. AutoBuild takes the smaller term and holds the result between 1 and `max_concurrency`. When memory is unmeasured, only the processor term counts. You can set a fixed number with `run.concurrency` on any system, and the [profile reference](reference/profile.md) gives the defaults.

## The scratch root

The scratch root is the folder where AutoBuild keeps everything it writes outside your repository. By default it is `autobuild` inside the system's temporary folder. Set it with `[run] scratch_root` in the profile, where a relative path resolves against the profile's folder, or with `--scratch-root` for one command. A relative `--scratch-root` also resolves against the profile's folder, or the repository when there is no profile, in every command that takes it. The [command reference](reference/commands.md) covers each command.

We recommend a scratch root in a folder that lasts, set with `[run] scratch_root`. The default sits in the system's temporary folder, which the operating system or a cleaning tool may empty, and the scratch root holds work that a later campaign picks up again.

When AutoBuild starts, it sets these variables in its own process:

| Variable | Value |
|---|---|
| `TMPDIR`, `TEMP`, `TMP` | `<scratch>/tmp` |
| `UV_CACHE_DIR`, `PIP_CACHE_DIR`, `PYTHONPYCACHEPREFIX`, `XDG_CACHE_HOME`, `NPM_CONFIG_CACHE` | `uv-cache`, `pip-cache`, `pycache`, `xdg-cache` and `npm-cache` under the scratch root |
| `NO_COLOR` | `1` |

Every command AutoBuild starts inherits these, and AutoBuild then gives each seat and each validator run temporary folders of its own. A seat gets its folders under `harness/<lane>/scratch/`, and a validator under `items/`, as the [harness adapter reference](reference/harness-adapters.md) lists. AutoBuild names each of those folders from the cleaned item id and a hash of 12 characters, fixed in the code, so that the paths stay short on Windows.

### Resume and kept worktrees

To resume an item is to pick it up again in a later campaign. At its start, every campaign resumes each item that the tracker still shows as claimed by AutoBuild's builder, inside the campaign's selection, from the item's kept worktree and its phase marker. A phase marker is the file `runs/<run-id>/evidence/<item>-phase.json`, which records the stage the item last reached, its worktree and its head commit. An item is left claimed when its builder ran out of time and the campaign stopped before it could continue, when its park itself failed, or when the campaign was killed while the item ran. A parked item is never resumed, even when Pinax still shows the builder's claim on it, and its worktree stays as evidence until you remove it.

Because a resume needs both the kept worktree under `workspace/worktrees/` and the phase marker under `runs/`, removing either one parks the item as `resume:missing-worktree` or `resume:missing-marker`, and the builder's uncommitted work is lost. Before you clear `workspace/` or `runs/`, check that no item still needs them:

- In the campaign report, the Unfinished section lists each item that ended without a recorded park, with its worktree path. The Failed section lists each builder continuation that the campaign stopped, with the reason `timeout:builder; kept with its claim and worktree for the next campaign`. Clear nothing while either lists an item.
- In the repository, `git worktree list` shows every worktree that Git still knows. AutoBuild finds kept worktrees through that list, among the entries under `<scratch>/workspace/worktrees/`.
- In the tracker, an item that still reads as claimed by `builder@<lane>` will be resumed.

When you remove a worktree folder by hand, AutoBuild skips Git's record of it, because it ignores a listed worktree whose folder no longer exists. Git keeps that record, and the item's local branch `autobuild/<campaign>/<item>-<8 characters>`, until you remove them; `git worktree prune` removes the record.

### Each part, and when to clear it

AutoBuild removes the worktree of an accepted item, its own validation and report checkouts, and the worktrees of `autobuild subscription-review`. It removes no other folder, so the scratch root grows over many campaigns. Clear a part only while no campaign and no AutoBuild command runs on this scratch root.

| Path | Holds | When you may clear it |
|---|---|---|
| `runs/<run-id>/` | The run folder of one campaign: `manifest.json`, `events.jsonl`, `progress.log`, `report.txt`, a pause request, and `evidence/` with phase markers, builder reports and park snapshots | When no item needs a resume, by the checks above. You lose that run's record and the park snapshots in it. |
| `workspace/worktrees/` | The worktrees of claimed, parked and unfinished items | When no item needs a resume, by the checks above. Run `git worktree prune` afterwards. |
| `workspace/evidence/`, `workspace/reports/`, `workspace/validation/` | The review patch of each worktree, and the checkouts of report delivery and batch validation | With `workspace/worktrees/`, by the same checks. |
| `harness/<lane>/` | Each lane's prompts, result schemas, results, logs, the full message stream of each `claude-sdk` seat, and the seats' temporary folders | Any time. The run folder's references to a seat's raw output then point at nothing. |
| `commands/` | The standard output and standard error of every command AutoBuild ran: seats, validators, batch runs, probes and the usage meter | Any time. The run folder's references to command output then point at nothing. |
| `items/` | The temporary and pytest folders of each item's validator runs | Any time. |
| `tmp/`, `uv-cache/`, `pip-cache/`, `pycache/`, `xdg-cache/`, `npm-cache/` | AutoBuild's own temporary files and the package caches | Any time. The next campaign fills the caches again. |
| `leases/` | The leases that keep two campaigns off one tracker and one worktree | Any time. |
| `lanes.json` | The lane coolings, unless you set `run.lane_state_root` | Any time. The next campaign starts with no lane cooled. |
| `usage-meter/` | The shared usage reading, one file per meter | Any time. The next reader asks the meter again. |
| `usage/` | The working folder of the usage meter's commands | Any time. |
| `subscription-admission/` | Under subscription routing, the admission ledger: the seats that have a share of a lane's allowance set aside, and the seat cost AutoBuild has observed | Any time. AutoBuild then estimates each seat's cost from its smallest estimate again. |
| `subscription-reviews/` | The receipts of `autobuild subscription-review`; its checkouts are removed after each review | Any time. You lose the receipts. |
| `preflight/<id>/` | The private lane state of one `autobuild preflight` | Any time. |

Records and output under the scratch root can hold secrets. Every validator run inherits AutoBuild's whole environment, and every seat inherits it apart from the variables its lane removes, so a validator that prints its environment writes every credential in it to `commands/`. A park snapshot copies every untracked file of the worktree that Git does not ignore. Keep the scratch root as private as the machine's own credentials.

Campaigns see each other's leases and usage readings only when they share a scratch root, and each other's coolings only when they share a lane state root, which is the scratch root by default. Give every campaign on one repository the same scratch root. Campaigns on other repositories may share it as well, and then you name their runs with `--run`.

## Environment variables AutoBuild reads

AutoBuild reads these variables from your environment, and for most of them it checks only whether they are set. Every command it starts also inherits its whole environment, apart from the variables a lane removes from its seats, as the [harness adapter reference](reference/harness-adapters.md) describes.

| Variable | Use |
|---|---|
| `TMPDIR`, `TEMP`, `TMP` | read by Python to find the system's temporary folder, which holds the default scratch root |
| `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `SSL_CERT_FILE`, `SSL_CERT_DIR`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `NODE_EXTRA_CA_CERTS`, `NODE_TLS_REJECT_UNAUTHORIZED`, `GIT_SSL_CAINFO`, `GIT_SSL_NO_VERIFY`, `SSLKEYLOGFILE` | the `interception` setup check, which looks for settings that redirect or inspect network traffic, refuses the launch when one is set, unless you name it in `[preflight] accepted_environment` |
| `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` | presence only: the setup check warns, because AutoBuild removes both from every seat |
| `COPILOT_GITHUB_TOKEN`, `GH_TOKEN`, `GITHUB_TOKEN` | presence only: any one of them passes the Copilot sign-in check |
| `PYTEST_ADDOPTS` | kept for each seat, with a pytest `--basetemp` added |
| `COMSPEC` | Windows only: the command interpreter for `.cmd` and `.bat` files, `C:\Windows\System32\cmd.exe` when it is not set |

Under subscription routing, AutoBuild also reads the name of every variable in its environment, to find the ones it removes from each seat; the harness adapter reference lists them. The Claude usage meter does not read `CLAUDE_CONFIG_DIR`, and it needs Claude Code's own sign-in. AutoBuild never prints that sign-in or stores it. The meter reads an endpoint that is undocumented and may change or stop working without notice.

AutoBuild also sets one variable for your validator. `[validator] patient_argv` is an optional, gentler form of your validator command for a busy machine. High load is the level set by the `[pressure]` table, by default a processor mean of 90 percent or available memory below its limit. A validator that starts at high load, or in the 60 seconds after it, a figure fixed in the code, runs `patient_argv`. When you set none, it runs your usual command with `AUTOBUILD_HOST_PRESSURE=1`.

## Windows

AutoBuild uses its Windows process handling when Python reports a Windows system. It starts each command in a new process group, and it stops a command by ending the whole process tree with `taskkill /PID <pid> /T /F`. The progress deadline, `run.seat_stall_seconds`, is how long a seat may go without visible progress. AutoBuild reads each child's processor time from Windows, so it stops a seat whose output, worktree and processor time have all stayed unchanged for that long. It matches the names of variables it removes in upper case, because Windows treats variable names without regard to letter case.

AutoBuild reads the machine's figures through the Windows kernel: the number of logical processors, how busy they are over the sample window, total and available physical memory, and the commit limit and headroom. A call that fails gives `unmeasured` with a note, and when the kernel library cannot be loaded, only the processor count is read.

A `.cmd` or `.bat` file starts through the command interpreter, as `%COMSPEC% /d /s /c`. Because of this, a validator or a coding assistant command that is a batch file runs through a shell on Windows, while every other command runs with no shell. When the `claude` command resolves to the `.cmd` or `.bat` file that npm installs, AutoBuild runs Claude Code's own `claude.exe` from the same package, or `node` with its `cli.js` when only that is present.

AutoBuild locks its lease files with the Windows file locking call, and judges whether a lease holder is alive by asking Windows for that process and its exit code. When Windows refuses to create the lane state lock file while an earlier copy is being deleted, AutoBuild treats it as a busy lock and waits for the lock's time limit of 5 seconds, fixed in the code.

Subscription routing is the mode, turned on with `[subscription_routing] enabled = true`, that spreads seats across Claude and Codex subscriptions; the [guide to lanes and usage](lanes-and-usage.md) explains it. In that mode AutoBuild sets aside a share of a lane's allowance for each seat before the seat starts (a reservation). A reservation that its seat never released keeps counting until it has expired, at the seat's time limit plus 300 seconds (fixed in the code), and its process has ended. On Windows AutoBuild treats every process as alive, so the unreleased reservations of a campaign that crashed keep counting until their usage window resets, which for a weekly window can take days. A reservation only lowers what AutoBuild admits on that lane; it spends nothing from your provider's allowance.

## macOS

**Usage levels do not act with a Keychain sign-in.** When Claude Code keeps its sign-in in the macOS Keychain, the Claude usage meter cannot read your usage, and every reading fails, because AutoBuild offers no other way to reach that sign-in. With the default `[usage] on_probe_failure = "allow"`, each seat then starts without a reading, so your stop levels never act; watch your usage yourself. With `refuse`, the launch is refused.

AutoBuild uses its POSIX process handling on macOS. It starts each command in a new session, and it stops a command by sending its process group `SIGTERM`, then `SIGKILL` after 2 seconds, a delay fixed in the code. It locks its lease files with `flock`, and it judges whether a lease holder is alive by sending that process the null signal.

AutoBuild reads the full machine figures only on Windows and Linux, which it tells apart by the platform's name. On every other system, macOS included, it measures only the number of processors. Memory, processor load and commit are all `unmeasured`, and the automatic number of items comes from the processor count alone. The load monitor records `campaign.pressure_unmeasured` once and keeps the starting number of items for the whole campaign. Because the load is never measured, a validator never switches to `patient_argv`, and a seat's time limit is never lengthened for high load.

Because AutoBuild cannot read a child's processor time on macOS, and an unknown reading never stops a seat, the progress deadline never stops a stalled seat of the `claude-code`, `codex` or `github-copilot` lanes there. A seat of those lanes that hangs is stopped by its time limit or by cancellation. The time limit is `run.seat_timeout_seconds`, or the limit of the seat's item class, a named time limit that a brief picks with its `Item class:` line from `run.item_classes` (see the [profile reference](reference/profile.md)). Set that limit to the longest time you are willing to wait for one seat. The `claude-sdk` lane judges progress by the SDK's messages and the worktree, in the same way on every system, so its progress deadline applies on macOS too.

## Linux

AutoBuild uses the same POSIX process handling on Linux as on macOS, including the lease locks. It reads the machine's figures from `/proc`. Separately, it reads each child's processor time from `/proc/<pid>/stat`, so it stops a seat at its progress deadline as on Windows.

Total memory comes from `MemTotal`. Available memory is measured only when `/proc/meminfo` reports `MemAvailable`; otherwise AutoBuild leaves it unmeasured and never estimates it. Without it, the load monitor stops at the starting number of items, as on macOS, with no `patient_argv` and no longer time limit under load. Commit figures are measured only under strict overcommit, `vm.overcommit_memory = 2`. On every other setting the commit lines read `unmeasured`.

## Limits and proof

The package declares no supported operating system. No pairing of an operating system and a coding assistant is tested end to end. AutoBuild stops a stalled seat on Windows and on Linux. The test suite covers the stall decision and, with a stand-in for the Windows kernel, the Windows processor reading, and no test covers the Linux reading.

Two kinds of evidence apply to your machine. Run `autobuild preflight` and one small campaign there before you rely on AutoBuild. You can also run the test suite from a checkout of the source repository with `uv run pytest`, as the [architecture guide](architecture.md) describes. It includes cases that run only on Windows and cases that run only on POSIX systems such as Linux and macOS.
