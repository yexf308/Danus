# Danus core data model — local memory · global memory · fact graph

This is the **authoritative, detailed spec** of the three core data structures of
the Danus system, their relationships, and the logic for using them.
Skills, prompts, and the orchestration layer are written against this document —
read it before touching any of them.

> **The model, by scope.** Danus keeps one private per-worker running log and
> splits shared knowledge by **scope**:
>
> | name | scope | who reads it |
> | --- | --- | --- |
> | **local memory** | per-worker private | only that worker |
> | **global memory** | project-shared — the *categorized findings* | all workers + main agent |
> | **fact graph** | project-shared, verified | all workers + main agent |
>
> Strong **categorization** (typed channels) and the append-only JSONL + BM25
> mechanism live on the **global memory** layer (shared), because sharing typed
> findings — including dead ends — is the whole point. Per-worker `local memory`
> keeps only the rough "what I did" log. `fact_graph` is a content-addressed
> verified DAG.

---

## 0. The three tiers at a glance

```
                scope            structure          unit                 truth?
local memory    per-worker       loose / rough      "what I did" log     no  (private scratch)
global memory   project-shared   strongly typed     a CLAIM + evidence   no  (shared findings, incl. dead ends)
fact graph      project-shared   fully structured   a VERIFIED fact      YES (only correctness source)
```

A finding flows **left → right**, getting more structured and more trusted:

```
local memory  ──(one consolidated shareable phase checkpoint)──▶  global memory
global memory ──(verifiable=true → send to verifier → fix until correct)──▶  fact graph
```

**The load-bearing invariant:** a proof may only build on `fact graph` entries
(cite a `fact_id`). `global memory` is for **awareness** — dedup, ideas,
knowing which paths died — and is **never** a correctness source, even though it
holds claims with evidence. Only the verifier promotes a claim into the fact
graph. This is the same discipline as "peer-consult informs direction, the
verifier decides correctness."

---

## 1. local memory (per-worker private, rough)

**Purpose.** Each worker's private running log of what it thought and did, so it
can recall its own context later in the run. Deliberately rough — no schema
police. Other workers never read it.

**Scope.** Per-worker. Rooted at the worker's own directory.

**Storage.** `<worker_dir>/local_memory/<channel>.jsonl`, append-only JSONL,
BM25 recall. Default channels (small, can grow):

- `notes` — free-form thoughts, partial reasoning, things-to-try.
- `events` — a log of actions taken (searches run, skills invoked, a claim
  published to global memory, a fact submitted). Auto-logged + explicit.

**Entry envelope.**

```json
{ "timestamp_utc": "...", "channel": "notes", "record": { ...any JSON... } }
```

`record` is free-form — that is intentional; local memory holds "various
unformed content."

**Operations.** `append(channel, record)`, `search(query, channels, limit)`
(BM25), `read(channel)`.

**When a worker writes it.** Continuously, as it works — raw reasoning into
`notes`, actions into `events`. In `reasoning_first_v1`, intermediate claims,
searches, and failed micro-steps stay local. An admitted worker publishes at most
one consolidated shareable result/candidate checkpoint for the phase and, only
if the line genuinely fails, one consolidated obstruction checkpoint. Global
memory is a coordination surface, not an automatic transcript.

---

## 2. global memory (project-shared, strongly typed findings)

**Purpose.** The shared pool of *findings* — every formed claim plus its
supporting evidence, **including dead ends** — so workers learn from each other
(don't re-walk a dead path, build on each other's ideas) and so verifiable
claims get driven toward the fact graph. Failures are shared, not locked in each
worker's private memory where only successes surface.

**Scope.** Project-shared. Rooted at the project directory; all workers and the
main agent read/write it.

**Storage.** `<project_dir>/global_memory/<kind>.jsonl` — **one append-only
JSONL file per kind** (one file per channel, shared) + BM25.
Status transitions are append-only too (see "status").

**Entry schema.**

Unlike local memory (§1), a global-memory entry is **flat** — every field is at
the top level; there is no `record` envelope (that nesting is local memory's, not
this one's).

```json
{
  "id":         "<stable id>",
  "timestamp_utc": "...",
  "author":     "<worker id | main_agent>",   // shared store ⇒ attributed
  "kind":       "counterexample",              // the strong category (§2.1)
  "claim":      "...",                          // what is asserted / explored
  "evidence":   "...",                          // verifiable: an explicit proof/construction; judgment: the reasoning behind it
  "verifiable": true,                           // ★ objectively checkable, or a judgment?
  "status":     "unverified",                   // lifecycle, §2.2
  "fact_id":    null,                            // back-link once promoted
  "links":      { "subgoal": "...", "predecessors": ["<id|fact_id>", "..."] },
  "glossary":   { }                              // symbol → definition introduced with this finding
  // …plus any kind-specific free-form fields (**extra) — e.g. a verification
  // entry's verdict/fact_id/write_error, or a master_guidance entry's bounded
  // consult_provenance/input_tokens/cost_usd — also flattened at the top level,
  // NOT inside a record.
}
```

For reasoning-first worker writes, the gateway injects protected
`links.coordination` from the paid slot. A model never self-reports or overrides
generation/lane/slot fields. A critic confirmation supplies only
`links.confirms_entry_id` with the exact returned root global-memory id. After its
independent analysis, the designated critic retrieves that record with exact
`gm_get`, not a BM25 search result.

### 2.1 Kinds (categorization)

| kind | verifiable (default) | author | what goes in |
| --- | --- | --- | --- |
| `conclusion` | true | worker | a derived consequence of the statement (needs justification/proof as evidence) |
| `example` | true | worker | a (toy) example satisfying assumptions+conclusion; the construction is the evidence |
| `counterexample` | true | worker | a construction refuting a claim; the construction is the evidence |
| `proof_attempt` | true (when a sub-result is proved) | worker | an attempt on a subgoal; if a self-contained sub-result is proved, that is a verifiable claim |
| `plan` | false | worker | a subgoal decomposition / strategy (a judgment, not objectively checkable) |
| `dead_end` | usually false | worker | why a path failed; if killed by a counterexample it can be verifiable |
| `direction` | false | worker | "worth exploring X" — an unverifiable judgment |
| `obstacle` | false | worker | "X seems to block this route" — an unverifiable judgment |
| `master_guidance` | false | **main agent (after an actual consult review)** | an optional consulted strategic steer. It is absent when consult is off; workers heed its mathematical direction when present, but it is never a correctness or control source. |
| `verification` | false | the worker's `fact_submit` (auto) | a trace of a verification outcome: `verification_verdict` records mathematical judgment, while `promoted` / `submission_status` and `fact_id` record whether the graph write completed; `write_error` explains a verified-but-not-promoted or rare promotion-unknown result and `repair_hints` explains rejection. `fact_submit` attempts a durable append; a failure is returned as `trace_error` without hiding a written fact id. Siblings read traces to learn from rejections and failed or ambiguous promotions. |
| `elaboration` | false | **main agent** | an event-driven high-signal synthesis: mathematical verdict, closed/obsolete routes, interface contracts, dangerous heuristics, missing bridge lemmas (§2.4). It drives direct assignment when consult is off and is the input if an API/CLI consult is explicitly selected. |
| `advisor_checkpoint` | false | **main agent** | a bounded late-intervention summary created only from the exact current coordinator recommendation derived from root obstruction plus independent critic confirmation. Broad blocked/dead-ended evidence alone is insufficient. It records verified fact ids, failed routes with evidence, the unresolved bottleneck, and one candidate decision question; it is not permission to transmit. |

Process-only categories (`branch_states`, `events`) stay in
**local memory**, not here — they are not findings. `verification_reports` is
not a kind: a verifier verdict attaches to an entry's `status` (§2.2).

**Scope rule that controls noise.** Every global-memory entry has a clear
scope: it is *a claim plus its evidence*. Objectively-checkable kinds
(`conclusion`/`example`/`counterexample`/`proof_attempt`) **must** carry an
explicit proof or construction as `evidence`. Unverifiable judgments
(`plan`/`direction`/`obstacle`/`master_guidance`) **must** set `verifiable:false`
so readers know they are opinions to test, not established results. This scoping
— not a politeness rule — is what keeps the shared store from becoming a dump.

**Writing guideline — unified definitions.** When you write a finding, **define
your symbols and reuse the project's terminology consistently** — check the
project glossary before naming something, and use the same symbol for the same
object as everyone else. A finding may carry an optional `glossary`
(symbol → definition). This is the same self-containment discipline the fact
graph enforces (§3); applying it early keeps global memory readable and lets a
verified finding carry cleanly into a fact without a terminology rewrite.

### 2.2 Status lifecycle

`status` is updated by appending a status event (append-only; current status =
the latest event for that `id`):

- verifiable entries: `unverified → verifying → verified (sets fact_id) | refuted`
- judgment entries: `open → supported | challenged`

A worker is **encouraged to keep pushing** `verifiable=true` entries through the
verifier: send to verify, on `wrong` revise the evidence, re-verify, until
correct — then the gateway attempts its locked context-CAS/add (§3, §4). A
stale snapshot is not promoted. The verify-and-repair loop operates per-claim on
the shared store.

### 2.3 master_guidance — the optional strategic channel

New projects default to `reasoning_first_v1` with roster `max:2,high:5`: the
coordinator pins the two `max` workers as root and independent critic for a
generation. The five dormant `high` workers wait without a paid turn and are not
automatically rotated, promoted, or used as failover. Explicit roles override
this roster; explicit legacy without roles retains `high:3,xhigh:4`. Each
new terminal coordination slot receives a fresh app-server thread; only crash
recovery of that same pinned slot resumes its exact thread. The 2700-second cap
bounds each paid turn, not completion of the whole root/critic phase. Legacy
`exec` and explicitly legacy app-server continuation remain separate semantics.

For a paid reasoning-first lane, `danus assign` first stores one bounded exact
task in the protected coordination database. The host `TASK.md` is only an
operator-facing projection. Admission copies the task bytes, byte count, and
SHA-256 into the immutable round slot; the pinned kickoff names the exact slot,
generation, and task digest, and the model-workspace `TASK.md` is rebuilt from
that slot snapshot rather than from a mutable host file. Before resolving
`owner_action_required`, the owner must stage tasks for every paid lane in
generation `N+1` (`task_staging.ready=true`); the resolution transaction freezes
that complete set. A generation that advances without an owner gate carries its
previous frozen task set forward exactly. Missing or conflicting bindings fail
closed instead of silently reusing a stale assignment.

Human morale support is a separate, optional surface. `danus encourage` binds a
non-authoritative note to the exact currently started thread/turn and uses
fail-only delivery: it neither queues for a later turn nor starts paid work. The
note is not a task update, coordination directive, mathematical claim, proof,
verification result, or scope permission. Danus never sends encouragement
automatically and makes no claim that it causally improves reasoning.

On an event with genuinely new shared state, the main agent writes an
`elaboration`. Strategy consult defaults to `off`, in which case it assigns the
fixed root/critic from that synthesis without fabricating `master_guidance`.
`gpt_pro`, `claude_api`, and `claude_code` are explicit attended opt-ins. Only an
actual reviewed reply is recorded as `master_guidance`. Workers treat it as
strategic steering, never correctness or control authority.

API/CLI consult replies follow the consult skill's normal recording contract. A
`chatgpt_pro_browser` completion is different: its receipt stores response
SHA-256/size and attestations, never page plaintext. `import` requires the owner
to resupply exact matching bytes and exposes them only transiently as untrusted
page content with no authorities. The main agent/owner must review and synthesize
strategy-only text distinct from the raw response, then record the broker's
explicit `adopt` transition. Only
that adopted text may be written as `master_guidance`, accompanied by the bounded
`consult_provenance` receipt validated in `danus.core.schema`, a
`links.recommendation_id` equal to the exact current coordinator recommendation,
and binding by the gateway to the same project's actual adopted broker row. For
the browser transport this receipt requires the exact request, stable context,
per-intervention recommendation, binding, receipt, prompt, reply, and
adopted-strategy hashes, `trust="adopted_strategy"`, UI mode `Pro`,
`billing_basis="subscription"`, and null model/token/cost telemetry. Raw imported
text is not eligible, and the stored guidance evidence must hash exactly to the
adopted synthesis. Runtime main/all role is required for `master_guidance` and
`elaboration`; an author label is not authority. None of this changes the
FactGraph write gate.

The gateway digest-fences every `gm_add` kind against the same project's raw
browser reply/clarification digests across claim, evidence, glossary, and links;
an exact raw scalar cannot be hidden in another global-memory channel. The only
exempt evidence is an exact adopted synthesis whose full broker provenance has
already validated. Semantic paraphrase is not string-guessed: trusted main review
and explicit synthesis remain the authority boundary.

**Operations.** `append(kind, claim, evidence, verifiable, author, links, **extra) -> id`,
`set_status(id, status, fact_id=None)`, `read(kind)` (entries, status folded),
`get(id)` (exact canonical 16-lowercase-hex id, absent/duplicate rejected,
serialized result capped at 16 KiB), `search(query, kinds, limit)` (BM25
discovery). Designated critic review uses `get(id)`; search cannot substitute.

### 2.4 elaboration — the synthesis and optional-consult input

On an event cadence driven by genuinely new state, the main agent writes
an **elaboration**: a single high-signal-to-noise synthesis of the project's
current state, read **only from the shared stores** — global memory (findings,
dead ends, recent verifications) and the fact graph (verified facts, the DAG,
proved vs. open) — **never** from a worker's private local memory (a layer
boundary). It follows a fixed template (mathematical verdict → closed/obsolete
routes → interface contracts → dangerous heuristics → missing bridge lemmas) and
a strict honesty discipline (goal stays fixed, cite `fact_id`s only, no numerical
distance estimates, no process telemetry). The *how* lives in the **`elaboration`
main-agent skill**, not in code.

The elaboration is recorded as an `elaboration` entry (`claim` = the one-line
verdict, `evidence` = the full templated body, `links` = cited `fact_id`s). With
consult off, it drives direct fixed-lane assignment. If a configured API/CLI
transport is explicitly selected, the exact elaboration becomes its prompt and
the reviewed reply may become `master_guidance` (§2.3). It is also what the main
agent draws on to keep the human informed.

### 2.5 advisor_checkpoint — attended late intervention

Only an exact current coordinator recommendation—derived from the pinned root
obstruction and an independent critic confirmation linked to that exact root
entry—permits the active main agent to create one bounded
`advisor_checkpoint`. Broad evidence that routes are blocked, dead-ended, slow,
expensive, or near exhaustion is insufficient. This is a possible Pro advisor
question, not a regular strategy beat. It must contain exactly these
ordered sections: `Verified facts`, `Failed routes and evidence`, `Unresolved
bottleneck`, and `Candidate decision question`. The whole evidence is at most
16 KiB, `links.fact_ids` contains at most 12 valid verified fact ids, and no
worker-local memory or secrets are included.

The gateway enforces that only the runtime `main` authority (not a caller-chosen
author string) can append this kind, resolves an explicit/pinned project, and
checks every cited id against the active integrity-validated FactGraph before the
append. Phantom, revoked, malformed, duplicate, or unscoped ids fail closed.

The main agent may create a durable browser `prepared` receipt only from that
exact current recommendation and checkpoint, then must stop and ask the owner to
authorize that exact question.
Neither a timer, an unattended loop, the cost gate, nor the prepared receipt may
authorize Chrome or Send. Only the owner's per-question acknowledgement advances
the broker to `authorized`; import/review/adopt then follows §2.3. None of
prepare, import, adopt, or `master_guidance` releases a coordinator in
`owner_action_required`. Resuming the generation requires an audited owner-only
`resolve-recommendation` exact CAS. The owner must repeat the recommendation id
and explicitly acknowledge paid reasoning resume. Adopted guidance must link the
same recommendation; continue-without-advisor also requires no active,
delivery-ambiguous, or completed-but-unimported browser request. A browser
conversation may keep its stable `context_id`, but a later intervention always
uses the new current `recommendation_id`.

### 2.6 Verification-candidate overlay

Reasoning-first verification admission is a project-level live-slot overlay, not
a global-memory status guess. While a candidate is active, new paid admission is
frozen. If its durable outcome is `outcome_unknown`, the source worker stops and
must not retry or resubmit the exact question. The overlay has no TTL and elapsed
time never implies success or failure. Only the owner-only `resolve-candidate`
transition, bound to the exact receipt/source slot and checked against FactGraph
identity, may release it; resolution never re-calls the verifier.

---

## 3. fact graph (project-shared, verified)

> **Terminology.** An **"ugly-proof"** — an "ugly-but-rigorous", self-contained,
> machine-checkable proof record — is exactly **a fact in this fact graph**, same
> thing. If the operator says "ugly-proof", they mean a fact node. ("Ugly" is the
> deliberate contrast with the *polished* arXiv paper, which a separate pipeline
> produces from the fact-graph DAG.)

**Purpose.** The single correctness source: a content-addressed DAG of
verifier-accepted facts that compose into a paper and support cascade revocation.
The essence is deliberately minimal — a readable node, a content-addressed id, a
predecessor DAG, cascade revoke — with no status, verifier_outcome, claim_summary,
see_also, or drafts/ on the node.

**Scope.** Project-shared, rooted at the project directory.

**Storage.** `<project_dir>/fact_graph/facts/<fact_id>.md` — one readable
markdown file per fact (file name = the bare-hex id) + `revocation_log.jsonl` +
flat `_revoked/<fact_id>.md` archive + `glossary.json`. There is no `drafts/`
(rejected claims stay in global memory as `refuted`).

**Fact node — 7 frontmatter fields + markdown body:**

```yaml
---
fact_id: 0056a49384644046          # content-addressed (bare hex)
problem_id: KMMP
author: KMMP_pro3                  # which worker produced it
predecessors: [7b6dd3df2e88fff5]   # bare-hex ids this depends on (the DAG)
has_intuition: true                 # makes the optional body boundary unambiguous
glossary_introduces: {"X": "a complex manifold", "K_F": "the canonical class of the foliation F"}
external_refs: [{"key": "HL26", "authors": ["Han", "Liu"], "title": "...", "arxiv": "2603.03817", "year": 2026, "cited_for": "Theorem 1.2"}]
---

## statement
<what was proved — self-contained: every symbol is defined here, in a cited
 predecessor's glossary, or in the global glossary; a reused project term must
 cite a direct active fact that introduced its definition>

## proof
<the argument (markdown)>

## intuition          # optional
<one-liner>
```

New files encode glossary mappings as a JSON flow-object (valid YAML) so arbitrary
Unicode, colons, and newlines round-trip exactly; legacy YAML block mappings remain
readable. `## proof` is reserved as the statement/proof boundary, and the optional
intuition text may not itself contain a standalone `## intuition` heading.

- **`glossary_introduces` is KEPT (essential).** Without it the fact graph
  becomes unreadable — a fact could use a symbol nobody ever defined. Each fact
  records the symbols it introduces (symbol → definition); the project glossary
  `glossary.json` accumulates them. A **glossary-coverage check** (`fact submit`)
  flags any interesting symbol used in the body that is not defined anywhere
  **available**, where availability is the union of four layers (precedence
  low→high): **global glossary** → **project glossary index** → *cited
  predecessors'* `glossary_introduces` → *this fact's*
  `glossary_introduces`. Project entries may not change a global term's meaning.
  The project glossary is a discovery/index layer, never an implicit verifier
  premise. Whenever a new fact inherits a project definition, it cites an active
  predecessor whose fact-local glossary carries that definition. This makes the
  semantic dependence an ordinary DAG edge, so revoking the source
  cascade-revokes declared dependents. The **global glossary**
  (`danus/core/glossary_global.json`, repo-wide, shared by **all** projects) holds
  universal notation — Z, Q, R, C, floor/ceil, gcd/lcm, intervals, the Greek
  parameter names, … — so a fact need not redefine `epsilon` or `Z+` every time;
  only project-specific symbols go in the lower layers. (Heuristic, advisory; the
  verifier is the backstop. The *other* proof-lint rules — handwave,
  chart-position refs — are **prose**, not code.)
  Project terms are append-only in meaning while active: adding the same term
  with the same definition is idempotent, while a conflicting redefinition is
  rejected. Revocation deterministically rebuilds the project glossary from
  remaining active introducers before moving facts. Because verification never
  hydrates this mutable index, a stale or differently spelled term cannot silently
  become authoritative mathematical context.
- **`external_refs` is structured bibliography for cited external results** (a
  list of `{key, authors, title, arxiv, year, venue, doi, cited_for}` dicts;
  serialized as a one-line JSON flow-array; `[]` / absent for older facts). The
  worker fills it at `fact_submit` (grounded via `search_arxiv_theorems`); the
  paper pipeline's **reference auditor** corrects it post-hoc. It is **mutable
  metadata, NOT part of the `fact_id`** — hashing it would change the id (and break
  the DAG) on every audit, and would perturb every pre-existing fact's id. The
  cited keys themselves already live in `proof`, which *is* hashed. Read it via
  `external_refs(fact_id)`; rewrite it via `set_external_refs(fact_id, refs)`
  (touches only the metadata line, never the body or id).
- **`fact_id` is content-addressed:**
  `SHA256(json{problem_id, sorted(predecessors), sorted(glossary_introduces), normalized(statement), normalized(proof)})[:16]`.
  Same content ⇒ same id ⇒ natural dedup. Nodes are immutable
  (a changed statement/proof/glossary ⇒ a different id ⇒ a new file). `external_refs`
  is deliberately excluded (mutable metadata, above).
- **DAG:** `predecessors` are the bare-hex fact ids this fact depends on
  (its "depends-on"). References use bare hex everywhere — one convention.
- **DAG integrity and revocation:** `add` refuses unknown or revoked predecessors.
  Revoking a fact moves it (and every descendant) to `_revoked/` and logs to
  `revocation_log.jsonl`.

**Deliberately not on the node (and where it lives instead):** `status` (a fact in
`facts/` is verified by definition) · `verifier_outcome` (redundant) ·
`claim_summary` (derive for an index when needed) · `see_also` · `drafts/`
(→ global memory `refuted`) · the text-hygiene lint rules — handwave,
chart-position refs, quantifier ranges (→ **prose** in the worker/verifier
prompts) · a persisted `verified_facts.jsonl` board (→ the **derived index** below: a
BM25 view computed on demand from `facts/`, never a stored second truth, so no
double-write drift). **Kept:** the glossary (data + the coverage check) — it is
what makes the graph readable.

**Derived fact index (`search`).** `search(query, limit)` rebuilds a BM25 view
over the fact bodies (statement + proof + glossary) **on demand** from `facts/*.md`
and returns the top `{fact_id, score, statement}`. It is the cross-worker recall
the LLM can't do by reading, serving **novelty** ("does a fact like this already
exist? — don't re-prove it") and **citation lookup** ("which verified facts bear
on my subgoal?"). Exposed as the `fact_search` MCP tool (worker + main). It is a
*read view*; the fact files stay the single source of truth.

**Lazy explicit context (`context`).** `context(fact_ids, predecessor_depth,
proof_mode, max_chars)` reads only fact files reachable from the caller-ordered
ids. It returns statements, predecessor edges, fact-local definitions, and only
the project/global glossary entries whose notation is actually referenced;
`selected` hydrates proofs for requested roots and `all` hydrates every included
proof. Bounded depth or full closure is deterministic. Budgets omit whole
lower-priority records or definitions—never slice them—and the result reports
`complete`, `truncated`, missing/revoked/omitted ids or terms, and character
usage/budget. `complete` is relative to the declared scope. A versioned scope plus
SHA-256 digest binds the requested ids, hydration mode, exact records, and selected
glossary snapshot. Interactive callers may include the project discovery index;
the verification write-gate sets `include_project_glossary=false`, leaving only
declared fact-local definitions and immutable global entries. Exposed as
`fact_context` to worker and main.

The internal `fact_submit` verifier projection is independently versioned. Its
v3 round zero contains the complete transitive ancestor statement/edge/local-
definition closure and no proof; later rounds attach only explicitly requested
strict-ancestor whole proof records. Completeness/omissions, expansion scope and
round, exact proof bytes, and budget accounting are digest-bound. This does not
change the public `fact_context` API above.

**Operations (code = data-structure I/O only).** `compute_fact_id(...)`,
`add(problem_id, author, statement, proof, predecessors=[], intuition="",
external_refs=[]) -> fact_id`, `get_raw(fact_id)`, `list()`, `search(query, limit)`,
`context(fact_ids, predecessor_depth, proof_mode, max_chars)`,
`predecessors(fact_id)`, `descendants(fact_id)`, `external_refs(fact_id)`,
`set_external_refs(fact_id, refs)`, `revoke(fact_id, reason)`.

---

## 4. Relationships & data flow (how they connect)

```
            ┌─────────────── per worker (private) ───────────────┐
 worker  →  │ local memory: notes / events  (rough recall log)    │
            └───────────────────────┬────────────────────────────┘
                    publish one consolidated phase checkpoint
            ┌───────────────────────▼──────────── project-shared ─┐
 all     →  │ global memory: <kind>.jsonl                         │
 workers    │   conclusion/example/counterexample/proof_attempt   │
 + main     │   plan/dead_end/direction/obstacle/master_guidance  │
 agent      │   each = claim + evidence + verifiable + status     │
            └───────────────────────┬────────────────────────────┘
                 verifiable=true → verify → repair → correct
            ┌───────────────────────▼──────────── project-shared ─┐
            │ fact graph: facts/<fact_id>.md (content-addressed)  │
            │   ★ the only thing a proof may build on (cite id)   │
            │   predecessors = DAG · cascade revoke               │
            └─────────────────────────────────────────────────────┘
```

**Invariants (load-bearing):**

1. **Correctness source = fact graph only.** Proofs cite `fact_id`. Global
   memory (even verifiable-but-unverified claims) is awareness, never a brick.
2. **Promotion is verifier-gated.** A claim enters the fact graph only by passing
   the verifier (§4 promotion).
3. **Append-only** everywhere (local + global + revocation log); status changes
   are appended events, not mutations. Fact files are immutable once written
   (a changed statement/proof ⇒ a different `fact_id`).
4. **Isolation of local memory.** Workers never read each other's local memory;
   the only cross-worker channels are global memory (awareness) and fact graph
   (truth).
5. **Content addressing.** `fact_id` is a pure function of content ⇒ dedup,
   stable references, cascade revocation.

**Promotion.** A worker submits the finding only through the gateway's
`fact_submit`. Before candidate admission or verifier spend, the gateway takes a
read-only glossary snapshot and rejects any already-known project/global symbol
conflict (including malformed glossary state). This preflight is a cost guard,
not correctness authority: after verification, the existing exclusive
context/glossary CAS independently rechecks the write. The gateway builds and
validates lazy predecessor context, calls the verifier only after that preflight,
and invokes `FactGraph.add_if_context_unchanged` only for a valid `correct`
verdict whose locked context still matches. The resulting fact id may back-link
the global finding.
There is deliberately no second `promote()` path that could bypass the gate.

### Code vs prose (the boundary)

| code (this library — touches the fixed JSONL / fact-graph files) | prose (prompts/skills — agent behavior) |
| --- | --- |
| local/global memory: append / exact bounded get / read / search (BM25) | when to publish a consolidated local→global checkpoint; which `kind`/`verifiable` |
| fact node: serialize/parse, `compute_fact_id`, integrity-checked lazy context, atomic add/revoke | proof strategy and repair choices |
| evidence-required-for-verifiable check; unknown/revoked-predecessor refusal | "global memory is awareness, never a brick — cite `fact_id`" |
| | "facts must be self-contained; no handwave / chart-position refs" |

Keep it that way: do not add orchestration code. If something is a *decision*, it
is prose.

---

## 5. Usage logic (typical reasoning-first generation)

1. **Coordinator:** stage and freeze generation-bound tasks, pin one root and one
   independent critic, and leave observers dormant without paid turns. A new
   terminal coordination slot gets a fresh app-server thread, and only same-slot
   crash recovery resumes its exact task/thread binding.
2. **Main agent, on material new state:** append an `elaboration`; dispatch the
   fixed lanes directly by default (`off`) or explicitly opt into an API/CLI
   consult and record only its actual reviewed reply as `master_guidance`.
3. **Root:** reason deeply within the 2700-second paid-turn cap, keep intermediate
   work local, and publish one consolidated candidate or obstruction.
4. **Critic:** first preserve independent analysis. If directed to review a root
   checkpoint, retrieve its exact 16-hex id with `gm_get` (never a BM25 substitute),
   then publish one consolidated response. Confirmation uses only
   `links.confirms_entry_id`; the gateway injects protected coordination links.
5. **Verification:** submit a consolidated verifiable candidate through
   `fact_submit`; build further work only on `promoted:true` plus a non-null
   `fact_id`. On `outcome_unknown`, stop without retry and surface exact owner
   resolution.

This document is the contract those behaviors are written against. The Python
implementation lives next to it in `danus/core/` (`local_memory.py`,
`global_memory.py`, `factgraph.py`, `schema.py`, `bm25.py`); see `README.md` for
the API and usage instructions.
