# Danus — Architecture

Danus turns the Rethlas single-problem proof engine (a codex agent plus proving
skills, inherited rather than redesigned) into a multi-agent, long-running,
strategy-steered research system, and renders verified results into papers and
human progress reports. This is the as-built map: the layered model, the folder
layout, the invariants, and the pinned cross-module contracts.

For the main agent's operating contract, see `AGENTS.md`
(→ `agents/contracts/main_agent.md`).

---

## 1. Layered model

```
operator → ① orchestration (main agent + danus CLI)   — conducts, never does math
              ② strategy   (elaboration → consult → master_guidance)
              ③ execution  (durable admission: ≤1 root + ≤1 independent critic paid turn;
                            generation/slot-bound tasks; dormant roster loops wait without codex)
   gm_* │         │ fact_submit
        ▼         ▼
   ⑤ truth      ④ verification (one paid leader; FIFO distinct queue; exact coalescing/cache;
                                cold-start judge; correct ⟺ no critical_errors AND no gaps)
   (fact graph + memory)   — correct verdict + locked context CAS/add
        ▲
        │ every read/write goes through …
   ⑥ gateway (role-gated MCP: 8 tools; main has NO fact_submit; verifier read-only)

cross-cutting: ⑦ observability (content-free reasoning/tool/wait telemetry · dashboard · theorem-search · human-summary · initialize)
               ⑧ ops/runtime (bootstrap · services · doctor · config)
bottom (inherited, don't redesign): Rethlas proof core = codex + worker proving skills
output: write-paper (publication) · human-summary (progress report) — each rendered by an isolated codex
```

---

## 2. Folder layout

```
Danus/
├─ ARCHITECTURE.md              this file (map + invariants + interface contract)
├─ README.md   pyproject.toml   top-level intro + the installable `danus` package
├─ .gitignore  .codex/          MCP wiring (`config.toml`): the `danus` gateway + the `write-paper` and `human-summary` services
├─ config/                      env templates (BYO key; only *.env.example committed)
├─ danus/                       THE ENGINE (installable Python package)
│  ├─ core/                     ⑤ truth: schema · factgraph · global/local memory · bm25 · glossary
│  ├─ gateway/                  ⑥ role-gated MCP: 8 tools · role table (roles.py)
│  ├─ verify/                   ④ verification HTTP service · prechecks · cold-start codex launcher
│  ├─ execution/                ③ worker swarm: round loop · project/worker lifecycle + layout
│  ├─ strategy/                 ② consult gateway + explicit durable browser-advisor broker
│  ├─ orchestration/            ① the `danus` CLI verbs
│  ├─ integrations/             arXiv theorem search (Matlas)
│  ├─ observability/            read-only dashboard
│  ├─ authoring/                shared main-only renderer primitives: the one-shot isolated codex driver + common helpers
│  ├─ write_paper/              write-paper MCP service: assembler + tools (drives `danus.authoring`)
│  └─ human_summary/            human-summary MCP service: scrubbing assembler + tool (drives `danus.authoring`)
├─ agents/                      AGENT CONTRACTS + CODEX-FACING SKILLS (data, not Python)
│  ├─ contracts/                main_agent.md · worker.md · verifier.md
│  └─ skills/
│     ├─ worker/                9 proving skills (inherited from Rethlas)
│     ├─ verify/                3 verify skills
│     └─ write-paper/           paper role prompts + house style (embedded by the write-paper MCP)
├─ .agents/skills/              MAIN-AGENT SKILLS (codex auto-discovers; → .claude/skills/)
│  ├─ elaboration/  consult/  human-summary/  initialize/
│  └─ write-paper/              the recipe SKILL.md + driver/ scripts + templates/
├─ bin/                         thin wrappers: danus · danus-mcp · write-paper-mcp · human-summary-mcp · codex · consult · consult-browser
├─ scripts/                     bootstrap · doctor · services · env · setup/check-codex · start-verify/-dashboard · recover · install-tex
├─ docs/                        human docs: getting started · concepts · operating guide · security & trust · …
└─ examples/                    unattended-ops examples + a toy project
```

---

## 3. Design invariants (must not regress)

1. Three memory tiers, one correctness boundary: local (private) → global
   (shared awareness) → fact graph (the only truth). A proof may build only on
   `fact_id`s; global memory is never a correctness source.
2. Permission is enforced by which tools a role can even see (the gateway role
   table), not by prompt convention. `main` cannot `fact_submit`; `verifier` is
   read-only.
3. The verifier is the sole mathematical authority, but `correct` is necessary,
   not by itself sufficient for a write. `fact_submit` also rechecks the exact
   context and adds under the graph mutation lock. Before candidate admission or
   paid verification, a read-only linearizable glossary preflight rejects known
   conflicts; promotion repeats the glossary check under the lock because the
   preflight is not write authority. The compatibility field
   `accepted` reports verifier acceptance; only `promoted: true` plus a non-null
   `fact_id` reports end-to-end publication. Stale context or another write
   failure preserves `verification_verdict: "correct"` but returns
   `submission_status: "verified_not_promoted"` and no fact id. If an fsync
   failure makes both commit and rollback crash outcomes possible, the gateway
   instead returns `promoted: null` and `submission_status: "promotion_unknown"`;
   it never misstates that ambiguity as a definitive failed promotion.
4. Content-addressed, cascade-revocable fact graph. `fact_id` hashes content
   (problem_id + predecessors + glossary_introduces + statement + proof);
   `external_refs` is deliberately excluded so the paper pipeline can rewrite
   citations without breaking the DAG.
5. Lazy context is fail-closed: discovery is statement-only; explicit hydration
   carries scope/completeness/digest metadata and only referenced immutable global
   definitions. Verification round zero sends the complete transitive ancestor
   statement/edge/fact-local-definition closure and no ancestor proof. A bounded
   `needs_context` response can request exact strict-ancestor proofs; the gateway
   alone authenticates and hydrates whole canonical records, then starts a fresh
   session. The service attests every round digest, and the gateway atomically
   rebuilds the final expansion snapshot before writing under the graph mutation
   lock.
6. Autonomy and resumability. Workers run detached; durable memory preserves
   verified work across crashes. In `reasoning_first_v1`, one root and one critic
   are fixed for the generation; dormant observers are not automatically rotated
   or promoted. Every new terminal coordination slot receives a fresh app-server
   thread, while crash recovery of that same slot resumes only its exact pinned
   thread. Its paid task is copied from the exact durable generation snapshot,
   hash-bound into the slot/prompt, and projected into the model workspace;
   mutable host `TASK.md` is not paid authority. Owner resolution requires and
   freezes complete next-generation task staging, while a no-owner advance
   carries the prior frozen set forward exactly. The 2700-second bound caps each
   paid turn, not the whole phase.
7. Strategy consult is optional. It defaults to `off`, where the main agent
   dispatches from its own current shared-state synthesis. `gpt_pro`,
   `claude_api`, and `claude_code` are explicit attended opt-ins whose actual
   replies may become `master_guidance`. A late `chatgpt_pro_browser` intervention
   requires an exact current coordinator recommendation, then one bounded
   `advisor_checkpoint` and fresh owner authorization for its exact question.
   General blocked/dead-ended evidence alone is insufficient. It is never a
   timer/environment/unattended-loop transport. Repository code only records the
   durable handoff.
   Imported browser text is untrusted until reviewed and adopted as strategy,
   has no truth/control authority, and carries null subscription telemetry.
   Adoption does not release `owner_action_required`; an audited owner-only
   `resolve-recommendation` exact CAS—with repeated recommendation id and paid-
   resume acknowledgement—is required. A stable browser conversation context
   is distinct from the new recommendation id assigned to each intervention.
8. Portable and BYO. No hardcoded absolute paths, no committed secrets; keys come
   from gitignored `config/*.env` (templates committed as `*.example`).
9. Clean author context. Any agent that produces an artifact for an outside
   audience (a paper, a human report) is a fresh isolated codex fed a scoped,
   machinery-free prompt, never the orchestrator's own contaminated window. It
   cannot leak `fact_id`s or swarm vocabulary it never received.
10. Human hot-join does not cross authority boundaries. `say` is research
    direction, never truth or process control. `encourage` is narrower:
    non-authoritative morale support bound to the exact currently started paid
    turn, fail-only and never queued or turn-starting. Live paid intents are
    projected as in-progress without unsafe abandon advice; recovery appears only
    after fail-stop/PID-unsafe state.

---

## 4. Interfaces & ports — the coordination contract

> **Rule:** these rows are the seams where two modules meet. If a change touches a
> row, update both ends in the same change. Ports and contract shapes are pinned:
> one side must treat the other's contract as fixed, and must not change a port or
> interface unilaterally.

**Network ports (loopback — do not renumber):**

| port | service | producer → consumer |
|---|---|---|
| 8091 | verify `/verify`, `/health` | `danus.gateway` `fact_submit` → `danus.verify` (via `DANUS_VERIFY_URL`) |
| 8099 | dashboard | operator browser → `danus.observability` (read-only) |

**Cross-module contracts (both ends must agree):**

| contract | pinned shape | ends |
|---|---|---|
| MCP tool set + role gating | 8 tools; `roles.py` `ROLE_TOOLS` (worker/main get exact bounded `gm_get` and lazy `fact_context`; main has NO `fact_submit`; verifier read-only) | `danus.gateway` ↔ worker/main/verifier agents |
| MCP launch | `python -m danus.gateway` + `DANUS_ROLE` env | `danus.verify` launcher · worker `.codex/config.toml` · `.codex/config.toml` (main) → `danus.gateway` |
| verify HTTP | `GET /health` attests exact `{status,pid,instance_nonce,output_protocol_version:3,verifier_bundle_digest}`; `POST /verify {expected_verifier_instance_nonce,expected_output_protocol_version:3,expected_verifier_bundle_digest,statement,proof,glossary_introduces?,fact_context?}` → schema-v3 result plus bounded scheduler headers; every final finding carries an exact original candidate `{source,line,exact_line}` anchor, checked independently by launcher and gateway; supplied context must be complete and digest-attested; only `final/correct` with zero findings can authorize the locked write | `danus.gateway.fact_submit` ↔ `danus.verify` |
| reasoning-first coordination | new projects persist `reasoning_first_v1` and default to `max:2,high:5`; a protected, content-bounded SQLite CAS pins the two `max` workers as root and critic (no automatic rotation/failover), leaves five `high` observers dormant without paid turns, gives each new terminal coordination slot a fresh app-server thread, resumes only same-slot crashes, caps each paid turn at 2700 seconds without promising whole-phase completion, and stores the bounded task snapshot needed to bind the slot/prompt/model-workspace `TASK.md` to an exact generation-task digest; paid `assign` stages before host projection, owner resolution requires and freezes all `N+1` paid tasks, and ordinary advance carries the previous frozen set exactly; active candidates freeze admission/retask; an exact root obstacle/dead-end moves to `critic_obstacle_review`, where only the fixed critic can confirm it and emit an `owner_action_required` Pro recommendation with browser authorization false; explicit roles override the roster and legacy keeps `high:3,xhigh:4` | `danus.coordination` ↔ worker loop · gateway · CLI status |
| glossary preflight and promotion | one shared-lock `glossary_conflicts` snapshot rejects known project/global conflicts before candidate admission, active-exact reuse, or verifier spend; `add_if_context_unchanged` independently rechecks under the exclusive graph mutation lock | `danus.core.FactGraph` ↔ `danus.gateway.fact_submit` |
| exact-turn encouragement | `danus encourage <project>/<worker> [--text\|--file\|--stdin] [--client-id ID]` requires authenticated live PID plus canonical `started` intent, persists immutable expected thread/turn ids with `fallback=fail`, and never queues/starts paid work; envelope authority is morale only | orchestration CLI ↔ hot-join store/broker ↔ worker contract |
| fact id inputs | `problem_id + sorted(predecessors) + sorted(glossary) + normalized(statement,proof)`; **external_refs EXCLUDED** | `danus.core` ↔ everyone (write-paper reads `external_refs`) |
| global-memory kinds | the 12 `GLOBAL_KINDS` (incl. `master_guidance`/`elaboration`/`advisor_checkpoint`/`verification`) | `danus.core` ↔ agents · strategy · consult |
| consult receipt/envelope | API/CLI `{transport,reply,usage,cost_usd,…}`; browser exact-CAS digest-only completion, transient exact-byte import, then adopted synthesis/provenance with null telemetry; stable `context_id` names conversation lineage while per-intervention `recommendation_id` binds the open coordinator decision, receipt, and guidance link; gateway binds browser guidance to the same-project adopted row | `danus.strategy` CLI/broker ↔ owner consult skill ↔ `gm_add` |
| write-paper prompt assets | codex role prompts + style read from `agents/skills/write-paper/` (via `DANUS_WRITE_PAPER_SKILL_DIR`) | `danus.write_paper` assembler ↔ `agents/skills/write-paper/` |
| env-var contract | `DANUS_* / CODEX_* / VERIFY_* / CONSULT_*` names; the codex CALL + env (bin/model/effort/PATH/`exec` prefix) is resolved through the shared `danus.codex` launcher: neutral `DANUS_CODEX_BIN` / `DANUS_CODEX_MODEL` / `DANUS_CODEX_EFFORT` + per-service `DANUS_{VERIFY,WRITE_PAPER,HUMAN_SUMMARY}_{MODEL,EFFORT}` overrides | `danus.codex` + `config/` + `scripts/env.sh` ↔ every codex-exec site (`danus.execution.loop` · `danus.verify.launcher` · `danus.authoring.driver`) |
