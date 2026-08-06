# danus/strategy — optional strategy consults and the owner-gated browser broker

At a genuine strategic event, the main agent may send the current **elaboration**
to a configured strong reasoning model and turn its reply into strategy. The
default is `off`; API/CLI transports are explicit opt-ins and are stateless apart
from the spend ledger. The explicit browser advisor has a separate durable
owner handoff under `<project>/.advisor/`; no repository command opens Chrome. Run
ordinary consults via `bin/consult` and browser receipts via `bin/consult-browser`.

```
danus/strategy/
  cli.py         parse args, drive a transport, print the JSON envelope
  config.py      ConsultConfig + resolve_transport (env, read at call time)
  transport.py   the transports + the consult call, cost math, param step-down
  ledger.py      append-only spend ledger (<project>/spend/consult.jsonl) + running total
  browser_advisor.py  durable exact-CAS browser receipt broker (no browser code)
  browser_cli.py owner-only browser receipt state transitions
  __main__.py    `python -m danus.strategy` (what bin/consult execs)
  tests/{test_strategy.py, test_claude_code_transport.py, test_claude_api_transport.py}
```

## Transports (`DANUS_CONSULT_TRANSPORT`)

- **`off`** (default) — no external consult; the main agent reasons from the
  current durable elaboration (the CLI returns a valid `$0` envelope with a
  non-zero exit as an expected signal).
- **`gpt_pro`** (explicit opt-in) — a paid OpenAI-compatible Responses endpoint
  (`DANUS_CONSULT_API_KEY`/`_BASE_URL`/`_MODEL`). Driven `background=True,
  stream=True, store=False` with the canonical message-list `input` shape (a sync
  xhigh call would hang the proxy, and consult prompts must not be server-stored).
  Gateways that explicitly reject `background` or `max_output_tokens` are retried
  without that parameter while preserving effort/tools. Other 400s use the
  graceful lower-effort step-down (`full → no-tools → no-effort → bare`);
  `max` instead preserves its effort (`full → no-tools → no-summary →
  effort-only`). Cost is computed per-call. Streamed output-text deltas are
  retained as the reply when a compatible gateway returns a sparse final
  `response.completed` object.
- **`claude_api`** — the native Anthropic API (per-token, BYO key; the envelope cost
  is the response's REAL usage × the per-1M rates). Streamed; adaptive thinking +
  `output_config.effort`; server-side web search; refusal-fallback param attached
  by default (`DANUS_CONSULT_CLAUDE_API_FALLBACK`, `off` disables); **400-only**
  step-down (`full → no-tools → no-thinking → bare`); `pause_turn` continued.
  Knobs: `DANUS_CONSULT_CLAUDE_API_KEY`/`_BASE_URL`/`_MODEL`/`_FALLBACK`/`_PRICE_*`.
- **`claude_code`** — your Claude subscription via the Claude Code CLI (no separate API key;
  draws on your plan's quota — beyond-plan or premium-model usage can bill extra, and
  the consult is metered into the ledger at the `DANUS_CONSULT_CLAUDE_CODE_PRICE_*` estimate
  rates. Do NOT set `ANTHROPIC_API_KEY`: the transport scrubs it so the consult cannot
  silently switch to per-token API billing — that is what `claude_api` is for).
  Knobs: `DANUS_CONSULT_CLAUDE_CODE_MODEL`/`_BIN`/`DANUS_CONSULT_CLAUDE_CODE_MAX_WALL`.
`chatgpt_pro_browser` is intentionally not an environment choice. A prepare is
permitted only for the exact current content-free recommendation emitted by the
reasoning-first coordinator, and still requires fresh owner authorization for the
exact question. Broad evidence that work is merely slow or blocked is insufficient.
The existing **`gpt_pro` name still means the paid OpenAI-compatible API**.
After that recommendation is visible in `danus status --json`, the attended main
agent may prepare the bound question locally:

```bash
bin/consult --file elaboration.md --project <project-dir> \
  --transport chatgpt_pro_browser --owner-browser-prepare \
  --browser-context-id <stable-conversation-id> \
  --browser-recommendation-id <current-recommendation-id> \
  --browser-checkpoint-id <advisor-checkpoint-id> \
  --browser-checkpoint-sha256 <checkpoint-sha256> \
  --browser-checkpoint-bytes <checkpoint-bytes>
```

The file bytes must exactly equal that checkpoint's durable `evidence` bytes.
That prepare exits `4`, creates a durable receipt, and starts no browser/model.
Environment-only selection fails closed without creating the broker. Continue
with `bin/consult-browser`; see `docs/browser-advisor.md` for the exact verbs,
state machine, recovery rules, and adoption boundary.

The browser broker also supports verified local same-conversation follow-ups.
`context_id` is the stable conversation-lineage identity; the coordinator's
`recommendation_id` is a separate per-intervention identity. Each follow-up has
a new current recommendation, prompt, request id, and owner authorization while
retaining the same context. `prepare` binds a same-project/context known-terminal
predecessor and a transient exact conversation URL (file/stdin; hash only at
rest); the fresh `dispatch-started` must resupply that URL before it can return
one-shot click permission. Missing/stale/resolved recommendations,
unknown/external predecessors, stale lineage heads, automatic follow-ups, and
prompt resends fail closed. At most one browser request binds a non-null
recommendation. The generic `bin/consult` prepare remains new-chat only; use
`bin/consult-browser` for continuation.

## Reasoning effort

`--effort` accepts `minimal`, `low`, `medium`, `high`, `xhigh`, and `max`.
All transports support through `max`. On `gpt_pro`, a `max` request may simplify
unsupported summary/tool parameters, but it never falls back to a request with no
reasoning effort; an endpoint that rejects `max` therefore fails visibly instead
of producing a misleading strongest-level ledger entry. Lower levels retain the
documented compatibility step-down, exposed through the envelope's `attempt` field.

## The envelope (pinned §6 contract with the consult skill)

One JSON line: `{transport, model, effort, attempt, status, seconds, usage, cost_usd,
tool_calls, reasoning_summary, reply}` (+ `project_total_usd` when `--project` given).
Callers depend on `reply`, `cost_usd`, `transport`, `usage`. API/CLI replies use
the existing master-guidance contract. A browser import is different: it has
`trust=untrusted_strategy`, no authorities, and is not eligible for
`master_guidance`. Only an explicitly reviewed/synthesized `adopt` result carries
bounded `consult_provenance` and becomes eligible. Browser telemetry is exactly
`model:null`, `usage:null`, `cost_usd:null`, with
`billing_basis:"subscription"`. Completion and `needs-input` store only response
digest/size and attestations; `import` requires the owner to resupply the exact
response via file/stdin and never persists it. Only the reviewed synthesis passed
to `adopt` becomes project plaintext. Neither adoption nor published guidance
releases `owner_action_required`. Publish adopted guidance with
`links.recommendation_id`, use `danus assign` to stage exact next-generation
tasks for both the root and critic (`status --json` must report
`task_staging.ready=true`), and then run the exact owner-only
`danus resolve-recommendation` transition. Resolution freezes that complete task
set while explicitly acknowledging both the recommendation id and the restart
of paid reasoning. Continuing without advisor guidance is rejected while its
browser request remains active, completed but unimported, or delivery-ambiguous.

## Tests

`python -m pytest danus/strategy/` (offline; API/CLI clients are stubbed and the
browser broker tests never open a browser or use the network).
