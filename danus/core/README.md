# danus/core — the Danus data model (library)

Pure-Python, protocol-agnostic. **Read [`DATA_MODEL.md`](DATA_MODEL.md) first** —
the authoritative spec. This is the API quick reference.

**Code only touches the fixed data structures (the JSONL files + the fact-graph
nodes).** Everything behavioral — when to publish a finding, when to send it to
the verifier, when to promote it to a fact, the control loop, strategy — is
**prose** (prompts/skills), not code. That boundary is deliberate; don't add
"orchestration" code here.

```
danus/core/
  DATA_MODEL.md      ← detailed spec (read first)
  local_memory.py    LocalMemory   — per-worker private, rough recall log
  global_memory.py   GlobalMemory  — project-shared, strongly typed findings
  factgraph.py       FactGraph     — project-shared, verified content-addressed DAG
  schema.py          Fact, GLOBAL_KINDS, STATUSES, compute_fact_id
  bm25.py            BM25 recall
  _util.py           append-only JSONL helpers
  tests/test_core.py smoke test
```

Design: local/global memory use an append-only-JSONL + per-channel + BM25
mechanism; the fact-graph node and `compute_fact_id` are deliberately thin
(5 frontmatter fields, no status/verifier_outcome/claim_summary in the node; the
**glossary is kept** — it makes the graph readable). Stores take **explicit
roots** — orchestration decides where worker/project directories live.

## API

```python
from core import LocalMemory, GlobalMemory, FactGraph

# local memory (per worker; root = the worker's own dir) — rough recall.
# No CLI wraps this: the worker reads/writes/greps its own files directly.
lm = LocalMemory(worker_dir)
lm.append("notes", {"thought": "..."}); lm.read("notes")

# global memory (shared; root = the project dir) — typed findings
gm = GlobalMemory(project_dir)
gid = gm.append("counterexample", claim="...", evidence="...QED",   # evidence required
                author="worker_xhigh", glossary={"X": "a manifold"})  # for verifiable kinds
gm.append("master_guidance", claim="...", evidence="GPT-5.5-pro: ...", author="main_agent")
gm.set_status(gid, "verified", fact_id="<id>")   # agent-driven status note
gm.read("plan"); gm.search("query", kinds=["dead_end"])

# fact graph low-level API (shared; production writes go through gateway fact_submit)
fg = FactGraph(project_dir)
fid = fg.add(problem_id="KMMP", author="KMMP_high", statement="...", proof="...",
             predecessors=["<id>"], glossary_introduces={"K_F": "canonical class of F"})
fg.undefined_symbols(statement="...", proof="...", predecessors=["<id>"])  # coverage check
fg.search("query")                        # statement-only ranked summaries
fg.context([fid], predecessor_depth=None, proof_mode="selected", max_chars=200000)
fg.get_raw(fid); fg.list(); fg.predecessors(fid); fg.glossary(); fg.descendants(fid)
fg.revoke(fid, reason="...")     # cascades to dependents
```

**There is no second promotion path.** Agents call the gateway's `fact_submit`;
it validates lazy context, invokes the verifier, atomically rechecks the graph,
and only then calls this low-level `fg.add(...)`. The core library deliberately
does not expose a separate `promote()` shortcut.

## Invariants the library enforces (mechanical only)

- Verifiable global-memory kinds require non-empty `evidence`.
- `fact_id` is content-addressed; identical content ⇒ identical id (dedup).
- `FactGraph.add` refuses unknown or revoked predecessors; `revoke` cascades to
  descendants.
- Project glossary terms cannot redefine global notation or change meaning while
  active. The project glossary is discovery-only: verifier context never treats
  it as an implicit premise. To inherit a project definition, declare its source
  fact as a predecessor; source revocation then cascades normally. Revoke rebuilds
  the discovery glossary from remaining active facts.
- `FactGraph.context` reports an explicit scope, completeness state, and digest;
  budgets omit whole records rather than returning partial facts.
- Append-only everywhere; status is an appended note folded at read.

Enforced by **prose**, not code: "global memory is awareness, never a correctness
source — a proof may only cite a `fact_id`"; "no handwave / chart-position refs."
(Symbol coverage *is* mechanical — `FactGraph.undefined_symbols`, run by
`fact submit` — but the prompt still tells the worker to define its symbols.)

## Test

```bash
python3 danus/core/tests/test_core.py
```

## Known follow-ups

- BM25 re-tokenizes per call → persistent index (sqlite FTS5),
  ranking preserved. Perf only.
- The derived board/index (fast cross-worker BM25 over `facts/`) — regenerate
  from `facts/` if/when needed; not stored as a separate truth.
