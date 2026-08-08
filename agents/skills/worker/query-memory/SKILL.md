---
name: query-memory
description: Recall what is already known — your own prior reasoning, the swarm's shared findings (including dead ends and verifier feedback), and the verified facts — before doing new work. Use when prior conclusions, examples, dead branches, verification outcomes, or verified results may inform the current question, claim, subgoal, or branch decision.
---

# Query Memory

Before spending effort, check what already exists. There are three places to
look, in the three-memory model:

1. **Your own local memory** (private): read/grep `local_memory/notes.jsonl` and
   `events.jsonl` for your prior reasoning and what you already tried.
2. **Global memory** (shared findings): `gm_search(query, kinds=...)` over the
   swarm's findings. Especially useful kinds:
   - `dead_end` / `obstacle` — paths that already died (skip them);
   - `verification` — outcomes of others' `fact_submit` (learn from rejections);
   - `conclusion` / `example` / `counterexample` / `plan` — others' results to build on.
   You can also read the `global_memory/<kind>.jsonl` files directly.
3. **Fact graph** (verified truth): `fact_search(query)` (BM25 over the verified
   facts) to find results you can cite or that show your subgoal is already
   proved — it returns `{fact_id, statement}` only. Use `fact_context` with
   explicit ids for relations or ancestors and explicitly select proof hydration
   on a relevant hit. A proof may build **only** on facts (cite a `fact_id`).

## Procedure

1. Obey the current prompt's restrictions first. If it forbids a direction, file,
   or search, that overrides default recall. If it recommends specific results or
   directions, raise their priority.
2. Start with the cheapest relevant source: your own local memory for your
   context; `gm_search` for the swarm's findings; the fact graph for verified
   building blocks.
3. Prefer a narrow, targeted query (specific `kinds`, a sharp query string) over
   reading everything.
4. **Workspace boundary:** stay inside your own working directory and the shared
   project stores. Do not scan parent directories, other workers' private
   `local_memory/`, or other projects.

## Retrieval priority

- A relevant **verified fact** (fact graph) is the strongest hit — you can build
  on it directly by citing its `fact_id`.
- A sibling's **`dead_end`/`obstacle`** saves you from re-walking a dead path.
- A sibling's **`verification`** rejection tells you why a similar claim failed.
- A `conclusion`/`example`/`counterexample` is awareness — useful, but **never a
  brick** (only facts are). Re-verify anything you intend to build on.

## Output

Note what you recalled and how you used it in your local memory (`events`). Do not
re-publish others' findings; just use them.

## Tools

- `gm_search` (recall shared findings; BM25 over global memory)
- `fact_search` (recall verified facts; BM25 over the fact graph — novelty + citation lookup)
- `fact_context` (lazy explicit-id relations/ancestors; proofs only when requested;
  require `complete=true` **and** check `scope.predecessor_depth`; pass `null` when
  your use requires the full ancestor closure)
- local memory is read directly (no tool — read/grep the files)
