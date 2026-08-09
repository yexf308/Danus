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
| **worker** | `gm_add`, `gm_search`, `fact_submit`, `fact_search`, `fact_context`, `search_arxiv_theorems` |
| **main** (the orchestrator) | `gm_add`, `gm_search`, `fact_search`, `fact_context`, `fact_revoke`, `search_arxiv_theorems` — **no `fact_submit`** |
| **verifier** | `search_arxiv_theorems` **only** (read-only) |

Load-bearing separations:

- **The orchestrator can never fabricate a fact.** `main` has no `fact_submit`;
  only a `worker` can submit, and only the verifier can accept.
- **The verifier is read-only.** It can look up literature; it writes nothing to
  the truth stores.
- **Fail-closed.** An unknown, mis-typed, or *unset* role falls back to the
  most-restrictive (verifier) set, so a misconfiguration cannot grant write
  access. The full dev set requires the explicit `DANUS_ROLE=all`.

## 3. The write-gate

The single path a fact enters truth is a worker's `fact_submit`, which is a
state machine, not a suggestion:

1. load the complete transitive ancestor statement/edge/fact-local-definition
   closure and selected immutable definitions, with no ancestor proof in round
   zero, and block missing, revoked, incomplete, or over-budget context;
2. call the verify service with the statement, proof, and complete authoritative
   fact context; both context and returned verdict are checked by deterministic
   schemas, not only prompts, and the service must attest the context digest;
3. if the verifier needs exact ancestor proofs, allow only strict closure ids and
   hydrate their whole canonical records within bounded fresh-session rounds;
   Graphify/discovery never participates in completeness, hydration, digest, or
   judgment;
4. after final verification, rebuild the exact expansion snapshot under the graph's
   cross-process mutation lock;
5. only after a `final/correct` verdict and unchanged locked snapshot, attempt the fact
   add; revoke uses the same lock, closing add/revoke races, while storage errors
   remain explicit verified-but-not-promoted outcomes;
6. durably attempt to trace every adaptive round and the final verdict to global memory (accept, reject,
   or write-failed). A trace I/O failure is returned explicitly as `trace_error`
   without hiding an already-written fact id.

If the verify service is unreachable, `fact_submit` returns a clean error and
writes nothing — nothing is silently accepted.

Rolling verifier upgrades fail closed before paid work. The service captures its
output schema, AGENTS contract, and verifier skills once at import, checks the
schema version against the loaded validator, and serves their combined digest on
`/health`. The gateway requires protocol 3 plus that digest before POST and
echoes them into `/verify`; missing/mismatched handshakes are rejected before a
result directory or Codex process exists. Because each verifier's MCP gateway is
a fresh Python process, an editable checkout is still not an immutable code
deployment: production must use one pinned wheel/image and restart services to
upgrade.

The response separates mathematical acceptance from publication. The legacy
`accepted` field means the verifier returned `correct`; the authoritative
end-to-end signal is `promoted: true` together with a non-null `fact_id`.
Glossary conflicts, stale context, and storage failures preserve
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

Each worker round passes its complete required gateway server object directly on
the Codex CLI. The project-local `.codex/config.toml` is retained for inspection,
but is not a production trust dependency; failure to discover that file cannot
silently remove the worker's gateway tools.

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

For this transport the actual model cwd is `<worker>/model_workspace`, not the
worker control directory. Host PID/role/status/log files and the canonical
SQLite lifecycle audit stay outside the declared writable roots. The requested
model and effort come from protected project metadata, not the model-writable
`.role` projection. Codex app-server remains experimental; generated-schema and
runtime attestations therefore fail closed rather than guessing across protocol
versions.

The protected audit distinguishes live-stream reroute observations from
post-crash unknown history, and labels token usage as observed rather than
schema-attested final. A recovered prior paid turn with unavailable reroute
history is quarantined with automatic retry disabled; it is never silently
accepted or duplicated.

Long terminal histories have a separate fail-closed boundary. Codex 0.147
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
`intent_ledger_error`, never inferred state. `prepared` is authoritatively
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

This input can change research direction, but cannot grant correctness or write
authority. The ledger/transcript has no direct channel into FactGraph contexts,
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
