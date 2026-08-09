# danus/execution — the worker swarm (round loop + scaffolding + layout)

Where autonomous `codex` workers actually prove. This module owns the on-disk
**layout**, project/worker **scaffolding**, and the per-worker **round loop**. The
`danus` CLI (`danus/orchestration`) is a thin UX layer over this; the real lifecycle
lives here.

```
danus/execution/
  layout.py     paths + names; WorkerLayout; parse_roles("high:3,xhigh:4")
  scaffold.py   do_new (project + worker dirs, .codex config, symlinks), spawn_loop
  loop.py       the round loop: kickoff prompt, run_round, stop conditions, status
  __main__.py   `python -m danus.execution <worker_dir>` → loop.main
  tests/{test_execution.py, test_loop.py}
```

## On-disk layout (`layout.py`)

`<agents_root>/<project>/` holds the shared `global_memory/` + `fact_graph/` +
`project.json`; each `workers/<worker>/` is a codex cwd with `AGENTS.md` →
`agents/contracts/worker.md`, `.agents/skills` → `agents/skills/worker`, a
`.codex/config.toml` (MCP = `python -I -B -m danus.gateway`, `DANUS_ROLE=worker`,
`DANUS_VERIFY_URL`, `tool_timeout_sec=3600`, tool approval `approve`,
`required=true`) for interactive inspection, `TASK.md`, `local_memory/`, and the
control files (`.status.json` `.pid` `.stop` `logs/`). `agents_root` =
`DANUS_AGENTS_ROOT` (default `runtime/projects`).

## The round loop (`loop.py`)

A **round = one Codex turn** that resumes from persisted
memory (NOT one increment). Launched detached in its **own process group**
(`start_new_session`), so it survives your shell. Stop conditions checked at the
round boundary: `.stop` flag,
`.run_deadline`, `DANUS_MAX_ROUNDS` (0 = unlimited), `DANUS_MAX_CONSEC_FAILURES`
(5). Config read at call time (`DANUS_ROUND_HARD_TIMEOUT` 4h, `DANUS_ROUND_BEAT` 5s).
`.status.json` is written atomically. **Resumability is continuity in the stores**,
not process state — a fresh `start` rebuilds context from memory + the fact graph.
`stop --force` is also a durable request, not an external numeric-PID signal:
the worker observes it during an active round, interrupts an app-server turn or
asks its retained owned-child host to terminate the complete Codex/MCP group,
audits the outcome, and exits. The host retains a worker-liveness pipe; abrupt
worker death closes that pipe and triggers the same group cleanup. It also holds
the worker's `.paid.lock` until the group is terminal and reaped, so an immediate
replacement worker cannot overlap a second paid launch.

Two transports are available. `DANUS_WORKER_TRANSPORT=exec` is the compatibility
default. `DANUS_WORKER_TRANSPORT=app-server` uses Codex app-server over local
stdio, persists each worker's Codex thread id, and enables owner hot-join via
`danus say`. It is a thin transport change: Codex still owns the thread/turn and
the existing contract, skills, MCP gateway, FactGraph, and adaptive verifier are
unchanged. Before any paid turn it generates the installed binary's protocol
schema and fail-closes unless exact `turn/start`, `turn/steer`, idempotency, token
usage, and terminal-event contracts are present.

The model runs in `<worker>/model_workspace`; host-owned PID, role, status,
logs, and the hot-join database remain in the parent worker/project control
directories and are not app-server writable roots. `TASK.md` is projected into
that workspace for each turn, while the protected `project.json` fixes the paid
model, effort, and author. Canonical lifecycle/model/token audit events live in
the protected SQLite ledger; the worker round log is only a bounded projection.
Token totals are labelled observation-only, not schema-attested final. Exact-turn
`model/rerouted` notifications cause a fail-stop; after an adapter interruption,
a recovered prior turn is also quarantined because historical reroute events are
not replayable. Its terminal evidence is retained and the paid turn is never
blindly resent.
Known protocol/configuration/authentication/delivery failures and non-completed
terminal outcomes are audited and fail-stop rather than automatically retried.
The configured multi-round contract is unchanged: rc 124 remains a normal
round-boundary continuation. Before continuing a persisted terminal thread,
the adapter performs bounded `thread/read(includeTurns=false)` state attestation.
Codex 0.147 has no bounded `thread/resume`; if its mandatory full history exceeds
the 8 MiB JSONL ceiling, that next host attempt stops before `turn/start`.
`.status.json` separates `last_paid_turn` (including the preceding timeout) from
`last_attempt` (the pre-dispatch resume failure) and supplies an explicit
owner-only `rotate-thread` recovery argv. Rotation is never automatic and does
not touch FactGraph, global memory, or local memory. It is valid only after the
worker has fail-stopped: a live worker or busy lifecycle lock is rejected, and
the PID check plus mapping CAS share `start`'s `.pid.lock`.
The same fail-stopped/live/busy rule applies to `reset-thread`; neither mapping
operation can race the spawn-to-PID-registration window.
An oversized resume with a `dispatching`, `started`, or `delivery_unknown`
intent exposes no rotation recovery: the intent remains unchanged and status
requires explicit owner reconciliation. If reconciliation is impossible, only
the fail-stopped owner may run `abandon-intent` with the exact
target/thread/client/state, a reason, and
`--acknowledge-paid-outcome-unknown`. Its atomic terminal transition and
append-only operator receipt never resend or delete history, and fence that old
thread from another paid dispatch until a separate `reset-thread` or
`rotate-thread` CAS succeeds. An unreadable intent ledger fails closed in the
same state, with no new paid turn or automatic retry.
The owner obtains the exact CAS inputs from `status --json`'s canonical
`unfinished_paid_intent`; a failed read is surfaced separately as
`intent_ledger_error`. Status emits an `abandon_intent` command skeleton only
for ambiguous states. A `prepared` intent is proven pre-dispatch and instead
offers `resume_prepared_intent`/`start` with the same prompt hash, model, and
effort. If that immutable configuration has drifted, status also supplies
`cancel-prepared-intent` with exact thread/client ids and an owner reason. This
exact `prepared` CAS needs no unknown-paid-outcome acknowledgement, runs under
the same fail-stopped lifecycle lock, appends a receipt, preserves every ledger,
and requires a separate later reset or rotation.
Codex app-server is still an experimental protocol, so schema drift fails closed
and the legacy `exec` transport remains the explicit rollback path.

Human messages live in `<project>/.human-intervention/events.sqlite3` (mode 0600,
parent mode 0700), outside each worker's workspace-write root. A single JSONL
reader preserves notification/response ordering. The broker durably records
`persisted → routing → steer_accepted → turn_completed`; an ack-lost crash becomes
`delivery_unknown`, never an automatic paid resend. A queued message is steered
into the next active turn. Only the typed `interrupt-turn` command calls
`turn/interrupt`; text such as “stop” is ordinary guidance. Direct
`userMessage` records and raw app-server streams are excluded from round logs;
the bounded final agent response is retained for research audit and may itself
quote or paraphrase guidance, so worker logs are not a confidentiality boundary.
Bounded `fact_submit` tool projections carry `promoted`, `submission_status`,
`verification_verdict`, and `fact_id`; monitoring treats only promotion plus a
valid fact id as publication. For an older gateway response without `promoted`,
the projection derives success from the valid fact id, never from legacy
`accepted` alone.
The ledger/transcript has no direct verifier channel. If a worker explicitly
uses human-supplied mathematics in a submitted candidate, that statement/proof
still goes through the ordinary verifier like every other candidate.
Before any worker Codex process starts, a separate interpreter probe imports the
gateway server and FastMCP runtime; a failed import ends the worker without a
Codex call. Every production round also injects the entire
`mcp_servers.danus={command,args,env,tool_timeout_sec,default_tools_approval_mode,required}`
object as one CLI `--config` value, with `command` pinned to the loop's exact
`sys.executable` and `-I -B` isolation. Therefore execution does not depend on Codex auto-loading the
worker's `.codex/config.toml`; the inline gateway is required and fail-closed.

## Connects to

Reads `TASK.md` (from `danus assign`) + `master_guidance` (strategy). Writes facts
only via a worker's `fact_submit` (gateway → verify). The loop itself never writes
the truth stores. The compatibility `exec` transport requests JSONL output and
attributes status only from an explicit completed `danus.fact_submit` result with
`promoted: true` and a valid fact id; arbitrary log text, fact context, and diffs
are never treated as publication evidence.

## Tests

`python -m pytest danus/execution/` (offline; a fake codex stub drives the loop /
stop / scaffolding).
