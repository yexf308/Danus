# Interfaces on the core data structures

What interactions exist on `local memory` / `global memory` / `fact graph`, and
which of them need a real interface vs. which the agent just does itself. Read
[`DATA_MODEL.md`](DATA_MODEL.md) first (the structures); this defines the *verbs*.

## Principle: wrap only what the LLM can't do reliably

An LLM agent (codex) can already **read any file, write a line to a file,
and grep**. So those need **no interface** — wrapping them is noise. Wrap a verb
as an interface **only** when the LLM can't do it reliably itself:

1. **deterministic computation** the model can't be trusted to do by hand —
   a content-addressed `fact_id` (SHA-256), BM25 ranking;
2. **multi-file integrity** — cascade revoke (find descendants, move, log);
3. **a load-bearing gate** — a fact may exist only if the verifier accepted it;
4. **bounded, integrity-checked hydration** — large fact graphs need explicit-id
   context with proof opt-in, completeness metadata, and stable digests; a
   designated critic likewise needs exact bounded global-memory hydration.

Everything else is the agent writing/reading files per a format the **prompt**
specifies; fact retrieval is the deliberate exception because uncontrolled reads
inflate context and bypass completeness checks.

### Why the surface stays minimal

The interface surface is small on purpose. Writes that need a guaranteed
envelope, deterministic hashing, BM25, or bounded fact hydration are wrapped.
Local/global-memory browsing can stay direct, but exact designated-entry review
uses `gm_get`; fact reads use `fact_search` and `fact_context`. At the extreme, a files-direct deployment wraps **nothing**: the agent
reads/searches/writes `{run_dir}/memory/<channel>.md` directly, and the only
scripts are an arXiv-search API helper and a LaTeX compiler — neither a
data-structure interface. A heavy design would sprawl into dozens of MCP tools
(memory, board, submit, swarm, human, …); that is over-built.

We target the files-direct end: the agent does files directly; we add only the few
verbs above that genuinely need code.

## Default: no interface (the agent does it)

These are **LLM-direct** — the prompt tells the agent the file layout/format and
it reads/writes/greps:

- **local memory** — append a rough note, read it back, grep it. It's private and
  rough; no schema to enforce.
- **global memory — browse** a `<kind>.jsonl` (it's readable), and grep it.
  Designated critic review is the exception: use exact `gm_get`, not a BM25/grep
  match.
- **fact graph — local debugging** may still inspect readable markdown files;
  agent retrieval uses full-text `fact_search` with statement-only results, followed by explicit-id
  `fact_context` so proofs are hydrated only on request.
- **"has this been found / is it a duplicate?"** — grep / read global memory and
  the fact graph and judge. (No `check_fact_novelty` tool.)
- **branch / status reflection in one's own head** — a note. (No `branch_update`
  tool.)

## The interfaces we DO need

Minimal set. Form = **MCP** — one stdio MCP server (`danus/gateway/`, a thin
wrapper over `danus/core/`) exposing 7 data-structure tools **+
`search_arxiv_theorems`** (one external integration — Matlas arXiv theorem search,
returning *verbatim* statements; `danus/integrations/matlas.py`, not a
data-structure interface but the LLM still can't do it itself). codex
consumes MCP natively; `DANUS_ROLE`
selects the per-agent tool subset (worker:
`gm_add`/`gm_get`/`gm_search`/`fact_submit`/`fact_search`/`fact_context`/`search_arxiv_theorems`; main:
`gm_add`/`gm_get`/`gm_search`/`fact_search`/`fact_context`/`fact_revoke`/`search_arxiv_theorems`; verifier:
`search_arxiv_theorems` only — stateless, with required fact context supplied in
the request).
Install: `danus/gateway/INSTALL.md`.

### fact graph (the strict one — id determinism + the verified gate)

- **`danus fact submit`** — the only sanctioned way to write a fact (the
  verified gate lives **inside** submit — the invariant
  is enforced by code, not prose). It:
  1. computes the content-addressed `fact_id` (a deterministic hash);
  2. runs the advisory **glossary-coverage check** plus a hard provenance gate:
     every reused project term must have a direct active predecessor that
     introduced the same definition, and project entries cannot redefine global
     notation;
  3. constructs verification context from declared predecessors: the complete
     transitive ancestor statement/edge/fact-local-definition closure, immutable
     selected definitions, and no ancestor proof, blocking missing, revoked,
     incomplete, or over-budget context;
  4. **calls the verifier**; output schema v3 requires every final finding to
     carry an exact original candidate `{source,line,exact_line}` anchor, checked
     by both the verify launcher and gateway. A `needs_context` control response may hydrate only
     exact strict-ancestor whole proofs from canonical fact files within the
     configured round/count/record budgets, with a fresh session per round. After
     a final acceptance it rechecks the exact expanded context and
     adds under the graph mutation lock, also merging the fact's introduced
     symbols into the project glossary. A stale snapshot returns `write_error`;
     on reject it returns repair hints (and any undefined symbols) and writes no fact.
  5. **either way, logs the verification outcome** to global memory (kind
     `verification`: per-round ids/digests/requests plus the final verdict and
     `fact_id` on accept / `repair_hints` on reject), so
     the verifier's feedback is not lost and siblings learn from rejections. The
     verifier itself stays stateless — `fact_submit` (the worker's tool) does this
     write.
  - in: `problem_id, author, statement, proof, predecessors[], glossary_introduces{}` (+ optional `intuition`, `source_id`, `external_refs[]` — structured bibliography for cited external results, metadata only, not in the `fact_id`)
  - out: every response carries `promoted`, `submission_status`, and
    `verification_verdict` (`null` until a valid final mathematical verdict).
    `accepted` remains the backward-compatible verifier-acceptance field and is
    **not** the publication signal:
    - `{accepted: true, verification_verdict: "correct", promoted: true,
      submission_status: "promoted", fact_id}` — end-to-end success;
    - `{accepted: true, verification_verdict: "correct", promoted: false,
      submission_status: "verified_not_promoted", fact_id: null, write_error}` —
      the verifier accepted the mathematics but the locked graph/glossary write
      failed; repair/retry and never cite it as a fact;
    - `{accepted: true, verification_verdict: "correct", promoted: null,
      submission_status: "promotion_unknown", fact_id: null, write_error}` — an
      fsync failure prevented a durable choice between commit and rollback; do
      not count the response as a publication, and let graph recovery resolve it;
    - `{accepted: false, verification_verdict: "wrong", promoted: false,
      submission_status: "rejected", repair_hints, undefined_symbols}`;
    - `{accepted: false, verification_verdict: null, promoted: false,
      submission_status: "error", verdict: "error"}` (no valid final verifier
      verdict, for example service down — retry).
    Consumers count a publication only when `promoted` is true and `fact_id` is
    present. During rolling compatibility with an older gateway that lacks
    `promoted`, a valid non-null `fact_id` is the only safe success fallback;
    `accepted` alone is never sufficient.
  - **Guarantee:** once a verdict exists (promoted / reject /
    verified-but-not-promoted / promotion-unknown)
    the outcome is **always** logged (step 5) before returning — a verdict is never
    stored by nobody. The verifier is stateless; `fact_submit` (the worker's tool)
    is what persists, so the write must not be skippable by a later failure (it
    runs after the fact write, with the fact write wrapped). A pre-verification
    context error or verify-service error
    yields no verdict, so nothing is stored and the worker retries.
  - the worker's verify-and-repair loop is just: submit → fix from hints → submit …
- **`fact_search <query> [--limit N]`** — BM25 over the verified fact bodies
  (statement + proof + glossary). The **derived fact index**, rebuilt **on demand**
  from `facts/*.md` — *not* a persisted board (so no double-write drift; the fact
  graph stays the single source of truth). Ranked recall over N workers' facts is
  computation the LLM can't do by reading, and it serves both **novelty** ("does a
  fact like this already exist? — don't re-prove it") and **citation lookup**
  ("which verified facts bear on my subgoal?"). Returns `{fact_id, score,
  statement}`; the agent calls `fact_context` for an explicit hit when it needs
  relations or proof text. This is the read view over verified facts (novelty +
  citation lookup).
- **`fact_context <fact_ids> ...`** — deterministic lazy hydration for explicit
  ids. Statements/edges/fact-local definitions plus only referenced
  project/global definitions are the interactive default; proofs are opt-in. The
  verification write-gate disables project-glossary hydration, so mathematical
  definitions there come only from declared fact cards, the candidate, or the
  immutable global glossary. The response
  carries a versioned scope, digest, and fail-closed completeness/budget metadata;
  `complete` means complete for that declared scope.
- **`danus fact revoke <fact_id> --reason ...`** — cascade revoke (walk
  descendants, move to `_revoked/`, log). Multi-file integrity ⇒ code. Low
  frequency (operator / main agent).

### global memory (BM25 + schema-enforced write)

- **`danus gm add --kind K --claim ... --evidence ... --author ...`** — write a
  finding with the schema enforced (valid `kind`, evidence required for
  verifiable kinds, correct envelope, optional `--glossary`). On the shared store
  we want the format guaranteed, so this is a real interface (not LLM-direct).
- **`gm_get <entry_id>`** — exact hydration by canonical 16-lowercase-hex id.
  Absent or duplicate ids fail and serialized output is capped at 16 KiB. A
  designated critic uses this exact record before confirmation; BM25 cannot
  substitute.
- **`danus gm search <query> [--kinds ...]`** — BM25 over the shared findings.
  The shared store gets large (N workers); ranked recall is computation the LLM
  can't do by reading.

### local memory

- **none.** Private and rough — the worker reads/writes/greps its own
  `local_memory/*.jsonl` directly. (No `lm search`: grep is enough.)

## What we explicitly do NOT wrap

`memory_init` (no fixed channel set to pre-create) · `memory_append` /
`read_memory_file` / `read_result_file` (LLM does files) · `branch_update`
(a note) · `verified_facts_append` (the board is not a second source of truth;
facts enter only via `fact submit`, and the read view is the on-demand
`fact_search` index, never an appended board) · `submit_blueprint_candidate`
(folded into `fact submit`) · the `swarm_*`, `ask_human`, `read_directives`,
`consult_*`, `shell_run` tools (out of scope here — control / human / consult /
runtime). (Verified-fact discovery and hydration are covered by `fact_search`
and `fact_context` above.)

## Summary — the whole interface surface

```
MCP tools (danus/gateway/server.py):
  fact_submit   # glossary-coverage check + verify + write a fact (the gate); returns fact_id or repair hints
  fact_search   # BM25 read view over verified facts (derived index, on demand) — novelty + citation lookup
  fact_context  # explicit-id lazy statements/edges/proofs + completeness metadata and digest
  fact_revoke   # cascade revoke
  gm_add        # schema-enforced write of a finding (kind / evidence rule / optional glossary)
  gm_get        # exact 16-hex global-memory entry, unique and bounded to 16 KiB
  gm_search     # BM25 over global memory
```

Seven data-structure tools (+ `search_arxiv_theorems`). Everything else — local
memory, novelty *judgment* — is the agent reading/writing files, guided by prose.
The server is a thin wrapper: `fact_submit` = `FactGraph.undefined_symbols` + the
verify call + a post-verification context recheck + `FactGraph.add`; `fact_search`
= BM25 over the fact files (the derived index, rebuilt per call — no persisted
board to drift); `fact_context` = reachable-file hydration only. `fact_submit`
needs the verify-service URL (`DANUS_VERIFY_URL`); the others work without it.
