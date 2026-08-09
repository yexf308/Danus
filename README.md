# Danus: Orchestrating Mathematical Reasoning Agents with Fact-Graph Memory

<p align="center">
  <a href="https://arxiv.org/abs/2607.06447"><img src="https://img.shields.io/badge/arXiv-2607.06447-b31b1b" alt="Danus paper on arXiv"></a>
  <a href="https://frenzymath.com/blog/danus/"><img src="https://img.shields.io/badge/Technical%20Report-frenzymath.com-1f6feb" alt="Technical report"></a>
  <a href="https://github.com/frenzymath/Rethlas"><img src="https://img.shields.io/badge/Rethlas-GitHub-181717?logo=github" alt="Rethlas on GitHub"></a>
  <a href="https://www.xiaohongshu.com/discovery/item/6a4da1ba00000000070201ef?source=webshare&xhsshare=pc_web&xsec_token=ABfiiMB7yyB-dW_hMzh3MW7ZRG2ddm5in_wBnBALXO6DE=&xsec_source=pc_share"><img src="https://img.shields.io/badge/rednote-%E5%B0%8F%E7%BA%A2%E4%B9%A6-FF2442?logo=xiaohongshu&logoColor=white" alt="rednote (Xiaohongshu) post"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-Apache%202.0-4c1" alt="Apache 2.0 license"></a>
</p>

Danus orchestrates mathematical reasoning agents with fact-graph memory. A main
agent (Claude Code) steers a swarm of autonomous codex workers that prove; a
cold-start verifier is the sole authority on correctness. A `correct` verdict is
necessary, and the gateway then atomically rechecks the verified context and adds
the fact; a stale or unavailable snapshot returns a write error instead. Verified results accumulate in a content-addressed fact
graph — the system's only source of truth — and a strategy loop (a strong
reasoning model) decomposes the problem and steers the swarm. When you have the
answer, Danus renders it into a human report or a publishable LaTeX paper.

Danus builds on the worker–verifier core of our earlier system
[Rethlas](https://github.com/frenzymath/Rethlas)
([arXiv:2604.03789](https://arxiv.org/abs/2604.03789)). The
[paper](https://arxiv.org/abs/2607.06447) and the
[technical report](https://frenzymath.com/blog/danus/) tell the full story:
the system, six research-level case studies it resolved, and what we learned
along the way.

See `ARCHITECTURE.md` for the layered design and the map of every module.

## How it works

<p align="center"><img src="docs/assets/architecture.png" width="820" alt="Danus architecture: a main agent orchestrates a worker swarm; a stateless verifier gates every fact; global memory and the fact graph are the shared storage"></p>

The design follows a strict separation of powers: the main agent performs the
global planning and coordination, the workers carry out the detailed proof
search, the verifier is the sole authority on correctness, and the fact graph
holds every verified result and is the system's only source of truth.

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
The verifier is stateless: a fresh instance
judges each submission and retains nothing afterwards. Because each worker draws
on only the facts it needs for its current claim and submits one fact at a time,
the working context stays small even as the proof grows to many pages — and many
workers' contributions accumulate into one shared structure.

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
danus/                 the engine (installable Python package)
  core/                truth layer: content-addressed fact graph + typed memory + schema
  gateway/             role-gated MCP server — the only door to the truth stores
  verify/              cold-start mathematical authority behind the write-gate
  execution/           worker swarm: the autonomous per-worker round loop + scaffolding
  orchestration/       lifecycle plus durable human hot-join (`say`/`messages`)
  strategy/            consult gateway (elaboration → strong model → master_guidance)
  integrations/        arXiv theorem search
  observability/       read-only dashboard
  authoring/           shared one-shot isolated-codex driver for the two renderers below
  write_paper/         write-paper MCP service (fact graph → publishable LaTeX paper)
  human_summary/       human-summary MCP service (fact graph → progress-report PDF)
agents/                codex agent contracts (main/worker/verifier) + worker & verify skills
.claude/skills/        main-agent skills: elaboration · consult · human-summary · initialize · write-paper
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

# 4. connect Claude Code rooted at this repo dir; on first run it runs `initialize`.
#    --dangerously-skip-permissions lets the main agent operate autonomously (no
#    per-action permission prompts). That is the intended mode, but it means the
#    agent acts with your shell privileges — run Danus on an isolated, disposable
#    host, and read docs/security-and-trust.md first.
claude --dangerously-skip-permissions
```

Everything runs on your own keys (BYO). Workers and the verifier run on your codex
backend; the strategy consult runs on a top-tier reasoning model over the `gpt_pro`
transport (paid), `claude_api` (the Anthropic API, per-token), or `claude_code`
(your Claude subscription), or `off` to skip it.

To let the human owner join a running worker's native Codex turn, start that
worker with the app-server transport and use the durable mailbox:

```bash
export DANUS_WORKER_TRANSPORT=app-server
bin/danus start my-project/max
bin/danus say my-project/max --text 'Try the one-sided Sigma-Delta route next.'
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

If Codex reports that a persisted thread was deleted or is no longer
resumable, Danus fails closed instead of silently starting a second paid
conversation. Inspect the exact id with `bin/danus status <project>/<worker>
--json`, then explicitly clear only that mapping with `bin/danus reset-thread
<project>/<worker> --expected-thread-id <id>`. The reset is CAS-fenced,
append-only audited, and valid only after the worker has fail-stopped: a live
worker or busy lifecycle lock is rejected, with the liveness check and mapping
CAS serialized under `start`'s `.pid.lock`. It is also refused while a paid
round is unfinished or has unknown delivery. Resolving an ambiguous paid round
remains a separate incident decision; reset never retries or abandons it.

For a different failure mode, a multi-hour terminal thread can make 0.147's
mandatory full-history `thread/resume` response exceed Danus's 8 MiB JSONL
ceiling. Danus first checks `thread/read(includeTurns=false)` to attest that the
thread is inactive, then attempts the configured continuation round. If the
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
`delivery_unknown` paid intent, Danus preserves that ambiguous intent and
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
`unfinished_paid_intent` and a matching `recovery_required.argv`; a separate
`intent_ledger_error` is reported instead of guessing if that read fails. A
`prepared` intent is authoritatively not dispatched and therefore recommends
resuming the same immutable intent with `start`, never unknown-outcome abandon.
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
Danus preserves the turn result but quarantines it and disables automatic retry
instead of claiming that the requested model was proven.
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
