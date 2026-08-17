# Danus — Operations Runbook

Day-to-day operation of a Danus deployment: the persistent services, health checks,
recovery after a restart, and unattended-operation helpers.

> In normal operation you do not run these commands yourself — you talk to the
> **main agent** (codex), and it runs them for you. This page documents what
> happens underneath, and doubles as your fallback for the moments the main agent
> is not there to act (a fresh host restart, a session that will not start,
> debugging the stack by hand).

## The persistent services

Two services must be managed via `scripts/services.sh`. It starts one persistent
guardian per service, so the service **survives your shell / SSH session ending**
and remains controllable through an authenticated local channel. Start them only
this way.

```bash
bash scripts/services.sh up verify            # REQUIRED — no verify ⇒ fact_submit fails ⇒ no facts
bash scripts/services.sh up dashboard <p>     # optional read-only view of project <p>
bash scripts/services.sh status               # what's up (+ a verify /health probe)
bash scripts/services.sh logs <svc>           # bounded last-50-lines snapshot
bash scripts/services.sh down <svc> | all     # stop
```

- **verify** — `127.0.0.1:8091`. The correctness gate. Must be up before starting
  any workers.
- **dashboard** — `127.0.0.1:8099`, read-only. View it via an SSH port-forward
  (do not expose it to a network).

> **Shared-host caveat.** These ports are per-host, not per-deployment. If a second
> Danus deployment (another user/checkout) is already bound to `8091`, your
> `services.sh up verify` will **fail to bind** (`address already in use`). A bare
> health probe cannot tell your verify from the other one. The guardian therefore
> gives each launch a random 128-bit instance nonce; `/health` must echo that nonce,
> the actual child PID, output protocol, and pinned verifier-bundle digest. The
> probe first authenticates the guardian over its owner-only Unix socket and then
> matches the bounded HTTP response. A wrong nonce, PID, protocol/digest, oversized
> body, or non-200 response is **foreign/failing**, never a false `ok`. On a shared
> host, give each deployment its own `VERIFY_PORT` / `DASHBOARD_PORT`
> (`config/danus.env`). Dashboard readiness uses the same instance nonce and PID.

`services.sh` keeps owner-only guardian records and Unix sockets under
`runtime/run/`. The legacy `.pid` record suffix is only a filename: any PIDs in a
record or status response are diagnostic and are never external signal authority.
Only the retained guardian/service-host chain may signal its own exact,
still-unreaped child process groups. If the guardian dies, its service host retains
the lifecycle lock while it tears down the old group; a new start cannot overlap
that cleanup.

The trusted service process also adopts the same flock open-file-description,
validates it against the no-follow lock path, marks it close-on-exec, and removes
its descriptor/path variables from the environment. It is never printed or
exposed through CLI, HTTP, logs, or model input. The verifier passes it explicitly
only to its retained paid-child host—not to Codex—so a guardian/service crash
cannot admit a new verifier until the old paid process group is terminal. During
that bounded fence, authenticated status reports `cleanup_in_progress`, not a
false `down`.

The versioned `autostart` manifest is durable desired state, not a best-effort
history. `up` fsyncs an intent generation before launch and rechecks that exact
generation while holding the service lock; launch failure rolls back only a
generation created by that invocation. `down` durably removes the intent before
asking the guardian to stop. This ordering makes concurrent recovery/down and
crash cuts monotone.

Service logs are opened without following symlinks and must be owner-owned regular
files with one link. `logs` returns a bounded snapshot. Continuous `-f` following
is intentionally refused because it cannot preserve the same authenticated-file
guarantee across rotation; request another snapshot instead.

## Health checks

```bash
bash scripts/doctor.sh          # green / FAIL / warn across the whole stack
bash scripts/check-codex.sh     # one live codex ping + scan recent logs for API errors
```

- `doctor.sh` is read-only: config files, python + verify deps, node, the codex
  wrapper + backend, a live verify `/health`, and soft checks for `pdflatex` /
  Chrome. Run it whenever something looks off. A real healthy run (chatgpt backend,
  no TeX installed):

  ```
  == Danus doctor ==
  DANUS_ROOT=/home/you/Danus
    ok   config/danus.env present
    ok   config/codex.env present
    ok   python: .../runtime/venv/bin/python
    ok   python dep: mcp
    ok   python pkg: danus (importable from any cwd)
    ok   python deps: fastapi/uvicorn/pydantic
    ok   python dep: openai (gpt_pro consult)
    ok   python dep: anthropic (claude_api consult)
    ok   node: .../runtime/node22/bin/node
    ok   codex: codex-cli 0.147
    ok   codex login ok (/home/you/codex-home)
    ok   verify service up :8091 (ours)
    warn no pdflatex on PATH (write-paper PDF render needs it; set TEX_ENGINE or install TeX)
    ok   chrome: /usr/bin/chromium-browser (human-summary PDF)
  consult transport: off
  done.
  ```

  (`warn` lines are soft/optional deps, not failures; on the api backend the codex
  lines read `codex backend: api provider configured` + `codex API live ping ok`.)
- `check-codex.sh` exits `0` if the backend answered; history in
  `runtime/logs/codex-health.jsonl`. Use it when workers/verify show API errors.

## Recovery after a host restart

```bash
bash scripts/recover.sh
```

One command: re-runs `bootstrap.sh` (rebuilds the possibly-dangling venv + codex
provider), reconciles only structurally safe stale guardian records without
signalling any recorded PID, takes a locked typed manifest snapshot, and
**replays the still-current intent generations**. Before each launch the guardian
rechecks that its generation is still present, so a concurrent `down` wins.
Recovery aggregates every reconcile/start failure, runs the final authenticated
health gate, and exits nonzero if any step failed. Idempotent.

> Note: after a restart, worker loops are **not** auto-resumed by `recover.sh` — it
> restores the services. Restart workers with `danus start <project>` (they resume
> from persisted memory).

Worker intent repair is deliberately separate from service recovery. If a
worker is authoritatively live, `status --json` treats canonical `prepared`,
`dispatching`, and `started` intents as `paid_intent_status: "in_progress"` and
does not publish a `recovery_required` action. A live `delivery_unknown` intent
is labeled `outcome_unknown_while_worker_live`, again without an abandon argv;
let the worker reconcile or fail-stop first. Recovery commands appear only for
a fail-stopped/PID-unsafe worker and still recheck lifecycle state under the
worker lock.

If a prepared app-server intent is authoritatively known to be unspent, use
`danus cancel-prepared-intent <project>/<worker> --thread-id ID --client-id ID
--reason TEXT`; it performs an exact CAS under the worker lifecycle lock and
appends a cancellation receipt. Reset or rotate the thread only as a separate
explicit action. If paid execution may already have begun and the outcome is
unknown, this command refuses; use the stronger acknowledged `abandon-intent`
workflow instead.

An `outcome_unknown` reasoning-first verification candidate is also separate.
It has no TTL and must never be retried or resubmitted. Inspect the exact receipt
with `danus status <project> --json`, fail-stop/reconcile the source slot, then use
the owner-only `danus resolve-candidate` transition. It audits the FactGraph
identity and releases only that exact live candidate slot.

## Worker lifecycle (operational view)

```bash
danus status <project>          # per-worker liveness + round + last activity
danus start  <project>          # (re)launch the worker loop(s); resumes from memory
danus stop   <project>          # graceful: finish the round, then exit
danus stop   <project> --force  # durable request: interrupt active owned work, then exit
```

- Workers run detached in their own process groups, so they outlive your session and
  a graceful stop lets an in-flight paid turn finish (no lost verified work). In
  `reasoning_first_v1`, one root, one critic, and any explicitly configured
  `explorer1`/`explorer2` lanes are fixed for the generation. Dormant observers
  consume no paid turns and are not automatic failover. Explorers can publish
  ordinary research and verifier-gated candidates, but cannot confirm root
  obstacles, enter the critic review, create advisor recommendations, or resolve
  owner gates. A new terminal coordination slot starts a fresh app-server thread,
  while same-slot crash recovery alone resumes its exact pinned thread. The
  2700-second cap is per paid turn, not a whole-phase completion guarantee. `--force`
  never signals a numeric PID/PGID from the CLI: the authenticated worker reads
  the durable request, interrupts its app-server turn or cleans its retained
  direct-child process group, reaps it, audits the result, and exits.
- `status --json` separates physical liveness, exact per-worker lane,
  `explorer_workers`, paid admission, candidate state, and content-free reasoning
  diagnostics. `unavailable`/`partial` telemetry is not zero, proof state, or
  authority to trigger an advisor.

## Unattended operation (examples, not core)

Under `examples/ops/` (parameterized; nothing in the engine depends on them):

- `main-agent-tmux.sh` — run codex (the main agent) detached in a tmux
  session, so strategic beats continue while you are away. **The only unattended
  mode.**
- `strategy-loop.sh <project>` — a legacy/example timer wrapper for API/CLI
  consults. It is not the core reasoning-first coordinator and cannot interpret
  root/critic evidence, create an advisor checkpoint, or authorize browser Pro;
  prefer the attended event-driven strategy loop for new projects.
- `watchdog.sh <project>` — probe verify `/health` + parse `danus status`; alarm via
  a generic `DANUS_NOTIFY` hook on a `stuck?`/`dead`/`error` worker or a down verify.

## Common issues

| symptom | check |
|---|---|
| no facts appearing | is `verify` up? `services.sh status`; `doctor.sh` |
| workers erroring in rounds | `check-codex.sh`; `runtime/logs/codex-health.jsonl`; the worker's `logs/round_*.log` |
| a `paper_*` tool came back non-`ok` | read the returned `log_path` (`<project>/paper/.runs/<utc>-<tool>/log.md`) |
| dashboard blank | port-forward `:8099`; is the dashboard service up for that project? |
| after reboot, nothing runs | `recover.sh`, then `danus start <project>` |

See `configuration.md` for the variables that tune all of the above, and
`security-and-trust.md` for the trust assumptions behind the sandbox-bypassed codex
sessions.
