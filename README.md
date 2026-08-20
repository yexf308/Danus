# Aristarch: Verification-First Control of Long-Horizon Mathematical Research Agents

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-Apache%202.0-4c1" alt="Apache 2.0 license"></a>
  <a href="https://arxiv.org/abs/2607.06447"><img src="https://img.shields.io/badge/upstream%20Danus-arXiv%3A2607.06447-b31b1b" alt="Upstream Danus paper"></a>
  <a href="https://arxiv.org/abs/2604.03789"><img src="https://img.shields.io/badge/upstream%20Rethlas-arXiv%3A2604.03789-b31b1b" alt="Upstream Rethlas paper"></a>
</p>

Aristarch orchestrates mathematical reasoning agents under one design stance:
**no claim enters truth unmarked**. A main agent steers a swarm of autonomous
codex workers that prove; a cold-start verifier is the sole authority on
correctness. A `correct` verdict is necessary, and the gateway then atomically
rechecks the verified context and adds the fact; a stale or unavailable
snapshot returns a write error instead. Verified results accumulate in a
content-addressed fact graph — the system's only source of truth — and a
strategy loop (a strong reasoning model) decomposes the problem and steers the
swarm. When you have the answer, Aristarch renders it into a human report or a
publishable LaTeX paper.

Around that core, Aristarch adds the control machinery that makes long
unattended research runs survivable and auditable: exact paid-turn binding,
guarded advisor escalation, durable human hot-join with attested delivery,
contamination-gated literature retrieval, and a reproducible evaluation
protocol on research-level benchmarks.

The name: an *aristarch* is a severe critic — after Aristarchus of Samothrace,
the Alexandrian philologist who went through Homer line by line and marked
every doubtful verse with an obelus. That is this system's verifier in one
sentence.

See `ARCHITECTURE.md` for the layered design and the map of every module.

## Provenance

Aristarch is a derivative work of **[Danus](https://github.com/frenzymath/Danus)**
([arXiv:2607.06447](https://arxiv.org/abs/2607.06447),
[technical report](https://frenzymath.com/blog/danus/)) by the **frenzymath**
team — their successor to
**[Rethlas](https://github.com/frenzymath/Rethlas)**
([arXiv:2604.03789](https://arxiv.org/abs/2604.03789)). The worker–verifier
core, the fact-graph design, and the published case studies are upstream's
work; if you use Aristarch, cite their papers (see `CITATION.cff`). This
repository preserves the full upstream commit history, so the provenance of
every line is one `git blame` away. "Danus" and "Rethlas" appear in this
document only to describe origin and compatibility; Aristarch is not endorsed
by or affiliated with the upstream team.

Aristarch is the fork line's contribution, in two generations:

- **Generation 2 — this repository (Danus-hosted, mainline).** Everything in
  "What Aristarch adds" below; measured against the upstream merge-base,
  +65k lines across 153 files, roughly half of it deep modifications to the
  engine's core packages.
- **Generation 1 — [yexf308/Rethlas](https://github.com/yexf308/Rethlas)
  (Rethlas-hosted, frozen).** The first iteration of the same control-plane
  program: the durable human hot-join adapter, the guardian launcher, the
  advisor bridge, and safe three-route parallel generation (+117k lines, 86%
  in new files). It is kept frozen as the pinned generation-1 baseline for
  evaluation.

**Engine names are unchanged.** The Python package, the CLI (`bin/danus`),
the env files (`config/danus.env`), and `DANUS_*` environment variables keep
the upstream engine's name. *Aristarch* names the system; `danus` names the
engine it runs on. A code-level rename would break every operational script
for zero capability gain, so it is deliberately not planned.

## What Aristarch adds

Aristarch's `main` branch is Codex-native end to end. Workers and the verifier
already ran on Codex upstream; here the main agent does too, so the core
system requires no Claude Code installation. Optional paid strategy transports
remain explicit opt-ins and default to `off`.

- **Codex-native orchestration.** `AGENTS.md`, `.codex/config.toml`, and
  `.agents/skills/` provide the main-agent contract, MCP wiring,
  initialization, and rendering workflows. Worker project configuration is
  isolated from the main-agent workspace so one runtime cannot silently
  shadow another.
- **Reasoning-first coordination.** New projects pin one deep root and one
  independent critic while dormant observers consume no paid turns. An
  explicit one or two balanced explorer lanes can pursue independent
  alternate routes without gaining critic or advisor authority. Every new
  terminal coordination slot gets a fresh app-server thread; only recovery of
  that exact slot resumes it.
- **Exact paid-task binding.** Each paid assignment is staged before dispatch
  and bound to its generation, worker, byte length, and SHA-256 digest. A
  later host edit cannot retarget an admitted turn, and owner-gated
  generation changes require a complete frozen task set.
- **Guarded advisor escalation.** A root obstruction must receive independent
  critic confirmation before Aristarch can expose an owner recommendation.
  Every ChatGPT Pro browser question then requires fresh owner authorization,
  exact receipt binding, explicit review, and an audited resume decision.
- **Safer control and observability.** `say`, current-turn-only `encourage`,
  `messages`, and `interrupt-turn` have distinct authority. Status preserves
  paid-intent lifecycle, fact promotions, project discovery, and content-free
  reasoning, tool, wait, memory, compaction, and token telemetry. Missing
  data is reported as unavailable rather than zero.
- **Stricter strategy transports.** OpenAI-compatible Responses gateways
  support `max` effort, canonical message input, configurable `background`
  and `store`, and caller-controlled omission of refused parameters. Failures
  return a structured envelope with billing basis instead of a traceback.
- **Classified whole-paper verification.** The paper verifier separates
  `must-fix` gaps from `ignorable` steps that an undergraduate can fill
  unaided. Deliverability requires zero `must-fix` findings; ignorable
  findings remain visible without forcing transcription-heavy expansion.
- **Contamination-gated retrieval.** `danus/integrations/gated_search.py`
  wraps all external literature search in a fail-closed gate with three
  modes — `strict` (matlas.ai: peer-reviewed journals through 2025 plus
  textbooks, structurally no arXiv), `dated` (month-granularity arXiv cutoff
  with whole-month drop and over-fetch), and `off` (zero-retrieval control
  arm) — plus per-call append-only audit ledgers, source-paper interception,
  and response-size caps. Built for benchmark runs whose problems are sourced
  from recent arXiv papers, where the source paper's abstract contains the
  answer.
- **Review-driven trust-boundary hardening.** Trusted service children now run
  under isolated Python; FactGraph mutations use a guarded project-external,
  bounded lock; browser Send has a project-external no-clobber anchor; gateway
  and verifier share one exact prompt-byte preflight; and whole-paper verdicts
  are bound to the delivered `main.tex` digest.
- **A reproducible evaluation protocol.** A hard subset of MathArena
  arxivmath/brokenarxiv (2026-04 through 2026-06; 61 problems at the
  no-model-solves tier), with frozen append-only manifests, an audited
  rejected list, and a cross-family sycophancy judge on MathArena's 0/1/2
  scale. The dataset and its answer keys live outside every agent workspace
  and never enter this tree. (Release pending; problem content inherits
  CC BY-SA 4.0 from MathArena.)

The upstream project also publishes a
[Claude Code orchestrator line](https://github.com/frenzymath/Danus). Both
lines use the same Codex worker, verifier, fact-graph, and rendering engine;
the main-agent runtime and its configuration files are the intentional
difference.

## How it works

<p align="center"><img src="docs/assets/architecture.png" width="820" alt="Architecture: a main agent orchestrates a worker swarm; a stateless verifier gates every fact; global memory and the fact graph are the shared storage"></p>

The design follows a strict separation of powers: the main agent performs the
global planning and coordination, the workers carry out the detailed proof
search, the verifier is the sole authority on correctness, and the fact graph
holds every verified result and is the system's only source of truth.

New projects use `reasoning_first_v1` coordination by default. The physical
roster defaults to `max:2,high:5`: the durable project-wide admission gate pins
the two `max` workers as deep root and independent critic. By default all five
`high` loops stay dormant in `waiting_admission`: they consume no paid round and
do not start Codex. `--active-explorers 1` promotes `high` to `explorer1`, while
`--active-explorers 2` also promotes `high2` to `explorer2`; `high3` through
`high5` remain observers. The option is represented only by
`max_paid_workers=2+N`, and the requested roster must be large enough before any
project files are created. Explicit `--roles` still overrides the roster. Each
admitted paid turn has a 2700-second cap; that is not a promise that the whole
protected phase completes in 2700 seconds. A candidate from any paid lane
temporarily freezes project-wide admission. Pass `--coordination legacy` only
when the old open-loop behavior is intentionally required; legacy rejects
nonzero active explorers and otherwise retains `high:3,xhigh:4` without explicit
roles.

Each new reasoning-first terminal coordination slot starts on a fresh app-server
thread so the active reasoning context stays bounded. Only crash recovery of
that same pinned slot resumes its exact thread. Legacy `exec` rounds remain
fresh processes; explicit legacy app-server projects retain their separately
documented continuation semantics.

Paid work is also bound to an exact generation task. `danus assign` stages each
reasoning-first paid lane in the protected coordinator before updating the host
`TASK.md` projection; admission snapshots those bytes into the slot and binds
their SHA-256 into the kickoff prompt. The `TASK.md` inside the model workspace
is materialized from that slot, so a later host edit cannot retarget the paid
turn. An owner recommendation cannot be resolved until every next-generation
paid lane is staged; resolution freezes the set atomically. Advances that need
no owner decision carry the preceding frozen tasks forward exactly.

When the root records an exact `obstacle` or `dead_end`, the generation enters
`critic_obstacle_review`. Normal paid admission stops; the same fixed
critic receives one fresh review-phase thread and independently confirms or
rejects that exact root entry. Only an exact confirmation can create
`owner_action_required` and its per-intervention recommendation. An unconfirmed
terminal review advances to a fresh reasoning generation without Pro advice.
Explorers may publish ordinary findings and verifier-gated facts or candidates,
but cannot confirm obstacles, create reviews or recommendations, or resolve an
owner gate.

The single verifier remains the mathematical authority, but it is no longer a
reject-on-busy bottleneck. Distinct submissions wait in a bounded FIFO queue,
exact in-flight duplicates coalesce, and validated successful replies use a
nonce-bound bounded cache. Before candidate admission, exact-fact reuse, or any
paid verifier call, a read-only linearizable glossary preflight rejects a known
definition conflict. The authoritative promotion path repeats this check under
the graph lock to close concurrent-definition races. After a clean preflight, an
exact already-active fact is reused before any verifier call. App-server turns
also publish content-free, root-thread-only
diagnostics for reasoning, tool/control, waiting, memory, retrieval, compaction,
and token usage. Missing telemetry is reported as unavailable/partial, never as
zero and never as proof state.

For large graphs, agents do not inject the graph wholesale. They discover facts
through full-text search that returns statement-only summaries, then hydrate only
explicit ids. Verification round zero carries the complete candidate proof plus
all transitive ancestor statements, direct dependency edges, fact-local definitions,
and selected immutable global definitions. Round zero carries no ancestor proof.
If the verifier returns `needs_context`, the gateway may hydrate only the named
strict ancestors from canonical fact files (whole proof records, bounded to two
expansion rounds/eight proofs/200000 record characters by default) and starts a
fresh verifier session. The digest binds the complete envelope, expansion round,
and exact proof bytes. The verifier never reads project fact files directly.
The mutable project glossary is a discovery index only; it is never injected as
an implicit verifier premise.

Each kind of agent carries out its role through its own skills and its own
role-gated set of tools, so the separation is enforced by construction, not by
prompts: the main agent has no `fact_submit` (the agent that steers the search
structurally cannot introduce unverified mathematics into the fact graph), and
the verifier writes nothing at all.

<p align="center"><img src="docs/assets/agent-tools.png" width="820" alt="The three kinds of agent, each with its own skills and its own role-gated set of tools: the main agent orchestrates and renders, the workers prove and submit, the verifier only reads"></p>

Every claim enters truth through one cycle:

<p align="center"><img src="docs/assets/verify-loop.png" width="820" alt="The submit–verify–repair cycle: a worker submits a statement and proof citing existing facts; a fresh verifier instance accepts it into the fact graph or rejects it with repair hints"></p>

A worker typically focuses on one claim at a time — a lemma, a counterexample, a
toy example — rather than an entire proof. It repeatedly submits the claim with a
supporting proof and revises it under the verifier's feedback until it passes. The
gateway then rechecks the exact context under the graph lock and, if still current,
adds the claim as a fact with the facts its proof depends on as incoming edges.
The verifier is stateless: a fresh instance judges each submission and retains
nothing afterwards. Because each admitted terminal phase gets a bounded fresh
reasoning context and durable progress lives in the shared stores, working
context stays small even as the proof grows to many pages.

The graph below is the fact graph of a real research run: **3,157 verified facts
and 8,616 dependency edges**, in dependency chains up to 54 facts deep (nodes
darken and grow with dependency depth). The search was far broader than the proof
it left behind: 664 facts form the supporting closure of the final theorem, and
the clusters are separate lines of attack — among them conditional scaffolding
that the final proof never cites, and an independent re-derivation of one of its
bounds.

<p align="center"><img src="docs/assets/fact-graph.png" width="440" alt="The fact graph of a real run: 3,157 verified facts and 8,616 dependency edges, nodes darkening and growing with dependency depth"></p>

## Layout

```
danus/                 the engine (installable Python package; retains the upstream name)
  core/                truth layer: content-addressed fact graph + typed memory + schema
  gateway/             role-gated MCP server — the only door to the truth stores
  verify/              cold-start mathematical authority behind the write-gate
  execution/           worker swarm: the autonomous per-worker round loop + scaffolding
  orchestration/       lifecycle plus durable human hot-join (`say`/`encourage`/`messages`)
  strategy/            consult gateway + owner-only durable browser-advisor handoff
  integrations/        literature retrieval, including the contamination gate (gated_search)
  observability/       read-only dashboard
  authoring/           shared one-shot isolated-codex driver for the two renderers below
  write_paper/         write-paper MCP service (fact graph → publishable LaTeX paper)
  human_summary/       human-summary MCP service (fact graph → progress-report PDF)
agents/                codex agent contracts (main/worker/verifier) + worker & verify skills
.agents/skills/         main-agent skills: elaboration · consult · human-summary · initialize · write-paper
bin/ scripts/ config/  runtime layer (wrappers, bootstrap/services/doctor, env templates)
docs/                  human docs: getting started · concepts · operating guide · security & trust · …
examples/              unattended-ops examples + a toy project
```

## Quickstart

```bash
# 1. provision the toolchain (Node + venv + codex CLI) into runtime/
bash scripts/bootstrap.sh

# 2. configure — copy the templates and fill in YOUR keys (never committed)
cp config/danus.env.example config/danus.env
cp config/codex.env.example config/codex.env      # BYO OpenAI-compatible endpoint + key

# 3. health check + bring up the verify service (REQUIRED for any proving)
bash scripts/doctor.sh
bash scripts/services.sh up verify

# 4. connect codex rooted at this repo dir; on first run it runs `initialize`.
#    --dangerously-bypass-approvals-and-sandbox lets the main agent operate
#    autonomously (no per-action approval prompts). That is the intended mode, but
#    it means the agent acts with your shell privileges — run Aristarch on an
#    isolated, disposable host, and read docs/security-and-trust.md first.
codex --dangerously-bypass-approvals-and-sandbox
```

Everything runs on your own keys (BYO). Workers and the verifier run on your
codex backend. Strategy consult is optional and defaults to `off`; `gpt_pro`
(paid OpenAI-compatible API), `claude_api` (Anthropic API), and `claude_code`
(Claude subscription) are explicit opt-ins.

Only an exact current coordinator recommendation—derived from the pinned root
obstruction and independent critic confirmation—allows the attended main agent
to record one bounded `advisor_checkpoint` and prepare its exact question.
General evidence that work is blocked, dead-ended, slow, costly, or near
exhaustion is insufficient. Preparation then stops for fresh owner
authorization. For that owner-approved question, `bin/consult-browser` can
durably hand off to the existing signed-in ChatGPT Pro Chrome skill. This is the explicit
`chatgpt_pro_browser` path—not the `gpt_pro` API, and not selectable by the
environment or an unattended loop. Aristarch never opens Chrome itself; imported
page text is untrusted until
the owner/main agent reviews and adopts strategy-only guidance. See
[`docs/browser-advisor.md`](docs/browser-advisor.md).
Import, adoption, or `master_guidance` does not release a coordinator in
`owner_action_required`. Resuming that generation requires an audited owner-only
`danus resolve-recommendation` of the exact recommendation, with explicit
exact-id and paid-resume acknowledgements. Adopted guidance must link the
recommendation; continuing without guidance fails while its browser request is
active, delivery-ambiguous, or completed but not imported. Repeated advisor
interventions may continue the same verified local conversation: the context id
stays stable, but each uses a new coordinator recommendation, current-evidence
prompt/request, fresh owner authorization, and one-shot dispatch; the broker
retains only the predecessor URL hash.

To let the human owner join a running worker's native Codex turn, start that
worker with the app-server transport and use the durable mailbox:

```bash
export DANUS_WORKER_TRANSPORT=app-server
bin/danus start my-project/max
bin/danus say my-project/max --text 'Try the one-sided Sigma-Delta route next.'
# Morale-only, bound to the currently started paid turn; never queued:
bin/danus encourage my-project/max --text 'Keep going; trust your careful reasoning.'
bin/danus messages my-project/max
# Only this typed command interrupts; message text never does:
bin/danus interrupt-turn my-project/max
```

The app-server process is local stdio only. Every message is written durably
before routing, uses a stable Codex client-message id, and is either attested to
the exact active turn, visibly queued, failed, or marked `delivery_unknown` after
an ambiguous crash. This control transcript is research provenance only: there
is no direct transcript/ledger channel into verification, and ledger bytes are
not included in proof-context digests. If a worker deliberately incorporates
human-supplied mathematics into a candidate statement or proof, that candidate
text is of course sent through the normal production verifier.

`encourage` is deliberately narrower than `say`: it requires an
authoritatively live worker and its one canonical `started` intent, binds the
exact thread and turn, and fails if that turn changes. It cannot start a paid
turn or queue for the next one. The envelope marks the note as non-authoritative
morale support—not a task, coordination directive, mathematical evidence, fact,
or verification. Aristarch does not send these notes automatically and does not
claim a causal effect on reasoning quality.

If Codex reports that a persisted thread was deleted or is no longer
resumable, Aristarch fails closed instead of silently starting a second paid
conversation. Inspect the exact id with `bin/danus status <project>/<worker>
--json`, then explicitly clear only that mapping with `bin/danus reset-thread
<project>/<worker> --expected-thread-id <id>`. The reset is CAS-fenced,
append-only audited, and valid only after the worker has fail-stopped: a live
worker or busy lifecycle lock is rejected, with the liveness check and mapping
CAS serialized under `start`'s `.pid.lock`. It is also refused while a paid
round is unfinished or has unknown delivery. Resolving an ambiguous paid round
remains a separate incident decision; reset never retries or abandons it.

For a different failure mode, same-slot crash recovery—or an explicitly legacy
app-server project—can make 0.147's
mandatory full-history `thread/resume` response exceed the 8 MiB JSONL
ceiling. Aristarch first checks `thread/read(includeTurns=false)` to attest that
the thread is inactive, then attempts the configured continuation round. If the
resume itself is too large, the next attempt fails before `turn/start`; status
keeps the prior paid timeout under `last_paid_turn` and the resume failure under
`last_attempt`. No retry or context drop is automatic. After inspecting
`status --json`, the owner may explicitly accept conversation-context loss while
retaining FactGraph/global/local memory:

```bash
bin/danus rotate-thread my-project/max \
  --expected-thread-id <id-from-status> \
  --reason 'terminal history exceeds the app-server JSONL limit'
bin/danus start my-project/max
```

Rotation is CAS-fenced, append-only audited, and refused while a paid-turn
intent is unfinished, the worker is live, or its lifecycle lock is busy. The
PID identity/liveness check and thread-mapping CAS share `start`'s `.pid.lock`,
including the spawn-to-PID-registration window. Rotation only clears the thread
mapping; the second command is a separate owner decision that creates the
replacement conversation.
If an oversized resume coincides with a `dispatching`, `started`, or
`delivery_unknown` paid intent, Aristarch preserves that ambiguous intent and
publishes no rotation argv. If the remote outcome cannot be reconciled, the
owner can terminalize only the exact incident while the worker is fail-stopped:

```bash
# Copy client_id, thread_id, and state from
#   bin/danus status my-project/max --json
bin/danus abandon-intent my-project/max \
  --thread-id <exact-thread-id> \
  --client-id <exact-danus-round-client-id> \
  --expected-state delivery_unknown \
  --reason 'remote history cannot establish the paid outcome' \
  --acknowledge-paid-outcome-unknown
```

This risk-acknowledged CAS appends an operator receipt and marks the intent
`failed/owner_abandoned_outcome_unknown`; it never resends or deletes round,
message, delivery, or audit history. The abandoned thread remains fenced from
new paid turns until the owner separately runs `reset-thread` or
`rotate-thread`. A live worker, busy lifecycle lock, wrong target/thread/client/
state, missing acknowledgement, or unreadable ledger fails closed unchanged.
`status --json` exposes the canonical ledger row as
`unfinished_paid_intent`; a separate `intent_ledger_error` is reported instead
of guessing if that read fails. While the worker is authoritatively live,
`prepared`, `dispatching`, and `started` report
`paid_intent_status="in_progress"` and no recovery argv. Live
`delivery_unknown` reports `outcome_unknown_while_worker_live`, also without an
abandon suggestion. Only after fail-stop/PID-unsafe state does status expose the
matching owner recovery. For a fail-stopped worker, a `prepared` intent is
authoritatively not dispatched and therefore recommends resuming the same
immutable intent with `start`, never unknown-outcome abandon.
If the immutable prompt/model/effort has drifted, or the owner deliberately
wants to discard that unspent preparation, status also supplies an exact safe
cancel command:

```bash
bin/danus cancel-prepared-intent my-project/max \
  --thread-id <exact-thread-id> \
  --client-id <exact-danus-round-client-id> \
  --reason 'configuration changed before dispatch'
```

This command is valid only for exact `prepared` state under the same
fail-stopped lifecycle lock. It needs no paid-outcome acknowledgement because
no paid RPC was dispatched, appends an operator receipt, and preserves all
round/message/delivery/audit history. Clearing or rotating the old thread is a
separate subsequent command.

Every worker paid process is launched through a retained owned-child host. The
host holds a worker-liveness pipe and the worker's `.paid.lock` until the entire
Codex/MCP process group is terminal and reaped. Thus an owner `SIGKILL` closes
the pipe and cleans the paid group, while an immediate replacement worker
cannot overlap a second paid launch. `stop --force` remains a durable
cooperative request; the CLI never signals an inspected numeric PID or PGID.

The protected round audit records requested/attested model data, observed token
usage, and exact-turn `model/rerouted` events. Token totals are explicitly marked
as observed rather than schema-attested final. If an adapter restart recovers a
pre-existing paid turn, historical reroute notifications cannot be replayed;
Aristarch preserves the turn result but quarantines it and disables automatic
retry instead of claiming that the requested model was proven.
Known app-server protocol, configuration, authentication, delivery, and failed
terminal outcomes are also fail-stop conditions; the outer loop does not turn
them into repeated paid attempts.

**Notes**

- **Settle the stopping condition with the main agent before you start.** By
  default the main agent keeps the swarm running until every target is proved and
  stops it on its own once they are (a hard or slow problem is not a reason to
  stop). Talk through what "done" means for your problem at the outset, so the swarm
  does not keep spending tokens past the point you cared about.

- **Give the writing system a few exemplar papers.** Out of the box, `write-paper`
  produces a complete, compilable paper, but the prose can read like a stack of
  verified facts. In our experience the single highest-leverage fix is to provide
  a few papers of your own as exemplars when you ask for the write-up — the writer
  imitates them, and readability improves substantially.

## Design invariants (see ARCHITECTURE.md §3)

- Three memory tiers, one correctness boundary: only the verifier-gated fact graph
  is truth; global memory is awareness.
- Permission is enforced by the MCP role table (main cannot `fact_submit`; the
  verifier is read-only).
- Gateway-backed Codex launches first probe the exact Python interpreter in an
  isolated subprocess; a missing Danus/FastMCP runtime prevents the Codex call,
  and Codex also requires the configured MCP server to start.
- Content-addressed, cascade-revocable facts; a correct verdict plus the gateway's
  locked context-CAS/add is the sole write path.
- The finished paper is itself re-verified as written (a dedicated paper-math
  verifier reads the whole document) before delivery, on top of the per-fact
  verification.
