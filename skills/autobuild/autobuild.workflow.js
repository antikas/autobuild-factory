export const meta = {
  name: 'autobuild',
  description: 'Fleet-wide aggressive backlog execution + scope-aware refill. Route -> build -> blind dual audit -> judgment-tier adjudicate -> accept+commit on a branch or park; refill propose-only when dry. Every cycle writes back to the tracker (Pinax .ergon event log, else BACKLOG.md).',
  phases: [
    { title: 'Select', detail: 'scout reads the tracker, returns the next ready item + complexity' },
    { title: 'Build', detail: 'routed builder implements + runs the validator, no commit' },
    { title: 'Audit', detail: 'two blind opus auditors; FAIL if either fails' },
    { title: 'Adjudicate', detail: 'Epistates (judgment tier): accept / fold / park, in its own context' },
    { title: 'Track', detail: 'commit on the branch + write status back to the tracker, or restore clean' },
    { title: 'Refill', detail: 'when dry: comprehend -> propose -> blind critic -> land propose-only -> conservative promote' },
  ],
}

// ---- config (SSOT for fleet params; brief §"Workflow args") ---------------
// Accept a JSON-encoded string too (a stringified args object must never silently
// fall through to defaults — that once misdirected a run at the wrong repo).
let cfg = args
if (typeof cfg === 'string') { try { cfg = JSON.parse(cfg) } catch (e) { throw new Error('autobuild: args arrived as a non-JSON string — pass the config object') } }
cfg = cfg || {}
if (!cfg.repo) throw new Error('autobuild: cfg.repo is REQUIRED — never default the target repo')
if (!cfg.nextCmd) throw new Error('autobuild: cfg.nextCmd is REQUIRED — say how to read this repo\'s tracker')
const REPO = cfg.repo
const BRANCH = cfg.branch || 'autobuild'
// Isolated run worktree (single-writer discipline): the orchestrator runs
// `git -C <repo> worktree add <workdir> <branch>` BEFORE dispatch; every agent
// operates ONLY there, so a concurrent session in the repo's main tree can
// never clobber an uncommitted cycle (a concurrent session's checkout+reset
// once destroyed an uncommitted build in a shared tree — only the builder's
// improvised worktree saved the work).
const WORKDIR = cfg.workdir || REPO
// Optional pre-parsed ready set: the orchestrator parses the tracker itself
// and hands the scout a closed list — mechanical selection over a small list
// beats a model free-reading a 500KB+ tracker file. `let`, not `const`: a
// refill that promotes items invalidates the pre-parsed set (a stale closed
// universe once blocked the scout from freshly-promoted items), so the
// loop clears it and the scout re-reads the
// live tracker from then on.
let READY_SET = cfg.readySet
  ? (Array.isArray(cfg.readySet) ? cfg.readySet.map((it) => JSON.stringify(it, null, 1)).join('\n') : cfg.readySet)
  : ''
const NEXT_CMD = cfg.nextCmd                          // pinax next | parse BACKLOG.md | board instruction
const VALIDATOR = cfg.validatorCmd || ''              // e.g. npm test / pytest / cargo test
const MAX_FOLDS = Number.isInteger(cfg.maxFolds) ? cfg.maxFolds : 2
const MAX_ITEMS = Number.isInteger(cfg.maxItems) ? cfg.maxItems : 20
const PROPOSE_WHEN_DRY = cfg.proposeWhenDry !== false
const REFILL_THRESHOLD = Number.isInteger(cfg.refillThreshold) ? cfg.refillThreshold : 2
const PROMOTE_POLICY = cfg.promotePolicy || 'conservative'   // 'conservative' | 'off'
const RESERVE = Number.isInteger(cfg.budgetReserve) ? cfg.budgetReserve : 50000
// Recovery (SKILL.md §Recovery): item ids whose in-flight tree state a park must
// NEVER revert — a resumed cycle's uncommitted build is evidence for owner review,
// not disposable scaffolding. The orchestrator snapshots it too, but the tree copy
// must survive independently.
const PROTECTED = Array.isArray(cfg.protectedItems) ? cfg.protectedItems : []
// Optional environment hooks (both safe to omit):
//   cfg.recallTool — MCP tool name of a memory/recall tool used to enrich briefs with known traps.
//   cfg.fogLedger  — path of an out-of-repo fog ledger for direction-not-question refill candidates;
//                    when unset, fog lands in docs/FOG.md inside the repo.

// Tracker abstraction — how we READ the next item and WRITE status back.
// kind: 'pinax' (append to .ergon event log via pinax) | 'backlog' (edit BACKLOG.md).
// Pinax (https://github.com/antikas/pinax-tracker) reads with `pinax next` and writes
// events back to the .ergon log. A repo not on Pinax uses BACKLOG.md.
const tracker = {
  kind: cfg.trackerKind || (String(NEXT_CMD).includes('pinax') ? 'pinax' : 'backlog'),
  nextCmd: NEXT_CMD,
  doneCmd: cfg.doneCmd || '',   // e.g. `pinax done <id>` ; blank => the close agent infers it
  parkCmd: cfg.parkCmd || '',   // e.g. `pinax block <id> --reason "..."`
  // Optional explicit write-back instructions (override the kind defaults) — e.g. for a
  // durable-board repo: "set status: done + append Run history in board/<id>.md".
  writeBackDone: cfg.writeBackDone || '',
  writeBackPark: cfg.writeBackPark || '',
}

const MODEL = Object.assign(
  // proskopos + mechanical are sonnet, not haiku: scouting = multi-step tool use + the
  // eligibility/routing judgment, and the close/park agents do multi-step git hygiene —
  // haiku demonstrably fumbled both classes in live smoke runs. haiku keeps only
  // genuinely trivial BUILD items via buildTrivial.
  { coordinator: 'fable', proskopos: 'sonnet', buildHard: 'fable',
    buildStandard: 'sonnet', buildTrivial: 'haiku', audit: 'opus',
    mechanical: 'sonnet' },
  cfg.modelPolicy || {},
)

const buildTierFor = (complexity) =>
  complexity === 'hard' ? MODEL.buildHard
  : complexity === 'trivial' ? MODEL.buildTrivial
  : MODEL.buildStandard

// ---- codex lane — subscription-quota build lane, ChatGPT auth, NO API key ----
// (codex exit code != build outcome — see the driver's step 7; flags verified live against the codex CLI.)
const CODEX_BIN = cfg.codexBin || 'codex'   // drivers resolve via `command -v codex` first; set cfg.codexBin to an absolute path if codex is not on PATH
// Usage/rate-limit signature (regex, case-insensitive). Exact string self-documents on first real hit;
// the driver logs the raw matched text so the router can tighten this later. Overridable per repo.
const CODEX_LIMIT_RE = cfg.codexLimitRe || 'usage limit|rate limit|quota|too many requests|429|reached your .*limit|resets? (at|in)'

// ---- model registry + provider inference ----------------------------------
// A build seat is filled by a MODEL; the model's PROVIDER determines HOW it runs — a Claude model via
// agent(), a codex/OpenAI model via `codex exec`. Model TIERING is decoupled from PROVIDER: any build seat
// can be filled by any model and the router infers the execution mechanism. Extend the codex set to
// teach the router new OpenAI models (the /^(gpt-|o<n>|codex)/ pattern also catches most by name).
const CODEX_MODELS = new Set(cfg.codexModels || ['gpt-5.5', 'gpt-5.6', 'gpt-5.4', 'gpt-5.4-mini'])
const providerOf = (model) => (CODEX_MODELS.has(model) || /^(gpt-|o[0-9]|codex)/i.test(model)) ? 'codex' : 'claude'
// Build ladders are ordered lists of MODEL IDS (provider inferred), e.g. { buildStandard: ['gpt-5.5','sonnet'] }.
// DEFAULT-OFF: with no cfg.lanePolicy the router uses the legacy buildTierFor path — single-lane behaviour EXACTLY
// (the rollback guarantee). When set, the premium tier is off the build seats; JUDGMENT uses the ladders below.
const LANE_POLICY = cfg.lanePolicy || null
const classKey = (complexity) => complexity === 'hard' ? 'buildHard' : complexity === 'trivial' ? 'buildTrivial' : 'buildStandard'
// JUDGMENT CAN LEAVE CLAUDE: the invariant is FRESH CONTEXT + a
// CAPABLE TIER, not the provider — gpt-5.5 may take the audit/adjudicator seat, run FRESH via codexJudge().
// Policy: Claude is SAVED for coordination and for when it is absolutely needed; OpenAI is used aggressively
// for builds AND judgment to conserve the Claude window. HARD RULE: opus is cooled at >=88% weekly (preflight),
// so judgment falls to gpt-5.5 above the ceiling. Ladders (LANE_POLICY runs only), opus-first, gpt-5.5 fallback:
const JUDGE_LADDER = Object.assign({ audit: ['opus', 'gpt-5.5'], coordinator: ['opus', 'gpt-5.5'] }, cfg.judgmentPolicy || {})
// Right-tier guard: a non-capable tier must NEVER judge; scout/mechanical have no codex path (keep them Claude).
const NON_JUDGE_TIER = /mini|haiku|nano/i
for (const seat of ['coordinator', 'audit']) {
  for (const m of (LANE_POLICY ? (JUDGE_LADDER[seat] || [MODEL[seat]]) : [MODEL[seat]])) {
    if (NON_JUDGE_TIER.test(String(m))) throw new Error(`autobuild: ${seat} ladder entry '${m}' is a non-capable tier — judgment needs a capable tier (opus / gpt-5.5). Fresh context + right tier is the invariant.`)
  }
}
for (const seat of ['proskopos', 'mechanical']) {
  if (providerOf(MODEL[seat]) === 'codex') throw new Error(`autobuild: ${seat}='${MODEL[seat]}' has no codex path — keep the scout/mechanical seats on a Claude tier (sonnet).`)
}
let codexEnabled = cfg.disableCodexLane ? false : true   // set by preflight: off if codex isn't authed
let laneMem = {}          // in-memory mirror of ~/.autobuild/lanes.json — cooldown keyed by MODEL id
let stoppedReason = null  // set to 'all-lanes-cooling' when the run stops clean per §6

const GUARDRAILS =
  `Guardrails SSOT: ~/.claude/skills/autobuild/GUARDRAILS.md (scope/decision/destructive/observability gates, commit cadence, verification economy) — read it and comply. ` +
  `Repo: ${REPO}. RUN WORKDIR: ${WORKDIR} — every file read/write and every git command happens INSIDE this directory (use git -C "${WORKDIR}" ...). ` +
  `Touching any path or repo outside it is a guardrail breach: STOP and report it, do not proceed. ` +
  `Branch: ${BRANCH}, already checked out at the workdir (NEVER switch branches, NEVER main, NEVER push, NEVER merge). ` +
  `Tracker: ${tracker.kind}. Read the next item with: ${tracker.nextCmd}.`

// ---- schemas --------------------------------------------------------------
const SELECT_SCHEMA = {
  type: 'object', additionalProperties: false,
  properties: {
    hasItem: { type: 'boolean' },
    queueDepth: { type: 'integer', description: 'count of ready/eligible items remaining' },
    id: { type: 'string' },
    title: { type: 'string' },
    brief: { type: 'string', description: 'the full item spec, stated up front — enough for a fresh builder' },
    complexity: { type: 'string', enum: ['hard', 'standard', 'trivial'] },
    reversible: { type: 'boolean' },
    filterClass: { type: 'string', enum: ['eligible', 'irreversible', 'novel-architecture', 'blocked'] },
    precedent: { type: 'string', description: 'the ADR/entity/pattern that determines the call, or "none"' },
    reason: { type: 'string', description: 'why this item (or why the queue is dry)' },
  },
  required: ['hasItem', 'queueDepth', 'reason'],
}
const BUILD_SCHEMA = {
  type: 'object', additionalProperties: false,
  properties: {
    status: { type: 'string', enum: ['built', 'failed', 'refused', 'novel'] },
    filesChanged: { type: 'array', items: { type: 'string' } },
    validatorPassed: { type: 'boolean' },
    validatorOutput: { type: 'string' },
    summary: { type: 'string' },
    notes: { type: 'string' },
    // Lane-health signal. The codex lane driver sets it; Claude builders leave it 'ok'.
    // 'ratelimited' => the router COOLS the lane and reroutes the item; it is NOT a build failure.
    // 'harness-timeout' => the driver hit ITS OWN turn/token limit and killed a codex process that was
    // STILL ALIVE (two hard-lane builds were once lost when a driver force-reported over a
    // live codex). It is NOT a work failure — the loop PARKS it WITHOUT reverting the tree (the partial
    // build is evidence, never disposable). Distinct from 'ratelimited' (which cools the lane).
    laneStatus: { type: 'string', enum: ['ok', 'ratelimited', 'harness-timeout'] },
    limitSignature: { type: 'string', description: 'raw usage/rate-limit text when laneStatus=ratelimited (self-documents the exact signature on first hit)' },
  },
  required: ['status', 'filesChanged', 'validatorPassed', 'validatorOutput', 'summary'],
}
// What codex fills via `--output-schema` (codex honours a strict JSON schema for its final message).
// The lane driver reads this from codex's --output-last-message file, then adds laneStatus/limitSignature.
const CODEX_BUILD_SCHEMA = {
  type: 'object', additionalProperties: false,
  properties: {
    status: { type: 'string', enum: ['built', 'failed'] },
    filesChanged: { type: 'array', items: { type: 'string' } },
    validatorPassed: { type: 'boolean' },
    validatorOutput: { type: 'string' },
    summary: { type: 'string' },
  },
  required: ['status', 'filesChanged', 'validatorPassed', 'validatorOutput', 'summary'],
}
const AUDIT_SCHEMA = {
  type: 'object', additionalProperties: false,
  properties: {
    verdict: { type: 'string', enum: ['PASS', 'PASS_WITH_MINORS', 'FAIL'] },
    defects: { type: 'array', items: { type: 'string' } },
    mustFixBeforeShip: { type: 'array', items: { type: 'string' } },
    reasoning: { type: 'string' },
  },
  required: ['verdict', 'defects', 'reasoning'],
}
const ADJ_SCHEMA = {
  type: 'object', additionalProperties: false,
  properties: {
    decision: { type: 'string', enum: ['accept', 'fold', 'park'] },
    reason: { type: 'string' },
    commitMessage: { type: 'string', description: 'delta-notation commit message, if accept' },
    foldInstructions: { type: 'string', description: 'what the fold must fix, if fold' },
    parkReason: { type: 'string', description: 'the decision the item needs, if park' },
  },
  required: ['decision', 'reason'],
}
const TRACK_SCHEMA = {
  type: 'object', additionalProperties: false,
  properties: { ok: { type: 'boolean' }, action: { type: 'string' }, commit: { type: 'string' }, merge: { type: 'string', description: 'merged-to-<default>@<sha> | deferred-root-tree-busy | n/a' }, notes: { type: 'string' } },
  required: ['ok', 'action'],
}
const PROPOSE_SCHEMA = {
  type: 'object', additionalProperties: false,
  properties: {
    scopeModel: { type: 'string', description: 'what the project IS: thesis, goals, current state, gaps' },
    candidates: {
      type: 'array',
      items: {
        type: 'object', additionalProperties: false,
        properties: {
          title: { type: 'string' }, rationale: { type: 'string' }, value: { type: 'string' },
          reversibility: { type: 'string', enum: ['reversible', 'irreversible'] },
          complexity: { type: 'string', enum: ['small', 'medium', 'large'] },
          inScope: { type: 'boolean' }, precedent: { type: 'string' },
          filterClass: { type: 'string', enum: ['eligible', 'irreversible', 'novel-architecture', 'blocked'] },
        },
        required: ['title', 'rationale', 'value', 'reversibility', 'complexity', 'inScope', 'filterClass'],
      },
    },
  },
  required: ['scopeModel', 'candidates'],
}
const CRITIC_SCHEMA = {
  type: 'object', additionalProperties: false,
  properties: {
    kept: {
      type: 'array',
      items: {
        type: 'object', additionalProperties: false,
        properties: {
          title: { type: 'string' }, score: { type: 'integer' }, onThesis: { type: 'boolean' },
          reversibility: { type: 'string' }, complexity: { type: 'string' }, promotable: { type: 'boolean' },
        },
        required: ['title', 'score', 'onThesis', 'promotable'],
      },
    },
    dropped: { type: 'array', items: { type: 'string' }, description: 'title + why (off-thesis/redundant/low-value)' },
    fog: { type: 'array', items: { type: 'string' }, description: 'direction-not-question candidates: on-thesis but the QUESTION is not precisely stateable yet — title + the open question blocking it. Routed to the vault fog ledger, never the queue.' },
  },
  required: ['kept', 'dropped', 'fog'],
}
const TRAPS_SCHEMA = {
  type: 'object', additionalProperties: false,
  properties: { traps: { type: 'string', description: 'terse bullet list of the top <=3 real known traps + their fix/avoidance, or "" if none' } },
  required: ['traps'],
}

// ---- helpers --------------------------------------------------------------
// Brief enrichment: surface KNOWN TRAPS (via a memory/recall tool if one is configured, plus the
// repo's config-row caveats) into the brief BEFORE routing, so the builder — INCLUDING the codex
// lane, which cannot call recall — never re-solves a solved problem. Bounded (one low-effort agent,
// <=3 hits) and NON-FATAL: if no memory tool is available the build proceeds unenriched, so a
// memory-down run never stalls.
async function enrichBrief(item) {
  const res = await agent(
    `You are a cheap pre-dispatch scout for ${REPO}. Your ONLY job: surface KNOWN TRAPS so the builder does not re-solve a solved problem — you are NOT building.\n` +
    (cfg.recallTool
      ? `Call the memory/recall tool "${cfg.recallTool}" (load it first via ToolSearch if it is not already available) with a query like: "${REPO} ${item.title} known errors traps build gotchas".\n\n`
      : `If a memory/recall MCP tool is available in this environment, query it for known traps around "${REPO} ${item.title}"; if none is available, rely ONLY on the config caveats below (if any).\n\n`) +
    `Item ${item.id} — ${item.title}\nBrief:\n${item.brief}\n\n` +
    (cfg.knownTraps ? `The repo's autobuild config-row caveats — include these VERBATIM, they are load-bearing:\n${cfg.knownTraps}\n\n` : '') +
    `Return the TOP <=3 traps relevant to THIS item as a terse bullet list (each: the trap + its fix/avoidance). Only REAL recalled or config-row traps — never invent one. If recall surfaces nothing relevant and there are no config caveats, return traps="" (empty). Under ~200 words.`,
    { label: `enrich:${item.id}`, phase: 'Build', schema: TRAPS_SCHEMA, model: MODEL.proskopos, effort: 'low' },
  )
  return (res && res.traps && res.traps.trim()) || ''
}

async function auditOnce(item, build, n) {
  const prompt =
    `You are Kritos, a blind independent auditor (pass ${n}). You did NOT build this and must not trust it.\n\n` +
    `${GUARDRAILS}\n\nItem ${item.id} — ${item.title}\nSpec:\n${item.brief}\n\n` +
    `The builder reports: ${JSON.stringify({ summary: build.summary, filesChanged: build.filesChanged, validatorPassed: build.validatorPassed, notes: build.notes })}\n` +
    `Builder validator evidence (commands, exit codes, output tails):\n${build.validatorOutput || '(none provided)'}\n\n` +
    `Your lens for this pass: ${n === 1
      ? 'CORRECTNESS — spec-completeness, real behaviour of the changed code, silent data loss.'
      : 'SAFETY — guardrail breaches, scope creep, files touched beyond the spec, IP/clean-room and policy violations.'}\n\n` +
    `Verify by READING, not re-executing (verification economy — GUARDRAILS.md; the builder's validator already ran once). Read the actual changed files against the spec, and check the validator evidence above for consistency: commands match the declared validator (${VALIDATOR || 'the project validator'}), exit codes are real, output is plausible for this diff. ` +
    `You may re-derive AT MOST ONE load-bearing claim with ONE cheap targeted command (a single test file, one CLI invocation), run ONCE — and only if reading leaves genuine doubt within your lens. NEVER re-run the full validator, a full suite, or any e2e/recording pipeline; never repeat a command to confirm its result. ` +
    `Missing, thin, or inconsistent validator evidence is itself a defect: return FAIL citing it — do not compensate by re-running the builder's work. ` +
    `Over-verification is ALSO a defect to cite (verification economy): if the builder's evidence shows it ran a suite BEYOND the declared scoped validator (${VALIDATOR || 'the project validator'}), re-ran an already-passed command, or invoked integration/e2e/recording suites the item's acceptance did NOT name, cite it as a DEFECT with the offending command — the scoped validator plus the item's named determinism/isolation tests are the whole gate; the full gauntlet is waste that stalls runs and falsely fails good work. ` +
    `Return FAIL on any real defect; PASS_WITH_MINORS only for cosmetic/non-blocking issues; PASS if clean. Be strict — a false PASS is worse than a false FAIL. ` +
    `Frame check (documents/prose deliverables): SELF-REFERENTIAL figures (a document counting its own rows, verdict tallies, self-coverage claims) are a defect of EXISTENCE — flag them for removal, never verify-and-correct them; state belongs in generated views, not prose.`
  return runJudge('audit', prompt, AUDIT_SCHEMA, `audit${n}:${item.id}`, 'Audit', 'high')
}
async function auditItem(item, build) {
  const votes = (await parallel([() => auditOnce(item, build, 1), () => auditOnce(item, build, 2)])).filter(Boolean)
  const failed = votes.some((v) => v.verdict === 'FAIL') || votes.length < 2
  const defects = votes.flatMap((v) => v.defects || [])
  const mustFix = votes.flatMap((v) => v.mustFixBeforeShip || [])
  return { failed, votes, defects, mustFix }
}

async function buildItem(item, tier, foldInstructions) {
  const isFold = !!foldInstructions
  const prompt =
    `You are Chiron, a fresh builder with no memory of prior items.\n\n${GUARDRAILS}\n\n` +
    `Item ${item.id} — ${item.title}\nFull spec (stated up front — implement all of it in one well-specified pass):\n${item.brief}\n` +
    (item.precedent && item.precedent !== 'none' ? `\nPrecedent that determines any borderline call (apply it, it is reversible): ${item.precedent}\n` : '') +
    (isFold ? `\nThis is a FOLD. A prior audit found: ${foldInstructions}. Fix exactly those, do not re-architect. If a file the fold references is missing from the tree (e.g. a prior restore cleaned it), regenerate it per the item spec first, then apply the fixes.\n` : '') +
    `\nThrash floor (verification economy): on ANY error, FIRST check the error signature against the "Known traps" in this brief, and — if a memory/recall tool is available — make ONE recall call for the signature before debugging blind — a trap already solved in canon must not be re-solved. If the SAME error survives TWO materially different fix attempts, STOP and return status "failed" with the full evidence trail (both attempts + their outputs); NEVER iterate a third time — a clean failed-with-evidence is the correct outcome, thrashing is waste. ` +
    `\nImplement inside the run workdir (${WORKDIR}) on ${BRANCH}. Then RUN the validator: ${VALIDATOR || '(the project validator)'} — in the FOREGROUND, reading the exit code (never a background run polled for a done-marker). ` +
    `Run ONLY the item's declared scoped validator (plus any determinism/isolation tests the item's acceptance names) — NEVER the full suite, and NEVER integration/e2e/recording suites the item did not name (over-verification is waste and an audit-citable defect). ` +
    `Return the evidence in validatorOutput: for EACH command run, the exact command line, its exit code, and the last ~30 lines of its real output. The auditors READ this instead of re-running your work — thin or missing evidence fails the audit. ` +
    `Do NOT commit and do NOT push — leave the changes in the workdir tree for the auditors. ` +
    `If the item cannot be done without touching files outside the workdir, do NOT touch them — return status "failed" with the reason in notes. ` +
    `State goal-and-constraints thinking, not micro-steps. Return the structured result (list every changed path).`
  return agent(prompt, {
    label: `${isFold ? 'fold' : 'build'}:${item.id}`, phase: 'Build',
    schema: BUILD_SCHEMA, model: tier, effort: 'high', isolation: undefined,
  })
}

// The CODEX LANE DRIVER. A sonnet WRAPPER agent (it does NOT build; codex builds) that drives the
// codex CLI to build one item in the run worktree on the owner's ChatGPT subscription (no API key), then
// translates codex's result into the SAME BUILD_SCHEMA a Claude builder returns — so audit -> adjudicate ->
// close downstream are lane-blind. Trust chain intact: codex's self-report is UNtrusted; the blind opus audit
// reads the artefacts independently. (Verified live: codex exit code != build outcome — see step 7.)
async function codexBuild(item, codexModel, foldInstructions) {
  const isFold = !!foldInstructions
  const effort = /mini/.test(codexModel) ? 'low' : 'medium'   // burn control (config default xhigh is too costly)
  const briefText =
    `You are building ONE backlog item in this repository. Implement the FULL spec, then verify.\n\n` +
    `Item ${item.id} — ${item.title}\n\nSpec (implement all of it in one pass):\n${item.brief}\n` +
    (item.precedent && item.precedent !== 'none' ? `\nPrecedent that determines any borderline call (apply it, it is reversible): ${item.precedent}\n` : '') +
    (isFold ? `\nThis is a FOLD of a prior attempt. A blind audit found: ${foldInstructions}. Fix EXACTLY those; do not re-architect. If a referenced file is missing, regenerate it per the spec first.\n` : '') +
    `\nAfter implementing, RUN the validator in the FOREGROUND and read its exit code: ${VALIDATOR || '(the project validator)'}. ` +
    `Run ONLY that scoped validator (plus any determinism/isolation tests the item's acceptance names) — NEVER the full suite, and NEVER integration/e2e/recording suites the item did not name (over-verification is waste and a defect). ` +
    `Thrash floor: if the SAME error survives TWO materially different fix attempts, STOP and report status "failed" with the evidence — never a third attempt. ` +
    `Do NOT commit and do NOT push — leave the changes in the working tree for the auditors. ` +
    `Report the structured result: status, EVERY changed path, validatorPassed, validatorOutput (for each command: the command line, its exit code, the last ~30 lines of real output), and a one-line summary.`
  return agent(
    `You are the CODEX LANE DRIVER — a mechanical wrapper. You do NOT build; codex builds. Drive the codex CLI, then read+translate its result. POSIX bash, foreground, read exit codes.\n${GUARDRAILS}\n\n` +
    `EXCEPTION to the workdir-only rule, for THIS mechanical driver ONLY: you MAY create and use ONE system temp dir via mktemp -d for codex CLI plumbing (the brief, schema, and codex output files) — those are not repo content and live OUTSIDE the worktree by design (keeping them out of the worktree's git status). codex's OWN repo writes still go ONLY to the worktree via --cd "${WORKDIR}". No other outside-workdir access.\n\n` +
    `Item ${item.id} — ${item.title}. Codex model: ${codexModel} (reasoning ${effort}).\n\n` +
    `STEPS:\n` +
    `1. Throwaway I/O dir for codex CLI plumbing ONLY (the one permitted write outside the worktree — plumbing, never repo content): T=$(mktemp -d).\n` +
    `2. Resolve the codex binary: CODEX="$(command -v codex || echo '${CODEX_BIN}')".\n` +
    `3. Write the schema codex must fill to "$T/schema.json": ${JSON.stringify(CODEX_BUILD_SCHEMA)}\n` +
    `4. Write the brief (everything between the BRIEF markers below, verbatim) to "$T/brief.md". LAUNCH codex in the BACKGROUND so you hold its pid (brief piped via STDIN avoids arg-length limits; codex reads the prompt from stdin when none is given), and record the pid to a file so it survives a lost shell:\n` +
    `   "$CODEX" exec --cd "${WORKDIR}" --dangerously-bypass-approvals-and-sandbox -m ${codexModel} -c model_reasoning_effort="${effort}" --output-schema "$T/schema.json" --output-last-message "$T/last.json" --json --color never < "$T/brief.md" > "$T/events.jsonl" 2> "$T/stderr.txt" &\n` +
    `   CODEX_PID=$!; echo "$CODEX_PID" > "$T/codex.pid"\n` +
    `5. WAIT FOR CODEX TO EXIT — poll on the PROCESS ITSELF, with NO fixed iteration or wall-clock cap (a real codex build legitimately runs many minutes; a fixed cap once lost two near-complete builds). Run this as ONE foreground command; it blocks until codex exits or a usage limit is seen:\n` +
    `   while kill -0 "$CODEX_PID" 2>/dev/null; do if grep -iqE '${CODEX_LIMIT_RE}' "$T/stderr.txt" "$T/events.jsonl" 2>/dev/null; then kill "$CODEX_PID" 2>/dev/null; break; fi; sleep 20; done; wait "$CODEX_PID" 2>/dev/null; CODEX_RC=$?\n` +
    `   HARD RULE — NEVER report a result while the codex process is still alive. A forced/early report DISCARDS a possibly near-complete build (a 34-file build was once reverted and a live build abandoned this way). You leave the wait on EXACTLY ONE of: (i) codex exited on its own, (ii) the limit signature matched (handled at step 6), or (iii) YOU are approaching your OWN turn/token limits — and in case (iii) your LAST action MUST be to kill codex cleanly, then report a HARNESS TIMEOUT (never a bare forced report): kill "$(cat "$T/codex.pid" 2>/dev/null)" 2>/dev/null; sleep 2; kill -9 "$(cat "$T/codex.pid" 2>/dev/null)" 2>/dev/null; and if the pid file is lost, find it: ps aux | grep 'codex exec' | grep "${WORKDIR}" and kill that pid. Then return {status:"failed", laneStatus:"harness-timeout", filesChanged:<git -C "${WORKDIR}" status --porcelain paths, so the partial tree is on record>, validatorPassed:false, validatorOutput:"", summary:"codex killed while alive — driver hit its own harness/turn limit", notes:"pid=<pid>; events.jsonl tail (last ~30 lines): <paste>"}. A harness-timeout is NOT a build failure: the loop PARKS it WITHOUT reverting the tree.\n` +
    `6. LIMIT CHECK: grep -iE '${CODEX_LIMIT_RE}' "$T/stderr.txt" "$T/events.jsonl". If it matches, clean up (rm -rf "$T") and return {status:"failed", laneStatus:"ratelimited", limitSignature:<the raw matched line VERBATIM>, filesChanged:[], validatorPassed:false, validatorOutput:"", summary:"codex usage limit hit", notes:<exit code>} and STOP — the router cools the lane on this.\n` +
    `7. EXIT CODE != OUTCOME (verified): codex exits 0 even when the BUILD failed. Only now that codex has EXITED, read "$T/last.json" (codex's structured {status,...}); take build success from its "status" field, NEVER the exit code. If "$T/last.json" is missing/unparseable AND there was no limit signature (codex exited on its OWN, you did not kill it), return status:"failed", laneStatus:"ok" with the stderr tail in notes. If codex was still alive you must NOT be here — see step 5.\n` +
    `8. Reconcile changed paths from the tree, not codex's self-list: git -C "${WORKDIR}" status --porcelain. Use the real changed paths for filesChanged.\n` +
    `9. Clean up: rm -rf "$T". Return the BUILD_SCHEMA object: {status (built|failed), filesChanged, validatorPassed, validatorOutput (codex's evidence VERBATIM — do NOT re-run the validator yourself, codex already ran it; you only READ and translate), summary, notes, laneStatus:"ok"}.\n\n` +
    `===BRIEF START===\n${briefText}\n===BRIEF END===`,
    { label: `${isFold ? 'codexfold' : 'codex'}:${item.id}`, phase: 'Build', schema: BUILD_SCHEMA, model: MODEL.mechanical, effort: 'medium' },
  )
}

// JUDGMENT via codex — FRESH context (judgment can leave Claude: the invariant is fresh context + a capable
// tier, not the provider). A sonnet wrapper drives codex READ-ONLY over the worktree artefacts and returns the
// verdict in the SAME schema a Claude judge returns. codex MUST NOT modify the tree; the wrapper snapshots the
// worktree before/after and FAILs the verdict if codex wrote anything. laneStatus/limitSignature are added so
// the router cools the lane on a usage-limit, exactly like codexBuild.
const withLane = (schema) => ({ type: 'object', additionalProperties: false,
  properties: { ...schema.properties, laneStatus: { type: 'string' }, limitSignature: { type: 'string' } },
  required: schema.required || [] })
async function codexJudge(codexModel, judgePrompt, laneSchema, label, phase) {
  return agent(
    `You are the CODEX JUDGE DRIVER — a mechanical wrapper. You do NOT judge; codex judges with fresh context. Drive the codex CLI READ-ONLY, then read+translate its verdict. POSIX bash, foreground, read exit codes.\n${GUARDRAILS}\n\n` +
    `EXCEPTION to workdir-only, for THIS mechanical driver ONLY: ONE mktemp -d for codex plumbing (brief/schema/output), outside the worktree by design. codex reads the worktree via --cd "${WORKDIR}" but MUST NOT modify it.\n\n` +
    `Judge model: ${codexModel}. Label: ${label}.\n\nSTEPS:\n` +
    `1. Snapshot the worktree: BEFORE=$(git -C "${WORKDIR}" status --porcelain | sha256sum).\n` +
    `2. T=$(mktemp -d). CODEX="$(command -v codex || echo '${CODEX_BIN}')".\n` +
    `3. Write the verdict schema to "$T/schema.json": ${JSON.stringify(laneSchema)}\n` +
    `4. Write the judge brief (everything between the markers below, VERBATIM) to "$T/brief.md". Run ONE invocation, brief piped via STDIN:\n` +
    `   "$CODEX" exec --cd "${WORKDIR}" --dangerously-bypass-approvals-and-sandbox -m ${codexModel} -c model_reasoning_effort="high" --output-schema "$T/schema.json" --output-last-message "$T/last.json" --json --color never < "$T/brief.md" > "$T/events.jsonl" 2> "$T/stderr.txt"\n` +
    `   Record the exit code.\n` +
    `5. LIMIT CHECK FIRST: grep -iE '${CODEX_LIMIT_RE}' "$T/stderr.txt" "$T/events.jsonl". If it matches, rm -rf "$T" and return {laneStatus:"ratelimited", limitSignature:<the raw matched line VERBATIM>} and STOP — the router cools the lane on this.\n` +
    `6. READ-ONLY CHECK: AFTER=$(git -C "${WORKDIR}" status --porcelain | sha256sum). If BEFORE != AFTER, codex MODIFIED the tree during a read-only judgment — rm -rf "$T" and return codex's verdict object but force its verdict/decision field to the FAIL/reject value and set defects to ["codex judge violated read-only: worktree changed during audit"], laneStatus:"ok". Never trust a PASS when the tree changed.\n` +
    `7. EXIT != OUTCOME: read "$T/last.json" (codex's structured verdict — it already matches the schema). If missing/unparseable and there was no limit signature, return a verdict object with its FAIL/reject value and defects ["codex judge produced no parseable verdict", <stderr tail>], laneStatus:"ok".\n` +
    `8. rm -rf "$T". Otherwise return codex's verdict object VERBATIM with laneStatus:"ok".\n\n` +
    `===JUDGE BRIEF START===\n${judgePrompt}\n===JUDGE BRIEF END===`,
    { label, phase, schema: laneSchema, model: MODEL.mechanical, effort: 'medium' },
  )
}

// ---- lane state, router, preflight ----------------------------------------
// The script cannot touch the filesystem or read the clock, so a cheap mechanical agent does both.
// State lives at ~/.autobuild/lanes.json so CONCURRENT runs on other repos share the cooling signal.
// Everything here is NON-FATAL: on any error lanes default to available — the balancer never stalls a run.
const LANE_STATE_SCHEMA = {
  type: 'object', additionalProperties: false,
  properties: {
    ok: { type: 'boolean' },
    lanes: { type: 'string', description: 'the resulting lanes.json content as a compact JSON string' },
    now: { type: 'integer', description: 'epoch seconds at read' },
    notes: { type: 'string' },
  },
  required: ['ok'],
}
async function laneStateAgent(instruction) {
  return agent(
    `You are a mechanical lane-state agent for autobuild's load balancer — no build, no judgment. State file: ~/.autobuild/lanes.json (mkdir -p ~/.autobuild; create the file as {} if absent). POSIX bash, foreground, read exit codes. NON-FATAL: on ANY error return {ok:false, notes:<the error>} and leave the file unchanged.\n\n` +
    `Current epoch seconds: run \`date +%s\`.\n\nTASK:\n${instruction}\n\n` +
    `Always return {ok, lanes:<the resulting lanes.json as a compact JSON string>, now:<epoch>, notes}.`,
    { label: 'lane-state', phase: 'Build', schema: LANE_STATE_SCHEMA, model: MODEL.mechanical, effort: 'low' },
  )
}
async function readLaneState() {
  const r = await laneStateAgent(
    `READ lanes.json. For any lane with cooling=true AND cooldownUntil <= now, clear it (cooling=false) — its cooldown expired — and write the file back. Return the resulting state.`,
  )
  if (r && r.ok && r.lanes) { try { laneMem = JSON.parse(r.lanes) || {} } catch (e) { /* keep prior memory */ } }
  return laneMem
}
async function laneAvailable(laneName) {
  const st = laneMem[laneName]
  if (!st || !st.cooling) return true
  const fresh = await readLaneState()   // cooling in memory — re-read so the agent applies clock-based expiry
  const s2 = fresh[laneName]
  return !s2 || !s2.cooling
}
async function coolLane(laneName, signature) {
  const sig = String(signature || 'ratelimited').slice(0, 300)
  await laneStateAgent(
    `COOL the lane "${laneName}". Read lanes.json; let R = (existing lanes["${laneName}"].repeats || 0). ` +
    `Set lanes["${laneName}"] = {cooling:true, repeats:R+1, lastError:${JSON.stringify(sig)}, cooldownUntil: now + Math.min(28800, 3600 * (2 ** R))}. ` +
    `EXCEPTION: if lastError contains an explicit reset time you can parse to epoch seconds, use THAT as cooldownUntil instead. Write the file back.`,
  )
  laneMem[laneName] = { cooling: true, repeats: ((laneMem[laneName] || {}).repeats || 0) + 1, lastError: sig }
}
// Run one build attempt on a specific MODEL, routing by provider: Claude via agent(), OpenAI via `codex exec`.
async function runBuilder(model, item, foldInstructions) {
  return providerOf(model) === 'codex'
    ? codexBuild(item, model, foldInstructions)
    : buildItem(item, model, foldInstructions)
}
// The ROUTER (deterministic — no model judgment). Walks the item-class MODEL ladder; first available model
// wins; a codex ratelimit cools THAT model and falls through; all models cooling => stop clean. Returns
// {build, laneName:<model id>} or {allLanesCooling:true}. With no LANE_POLICY it is the exact legacy path.
async function dispatchBuild(item, complexity, foldInstructions) {
  if (!LANE_POLICY) {
    const tier = buildTierFor(complexity)
    let build = await buildItem(item, tier, foldInstructions)
    if (!build || build.status === 'refused') { log(`Builder refused/failed on ${item.id} — falling back to opus.`); build = await buildItem(item, 'opus', foldInstructions) }
    return { build, laneName: tier }
  }
  const ladder = LANE_POLICY[classKey(complexity)] || ['opus']
  for (const model of ladder) {
    if (providerOf(model) === 'codex' && !codexEnabled) continue
    if (!(await laneAvailable(model))) { log(`model ${model} cooling — skipping for ${item.id}.`); continue }
    const build = await runBuilder(model, item, foldInstructions)
    if (build && build.laneStatus === 'ratelimited') {
      await coolLane(model, build.limitSignature)
      log(`model ${model} hit a usage limit — cooled; rerouting ${item.id}.`)
      continue
    }
    if (providerOf(model) === 'claude' && (!build || build.status === 'refused')) {
      log(`Builder refused/failed on ${item.id} (${model}) — falling back to opus.`)
      return { build: await buildItem(item, 'opus', foldInstructions), laneName: 'opus' }
    }
    return { build, laneName: model }
  }
  return { build: null, laneName: null, allLanesCooling: true }
}
// JUDGMENT ROUTER — routes a judgment/coordinator seat to its ladder (LANE_POLICY runs),
// else the legacy Claude agent(). Walks the ladder: first available model wins; opus cooled (the 88% rule or
// exhaustion) => gpt-5.5 via codexJudge; a codex usage-limit cools that lane and falls through. Returns the
// verdict object, or null if NO judge lane is available (caller treats null as a stop-clean condition).
async function runJudge(seat, judgePrompt, schema, label, phase, effort = 'high') {
  if (!LANE_POLICY) return agent(judgePrompt, { label, phase, schema, model: MODEL[seat], effort })
  const ladder = JUDGE_LADDER[seat] || [MODEL[seat]]
  const laneSchema = withLane(schema)
  for (const model of ladder) {
    if (providerOf(model) === 'codex' && !codexEnabled) continue
    if (!(await laneAvailable(model))) { log(`judge model ${model} cooling — skipping ${label}.`); continue }
    const res = providerOf(model) === 'codex'
      ? await codexJudge(model, judgePrompt, laneSchema, label, phase)
      : await agent(judgePrompt, { label, phase, schema, model, effort })
    if (res && res.laneStatus === 'ratelimited') { await coolLane(model, res.limitSignature); log(`judge model ${model} hit a usage limit — cooled; rerouting ${label}.`); continue }
    if (res) return res
  }
  log(`no judge lane available for ${label} — all judgment models cooling.`)
  return null
}
// Claude quota probe — OPTIONAL local script (not bundled): ~/.claude/skills/autobuild/quota.py, expected to
// print ONE JSON line of subscription-limit utilisation. Soft signal, NON-FATAL: script absent => no signal => proceed.
async function readClaudeQuota() {
  return agent(
    `Mechanical Claude-quota probe. Run (POSIX bash, foreground, ~ = home dir): python ~/.claude/skills/autobuild/quota.py --json — it prints ONE JSON line of Claude subscription limit utilisation and NEVER emits any token. Return {ok:true, json:"<that exact line>"} on success; {ok:false} if the script is missing or errors. No commentary.`,
    { label: 'claude-quota', phase: 'Select',
      schema: { type: 'object', additionalProperties: false, properties: { ok: { type: 'boolean' }, json: { type: 'string' } }, required: ['ok'] },
      model: MODEL.mechanical, effort: 'low' },
  )
}

// PREFLIGHT — fail the LAUNCH, not the mid-run. Runs once before the loop.
async function preflight() {
  const r = await agent(
    `You are a mechanical preflight agent for autobuild. POSIX bash, foreground. Check and REPORT (do not fix):\n` +
    `1. API-KEY LEAK: is ANTHROPIC_API_KEY or OPENAI_API_KEY set in the env? (env | grep -iE 'ANTHROPIC_API_KEY|OPENAI_API_KEY'). A run must be subscription-authed only.\n` +
    (LANE_POLICY && !cfg.disableCodexLane
      ? `2. CODEX AUTH: CODEX="$(command -v codex || echo '${CODEX_BIN}')"; run "$CODEX" login status. Report whether it says logged in via ChatGPT.\n`
      : `2. (codex lane not in use — skip)\n`) +
    `3. LANES FILE: mkdir -p ~/.autobuild; ensure ~/.autobuild/lanes.json is readable (create as {} if absent).\n` +
    `Return {apiKeyLeak, codexAuthed, lanesOk, notes}.`,
    { label: 'preflight', phase: 'Select', schema: {
        type: 'object', additionalProperties: false,
        properties: { apiKeyLeak: { type: 'boolean' }, codexAuthed: { type: 'boolean' }, lanesOk: { type: 'boolean' }, notes: { type: 'string' } },
        required: ['apiKeyLeak'],
      }, model: MODEL.mechanical, effort: 'low' },
  )
  if (r && r.apiKeyLeak) throw new Error(`autobuild preflight FAILED: a provider API key is set in the environment — this run must be subscription-authed only (no per-token billing). Unset it and relaunch. (${r.notes || ''})`)
  if (LANE_POLICY && !cfg.disableCodexLane) {
    codexEnabled = !!(r && r.codexAuthed)
    log(codexEnabled ? 'preflight: codex authenticated (ChatGPT subscription); codex lanes enabled.'
                     : `preflight: codex NOT authenticated — codex lanes DISABLED this run; router falls back to Claude lanes. (${(r && r.notes) || ''})`)
  }
  // Claude quota (live, exact) — cool any CRITICAL model-scoped Claude limit using its REAL reset time,
  // and flag global-weekly exhaustion (judgment can't leave Claude => accepts can't run => stop clean).
  if (LANE_POLICY && cfg.claudeQuotaProbe !== false) {
    const q = await readClaudeQuota()
    let usage = null
    if (q && q.ok && q.json) { try { usage = JSON.parse(q.json) } catch (e) { /* no signal */ } }
    if (usage && usage.ok && Array.isArray(usage.limits)) {
      log('Claude quota: ' + usage.limits.map((l) => `${l.kind}${l.model ? '[' + l.model + ']' : ''}=${l.percent}%/${l.severity}`).join('  '))
      for (const l of usage.limits) {
        if (l.severity === 'critical' && l.model) {
          await coolLane(String(l.model).toLowerCase(), `Claude ${l.kind} limit ${l.percent}% resets_at ${l.resets_at}`)
          log(`cooled Claude model ${l.model} until ${l.resets_at} (limit ${l.percent}%).`)
        }
      }
      const wk = usage.limits.find((l) => l.kind === 'weekly_all')
      // WEEKLY CEILING — UNSUPERVISED autobuilds only (this preflight runs only in the
      // workflow; a supervised session is never gated, the owner goes over when they choose). At >=88% weekly
      // EVERY Claude model is off-limits: stop the run clean to conserve the weekly window for the owner's
      // supervised sessions. Deliberately simple + coarse; day-of-week-relative pacing, codex-limit
      // instrumentation and cross-provider load-balancing are natural follow-ups. Individual-model exhaustion BELOW the ceiling
      // (e.g. Fable 100%) is cooled above; judgment/builds route to gpt-5.5/codex via the ladders.
      const CEILING = cfg.weeklyCeilingPercent || 88
      if (wk && typeof wk.percent === 'number' && wk.percent >= CEILING) {
        r.claudeWeeklyExhausted = true
        log(`preflight: ${CEILING}% WEEKLY CEILING — weekly Claude ${wk.percent}% (>=${CEILING}); EVERY Claude model off-limits; unsupervised autobuild stops clean to conserve Claude for the owner's supervised sessions.`)
      } else {
        r.claudeWeeklyExhausted = !!(wk && wk.severity === 'critical' && !codexEnabled)
        if (wk && wk.severity === 'critical') log(codexEnabled
          ? `preflight: WEEKLY Claude CRITICAL (${wk.percent}%) but under the ${CEILING}% ceiling — judgment can use gpt-5.5 (codexJudge); run continues.`
          : `preflight: WEEKLY Claude CRITICAL and codex unavailable — no judge lane; stop clean.`)
      }
    } else {
      log(`Claude quota probe: no signal (${(q && !q.ok) ? 'unavailable' : 'unparsed'}) — proceeding.`)
    }
  }
  return r
}

async function adjudicate(item, build, audit, folds) {
  const prompt =
    `You are Epistates, the coordinator-adjudicator, deciding in your OWN context (you did not build and did not audit).\n${GUARDRAILS}\n\n` +
    `Item ${item.id} — ${item.title}\nBuilder summary: ${build.summary}\nFiles: ${JSON.stringify(build.filesChanged)}\n` +
    `Blind audit after ${folds} fold(s): ${JSON.stringify(audit.votes, null, 2)}\n\n` +
    `Decide: ACCEPT (clean or only non-blocking minors -> commit on the branch), FOLD (only if folds remain and the defects are fixable — but the fold budget is ${MAX_FOLDS} and ${folds} used), or PARK (novel-architecture/irreversible-with-no-precedent/unresolved-real-defect -> restore clean, log the decision the item needs). ` +
    `A precedent-determined reversible call is an ACCEPT, not a park (the false-stall lesson). Give a delta-notation commitMessage on accept.`
  return runJudge('coordinator', prompt, ADJ_SCHEMA, `adjudicate:${item.id}`, 'Adjudicate', 'high')
}

// ---- capability B: refill (scope + propose, propose-only) ------------------
async function refill() {
  phase('Refill')
  const scope = await agent(
    `You are a scope-comprehension reader (top judgment tier) for ${REPO}.\n${GUARDRAILS}\n\n` +
    `Read the project's own docs — CLAUDE.md, README, the model/design docs, the current backlog/tracker, recent commits, and (if present) any project status or current-focus doc the repo points at. ` +
    `Build a scope model — what the project IS, its thesis/goals, current state, and the real gaps — then propose candidate features against THAT scope (never off-thesis expansion). ` +
    `For each candidate give {title, rationale, value, reversibility, complexity, inScope, precedent, filterClass}.`,
    { label: 'comprehend+propose', phase: 'Refill', schema: PROPOSE_SCHEMA, model: MODEL.buildHard, effort: 'high' },
  )
  if (!scope || !scope.candidates || !scope.candidates.length) return 0

  const critic = await agent(
    `You are Kritos, a blind critic. Score each candidate against the project THESIS (its CLAUDE.md / positioning). ` +
    `THREE dispositions: (1) DROP off-thesis, redundant, or low-value candidates — noise is a cost. ` +
    `(2) FOG: on-thesis but the frontier-vs-fog gate fails — the candidate's QUESTION cannot be stated precisely yet (it is a direction, a hunch, downstream of an unanswered question). Fog candidates are CAPTURED, never queued: list them in fog with the open question that blocks them. Do not pre-slice fog into buildable items. ` +
    `(3) KEEP the rest; mark promotable=true ONLY if clearly in-scope AND reversible AND small AND precedent-determined.\n\n` +
    `Scope model:\n${scope.scopeModel}\n\nCandidates:\n${JSON.stringify(scope.candidates, null, 2)}`,
    { label: 'critic', phase: 'Refill', schema: CRITIC_SCHEMA, model: MODEL.audit, effort: 'high' },
  )
  const keep = (critic && critic.kept) || []
  const fogList = (critic && critic.fog) || []

  // Land propose-only + conservatively promote — one cheap agent, explicit-path write.
  const land = await agent(
    `You are a mechanical scribe working inside the run workdir.\n${GUARDRAILS}\n\n` +
    `1) Append these critic-kept candidates to docs/PROPOSED-BACKLOG.md (create if absent), each with provenance (autobuild refill, this run) + class tags. Never silently expand scope; this file is propose-only.\n` +
    `Candidates:\n${JSON.stringify(keep, null, 2)}\n\n` +
    (PROMOTE_POLICY === 'conservative'
      ? `2) Then PROMOTE into the live tracker (${tracker.kind}: ${tracker.kind === 'pinax' ? 'pinax add ... --actor epistates@autobuild (the --actor flag is REQUIRED — run events attribute the seat, never the CLI default local user)' : 'add to BACKLOG.md ready set'}) ONLY the candidates with promotable=true. Everything else stays propose-only for owner curation.\n`
      : `2) Do NOT promote anything (promotePolicy=off) — leave all candidates propose-only.\n`) +
    (fogList.length
      ? `3) FOG candidates (direction-not-question — captured, never queued): append each to ${cfg.fogLedger ? `the fog ledger ${cfg.fogLedger}` : 'docs/FOG.md in this repo (create it if absent)'} as a new entry (title + the blocking open question as Context + a concrete 'Surface when:' trigger + 'source: autobuild refill ${REPO}'). Commit that change separately (explicit path). Fog NEVER enters PROPOSED-BACKLOG.md or the tracker.\nFog:\n${JSON.stringify(fogList, null, 2)}\n` : '') +
    `Commit these doc/tracker changes with EXPLICIT paths and a delta-notation message. Return {ok, action, commit, notes} incl. how many you promoted and how many went to fog.`,
    { label: 'land+promote', phase: 'Refill', schema: TRACK_SCHEMA, model: MODEL.buildStandard },
  )
  const m = land && /(\d+)\s+promot/i.exec(land.notes || '')
  return m ? Number(m[1]) : (PROMOTE_POLICY === 'conservative' ? keep.filter((k) => k.promotable).length : 0)
}

// ---- main loop: capability A (execute), with refill when dry --------------
const shipped = [], parked = [], failed = []
let processed = 0, refilledThisDry = false

if (LANE_POLICY) {
  const pf = await preflight()                 // multi-lane mode only; a legacy run (no lanePolicy) is byte-identical to the single-lane path
  await readLaneState()
  if (pf && pf.claudeWeeklyExhausted) { stoppedReason = 'claude-weekly-ceiling'; log('Not starting: weekly Claude at/over the unsupervised ceiling (or critical with no codex judge lane) — stopping clean to conserve Claude for the owner\'s supervised sessions. Run supervised over the ceiling whenever you choose.') }
}

while (!stoppedReason && processed < MAX_ITEMS && (!budget.total || budget.remaining() > RESERVE)) {
  phase('Select')
  const sel = await agent(
    `You are Proskopos, the forward scout for ${REPO}.\n${GUARDRAILS}\n\n` +
    (READY_SET
      ? `The orchestrator has pre-parsed the tracker. Select ONLY from this closed ready set — it is the complete universe; do NOT run any other tracker command or read any other project's tracker:\n${READY_SET}\n\nRead each candidate's full spec from the tracker file inside the workdir to judge it, and return the SINGLE next item to build, in critical-path order (a dependent never before its blocker). `
      : `Read the tracker (run: ${tracker.nextCmd}) and return the SINGLE next item to build, in critical-path order (a dependent never before its blocker). `) +
    `Classify its complexity (hard=long-horizon/ambiguous-but-specified; standard=well-specified normal work; trivial=mechanical — the router maps each class to a build lane), whether it is reversible, its filterClass, and the precedent that determines any borderline call. ` +
    `Aggressive filter: an item is eligible UNLESS (a) irreversible/destructive, (b) genuinely-open/novel-architecture with NO precedent, or (c) blocked — a precedent-determined REVERSIBLE call IS eligible. ` +
    `Skip ineligible items (report them in reason) and return the first eligible one. If none is eligible, hasItem=false. Also report queueDepth (remaining eligible count). Give the full brief up front. ` +
    `IMPORTANT: if the ready-set entry for an item carries RESUME/recovery instructions, copy them into the brief VERBATIM — they are load-bearing.`,
    { label: 'scout', phase: 'Select', schema: SELECT_SCHEMA, model: MODEL.proskopos },
  )

  if (!sel || !sel.hasItem) {
    if (PROPOSE_WHEN_DRY && !refilledThisDry) {
      log(`Queue dry (${sel ? sel.reason : 'no scout result'}) — running scope+propose refill.`)
      const promoted = await refill()
      refilledThisDry = true
      if (promoted > 0) { READY_SET = ''; log(`Refill promoted ${promoted} item(s); pre-parsed ready set cleared — scout re-reads the live tracker; resuming execute.`); continue }
      log('Refill produced no promotable items — stopping (proposals landed propose-only).')
    }
    break
  }
  refilledThisDry = false
  const item = sel

  // A supplied readySet is the CLOSED UNIVERSE for the run — enforced MECHANICALLY
  // here, never by scout prompt alone (in live runs, scouts have consulted the live
  // tracker despite the closed-set prompt and returned queue items outside the
  // owner's dispatch fence).
  // The fence lifts only when a refill-promote deliberately clears READY_SET.
  if (READY_SET && cfg.readySet && Array.isArray(cfg.readySet)) {
    const allowed = new Set(cfg.readySet.map((it) => String(it.id)))
    if (!allowed.has(String(item.id))) {
      stoppedReason = 'scope-fence-violation'
      log(`SCOPE FENCE: scout selected '${item.id}' — NOT in the closed readySet [${[...allowed].join(', ')}]. Stopping clean; nothing built.`)
      break
    }
  }

  // Enrich the brief with known traps BEFORE routing to any lane (codex included, since it
  // cannot recall). Bounded + non-fatal; a memory-down enrich returns '' and the build proceeds.
  const traps = await enrichBrief(item)
  if (traps) item.brief = `${item.brief}\n\n## Known traps (canon — check BEFORE debugging)\n${traps}`

  phase('Build')
  // Route to a lane (legacy path when no LANE_POLICY). Refusal->opus fallback lives in the router.
  const disp = await dispatchBuild(item, item.complexity)
  if (disp.allLanesCooling) {
    stoppedReason = 'all-lanes-cooling'
    log(`ALL build lanes cooling — stopping clean; ${item.id} left queued for the next run.`)
    break
  }
  let build = disp.build
  let laneName = disp.laneName
  // A driver that killed a STILL-ALIVE codex because IT hit its own turn/token
  // limit reports laneStatus 'harness-timeout'. That is NOT a work failure: PARK it and PRESERVE the
  // partial tree (the near-complete build is evidence, never disposable). Never route it to a revert.
  if (build && build.laneStatus === 'harness-timeout') {
    const why = `harness-timeout (driver hit its own turn/token limit and killed a live codex; tree PRESERVED for owner review) | lane ${laneName || 'codex'} | evidence: ${build.notes || 'builder result in the run journal'}`
    parked.push({ id: item.id, title: item.title, why })
    await restoreClean(item, build, why, { preserveTree: true, actor: `builder-${String(laneName || 'codex').replace(/[:]/g, '-')}@autobuild` })
    processed++; continue
  }
  if (!build || build.status === 'failed' || build.status === 'novel') {
    parked.push({ id: item.id, title: item.title, why: build ? (build.notes || build.status) : 'builder died' })
    await restoreClean(item, build, build ? (build.notes || build.status) : 'builder died', { actor: `builder-${String(laneName || 'claude').replace(/[:]/g, '-')}@autobuild` })
    processed++; continue
  }

  // Audit + fold loop
  let audit = await auditItem(item, build)
  let folds = 0
  while (audit.failed && folds < MAX_FOLDS) {
    folds++
    log(`${item.id}: audit FAIL — fold ${folds}/${MAX_FOLDS}.`)
    const fd = await dispatchBuild(item, item.complexity, [...audit.defects, ...audit.mustFix].join('; '))
    const fb = fd.build
    if (!fb || fb.status !== 'built') break
    build = fb; if (fd.laneName) laneName = fd.laneName
    audit = await auditItem(item, build)
  }

  phase('Adjudicate')
  let adj = await adjudicate(item, build, audit, folds)
  // Adjudicator-directed folds: the adjudicate seat can spend the remaining fold
  // budget too — an audit can PASS_WITH_MINORS while the deliverable still needs a
  // bounded fix. (a fold verdict here once fell through to the park
  // branch, wiping the build and parking the chain head.)
  while (adj && adj.decision === 'fold' && folds < MAX_FOLDS) {
    folds++
    log(`${item.id}: adjudicator fold ${folds}/${MAX_FOLDS}.`)
    const fd = await dispatchBuild(item, item.complexity, adj.foldInstructions || adj.reason)
    const fb = fd.build
    if (!fb || fb.status !== 'built') break
    build = fb; if (fd.laneName) laneName = fd.laneName
    audit = await auditItem(item, build)
    adj = await adjudicate(item, build, audit, folds)
  }

  phase('Track')
  if (adj && adj.decision === 'accept') {
    const tr = await agent(
      `You are a mechanical close agent working inside the run workdir.\n${GUARDRAILS}\n\n` +
      `The build for ${item.id} — ${item.title} is ACCEPTED. Do exactly two things, both trackable:\n` +
      `1) Commit ONLY the item's changed paths with EXPLICIT paths (git add <paths> && git commit -- <paths>; NEVER a pathless commit — a shared index must not sweep foreign work). Message: ${adj.commitMessage || item.title}\n` +
      `Changed paths: ${JSON.stringify(build.filesChanged)}\n` +
      `LANE ATTRIBUTION: this item was built on lane "${laneName || 'claude'}". Add a "lane: ${laneName || 'claude'}" trailer line to the commit message, and record the lane in the tracker completion event — pinax: include "lane: ${laneName || 'claude'}" in the done note/briefing. The pinax done event MUST carry --actor builder-${String(laneName || 'claude').replace(/[:]/g, '-')}@autobuild (REQUIRED — run events attribute the SEAT, never the CLI default local user). Makes "which lane built what" answerable from the log.\n` +
      `2) Write the status back to the tracker so the run is trackable: ` +
      (tracker.writeBackDone
        ? `${tracker.writeBackDone} The pinax write MUST carry --actor builder-${String(laneName || 'claude').replace(/[:]/g, '-')}@autobuild. Then commit that tracker change (explicit path).`
        : tracker.kind === 'pinax'
          ? `append a completion event to the .ergon log (${tracker.doneCmd || `pinax done ${item.id}`} --actor builder-${String(laneName || 'claude').replace(/[:]/g, '-')}@autobuild — the --actor flag is REQUIRED), then commit the .ergon change too (explicit path).`
          : `mark ${item.id} done in BACKLOG.md (explicit-path commit).`) +
      `\n3) MERGE-ON-ITEM: merge the run branch into the repo's default branch and push it — from the MAIN repo tree, never by switching the workdir: git -C <repo-root> fetch . ${BRANCH}:refs/heads/_ab_tmp 2>/dev/null || true; then git -C <repo-root> merge --no-ff ${BRANCH} -m "MERGE ${item.id} (autobuild)" run with the default branch checked out in the root tree IF AND ONLY IF the root tree is ON the default branch and clean; otherwise use a plain push of the run branch (git push origin ${BRANCH}) and report merge:"deferred-root-tree-busy" — never touch a dirty or campaign-checked-out root tree. After a successful merge, push the default branch. Unmerged accepted work must never accumulate silently: report merge state either way.` +
      `\nNEVER switch the workdir's branch. Return {ok, action, commit, merge, notes}.`,
      { label: `close:${item.id}`, phase: 'Track', schema: TRACK_SCHEMA, model: MODEL.mechanical },
    )
    shipped.push({ id: item.id, title: item.title, lane: laneName, folds, commit: tr && tr.commit, merge: tr && tr.merge, minors: audit.mustFix })
  } else {
    const why = adj ? (adj.parkReason || adj.reason) : 'no adjudication'
    await restoreClean(item, build, why, { actor: 'epistates@autobuild' })
    parked.push({ id: item.id, title: item.title, why })
  }
  processed++
}

async function restoreClean(item, build, parkReason, opts = {}) {
  // opts.preserveTree — a harness-timeout / protected park: keep the tree, never revert.
  // opts.actor — the seat identity the pinax write must carry; defaults to the mechanical seat.
  const isProtected = PROTECTED.includes(item.id) || !!opts.preserveTree
  const actor = opts.actor || 'mechanical@autobuild'
  const parkCmd = tracker.parkCmd || `pinax park ${item.id} --reason "<REASON>" --actor ${actor}`
  const blockCmd = `pinax block ${item.id} --reason "<REASON>" --actor ${actor}`
  return agent(
    `You are a mechanical restore agent working inside the run workdir.\n${GUARDRAILS}\n\n` +
    `Item ${item.id} is PARKED${parkReason ? ` (needs: ${parkReason})` : ''}. ` +
    (isProtected
      ? `PROTECTED / TREE-PRESERVE park: do NOT revert, restore, or clean ANY path — the uncommitted in-flight work stays in the tree for owner review. Do ONLY the tracker write-back below. `
      : `LIVE-CODEX GUARD — BEFORE touching the tree, check for a codex process still building in this workdir: ps aux | grep -i 'codex exec' | grep "${WORKDIR}" (ignore your own grep line). If a live codex process is found, STOP: do NOT revert or clean anything, leave the tree exactly as it is, and return {ok:false, action:"aborted-restore-live-codex", notes:"live codex pid <pid> still working in the workdir — refused to revert under a live process; escalate"} — reverting under a live build is exactly how a build once got lost. ` +
        `Only if NO live codex process is found: restore the working tree to clean so the NEXT item builds on a clean base — revert ONLY the paths this build touched (${JSON.stringify((build && build.filesChanged) || [])}) via git restore / git clean of those paths — do not touch any other work. `) +
    // The park reason must carry the failure SIGNATURE, not a vague "did not reach clean pass".
    `\n\nPARK REASON FORMAT — the reason string is STRUCTURAL, four pipe-separated fields:\n` +
    `  "<phase that failed: Build|Audit|Adjudicate> | <the failing command, verbatim> | <error signature, the FIRST line verbatim> | evidence: <path to the builder result in the run journal / transcript>"\n` +
    `Derive the fields from the builder evidence below; if a field is genuinely unknown put "unknown" — but a park reason with NO error signature is itself a defect the coordinator will BOUNCE, so extract the real failing command + error line whenever the evidence contains one.\n` +
    (parkReason ? `Adjudicator/loop-supplied context to fold into the reason: ${parkReason}\n` : '') +
    `Builder evidence to mine for the signature — status:${build ? build.status : 'n/a'}; notes:${build ? JSON.stringify(build.notes || '') : '""'}; validatorOutput tail:${build ? JSON.stringify(String(build.validatorOutput || '').slice(-1200)) : '""'}\n\n` +
    (tracker.writeBackPark
      ? `${tracker.writeBackPark} The pinax write MUST carry --actor ${actor} (never the CLI default local user). Commit that tracker change (explicit path) so the park is trackable.`
      : tracker.kind === 'pinax'
        ? `Then write a park event to the tracker with the structured reason above and the REQUIRED actor flag: ${parkCmd} (use ${blockCmd} instead if it is dependency-blocked rather than a judgment park). The --actor ${actor} flag is REQUIRED, not optional — without it the event lands attributed to the CLI default local user, which is wrong. Then commit that .ergon change (explicit path) so the park is trackable.`
        : `Then note the park + structured reason against ${item.id} in BACKLOG.md (explicit-path commit) so it is trackable.`) +
    `\n${isProtected ? 'Leave the working tree exactly as it is.' : `If you did revert, verify git status is clean of this item's changes before returning.`} Return {ok, action, commit, notes}.`,
    { label: `park:${item.id}`, phase: 'Track', schema: TRACK_SCHEMA, model: MODEL.mechanical },
  )
}

log(`autobuild ${REPO}: shipped ${shipped.length}, parked ${parked.length}, failed ${failed.length} (${processed} processed)${stoppedReason ? ` — STOPPED: ${stoppedReason}` : ''}.`)
return { repo: REPO, branch: BRANCH, tracker: tracker.kind, shipped, parked, failed, processed, stoppedReason, lanes: LANE_POLICY ? { policy: Object.keys(LANE_POLICY), codexEnabled, state: laneMem } : null }
