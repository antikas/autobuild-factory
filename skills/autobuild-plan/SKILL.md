---
name: autobuild-plan
description: Turn a one-line build ask into a blind-review-gated, autobuild-ready plan — memory-first research, ground-truth scouts over every surface the ask touches, an end-to-end plan (build → go-live → publication surfaces), a four-lens blind adversarial review folded to PASS, and the queue registered on the tracker, stopped one command short of launch. Use when asked to "create a plan to autobuild X", "make X autobuildable end to end", or to resurrect a previously-specified-but-never-executed build as a dispatchable queue. Not for running the build itself (/autobuild) or for single-item work (dispatch a builder directly).
user-invocable: true
---

# autobuild-plan

Produces the *deliverable plan* that `/autobuild` later executes. The owner types the ask; everything else — research, plan, adversarial gate, registration — is this skill's job. It STOPS at a registered, dispatch-ready queue: launching the run is the owner's call (plan-then-execute discipline), unless the ask itself pre-authorised it ("once passed, start building").

**SSOTs this skill points at, never restates:** the autobuild contract (`~/.claude/skills/autobuild/SKILL.md` — lanes, gates, validator table, recovery) and its guardrails (`~/.claude/skills/autobuild/GUARDRAILS.md`). If your environment carries its own dispatch canon (model-tier pinning rules, agent definitions), read that directly too.

## Phase 0 — ground the ask (memory first, canon direct)

1. **Memory before files:** the ask is usually not new. If a memory/recall tool is available, query it for the prior spec/plan/decisions; the hit set names the scope SSOT. Otherwise search prior plans and decision docs directly. Harness/dispatch canon is read directly, never via recall.
2. **Read the scope SSOT yourself** (the prior spec, the decision registers it points at). Note its date — a spec older than the repo is a hypothesis about the repo, not a fact.
3. Read the tracker state in every repo the ask touches with one call per repo (`pinax status --json` on a [pinax](https://github.com/antikas/pinax) repo; else the backlog file) — work-state by one call, never file archaeology. Confirm the ask is genuinely unexecuted; find queued items the plan must sequence against.

## Phase 1 — ground-truth scouts (sonnet, parallel, Workflow)

Fan out one scout per surface, pinned sonnet (never haiku), structured output with per-answer file evidence. Standard surfaces:
- **the build repo(s):** the add/extend recipe as documented AND as last actually executed (the two differ — diff them); coupling tripwires (exhaustiveness tests, registries, typecheck witnesses); test lanes + the fleet validator row; deploy machinery + any repo-vs-server drift; pinned upstream data (schema versions) vs what the spec assumed.
- **every publication surface** the mandate's "end to end" implies: marketing site, docs, announcement channels — structure, what one new entry touches, its deploy posture.
- **the programme docs:** open decisions and named gates touching the ask; what has been executed since the spec was written.

Instruct scouts to report *surprises* — contradictions of the questions' premises are the highest-value output. The author reads the pivotal decision docs and freshest review-verdict precedent directly; scouts summarise, load-bearing sources get first-hand reads.

## Phase 2 — author the plan (judgment seat, main session)

One plan folder — `plans/<date>-<slug>/plan.md` wherever you keep plans (a knowledge repo, or the target repo's `docs/plans/`). Shape:

1. **Where we are** — plain-English header a non-engineer follows cold.
2. **Mandate** — the owner's ask, verbatim intent, and what gate authorises what (activation vs launch).
3. **Ground-truth deltas** — numbered, evidenced: what changed since the spec and what each change does to the plan. Point at the spec as scope SSOT; never duplicate it.
4. **Decision dispositions table** — every open decision the queue would otherwise stall on: precedent-determined + reversible → *proceed-and-log* with a named default, its precedent, and an explicit **override window**; genuinely open or irreversible → *owner-gated item registered blocked* (never omitted, never silently defaulted). Content needing a named human approver: DRAFT-and-tripwire where precedent exists, park otherwise — blocked, not shrunk.
5. **The queue** — one section per item: scope (spec-pointer + deltas), lane class (standard/hard/trivial per the autobuild ladders), dependencies, and acceptance a builder + blind auditor can verify with **no human in the loop** (anything needing web/human/production mid-cycle carries a stated park path). End-to-end means the queue always carries: build phases → go-live enablement (build-side) → **production deploy (owner-gated, registered blocked)** → publication-surface update → **publish (owner-gated)**. Deploys never ride unattended runs.
6. **Sequencing DAG** — including how the new queue coexists with the repo's standing queue.
7. **Autobuild harness** — invocation, pinned lanes (lean routing: sonnet default, opus for hard, the premium tier for judgment only), validator from the fleet config row, gate typing, observability set, recovery pointer.
8. **Constraints** and **measurable success criteria** — end-to-end ones (live URL, published surface, zero silent deferrals, every autonomous decision logged with its window), verified not asserted.

## Phase 3 — blind adversarial review (opus, refute-by-default)

Workflow: four parallel reviewers, **opus, high effort, fresh context**, given file paths only — the plan, its sources, the repos, the autobuild contract — never the author's narration. Lenses (adapt the premortem to the domain; keep four seats):
1. **Coverage + dependency** — whole mandate, nothing silently weakened, dependencies real and ordered.
2. **Autobuildability** — items dispatchable unattended per the autobuild contract; gates typed so the filter never picks owner-gated items; validator/lanes right; M/L items survivable by the loop.
3. **Architecture-fit + SSOT** — every repo-truth claim spot-checked at code level; no invented pattern where the repo has one; no SSOT violation introduced.
4. **Premortem (decisive seat)** — write the incident backwards: production damage, end-user harm, cross-repo breakage, "cheap" decisions that aren't, run-mechanics death.

Verdict schema per seat: `PASS | PASS_WITH_FOLDS | FAIL` + blockers (file-level evidence mandatory) + judgment-free folds + the claims each seat actually checked. **Adjudicate as the judgment seat:** verify every blocker at code level yourself before accepting it (reviewers can be plausibly wrong — verify both ways); apply accepted folds to the plan; on any FAIL, revise and re-run the review on the revision (fresh seats). Record every round in `review-verdict.md` in the plan folder: verdict, lens votes, folds applied, blockers' dispositions.

## Phase 4 — register and stop

Only after PASS / PASS_WITH_FOLDS-with-folds-applied:
1. Register the queue in each repo's tracker in dependency order (`pinax add` on a pinax repo; acceptance in the item text with the plan + spec as named brief SSOTs). Owner-gated items get their gate typed **at registration** (`pinax block <id> --gate decision`). Registration commits ride the default branch (register-before-branch invariant).
2. Hygiene: index the plan folder wherever plans are indexed; log the day's delta; update memory if a durable lesson surfaced.
3. **Report and stop.** The owner gets: the plain-English plan summary, the verdict, every decision taken with its override window, the surfaced gates, and the one command that launches the run. Do not launch it. Do not ask "shall I?" — state what's armed and hand over the trigger.

## Acceptance criteria

- The ask's prior materials were found via memory/search, not directory-thrash; the scope SSOT is pointed at, not duplicated.
- Every open decision is either defaulted-with-window-and-precedent or registered as a blocked gate — zero silent defaults, zero omissions.
- The queue covers build → live → published, with owner-gated steps registered-blocked rather than absent.
- A four-lens blind review ran on the artefact from disk; blockers were code-verified by the adjudicator; the verdict trail lives beside the plan.
- The tracker answers "what would the run do?" with one status call; the owner holds the launch trigger.
