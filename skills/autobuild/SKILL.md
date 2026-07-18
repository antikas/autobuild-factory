---
name: autobuild
description: Aggressively execute a project's backlog autonomously — the best model builds each ready item, a blind top-tier audit gates it, fold-until-clean or park, commit per verified cycle on a branch. Fleet-wide, per-project, model-tier-pinned, monitored. When a queue runs dry it comprehends the project's scope and proposes (propose-only) the next features so nothing idles. Bare "/autobuild" targets ONLY the repo the session is invoked from and auto-detects fresh-vs-recover — an interrupted run (killed session, token limit) is RESUMED from its evidence, never rebuilt. Use when asked to "run the backlog", "burn down the backlog", "autobuild <project>", "recover/continue the autobuild", or to run unattended aggressive build work across a repo's queue. Not for a single specified item (dispatch a builder directly) or novel-architecture work (that needs a supervised session).
user-invocable: true
---

# autobuild

Mechanised, monitored, aggressive backlog execution plus scope-aware refill. **[GUARDRAILS.md](GUARDRAILS.md) (same folder) is the SSOT for the guardrails** — the scope / decision / destructive / observability gates, commit cadence, verification economy, and the morning report. This skill POINTS to it; it does not restate it. Read it before a run.

## What it does
Two capabilities, in one workflow per repo:

- **A. Execute** — consume the backlog. Per item, SEQUENTIAL by critical-path order (`pinax next`, else the parsed `BACKLOG.md` ready set) so a dependent never builds before its blocker: **select → route by complexity → build (no commit) → blind dual audit → fold-or-adjudicate → accept+commit on the branch + merge-to-default + push (per item), or park with a structured reason and restore the tree clean.** Loops until the eligible queue is dry, `maxItems`, or the token-budget reserve.
- **B. Scope + propose** — when the queue drops below `refillThreshold` (or on demand), comprehend what the project IS from its own docs, propose candidate features, blind-critic them against the project thesis, and land them **propose-only** to `docs/PROPOSED-BACKLOG.md`. Conservative auto-promote feeds only clearly-in-scope + reversible + small + precedent-determined items into the live queue; everything novel, architectural, or large stays propose-only for owner curation. **Frontier-vs-fog gate:** a candidate whose *question isn't precisely stateable yet* (a direction, not a question) is FOG — the critic routes it to a fog ledger (`cfg.fogLedger`, else `docs/FOG.md`, with the blocking question and a `Surface when:` trigger), never the queue and never PROPOSED-BACKLOG. Captured lossless, dispatched never; it graduates when the question sharpens.

## Tracking (every cycle is trackable)
The run is only worth as much as its trail. **Every cycle writes status back to the tracker**, not just to git:

- **`trackerKind: 'pinax'`** — a repo tracked with [pinax](https://github.com/antikas/pinax-tracker): read the next item with `pinax next` (critical-path order over the typed graph); on **accept** append a completion event (`pinax done <id>`) and commit it (explicit path); on **park** append a block/park event with the structured reason. The `.ergon` event log is then the SSOT for what the run did, replayable on any machine.
- **`trackerKind: 'backlog'`** — a repo not on pinax: read the `BACKLOG.md` ready set; mark items done / annotate parks in `BACKLOG.md` with explicit-path commits.
- **Register-before-branch (visibility invariant):** when a run is fed by items not yet in the tracker (a plan-authored backlog), register them and **commit the registration on the repo's DEFAULT branch before creating the run branch** — a tracker-only commit. The run branch then starts already carrying the items, so an in-flight run is visible from the base branch (not nonexistent), and the registration survives run-branch or machine loss. Only claims, close-outs, and parks ride the run branch. Mid-run refill promotes necessarily land on the run branch and become visible at merge.

Two robustness rules: (1) if the tracker command errors or isn't installed, the scout **falls back to parsing `BACKLOG.md`** and flags it in the report — a missing tracker never stalls the run; (2) tracker write-back is a **separate, cheap close/park agent** with explicit-path commits, so a tracker hiccup never contaminates the code commit.

## Dispatch philosophy
Best-tool-for-the-job. **The best model builds, review is separate and independent, the most-suitable model runs each job.** The premium judgment tier (Fable-class) is the most capable *and* most expensive — point it at the HARDEST, longest-horizon, highest-judgment work, never the mechanical tail.

## Role → model map (pinned per seat — never inherited from the session)
- **Epistates (coordinator + adjudicator) = the judgment tier.** The tough-decision seat: adjudicates the audit verdict, fold-vs-close, park-vs-proceed-on-precedent, scope-drift. Keeps judgment in its own context; never builds in its own context; delegates the mechanical close and fold-brief drafting to cheaper agents. The invoking session runs on the judgment tier so the full cycle context sits in one place for the call.
- **Chiron (builder), routed per item:** `sonnet` standard · the top tier for hardest/long-horizon/ambiguous-but-well-specified · `haiku` trivial mechanical. Fresh context per item, no memory across items.
- **Kritos (audit/gate) = opus** (or a capable OpenAI model via the codex lane when Claude quota is tight) — blind + independent, FRESH context. The invariant is fresh context + a capable tier, NOT model identity: opus-reviews-opus and gpt-reviews-codex are both fine so long as the auditor's context is fresh and its tier is capable. Trust chain: fresh builder builds → blind audit → adjudicator decides. No actor trusts its own work.
- **Proskopos (scout / select) = sonnet** — reads the backlog, classifies each ready item's complexity and eligibility, feeds the router. NOT haiku: scouting is multi-step tool use plus the routing judgment that decides where money gets spent; haiku demonstrably fumbles it.
- **Mechanical close/park + fold-brief drafting = sonnet** — delegated, to keep the adjudicator's context on judgment, not mechanics. Sonnet not haiku: these do multi-step git hygiene, and a fumbled close contaminates a clean cycle.

Accepted trade-off: the judgment-tier session pays premium rates on routine orchestration turns too. Right anyway — adjudication needs the full cycle context in one place; splitting it to a cheaper sub-call fragments the context that makes the decision good.

**Premium-tier hygiene.** The top tier is the JUDGMENT tier: coordinator + adjudicator, dense single turns. It is NOT a build tier — under multi-lane ladders, hard builds route to `opus` then the codex lane; no build seat runs on the premium tier unattended.

## Multi-lane build — continuous build on subscription quota
Goal: fleets keep building when one provider's subscription window exhausts — **judgment quality unchanged, zero API-priced tokens.**

**Models, provider inferred (tiering decoupled from provider).** A build seat is filled by a **model id**; the model's provider decides how it runs — a Claude model (`opus`/`sonnet`/`haiku`/`fable`) via the normal builder, an OpenAI model (`gpt-*`) via `codex exec` on the owner's ChatGPT subscription (ChatGPT auth, **NO API key**). Judgment seats — the two blind audits and the adjudicator — run on a **capable tier with FRESH context, Claude OR a capable OpenAI model**. A non-capable tier on a judgment seat throws (right-tier guard); the scout and mechanical seats stay Claude (no codex path). **Weekly ceiling (unsupervised runs only):** at ≥88% weekly Claude usage, every Claude model is off-limits and the unsupervised run STOPS CLEAN — conserving the window for the owner's supervised sessions. Individual-model exhaustion below the ceiling cools that model and judgment falls to the codex lane.

**Routing = push, by the coordinator (not pull-by-lane).** Each item class carries an ordered preference ladder of model ids; the router takes the first *available* model, deterministically. Example defaults when enabled: `buildHard: ['opus','gpt-5.5']` · `buildStandard: ['gpt-5.5','sonnet']` · `buildTrivial: ['haiku','gpt-5.4-mini']`.

**Cooldown + the shared signal.** A codex build returning a usage-limit signature COOLS that lane: the router records it in `~/.autobuild/lanes.json` (cooldown = a parsed reset time, else 60 min, doubling per repeat) and reroutes the item. That file is **machine-local and shared across concurrent runs**, so one repo hitting the window steers every running repo toward the fallback until reset. Each accept records its lane (commit trailer + tracker actor), so the log answers "which lane built what".

**Continuity.** Build lane cools → route around it. ALL build lanes cooling → **stop clean**: commit/park per normal discipline, the run returns `stoppedReason: all-lanes-cooling` and stays resumable (§Recovery). Judgment: opus cooled → judgment falls to the codex lane; the run stops clean only if NO judge lane is available.

**Claude-side quota instrumentation (optional, not bundled).** Preflight (multi-lane runs) looks for a local probe script at `~/.claude/skills/autobuild/quota.py` that prints one JSON line of live subscription-limit utilisation (per limit: kind, percent, severity, reset time, model scope). If you supply one, preflight cools any CRITICAL model-scoped limit with its real reset time and applies the weekly ceiling. Non-fatal and config-gated (`claudeQuotaProbe:false` disables); it is a *soft* signal (script absent or failing → no signal → proceed).

**Enabling it (DEFAULT-OFF — the rollback guarantee).** With **no `lanePolicy`** the router uses the single-lane path. A repo opts in by passing `lanePolicy`; the codex lane also needs a ChatGPT subscription with the `codex` CLI signed in. **Preflight (multi-lane runs only)** FAILS the launch if `ANTHROPIC_API_KEY`/`OPENAI_API_KEY` is in the env (the subscription-only guarantee) and DISABLES the codex lane if `codex login status` is not authenticated (router falls back to Claude — never a hard failure).

## The aggressive filter (attempt broad, gate hard, park loudly)
An item is eligible UNLESS: (a) irreversible/destructive → propose-only manifest, never the act; (b) genuinely-open or novel-architecture with NO precedent → park + log; (c) blocked. **A precedent-determined REVERSIBLE call IS eligible** — take the precedent, build, commit, surface in the report. Aggression is breadth-of-attempt plus gate-hardness, never relaxing the destructive/novel-architecture gates.

## Judgment-tier harness notes (baked into the build/coordinator prompts)
- **Full task spec up front, one well-specified turn, high effort.** Long-horizon coherence depends on a clear goal stated up front; a thin brief wastes the tier. De-prescribe: state goal + constraints, not micro-steps (over-stepping REDUCES quality).
- **Turns run minutes, not seconds** at high effort. The monitor's stall deadline MUST tolerate this — liveness = journal result events + commit activity, NEVER output-file mtime alone.
- **Refusal handling:** a premium-tier builder can refuse benign security-adjacent work — the router falls back to opus rather than parking the item.

## Invocation
Per repo, run the bundled workflow:

```
Workflow({ scriptPath: "~/.claude/skills/autobuild/autobuild.workflow.js", args: <the per-project config below> })
```
(Resolve `~` to the absolute home path when invoking.)

**Default scope — the current repo.** Bare `/autobuild` (or "run the backlog" / "recover the autobuild" with no repo named) targets ONLY the repo the invoking session's working directory belongs to. Fleet scope is never inferred: it requires the explicit ask (`autobuild fleet` / named repos). On invocation, detect the run state first (orchestrator step 0): an existing `autobuild/<date>` worktree or branch with work on it means RECOVER (§Recovery below); a clean slate means start fresh.

### Workflow args (per-project config — SSOT for fleet params)
```
{ repo, branch, workdir, readySet, nextCmd, validatorCmd, maxFolds, maxItems,
  proposeWhenDry, refillThreshold, promotePolicy,   // 'conservative' | 'off'
  trackerKind, doneCmd, parkCmd,                     // 'pinax' | 'backlog' + write-back cmds
  protectedItems,                                    // recovery: item ids a park must never revert (§Recovery)
  recallTool, fogLedger, knownTraps,                 // optional environment hooks (see the workflow header)
  modelPolicy: { coordinator:'fable', proskopos:'sonnet', buildHard:'fable',
                 buildStandard:'sonnet', buildTrivial:'haiku', audit:'opus',
                 mechanical:'sonnet' } }
```
`workdir` is the isolated run worktree every agent operates in (see orchestrator step 2) — a concurrent session in the repo's main tree must never be able to clobber an uncommitted cycle. `readySet` is the orchestrator's pre-parsed ready-set list handed to the scout as a closed universe — mechanical selection over a small list, never a model free-reading a 500KB+ tracker. No access to the premium tier? Set `coordinator`/`buildHard` to `'opus'` — the seat contract holds at any capable tier.
`trackerKind` defaults from `nextCmd` (contains `pinax` → `'pinax'`, else `'backlog'`). `doneCmd`/`parkCmd` are optional templates (e.g. `pinax done <id>` / `pinax block <id> --reason "..."`); left blank, the close/park agent infers them from `trackerKind`. Fill exact commands at first run per repo and commit the config row into your own fleet table.

### Config table (your fleet — template)
Keep one row per repo you run autobuild on, committed wherever you keep operational config. The row IS the per-repo memory: validator scope, known traps, owner-gated items the filter must never pick.

| repo | branch | tracker | legacy archive (frozen) | validatorCmd |
|---|---|---|---|---|
| `~/src/example-app` | `autobuild/<date>` | `pinax` | `docs/BACKLOG.md` | `pnpm type-check && pnpm lint` (+ touched-package tests as evidence; `pnpm install` in the worktree first). Owner-gated, forbid by name: `<ids of deploy/publish items>` |

Fill each `validatorCmd` at first run per repo and commit the row.

**CANON — validator scope: never the full test gauntlet, only the relevant tests.** An item's validator runs ONLY what's relevant to what it changed: the fast static gates (type-check + lint) plus the **touched packages'** unit tests, filtered to the item's blast radius. **NEVER run the whole test suite, and never the integration / e2e suites that need external services** (a registry, a browser, seeded DB state) — they are slow, environment-dependent, and are NOT the item's gate; running them stalls or falsely fails good work. Run tests in the **foreground and read the exit code** — never launch a background test and poll for a `DONE` marker. Determinism/isolation tests an item's acceptance names ARE in scope; run those directly. **The economy binds every seat, not just the validator:** audit seats READ artefacts and never re-run suites (at most re-derive one load-bearing claim — one test file, one command); the full suite runs ONCE per run, at accept/merge time for the whole branch — never per item, per fold, or per role; and a green run whose inputs haven't changed is never repeated. Builder over-verification — running any suite beyond the scoped validator, re-running an already-passed command, or invoking suites the item did not name — is a defect the blind audit **cites**; the builder prompt also carries a two-attempt thrash floor (recall the signature, stop after two failed fixes, return failed-with-evidence — never a third iteration). Full statement: [GUARDRAILS.md](GUARDRAILS.md) §Verification economy.

## What the orchestrator does on invocation (you just type `/autobuild`, `autobuild <repo>`, or `autobuild fleet`)
The skill is turnkey — the owner invokes it and reviews the result. **You never hand-create a branch or hand-arm a monitor.** On invocation the orchestrating (judgment-tier) session does, per repo, automatically:
0. **Run-state detection (fresh vs recover):** `git -C <repo> worktree list` + `git -C <repo> branch --list 'autobuild/*'`. An existing run worktree/branch carrying work (commits beyond the default branch, a dirty worktree, tracker write-backs on the branch) = an interrupted run → follow §Recovery and REUSE it. Only a clean slate proceeds to step 1; two dated runs never coexist for one repo.
1. **Branch (fresh runs):** if the run's backlog items are not yet in the tracker, register + commit them on the default branch FIRST (register-before-branch, §Tracking). Then create `autobuild/<date>` off the repo's default branch. Workflow AGENTS never touch main; the close seat merges each accepted item to the default branch per §Merge-on-item.
2. **Isolated run worktree (single-writer discipline):** `git -C <repo> worktree add <repo>-autobuild autobuild/<date>` and pass it as `workdir` — every workflow agent operates ONLY there. The main tree stays on whatever branch it had. After the run: `git -C <repo> worktree remove <repo>-autobuild` (`--force` if the run left it dirty).
3. **Pre-parse the ready set:** parse the tracker yourself (grep the BACKLOG statuses / `pinax next` / board frontmatter) and pass the closed candidate list as `readySet` — the scout selects from it, it never free-reads trackers.
4. **Observability:** arm ONE session-persistent Monitor for new-commit events before the first dispatch (the observability gate — a run that cannot be observed does not run).
5. **Dispatch:** invoke the workflow with the repo's config row (`proposeWhenDry` true, `promotePolicy` 'conservative', `maxItems` to chew the queue) — `args` as a real JSON object (the script hard-fails on a stringified object, but pass the object). The router points the top tier at the hardest items.
6. **Report + log:** at the end, write the per-repo morning report — **shipped** (merged to the default branch; note any `deferred-root-tree-busy` merges still sitting on the run branch) / **parked** (+ decision) / **proposed** (link the doc) / **failed** / **next** (on a pinax repo this is `pinax report`).

**Merge-on-item.** Each ACCEPTED item merges the run branch into the repo's default branch and pushes, at item close — main always equals "everything accepted so far"; rollback is `git revert` of that item's merge. Rationale: accumulated unmerged run branches obscure what pending work is in, and rot into hard merges. Rules: the close agent merges from the repo ROOT tree only when it is on the default branch and clean (a dirty or otherwise-checked-out root tree defers with `merge: deferred-root-tree-busy` + a pushed run branch — never clobber concurrent work); parks/blocks never merge; the once-per-run full acceptance confirmation still runs ONCE at run end on the merged default (per-item merges ride the scoped validator + blind audit, per the verification economy). Deploy note: never deploy from main-head in unattended runs — pin deploys to tagged releases, so merge-on-item ships nothing.

## Recovery — resuming an interrupted run (kill, crash, token limit)
A killed session loses its context, never its evidence. Recovery is evidence-first, in the SAME branch and worktree — never a fresh branch, never a rebuild of work that already exists:

1. **Read the state from git, not memory:** `git worktree list`; `git log <default>..autobuild/<date>` (what shipped); `git status` in the WORKTREE (an in-flight cycle's uncommitted build); and the tracker read from INSIDE the worktree — tracker write-backs live on the run branch, so the main tree's tracker view is stale by exactly the shipped items.
2. **Snapshot in-flight work BEFORE any dispatch:** `git stash create` + `git stash store` for tracked changes, plus a scratchpad copy of untracked new files (`stash create` does NOT capture untracked). No fold/park/restore step may ever be the only holder of an uncommitted build.
3. **Resume at the seam, don't restart:** an uncommitted build re-enters the FULL gate — the builder VERIFIES AND COMPLETES it against the spec (never resets, never starts over), then blind dual audit → adjudicate as normal. Carry the resume instructions in the item's closed-readySet entry (the scout copies them into the brief verbatim) and list the item in `protectedItems` so a park can never wipe the evidence.
4. **Cross-tree loss check:** diff the repo's MAIN tree for uncommitted edits to files the in-flight build also rewrites — a from-scratch rewrite on the branch silently drops a main-side addition at merge. QUOTE any such content into the resume brief (the workdir boundary forbids agents reading the main tree themselves).
5. **Continuation posture:** a recovery consumes the interrupted queue only — `proposeWhenDry: false`, `maxItems` = the remaining items — unless the owner explicitly widens it.
6. **Observability hygiene:** the killed run's subagents orphan their liveness markers and a watchdog can ghost-alert until they're gone — sweep the dead session's marker files, then re-arm the commit Monitor before dispatch. The observability gate applies to resumed runs too.

### The only genuine human gates (one-time, not per-run chores)
- **Data-retention config** for the premium tier, if your org requires it — confirm once before the first run (an org setting, not something the skill can set).
- **First-ever smoke test:** before the first *unattended fleet* run, do one supervised single-item run in one repo end-to-end to confirm route → build → blind audit → adjudicate → accept-on-branch works and the tree is clean between items. After that it's turnkey.

## Acceptance criteria
- `autobuild` invocable globally; runs a full route → build → blind-audit → adjudicate → accept cycle on a real item, lands it on a branch, tree clean between items.
- The adjudicator runs on the judgment tier and makes the adjudication/fold-vs-close/park calls in its own context; mechanical close + fold-brief drafting delegated to cheaper agents.
- Router sends hard items to the top tier, routine to sonnet, trivial to haiku; audit is opus (or a capable codex-lane judge).
- Propose pass produces a critic-filtered `PROPOSED-BACKLOG.md`; conservative auto-promote feeds only in-scope + reversible + small items into the queue; refill-when-dry keeps the loop fed without unbounded scope drift.
- Monitor deadline tolerates minutes-long judgment-tier turns; refusal falls back to opus.
- Guardrails enforced by reference to [GUARDRAILS.md](GUARDRAILS.md), not duplicated.
- Bare invocation scopes to the current repo; an interrupted run is detected (step 0) and resumed per §Recovery — same branch/worktree, in-flight build snapshot-protected and re-gated, never lost; fleet only on explicit ask.
