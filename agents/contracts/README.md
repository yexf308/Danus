# contracts/ — agent root contracts

The standing system prompt each agent **tier** reads at the top of every session —
the binding operating protocol, distinct from the on-demand skills under
`agents/skills/`. These are data (markdown), not code.

| File | Tier | Reads / writes |
| --- | --- | --- |
| `main_agent.md` | main agent (codex) | reads global memory (`gm_search` / exact bounded `gm_get`) + fact graph (`fact_search` / `fact_context`); writes actual consulted `master_guidance` / `elaboration` / exact-recommendation bounded `advisor_checkpoint` (`gm_add`); `fact_revoke`; high-autonomy orchestration. NO `fact_submit`. |
| `worker.md` | codex worker | local memory (private) · global memory (`gm_add` / exact bounded `gm_get` / BM25 `gm_search`) · fact graph (`fact_search` / `fact_context` / `fact_submit`); the adaptive proving loop. In reasoning-first mode, each new terminal coordination slot gets a fresh app-server thread, only same-slot crash recovery resumes, and the visible `TASK.md` is a projection of the slot's durable generation-task snapshot. Loaded per paid turn via the worker home's `AGENTS.md` symlink. |
| `verifier.md` | codex verifier (verify service) | judges a full candidate proof against an adaptive statement-closure context; emits either strict `final` verdict JSON or a bounded `needs_context` request for exact strict-ancestor proofs; called by `fact_submit`; read-only (only `search_arxiv_theorems`); the CLI captures its schema-constrained last message. |

codex, the primary main agent, reads its condensed contract from
the repo-root `AGENTS.md`; `main_agent.md` is the full contract and single source
of truth (the two must not contradict).

## The shared spine

Consistent across all three tiers:

- **The fact graph is the one source of truth** — a content-addressed DAG of
  verifier-accepted facts.
- **A fact enters only through `fact_submit`** (verifier-gated).
- **The verifier is the sole authority on correctness** — `correct` iff zero
  `critical_errors` AND zero `gaps`; no peer/LLM opinion substitutes.
- **Global memory** (incl. `master_guidance`) is shared awareness/strategy, never
  a correctness source — a proof builds only on `fact_id`s.
- **The shared stores change only through the sanctioned MCP tools**, never by hand.
- **Reasoning-first paid work is one fixed root plus one fixed critic.** Dormant
  observers consume no paid turns and are not an automatic rotation/failover
  pool; 2700 seconds caps one paid turn, not the whole phase. Paid assignments
  are generation/slot-bound snapshots, not mutable host `TASK.md` authority.
- **Known glossary conflicts fail before paid verification.** Promotion repeats
  the check under the graph lock because the read-only preflight cannot authorize
  a concurrent write.
- **Human encouragement is current-turn-only and non-authoritative.** It may
  support persistence but is never task/coordination input, mathematical
  evidence, a fact, or verification.

## Who binds to these files

- `danus/gateway` — the exact MCP tool set + role gating (`main` has no
  `fact_submit`; worker/main can read explicit lazy `fact_context`; `worker` adds
  submit; `verifier` is read-only (`search_arxiv_theorems` only)).
- `danus/core` — the three-memory data model, the global-memory `kind`s, `fact_id`,
  the global glossary. The contracts are the human-readable statement of that model.
- `danus/verify` — `verifier.md` **is** the verify service's system prompt; its
  P1/P3/P5/P6 prohibitions pair with the server's single-line prechecks (and are
  the sole enforcement wherever those prechecks are off).
- `danus/execution` — loads `worker.md` per round (worker home `AGENTS.md`
  symlinks to `agents/contracts/worker.md`); the worker reads the slot-bound
  model-workspace `TASK.md` + `master_guidance`.
- `agents/skills/worker` & `agents/skills/verify` — the contracts reference `$…`
  skills by name; the reconciliation note in `worker.md` tells inherited skills to
  defer to this data model.
