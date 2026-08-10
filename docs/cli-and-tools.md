# Danus — CLI & Tools Reference

Danus has two control surfaces:

1. **The `danus` CLI** — lifecycle verbs you (or the main agent on your behalf) run
   to manage projects and workers.
2. **The MCP tools** — what the main agent calls in-session. Three MCP servers are
   wired in `.mcp.json`: the role-gated **`danus`** gateway, **`write-paper`**, and
   **`human-summary`**.

You mostly talk to the main agent in natural language; it runs the CLI verbs and
calls the tools. This page is the reference for what exists.

---

## The `danus` CLI

Run via `bin/danus` (which sources `scripts/env.sh`). Every verb names a project;
there is no default project.

| verb | form | what it does |
|---|---|---|
| `list` | `danus list [--json]` | all projects + physical liveness and reasoning-first paid/waiting counts |
| `new` | `danus new <project> [--roles ROLES] [--model M] [--coordination reasoning-first\|legacy]` | scaffold a project + worker dirs; reasoning-first defaults to two `max` root/critic workers plus five dormant `high` observers, while explicit legacy defaults to `high:3,xhigh:4` |
| `assign` | `danus assign <project>/<worker> (--task "…" \| --file P \| --stdin)` | replace an assignment; for a reasoning-first paid lane, stage the exact task bytes for the current generation (or the gated next generation) before refreshing the host `TASK.md` projection |
| `say` | `danus say <project>/<worker> (--text "…" \| --file P \| --stdin) [--client-id ID] [--fallback queue\|fail]` | durably hot-join owner guidance into the exact active app-server turn; fallback defaults to `queue` |
| `encourage` | `danus encourage <project>/<worker> [--text "…" \| --file P \| --stdin] [--client-id ID]` | send non-authoritative morale support to the canonical currently started paid turn only; omitted text uses the built-in encouragement, and delivery never queues for a later turn |
| `messages` | `danus messages <project>[/<worker>] [--limit N] [--json]` | inspect human-message delivery receipts |
| `interrupt-turn` | `danus interrupt-turn <project>/<worker> [--client-id ID]` | explicit owner-only active-turn interrupt |
| `cancel-prepared-intent` | `danus cancel-prepared-intent <project>/<worker> --thread-id ID --client-id ID --reason TEXT` | exact-CAS cancellation of an authoritatively unspent prepared intent under the worker lifecycle lock; append-only receipt, then reset/rotate separately |
| `abandon-intent` | `danus abandon-intent <project>/<worker> --thread-id ID --client-id ID --expected-state STATE --reason TEXT --acknowledge-paid-outcome-unknown` | fail-stopped exact-CAS of one unreconcilable paid outcome; append-only risk receipt, no retry/deletion, and old-thread dispatch remains fenced until reset/rotation |
| `resolve-candidate` | `danus resolve-candidate <project> --receipt ID --outcome known-no-promotion\|abandon-unknown --acknowledge-paid-outcome-unknown` | explicitly reconcile an `outcome_unknown` reasoning-first verification candidate; explicit unknown-outcome acknowledgement is required for either resolution; checks the bound FactGraph identity under lock, writes an owner audit, and never retries verification |
| `resolve-recommendation` | `danus resolve-recommendation <project> --recommendation-id ID --resolution adopted-master-guidance\|continue-without-advisor --acknowledge-recommendation-id ID --acknowledge-resume-paid-reasoning [--master-guidance-entry-id ID]` | exact owner CAS that closes the current reviewed recommendation and starts a fresh paid generation; every next-generation paid lane must first have a staged task, which the resolution freezes atomically |
| `reset-thread` | `danus reset-thread <project>/<worker> --expected-thread-id ID` | CAS-fenced reset of a server-deleted thread mapping; fail closed for a live/busy worker and share `start`'s `.pid.lock` |
| `rotate-thread` | `danus rotate-thread <project>/<worker> --expected-thread-id ID --reason TEXT` | explicitly accept terminal conversation-context loss after bounded resume failure; fail closed for a live/busy worker, share `start`'s `.pid.lock`, and preserve research stores |
| `finalize` | `danus finalize <project> [--paper <paper_id>] [<fact_id> …]` | record the approved target theorem(s) in the paper's `TARGET.md` (what write-paper reads; default paper → `<project>/TARGET.md`, a non-default `--paper` → `<project>/papers/<paper_id>/TARGET.md`). **With no id:** print candidate terminal facts as suggestions (writes nothing) |
| `start` | `danus start <project>[/<worker>]` | launch the autonomous worker loop(s); a live loop is not necessarily a paid model turn |
| `status` | `danus status <project>[/<worker>] [--json]` | liveness, round, lane/generation, paid-active vs waiting admission, task staging, candidate/paid-intent state, and last-turn telemetry |
| `stop` | `danus stop <project>[/<worker>] [--force]` | graceful (finish the round, exit at the boundary) or `--force` (durably request the worker to interrupt its active turn/direct child and exit) |

Notes:
- In reasoning-first mode, the paid task source of truth is the coordinator's
  generation/slot snapshot, identified by its SHA-256 and byte count. The host
  and model-workspace `TASK.md` files are projections of that binding, not an
  independent source of paid work. `assign` stages a paid lane before updating
  the host projection. During `owner_action_required`, it targets generation
  `N+1`; stage every paid lane and confirm `task_staging.ready=true` in
  `status --json` before resolving the recommendation. The owner resolution
  freezes that complete set. A generation advance that needs no owner decision
  carries the preceding frozen task bytes forward exactly.
- In reasoning-first JSON status, `advisor_reachable` is only the structural
  fact that the roster has an independent critic and can eventually produce an
  advisor recommendation. It does not mean one exists or may be prepared.
  `advisor_recommendation_present` means the current generation has a durable
  exact recommendation; `advisor_recommendation_ready` additionally means its
  review and every paid slot in that generation are terminal and the exact open
  gate currently passes. Browser prepare requires the exact ready
  recommendation id, not merely `advisor_reachable=true`.
- `finalize` only **records** the answer; it does not stop workers. Deciding a
  verified fact is *the answer* is your call (the main agent surfaces it).
- `start` launches each worker detached in its own process group, so it survives
  your session. `stop --force` does not signal an externally inspected numeric
  PID; the worker owns and stops its in-flight direct child itself.
- Reasoning telemetry in `status --json` is content-free, root-thread-only, and
  diagnostic. Missing data is `unavailable`/`partial`, never inferred as zero and
  never used as proof state or browser authority.
- A live worker with a canonical `prepared`, `dispatching`, or `started` paid
  intent reports `paid_intent_status: "in_progress"` and no
  `recovery_required` action. Live `delivery_unknown` is reported separately as
  `outcome_unknown_while_worker_live`, also without an abandon command. Recovery
  argv appears only after the worker is fail-stopped (or its PID identity is
  unsafe) and the exact ledger state can be read.
- For `encourage`, `--client-id` binds the semantic channel, note, target, and
  exact expected thread/turn. It replays only that same active binding; reuse
  after a turn change conflicts. A lost steer acknowledgement may be
  `delivery_unknown`, but is never retried or queued for another turn; inspect
  it with `messages`.
- An `outcome_unknown` candidate has no TTL. Do not retry or resubmit it; use the
  exact owner-only `resolve-candidate` command after inspecting its receipt.

---

## MCP server 1 — `danus` (the gateway): the door to the truth stores

The gateway is **role-gated**: what a caller can see depends on `DANUS_ROLE`. The
main agent runs as `role=main`.

**The eight tools** (a trailing `?` marks an optional argument):

| tool | args | what it does |
|---|---|---|
| `gm_add` | `kind, claim, evidence, verifiable?, glossary?, links?, consult_provenance?, input_tokens?, output_tokens?, cost_usd?, project?` | publish a finding to shared global memory; consult metadata is master-guidance-only, and browser provenance is accepted only after explicit adoption |
| `gm_get` | `entry_id, project?` | retrieve exactly one global-memory record by its canonical 16-lowercase-hex id; absent/duplicate ids fail, serialized output is capped at 16 KiB; designated critic review must use this rather than BM25 search |
| `gm_search` | `query, kinds?, limit_per_kind?, project?` | search global-memory findings |
| `fact_submit` | `statement, proof, predecessors?, glossary_introduces?, intuition?, source_id?, external_refs?` | **the write-gate** — known glossary conflicts fail during a read-only preflight before candidate admission or paid verification; after `correct`, recheck the exact context and glossary under the graph lock before adding; only `promoted` + non-null `fact_id` mean publication |
| `fact_search` | `query, limit?, project?` | full-text BM25 discovery whose result payload contains statement summaries only |
| `fact_context` | `fact_ids, predecessor_depth?, proof_mode?, max_chars?, project?` | lazy explicit-id context; statements/relations by default, proofs opt-in, with completeness metadata |
| `fact_revoke` | `fact_id, reason, project?` | cascade-revoke a fact + its dependents |
| `search_arxiv_theorems` | `query, num_results?` | semantic search over arXiv theorem statements |

**Who can see what (`danus/gateway/roles.py`):**

| role | tools |
|---|---|
| **worker** | `gm_add`, `gm_get`, `gm_search`, `fact_submit`, `fact_search`, `fact_context`, `search_arxiv_theorems` |
| **main** | `gm_add`, `gm_get`, `gm_search`, `fact_search`, `fact_context`, `fact_revoke`, `search_arxiv_theorems` (**no `fact_submit`**) |
| **verifier** (the fact-checking verifier behind `fact_submit`) | `search_arxiv_theorems` only (read-only) |

The main agent thus **cannot write a fact** and **cannot** even see `fact_submit`;
the verifier can write nothing. See `security-and-trust.md`.

`fact_submit.accepted` is retained for compatibility and means only that the
verifier returned a valid final `correct` verdict. Consumers and monitors must
count a published fact only when `promoted` is true and `fact_id` is non-null.
`submission_status` is one of `promoted`, `verified_not_promoted`,
`promotion_unknown`, `rejected`, or `error`; `verification_verdict` keeps
`correct`/`wrong` separate from graph-write success. A glossary conflict already
present at submission preflight returns no verifier verdict and makes zero
verification calls; repair the introduction before resubmitting. The preflight
is an optimization, not write authority: a concurrent definition can still
appear, so promotion repeats the glossary and context checks under the mutation
lock. Such a post-verification conflict is `verified_not_promoted` with a
`correct` verification verdict and an explicit `write_error`. `promotion_unknown`
carries `promoted: null` when an fsync failure made the crash-recovery outcome
ambiguous; it is not counted as publication. For responses from an older gateway
without `promoted`, only a valid non-null `fact_id` is a safe publication
fallback.

---

## MCP server 2 — `write-paper`: fact graph → publishable paper

Six tools (`danus/write_paper/server.py`), each wrapping an isolated codex role.
The main agent calls them with small structured args; the heavy bytes (style guide,
fact-graph math) are assembled inside the tool and never enter the main agent's
context. Each tool returns a small honest envelope (status + paths + flags + a
`log_path` to a per-call diagnostic log), never the full `.tex`.

| tool | role | what it does |
|---|---|---|
| `paper_subgraph` | (curation, no codex) | return a compact, deterministic statements-only skeleton of the target-fact closure for the main agent to read and pick from — no codex, no writes; feed the chosen `fact_ids` to `paper_write` |
| `paper_write` | writer | draft the first complete `main.tex` from the target closure + house style |
| `reference_audit` | reference auditor | **offline** — flag bibliography entries it cannot vouch for (no tools/network) |
| `reference_verify` | reference verifier | **online** — verify flagged citations (arXiv + web) and update the reference ledger in place |
| `paper_revise` | reviser | revise `main.tex` for compile fixes / operator annotations / citation fixes (in-tool compile-retry loop) |
| `paper_verify_math` | (math re-verification) | re-check the whole paper's math, as written, through a dedicated verifier before delivery |

Most tools take an optional `paper_id` — a project can hold multiple papers (the
default paper uses the legacy `<project>/paper/` workspace; any other `paper_id`
gets an isolated `<project>/papers/<paper_id>/`). See the write-paper skill README
(`.claude/skills/write-paper/README.md`) for the full workflow.

---

## MCP server 3 — `human-summary`: fact graph → reader report

| tool | what it does |
|---|---|
| `summary_write` | render a human-readable, id-free progress report (compiled PDF) from the fact graph — precise problem statement, partial results with real proof sketches, the main obstacle, a neutral timeline, and the remaining lemma |

`summary_write` takes an optional `language` (else it follows the operator's
language in `OPERATOR.md`).

---

## Main-agent skills (invoked in-session, not MCP tools)

The main agent also has Claude Code **skills** under `.claude/skills/`:
`initialize` (first-run setup), `elaboration` (the strategy synthesis),
`consult` (the strategy consult), `human-summary`, and `write-paper`. These
orchestrate the tools and CLI above; see `operating-guide.md` for how they fit the
lifecycle.

### Owner-only ChatGPT Pro browser advisor

`bin/consult-browser` is a durable receipt CLI for an owner-controlled
`query-chatgpt-pro` Chrome skill. It never opens or controls Chrome, calls an API,
or invokes a model. Its verbs are:

`prepare`, `authorize`, `dispatch-started`, `submitted`, `complete`,
`needs-input`, `import`, `adopt`, `recover`, `fail-not-submitted`, `abandon`, and
`status`.

Only an exact current coordinator recommendation derived from the fixed root
obstruction and independent critic confirmation permits the attended main agent
to write one matching bounded `advisor_checkpoint` and call `prepare`. Broad
blocked/dead-ended/slow/costly evidence alone is insufficient. It must then stop
for owner authorization of that exact question. No timer, unattended loop, or
cost gate may trigger or advance it. Reasoning-first `prepare` requires both a
stable conversation `--context-id` and the exact current per-intervention
`--recommendation-id`, plus the immutable checkpoint's exact
`--checkpoint-id`, `--checkpoint-sha256`, and `--checkpoint-bytes`; generic
prepare spells these `--browser-context-id`, `--browser-recommendation-id`,
`--browser-checkpoint-id`, `--browser-checkpoint-sha256`, and
`--browser-checkpoint-bytes`. The prompt bytes must equal the checkpoint
`evidence` bytes. Import/adopt does not
unlock `owner_action_required`; run the audited `resolve-recommendation` verb
with exact-id and paid-resume acknowledgements before generation work resumes.

Run `bin/consult-browser <verb> --help` for the exact mandatory arguments, and
follow `browser-advisor.md` for the required ordering, exit codes, no-resend
recovery, hashed URL handling, and imported-versus-adopted trust boundary. The
older `gpt_pro` consult name is still the paid API. The browser path is selected
only by an explicit per-question owner invocation; it is not a valid automatic
`DANUS_CONSULT_TRANSPORT` mode. Supply conversation URLs through the documented
file/stdin flags, not argv. Completion stores response digest/size only; `import`
requires the exact response again via `--response-file` or `--response-stdin`.
For a verified same-Danus conversation follow-up, prepare a new dynamic prompt
with `--predecessor-request-id` plus
`--predecessor-conversation-url-file|--predecessor-conversation-url-stdin`, then
stop for fresh owner authorization. The fresh `dispatch-started` must resupply
that URL source. Each follow-up keeps the stable context but has a new current
recommendation, request/hash, and one-shot Send; unknown or
cross-project/context predecessors fail closed.

---

## The persistent services (run via `scripts/services.sh`)

| service | port | required? |
|---|---|---|
| `verify` | 127.0.0.1:8091 | **yes** — no verify ⇒ `fact_submit` fails ⇒ no facts |
| `dashboard` | 127.0.0.1:8099 | optional (read-only view; port-forward to see it) |

```bash
bash scripts/services.sh up verify            # required before any proving
bash scripts/services.sh up dashboard <p>     # optional
bash scripts/services.sh status
bash scripts/services.sh logs <svc>            # bounded snapshot; -f is refused
bash scripts/services.sh down <svc>|dashboard|all
```

Each resident service is owned by a lifecycle-locking guardian and controlled over
an owner-only, nonce-authenticated Unix socket. PIDs are diagnostics, never CLI
signal authority. `up`/`down` update the fsynced desired-state manifest before
launch/stop; recovery rechecks the exact intent generation. See `operations.md`
for the runbook and `configuration.md` for the environment variables that tune
all of the above.
