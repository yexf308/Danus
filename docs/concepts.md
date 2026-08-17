# Danus — Concepts

A human's mental model of Danus: the actors, the memory layers, the one truth
boundary, and the lifecycle of a project. Read this before the operating guide.
For the design map and module index see `../ARCHITECTURE.md`; for the trust model
see `security-and-trust.md`.

## What Danus is

Danus is an **automated mathematics proof-search system**. You give it a problem;
a swarm of autonomous agents tries to prove it; every claimed result is checked by
a single correctness authority; verified results accumulate in a shared graph; and
when you decide the answer is in hand, Danus renders it into a human progress
report or a publishable LaTeX paper.

The defining idea is a **hard separation between *producing* mathematics and
*deciding it is correct***. Many agents produce; exactly one authority (the
verifier) decides; and a result only *exists* once that authority has accepted it.

## The actors

- **You, the operator.** You pose the problem, make the judgment calls Danus
  surfaces (is this the answer? push the paper outward?), and set the spending and
  operating preferences. You talk to Danus in natural language through the main
  agent.
- **The main agent (codex).** The orchestrator and your entry point. It
  **steers — it does not do the mathematics.** It sets up projects, runs the
  strategy loop, assigns work, monitors, and drives the report/paper skills. It
  *structurally cannot* fabricate a result (it has no `fact_submit` tool).
- **The codex workers.** A roster of autonomous proof workers. In the default
  reasoning-first mode the roster is `max:2,high:5`: durable admission pins the
  two `max` workers as root and independent critic, while the five `high` loops
  stay dormant. Dormant loops do not start Codex and are not automatically
  rotated, promoted, or used as failover. Explicit roles may override the
  roster; legacy retains its `high:3,xhigh:4` default. Each new terminal
  coordination slot starts a fresh app-server thread; only crash recovery of
  that same pinned slot resumes. Each paid turn has a 2700-second cap, which
  does not promise whole-phase completion. Each admitted slot receives a
  hash-attested snapshot of that generation's durable paid-lane task; the
  `TASK.md` visible in its model workspace is a projection of that snapshot,
  not mutable host authority. Each admitted worker reads its bounded directive
  and shared state, reasons deeply, and submits consolidated results;
  an active candidate freezes new branch admission. An `outcome_unknown`
  candidate has no TTL or retry path and remains frozen until exact owner
  resolution.
- **The verifier.** A **cold-start** `codex` judge, started fresh for each distinct uncached check,
  that is the **sole authority on mathematical correctness**. A `correct` verdict
  authorizes the gateway to recheck the exact context and add under the graph
  mutation lock; a stale snapshot is not written. It is an LLM, not a formal
  proof assistant. Exact concurrent requests coalesce behind one paid leader,
  distinct work queues FIFO, and validated results use a bounded process-local
  cache; none of these performance paths changes the correctness gate. See
  `security-and-trust.md`.
- **The optional strategy consult.** The main agent always distills the project's
  state (an *elaboration*). It may explicitly opt into a strong reasoning model;
  only an actual reviewed reply becomes `master_guidance`. The default is off,
  so the main agent normally dispatches from its own synthesis.

## The three memory tiers, and the one truth boundary

Everything a project knows lives in three tiers that differ by **scope** and
**trust**:

| tier | scope | holds | is it truth? |
|---|---|---|---|
| **local memory** | one worker, private | that worker's rough notes / actions | no — private scratch |
| **global memory** | project-shared | typed *findings*: a claim + its evidence, including dead ends | no — shared awareness |
| **fact graph** | project-shared | verified facts | **yes — the only correctness source** |

A finding flows left to right, getting more structured and more trusted: a private
note becomes a shared finding (`global memory`), and a *verifiable* finding that
the verifier accepts becomes a **fact only after the gateway completes the locked
graph write**. Crucially:

- **Only the fact graph is truth.** Global memory — even a plausible, not-yet-checked
  claim — is awareness only. A proof may build **only** on facts (by citing their
  ids), never on unverified findings.
- **There is no promotion shortcut.** Nothing "promotes" a claim to a fact except a
  worker submitting it, the verifier accepting it, and the gateway completing its
  locked context check and add.

## The fact graph

The fact graph is the crown jewel. Each **fact** is one node — a human-readable file
with a statement, a proof, optional intuition, the ids of the facts it depends on
(`predecessors`), the symbols it defines (`glossary_introduces`), and its external
citations (`external_refs`).

- **Content-addressed.** A fact's id is a hash of its mathematical content
  (problem + predecessors + glossary + statement + proof). Same content ⇒ same id
  ⇒ natural dedup and stable references. `external_refs` is deliberately **excluded**
  from the hash, so the paper pipeline can correct citations later without breaking
  the graph.
- **A DAG.** `predecessors` are the edges; content-addressing forbids cycles.
- **Cascade-revocable.** Revoking a fact also revokes everything that transitively
  depended on it, and a proof can never build on a revoked predecessor.
- **Self-contained.** A lightweight glossary check keeps every fact readable — no
  fact silently uses an undefined symbol.

Before candidate admission or a paid verifier call, the gateway reads one
linearizable glossary snapshot and rejects definitions that already conflict
with project or immutable global terms. This avoids paying to verify a candidate
that is already known to be unpublishable. It is deliberately only a preflight:
the authoritative promotion path repeats glossary and context checks under the
graph mutation lock, so a concurrent definition still blocks the write.

For the on-disk shapes and exact fields, see `../danus/core/DATA_MODEL.md`.

## The strategy loop (how the swarm is steered)

The main agent does not micro-manage proofs; it steers, periodically, on genuine
new state:

1. **Elaborate** — distill the shared stores into a high-signal synthesis (verdict,
   closed routes, interfaces, dangers, missing bridge lemmas). *(the `elaboration`
   skill)*
2. **Optionally consult** — a strong reasoning model is an attended explicit
   opt-in. *(the `consult` skill)*
3. **Record & dispatch** — store an actual API/CLI reply under the consult
   contract, or explicitly review/adopt an authorized browser report. When off,
   dispatch directly from the elaboration. Assign the fixed root/critic lanes
   (`danus assign`). Paid-lane assignment is a durable generation staging
   operation; during an owner gate, all next-generation lanes must be ready
   before the exact resolution freezes them. An ordinary no-owner generation
   advance carries the preceding frozen tasks forward exactly.
4. **Monitor** — watch progress; repeat when there is genuinely new state.

The consult transport defaults to **`off`** (the main agent reasons from its
synthesis, no consult). **`gpt_pro`** (paid OpenAI-compatible API),
**`claude_api`** (Anthropic API), and **`claude_code`** (Claude subscription via
the CLI) are explicit opt-ins. Workers and the verifier always run on your own
codex backend.

Human morale support is a separate optional input. `danus encourage` can bind a
short note to the canonical currently started paid turn, but cannot create or
queue a turn. The note is explicitly non-authoritative: it is not a task,
coordination directive, mathematical premise, fact, or verification result.
Danus neither sends encouragement automatically nor claims that it causes better
reasoning.

Only the exact current coordinator recommendation, derived from the fixed root
obstruction and independent critic confirmation, permits the active main agent
to record a bounded `advisor_checkpoint` and prepare its exact question locally.
Broad evidence that routes are blocked, dead-ended, slow, costly, or near
exhaustion is insufficient. It then stops for owner authorization. This
late `chatgpt_pro_browser` intervention is not the `gpt_pro` API and is never
timer-, cost-gate-, or unattended-loop-driven. Repository code only records the
durable handoff; imported page text has no authority until reviewed and adopted
as strategy. Adoption does not release `owner_action_required`; the owner must
run the exact `resolve-recommendation` CAS and acknowledge paid reasoning resume.
Adopted guidance links the exact recommendation; a browser conversation may keep
its stable context across later interventions, but each intervention binds a new
recommendation. See `browser-advisor.md`.

## The lifecycle of a project

```
initialize ─▶ new project ─▶ strategy loop ⇄ worker swarm ─▶ verify ─▶ fact graph
   (setup)     (PROBLEM.md)   (elaborate→consult→assign→monitor)   (the write-gate)
                                     │                                  │
                              human-summary                  finalize (you confirm
                    (progress report — any time, no gate)        the answer)
                                                                        ▼
                                                                   write-paper
                                                               (publishable LaTeX)
```

- **initialize** — first-run setup: your profile, the codex backend, the consult
  transport, the git branch, the spend ceiling; brings the verify service up.
- **new** — create a project (`PROBLEM.md` + a worker roster). One project = one
  problem, its own memory and fact graph.
- **strategy ⇄ workers** — the loop above; workers prove, submit, and the fact
  graph grows.
- **verify** — every submission passes through the verifier; after `correct`, the
  gateway still performs the locked context-CAS/add before a fact is written.
- **human-summary** — a private PDF progress report rendered from the verified
  results; run it at **any time** during the run (it is not gated on finalize).
- **finalize** — *you* decide a verified fact is the answer (`danus finalize`).
  Danus does not decide "done" on its own — that judgment is surfaced to you. (When
  the main agent judges every target proved, it does stop the swarm's exploration on
  its own to save compute, then asks you to confirm the answer; `danus start`
  resumes exploration if you disagree.)
- **write-paper** — after finalize: render the target's verified facts into a
  publishable paper, re-verified as written — the whole document, through a
  dedicated paper-math verifier — before delivery.

## Why you can trust the shape

Every guarantee above — one correctness boundary, permission by construction, the
verifier plus gateway as the guarded write path, resumable workers, the judgment calls staying with
you — is enforced in code, not by prompt convention. For exactly what you are
trusting (and what to double-check), read `security-and-trust.md`; the full
engineering invariant list is `../ARCHITECTURE.md` §3.

## Where to go next

- **Set it up and run it:** `getting-started.md`, then `operating-guide.md`.
- **What can I run / call:** `cli-and-tools.md`.
- **What am I trusting:** `security-and-trust.md` (read this before relying on a
  result).
- **Design & module map:** `../ARCHITECTURE.md`; data shapes:
  `../danus/core/DATA_MODEL.md`.
