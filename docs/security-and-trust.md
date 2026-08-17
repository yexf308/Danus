# Danus — Security & Trust Model

Read this **before you rely on a Danus result** and before you deploy Danus on a
shared or networked host. It states, plainly, what you are trusting, how permission
is enforced, and where a human should stay in the loop.

## 1. The one thing to understand first: the verifier is an LLM, not a formal prover

Danus's entire notion of truth rests on the **verifier plus the locked graph
promotion**: a result becomes a fact only if the verifier returns `correct` and
the gateway successfully writes it, returning `promoted: true` and a non-null
`fact_id`. That verifier is a
**cold-start `codex` (LLM) judge**, not a formal proof assistant (no Lean/Coq/…),
and by default **there is no human in the loop.**

Consequences you must internalize:

- A `correct` verdict is a **strong LLM judgment**, not a machine-checked proof. It
  is far better than an unchecked draft, but it can be wrong.
- The mathematical findings are produced by the verifier agent. Production code
  then enforces the structural rule (`correct` ⟺ no critical errors **and** no
  gaps), rejects malformed/self-contradictory output, requires every final
  finding to quote one complete original candidate line with its source and
  1-based line number, and requires a server-side context-digest attestation.
  The launcher and gateway both compare that line code-point-for-code-point to
  the submitted statement/proof. This makes a rejection auditable and blocks
  invented or normalized evidence, but it does not prove that the verifier's
  interpretation of an authentic line is mathematically right; trust still
  ultimately flows from the verifier's reasoning.
- For a **high-stakes** result (a headline theorem you intend to publish or act on),
  **have a qualified human review it.** The write-paper pipeline re-checks the whole
  paper as written through a dedicated paper-math verifier, which raises confidence —
  but it is still LLM verification, not a formal certificate.

This is the system's single most important trust assumption. It is deliberate (the
whole point is to scale proof search beyond what a formal assistant can express
today), but it is on **you** to decide how much a `correct` verdict is worth for a
given result.

## 2. Permission is enforced by construction, not by prompts

Danus does not rely on agents "behaving". Every read/write to the truth stores goes
through the **gateway** (a role-gated MCP server), and **what a role can do is
exactly which tools it can even see** — ungated tools are physically absent from an
agent's tool surface, not merely discouraged.

The role table (`danus/gateway/roles.py`):

| role | tools it can see |
|---|---|
| **worker** | `gm_add`, `gm_get`, `gm_search`, `fact_submit`, `fact_search`, `fact_context`, `search_arxiv_theorems` |
| **main** (the orchestrator) | `gm_add`, `gm_get`, `gm_search`, `fact_search`, `fact_context`, `fact_revoke`, `search_arxiv_theorems` — **no `fact_submit`** |
| **verifier** | `search_arxiv_theorems` **only** (read-only) |

Load-bearing separations:

- **The orchestrator can never fabricate a fact.** `main` has no `fact_submit`;
  only a `worker` can submit, and only the verifier can accept.
- **The verifier is read-only.** It can look up literature; it writes nothing to
  the truth stores.
- **Fail-closed.** An unknown, mis-typed, or *unset* role falls back to the
  most-restrictive (verifier) set, so a misconfiguration cannot grant write
  access. The full dev set requires the explicit `DANUS_ROLE=all`.
- **Exact critic hydration is bounded.** `gm_get` accepts one canonical
  16-lowercase-hex id, rejects absent or duplicate ids, and caps the serialized
  record at 16 KiB. A designated critic uses it for the exact root checkpoint;
  BM25 `gm_search` is discovery and cannot authorize confirmation.

## 3. The write-gate

The single path a fact enters truth is a worker's `fact_submit`, which is a
state machine, not a suggestion:

1. take a read-only, linearizable glossary snapshot and reject any already-known
   project/global definition conflict before candidate admission or paid
   verification; a malformed glossary also fails closed at this boundary;
2. load the complete transitive ancestor statement/edge/fact-local-definition
   closure and selected immutable definitions, with no ancestor proof in round
   zero, and block missing, revoked, incomplete, or over-budget context;
3. call the verify service with the statement, proof, and complete authoritative
   fact context; both context and returned verdict are checked by deterministic
   schemas, not only prompts, and the service must attest the context digest;
4. if the verifier needs exact ancestor proofs, allow only strict closure ids and
   hydrate their whole canonical records within bounded fresh-session rounds;
   Graphify/discovery never participates in completeness, hydration, digest, or
   judgment;
5. after final verification, rebuild the exact expansion snapshot under the graph's
   cross-process mutation lock;
6. only after a `final/correct` verdict and unchanged locked snapshot, repeat the
   glossary conflict check and attempt the fact add; revoke and concurrent
   definitions use the same mutation lock, while storage errors remain explicit
   verified-but-not-promoted outcomes;
7. durably attempt to trace every adaptive round and the final verdict to global memory (accept, reject,
   or write-failed). A trace I/O failure is returned explicitly as `trace_error`
   without hiding an already-written fact id.

If the verify service is unreachable, `fact_submit` returns a clean error and
writes nothing — nothing is silently accepted.

If a crash leaves a reasoning-first candidate as `outcome_unknown`, its live
overlay has no TTL and no elapsed-time inference. Workers must stop all retry and
resubmission of that exact candidate. Only the owner-only `resolve-candidate`
transition, bound to the exact receipt/source slot and checked against current
FactGraph identity, may release it.

### Browser advisor output is untrusted strategy, never control or truth

`chatgpt_pro_browser` is an explicit owner-only handoff to the ChatGPT Pro web
UI. Danus repository code only maintains a private exact-CAS receipt database;
it never launches Chrome, calls an API/model, or lets a worker/verifier/cost gate
trigger the handoff. The older `gpt_pro` transport remains the paid API.

The active main agent may create a bounded `advisor_checkpoint` and local
`prepared` receipt only from the exact current coordinator recommendation derived
from the fixed root obstruction and independent critic confirmation. Broad
evidence that routes are blocked, dead-ended, slow, expensive, or near exhaustion
is insufficient. It then stops for the owner's exact-question authorization.
Timers and unattended loops cannot trigger it; preparation grants no Chrome or
transmission authority.

The broker rejects secret-shaped outbound prompts, binds authorization to the
exact prompt/project/context, and fences the click boundary durably. A lost or
replayed dispatch acknowledgement does not authorize another click. An outcome
that may have crossed Send is reconciled against the same visible conversation
or owner-abandoned with an explicit risk receipt; it is never automatically
resent. The visible `chatgpt.com` conversation URL is validated only at command
input (prefer file/stdin, never argv/history) and persisted only as a SHA-256
digest. Completed response and clarification plaintext is likewise never stored
in the project: the receipt contains only SHA-256, UTF-8 size, attestations, and
bounded control-signal names. Import requires the owner to resupply the exact
bytes from the same conversation and returns them transiently.

Same-conversation follow-up lineage is local and digest-bound: a new request
may name only a same-project/context known-terminal Danus predecessor and must
resupply its exact URL transiently at prepare and fresh dispatch. The ledger
stores the predecessor receipt/state and URL hash, not the URL. Lineage grants
no inherited authorization, so every evidence-specific follow-up needs a new
prompt hash, owner decision, and one-shot dispatch. Unknown, stale, external, or
wrong-URL predecessors are not represented as locally verified lineage.

A completed page is imported with `trust=untrusted_strategy`, an empty authority
set, and `eligible_for_master_guidance=false`. Page text cannot invoke
`fact_submit`, `fact_revoke`, verifier/worker controls, shell/scripts, secrets,
finalization, or publication. The main agent/owner must review and synthesize
strategy-only text and record an explicit `adopt` transition. Only that adopted
text has bounded `consult_provenance` acceptable on a `master_guidance` entry.
The gateway permits `master_guidance`, `elaboration`, and `advisor_checkpoint`
only for its runtime main/all role (an author string cannot spoof it). Browser
guidance additionally must match an actual adopted same-project broker row and
the guidance evidence hash must equal the adopted synthesis hash;
even then it remains awareness, never FactGraph truth. Browser subscription
telemetry is exact null (`model`, `usage`, `cost_usd`), not a fabricated estimate.
Import, adoption, and `master_guidance` do not release a coordinator in
`owner_action_required`. Resuming that generation requires an audited owner-only
`resolve-recommendation` exact CAS with matching recommendation-id and explicit
paid-resume acknowledgements. Adopted guidance must link that recommendation and
browser provenance must bind its adopted same-project receipt. Continuing
without guidance fails closed while the recommendation-bound browser request is
active, ambiguous, or completed but not imported. Stable conversation context
and per-intervention recommendation identity remain separate.
Before every `gm_add`, the gateway also compares each durable claim/evidence and
nested glossary/link string digest against that project's browser reply and
clarification digests. Exact raw matches fail closed for every global-memory kind;
only evidence already authenticated as the exact adopted synthesis is exempt.
The gateway does not guess whether a paraphrase is semantically "close": review
and synthesis are an explicit trusted-main responsibility. Unrelated API/manual
guidance remains valid. Browser digest registration and every sanctioned gateway
global-memory append share a cross-process fence under the loaded trusted Danus
release's fixed `runtime/advisor-control`, outside all worker writable roots.
Neither environment nor CLI/MCP input can redirect the authority root. The
fence is keyed by canonical project path plus filesystem identity and is checked
with no-follow, owner, mode, link-count, and inode guards. A worker-replaceable
project-local lock is never trusted. The underlying `GlobalMemory` class remains
a host-internal storage primitive; agent writes must go through the gateway.
See `browser-advisor.md` for the state and recovery contract.

Rolling verifier upgrades fail closed before paid work. The service captures its
output schema, AGENTS contract, and verifier skills once at import, checks the
schema version against the loaded validator, and serves their combined digest on
`/health`. The gateway requires protocol 3 plus that digest before POST and
echoes them into `/verify`; missing/mismatched handshakes are rejected before a
result directory or Codex process exists. Because each verifier's MCP gateway is
a fresh Python process, an editable checkout is still not an immutable code
deployment: production must use one pinned wheel/image and restart services to
upgrade.

The response separates mathematical acceptance from publication. A glossary
conflict present at preflight makes zero verifier calls and returns no
mathematical verdict; repair the definition and submit the changed identity for
a fresh check. The read-only preflight cannot authorize a later write, so a
definition introduced concurrently after it is still caught by the locked
promotion CAS. The legacy
`accepted` field means the verifier returned `correct`; the authoritative
end-to-end signal is `promoted: true` together with a non-null `fact_id`.
Post-verification glossary races, stale context, and storage failures preserve
`verification_verdict: "correct"` for repair diagnostics but return
`promoted: false`, `submission_status: "verified_not_promoted"`, `fact_id: null`,
and `write_error`. If storage cannot durably establish whether an uncertain
commit will be preserved or rolled back after a crash, the response instead has
`promoted: null` and `submission_status: "promotion_unknown"`. Workers and
monitors must not count either outcome as a fact.
When reading an older response without `promoted`, only a valid non-null
`fact_id` is a safe success fallback.

## 4. The verifier is read-only and ephemeral — still isolate readable secrets

The verifier uses a read-only shell sandbox, an ephemeral session, no user
config/rules, stdin prompt transport, and a schema-constrained CLI output file.
Worker execution remains separately configured and may be more permissive. Danus's
host-level safety still rests on two assumptions you must uphold:

Each worker round first writes and verifies a unique project-root marker in its
exact model cwd. Both worker transports pass that marker through Codex's
`project_root_markers` override with strict config parsing, so project config
discovery cannot reach the repository root's main-only MCP servers. Marker or
config failure stops before dispatch. The round then passes its complete required
worker gateway server object directly on the Codex CLI. The project-local
`.codex/config.toml` is retained for inspection, but is not a production trust
dependency; failure to discover that file cannot silently remove the worker's
gateway tools.

- **The agent home is trusted.** The verifier runs inside a fixed `AGENT_HOME`
  (its contract + skills). Treat that directory — and the worker/verifier prompts and
  skills — as **trusted code**: a malicious or tampered prompt/skill could act with
  the privileges of the process.
- **The host is isolated / disposable.** A read-only Codex sandbox prevents model
  writes, not reads of every host path. Run adversarial verification and autonomous
  workers on a dedicated VM/container/pod or low-privilege account without unrelated
  readable secrets, not a workstation holding sensitive data.

### Human hot-join is control input, never truth

With `DANUS_WORKER_TRANSPORT=app-server`, the operator can inject a native user
message into one exact active worker turn. The adapter uses local stdio only,
checks the installed Codex protocol schema before model spend, and writes the
message/receipt ledger under the project parent rather than the worker's
workspace-write root. Delivery is bound by `expectedTurnId` and a stable client
id; an acknowledgement-lost crash is marked `delivery_unknown` and is not retried
automatically.

`danus encourage <project>/<worker> [--text ... | --file P | --stdin] [--client-id ID]`
is the narrower morale-only channel. It requires an
authoritatively live worker plus exactly one canonical `started` paid intent,
snapshots that intent's thread and turn ids atomically, and always uses fail-only
delivery. A terminal-to-next-turn race therefore records failure before any
later-turn steer; the note is never queued and the command cannot create or
start a paid turn. Omitted note input uses the built-in encouragement. Danus
does not invoke this command automatically and makes no claim that encouragement
causes a reasoning improvement. Its client-id digest commits the note, target,
and expected thread/turn; the same key conflicts after a turn change. A lost RPC
acknowledgement may remain `delivery_unknown`, but is never retried or queued.

For this transport the actual model cwd is `<worker>/model_workspace`, not the
worker control directory. Host PID/role/status/log files and the canonical
SQLite lifecycle audit stay outside the declared writable roots. The requested
model and effort come from protected project metadata, not the model-writable
`.role` projection. Codex app-server remains experimental; generated-schema and
runtime attestations therefore fail closed rather than guessing across protocol
versions.

In reasoning-first mode, paid task authority also stays in the protected
coordinator. `danus assign` stages the exact current- or next-generation bytes
before refreshing the host `TASK.md`; admission copies the frozen assignment
into the slot, binds its digest into the kickoff prompt, and materializes the
model-workspace `TASK.md` from that slot snapshot. Neither `TASK.md` projection
can retarget paid work after the binding. Owner recommendation resolution
requires complete next-generation paid-lane staging and freezes it atomically;
an advance with no owner gate carries the prior frozen set forward exactly.

Reasoning telemetry is content-free and projected only from each paid app-server
thread. It is diagnostic, not proof, correctness, liveness, admission, or browser
authority. Missing or unsupported signals are `unavailable` or `partial`, never
fabricated as zero. The protected audit distinguishes live-stream reroute
observations from post-crash unknown history, and labels token usage as observed
rather than schema-attested final. A recovered prior paid turn with unavailable
reroute history is quarantined with automatic retry disabled; it is never
silently accepted or duplicated.

In `reasoning_first_v1`, each new terminal coordination slot normally starts a
fresh app-server thread; only crash recovery of that same fixed root, critic, or
explorer slot resumes its exact thread. Dormant observers are not
rotation/failover. The long
history boundary below therefore applies to same-slot recovery and explicitly
legacy app-server continuation, not to ordinary new terminal slots. Codex 0.147
supports a bounded `thread/read(includeTurns=false)` status check, but its
`thread/resume` response always contains all turns. If that response exceeds the
8 MiB JSONL limit, Danus records a pre-dispatch failure, sends no `turn/start`,
and preserves the preceding paid outcome separately in status. It never raises
the transport bound or silently discards the conversation. The owner may use
the CAS-fenced `rotate-thread` command to explicitly accept conversation loss;
unfinished or delivery-unknown paid intents block rotation, and all
FactGraph/global/local research memory remains untouched. Rotation is permitted
only after worker fail-stop: live workers and busy lifecycle locks are rejected,
while the PID identity/liveness check and SQLite mapping CAS remain serialized
with `start` on the worker's `.pid.lock`.
`reset-thread` has the same fail-stopped/live/busy lifecycle boundary and lock;
neither mapping operation can race worker spawn or PID registration.
If oversized resume meets a `dispatching`, `started`, or `delivery_unknown`
paid intent—or the intent ledger cannot be read safely—Danus emits no rotation
argv and preserves the ambiguity for explicit owner reconciliation. An owner who
cannot reconcile may use `abandon-intent` only with exact
target/thread/client/state, a nonempty reason, and explicit acknowledgement that
the paid outcome is unknown. The command requires worker fail-stop under the
same lifecycle lock, atomically appends an operator receipt plus terminal event,
and retains all prior message/delivery/round/audit and research data. It never
converts that incident into a fresh paid turn: the abandoned thread stays fenced
until a separate reset or rotation succeeds.
The exact CAS values are public only through the read-only `status --json`
projection `unfinished_paid_intent`; inability to read it yields a separate
`intent_ledger_error`, never inferred state. While the worker is authoritatively
live, `prepared`, `dispatching`, and `started` project as
`paid_intent_status="in_progress"` with no `recovery_required`; live
`delivery_unknown` is labeled `outcome_unknown_while_worker_live`, also with no
abandon argv. Recovery is surfaced only after fail-stop (or an unsafe PID
identity) and remains guarded by the command's lifecycle recheck. For a
fail-stopped worker, `prepared` is authoritatively
pre-dispatch and recommends resuming the immutable intent, not accepting an
unknown paid outcome. If immutable configuration drift makes that resume
impossible, the owner may instead run the exact status-provided
`cancel-prepared-intent <target> --thread-id ID --client-id ID --reason TEXT`.
It is an exact `prepared` CAS under the same fail-stopped lifecycle lock,
requires no paid-risk acknowledgement, appends an operator receipt, preserves
all prior ledgers, and leaves reset/rotation as a separate subsequent action.

Paid worker subprocesses run below a retained owned-child host rather than as
detached unowned sessions. The host retains a worker-liveness pipe and the
worker's `.paid.lock` until the complete Codex/MCP group is terminal and reaped.
Owner death therefore revokes the group, and an immediate replacement cannot
start overlapping paid work. External lifecycle commands never act on an
inspected numeric PID/PGID; `--force` is a durable cooperative request.

Ordinary `say` input can change research direction, but cannot grant correctness
or write authority. Encouragement is narrower still: its envelope tells the
worker to treat the quoted text as morale support only, never as a task,
coordination directive, mathematical evidence, proof step, fact, verification,
or permission to change scope. The ledger/transcript has no direct channel into
FactGraph contexts,
context digests, or verifier prompts. If a worker incorporates human-supplied
mathematics into a candidate statement or proof, that candidate text is sent to
the verifier through the normal `fact_submit` path. Direct `userMessage` protocol records are excluded from
round audit logs, but bounded agent responses are retained and may quote or
paraphrase guidance; treat worker logs as research records, not a confidentiality
boundary. `fact_submit` and its adaptive verifier remain the only truth write
path. Only the typed `interrupt-turn` control may call `turn/interrupt`;
natural-language text is never parsed into process control.

## 5. Network exposure: loopback by default

The two services bind **loopback only** by default:

- **verify** on `127.0.0.1:8091`,
- **dashboard** on `127.0.0.1:8099`.

Nothing is exposed to the network out of the box. To view the dashboard remotely,
use an SSH port-forward rather than binding a public interface. (`VERIFY_HOST` /
the dashboard `--host` can change the bind, but the safe default is loopback — do
not expose these to an untrusted network.)

## 6. Secrets: bring your own key, never committed

- All credentials (codex backend key, consult key, LaTeX-git token) live **only** in
  gitignored `config/*.env` files. The tree ships `*.env.example` placeholders only —
  **no working key is committed.**
- The codex backend key is **read at run time from an environment variable**; it is
  **not** written into any config file that Danus generates (e.g. the codex
  `config.toml` references the env var name, not the value).
- Before any commit, confirm `git status` shows no `config/*.env` and no `runtime/`.

## 7. The deterministic pre-checks (a safety net, and a caveat)

Before the LLM verifier runs, the verify service applies deterministic pre-checks
that **can only reject more**, never accept more: emptiness/vacuousness checks and a
few hard prohibitions (citing the problem statement as a source, unproven
conditional premises, vague "well-known" gestures without a citation).

**Caveat:** these prohibition patterns are **tuned to specific past incidents**
and are therefore domain-specific — the single most project-flavored part of the
verifier stack. They are safe (additive); keep, generalize, or disable them to fit
your domain.

## 8. What to trust, and what to double-check

- **Trust the shape:** the permission table, the write-gate, content-addressing,
  cascade revocation, and "no fact without a `correct` verdict" are enforced in code
  and are the well-tested part of the system.
- **Double-check the verdicts** on results that matter: a `correct` verdict is an
  LLM judgment. For a publishable headline result, add a human review; the
  write-paper verify gate helps but does not replace it.
- **Treat the worker/verifier prompts + skills as trusted code**, and keep the host
  isolated.
