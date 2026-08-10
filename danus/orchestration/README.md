# danus/orchestration — the `danus` CLI verbs

The operator's lifecycle commands. **Verbs/UX only** — the on-disk layout, the
round loop, and scaffolding live in `danus/execution`; this module parses arguments
and calls into it. Run via `bin/danus`.

```
danus/orchestration/
  cli.py        lifecycle verbs plus say / messages / interrupt-turn
  __main__.py   `python -m danus.orchestration` (what bin/danus execs)
  tests/{test_cli_verbs.py, test_orchestration.py}
```

## Verbs

| verb | does |
|---|---|
| `list [--json]` | projects + live worker counts + model |
| `new <p> [--roles ROLE:N,...] [--model M] [--coordination reasoning-first\|legacy]` | → `execution.scaffold.do_new`; reasoning-first defaults to `max:2,high:5`, legacy to `high:3,xhigh:4` |
| `assign <p>/<w> (--task/--file/--stdin)` | overwrite that worker's `TASK.md` |
| `say <p>/<w> (--text/--file/--stdin)` | durable owner message; same-turn steer when app-server transport is active |
| `messages <p>[/<w>] [--json]` | immutable message and per-delivery receipt view |
| `interrupt-turn <p>/<w>` | explicit owner request to interrupt the current model turn |
| `resolve-recommendation <p> ...` | exact owner resolution of the current reviewed recommendation; requires repeated-id and paid-resume acknowledgements |
| `abandon-intent <p>/<w> --thread-id ID --client-id ID --expected-state STATE --reason TEXT --acknowledge-paid-outcome-unknown` | fail-stopped owner exact-CAS for an unreconcilable `dispatching`/`started`/`delivery_unknown` paid outcome; appends a risk receipt, never retries/deletes, and fences that thread until reset/rotation |
| `cancel-prepared-intent <p>/<w> --thread-id ID --client-id ID --reason TEXT` | fail-stopped owner exact-CAS for an authoritatively unspent `prepared` intent; no paid-risk acknowledgement, append-only receipt, and reset/rotation remains separate |
| `reset-thread <p>/<w> --expected-thread-id ID` | explicitly clear a server-deleted thread mapping; CAS-fenced, refused for a live/busy worker, serialized with `start` on `.pid.lock`, and refused with unfinished paid work |
| `rotate-thread <p>/<w> --expected-thread-id ID --reason TEXT` | owner explicitly accepts terminal conversation-context loss after bounded resume failure; refused for a live/busy worker and serialized with `start` on `.pid.lock`; research stores are retained |
| `finalize <p> [<fact_id>…]` | record target(s) in `TARGET.md` (no id ⇒ suggest terminal facts); records only, does not stop workers |
| `start <p>[/<w>]` | → `execution.scaffold.spawn_loop` (idempotent via `.pid.lock`) |
| `status <p>[/<w>] [--json]` | per-worker liveness + round + `stuck?` soft signal |
| `stop <p>[/<w>] [--force]` | durable `.stop`; graceful exits at the round boundary, while `--force` asks the worker and its retained owned-child host to interrupt and clean the complete paid process group |

## Notes

- Liveness is **zombie-aware** (`os.kill(pid,0)` + a `/proc/<pid>/stat` Z-state
  check), so `status`/`list` don't lie and `start` can restart a crashed worker.
- No implicit intervention policy: only the owner decides when to send guidance
  or interrupt. `say --fallback queue` never stops a worker; `--fallback fail`
  reports lack of an active turn. Plain-text commands are never interpreted as
  process control.
- A too-large terminal `thread/resume` never triggers a replacement paid turn.
  `status --json` preserves the prior paid outcome and reports an argv for the
  separate CAS-fenced `rotate-thread` action; only the owner may execute it and
  then choose whether to `start` a replacement thread.
- An unreconcilable ambiguous paid intent requires the separate owner-only
  `abandon-intent` command with exact target/thread/client/state and explicit
  paid-outcome-unknown acknowledgement. The worker must be fail-stopped and its
  lifecycle lock free. The command appends an operator receipt and terminal
  round event but preserves all prior ledgers and research memory; the same
  thread cannot dispatch again until `reset-thread` or `rotate-thread` succeeds.
  `status --json` supplies the exact canonical `unfinished_paid_intent` fields
  and matching command skeleton, or `intent_ledger_error` if it cannot read the
  ledger. A `prepared` row is explicitly `not_dispatched` and recommends
  restarting the same immutable intent rather than risk acknowledgement. If
  immutable prompt/model/effort drift prevents that restart, status supplies an
  exact `cancel-prepared-intent` argv. It uses the same fail-stopped lifecycle
  lock, appends a receipt without a paid-risk acknowledgement, preserves all
  history, and leaves reset/rotation to a later explicit command.
- Each paid launch is owned by a retained host that holds a worker-liveness pipe
  and `.paid.lock` through complete process-group cleanup. Worker death cannot
  orphan the Codex/MCP group, and an immediate replacement cannot overlap paid
  work. The CLI never signals an inspected numeric PID/PGID.
- Touches core only indirectly: `new` creates the empty `global_memory/`/`fact_graph/`
  dirs (populated lazily by core on first write); it never writes the truth stores.

## Tests

`python -m pytest danus/orchestration/` (offline; fake codex + stub project).
